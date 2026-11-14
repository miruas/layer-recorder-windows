# SPDX-License-Identifier: GPL-3.0-only
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from katman.common import load_project, probe
from katman.export import export_project
from katman.windows_capture import (
    Capture, Display, capture_binary, capture_command, desktop_duplication_command, gsr_h264_encoder,
    set_video_framerate_mode, video_framerate_mode,
)
from katman.windows_audio import AudioSource, audio_encoder_command
from katman.windows_stream import CustomDestination, Livestream, stream_video_command


class ReplayTests(unittest.TestCase):
    def test_dxgi_stream_maps_pcm_input_without_shortest_queue(self):
        display = Display("DISPLAY1", 0, 0, 1920, 1080, True)
        command = desktop_duplication_command(display, 30, display, (0, 0))
        with (patch("katman.windows_stream.stream_video_command", return_value=command),
              patch("katman.windows_stream.subprocess.Popen") as start,
              patch("katman.windows_stream.threading.Thread", return_value=Mock())):
            Livestream(display, 30, "h264_nvenc", CustomDestination("test.mp4", "mp4"), audio=[object()])
        args = start.call_args.args[0]
        self.assertIn("ddagrab=output_idx=0:framerate=30:output_fmt=bgra,setpts=PTS-STARTPTS[video]", args)
        self.assertEqual([args[i + 1] for i, arg in enumerate(args) if arg == "-map"], ["[video]", "0:a:0"])
        self.assertNotIn("-shortest", args)

    def test_video_only_nvenc_stream_probes_dxgi_before_publishing(self):
        display = Display("DISPLAY1", 0, 0, 1920, 1080, True)
        with (patch("katman.windows_stream.displays", return_value=[display]),
              patch("katman.windows_stream.dxgi_targets", return_value={"display1": (1, 2)}),
              patch("katman.windows_stream.subprocess.run") as run):
            command = stream_video_command(display, 60, "h264_nvenc", None, False, None, None, None)
            self.assertIn("d3d11va=layer:1", command)
            self.assertIn("ddagrab=output_idx=2:framerate=60:output_fmt=bgra:draw_mouse=0", command)
            self.assertEqual(run.call_args.args[0][-5:], ["-frames:v", "1", "-f", "null", "-"])
            run.side_effect = subprocess.TimeoutExpired("ffmpeg", 5)
            self.assertIn("gdigrab", stream_video_command(display, 60, "h264_nvenc", None, None, None, None, None))

    def test_monitor_and_region_resolve_by_dxgi_device_name(self):
        primary = Display("DISPLAY1", 0, 0, 1920, 1080, True)
        secondary = Display("DISPLAY7", -1920, 0, 1920, 1080)
        region = Display("Selected region", -1856, 40, 640, 360)
        with patch("katman.windows_capture.CaptureEngine"), patch(
            "katman.windows_capture.displays", return_value=[primary, secondary]
        ), patch("katman.windows_capture.dxgi_targets", return_value={"display7": (1, 2)}):
            capture = Capture(tempfile.gettempdir(), [region], encoder="h264_nvenc", fixed_region=region)
            self.assertIsNone(capture._duplication_target(primary))
            self.assertEqual(capture._duplication_target(secondary), (secondary, (1, 2)))
            self.assertEqual(capture._duplication_target(region), (secondary, (1, 2)))
            command = desktop_duplication_command(region, 60, *capture._duplication_target(region))
            self.assertIn("d3d11va=layer:1", command)
            self.assertIn(
                "ddagrab=output_idx=2:framerate=60:output_fmt=bgra:video_size=640x360:offset_x=64:offset_y=40",
                command,
            )

    def test_desktop_duplication_crop_uses_monitor_relative_offsets(self):
        monitor = Display("Monitor", -1920, -120, 1920, 1080, True)
        region = Display("Selected region", -1856, -80, 640, 360)
        command = desktop_duplication_command(region, 60, monitor)
        self.assertIn(
            "ddagrab=output_idx=0:framerate=60:output_fmt=bgra:video_size=640x360:offset_x=64:offset_y=40",
            command,
        )

    def test_gsr_frame_rate_mode_reaches_both_capture_backends(self):
        display = Display("Primary", 0, 0, 1920, 1080, True)
        for command in (capture_command(display, 60, "libx264"), desktop_duplication_command(display, 60)):
            with self.subTest(command=command):
                set_video_framerate_mode(command, video_framerate_mode({"framerate_mode": "vfr"}), 60)
                self.assertEqual(command.count("-fps_mode"), 1)
                self.assertEqual(command[command.index("-fps_mode") + 1], "vfr")
                self.assertNotIn("-r", command)
                set_video_framerate_mode(command, "cfr", 60)
                self.assertEqual(command[command.index("-fps_mode") + 1], "cfr")
                self.assertEqual(command[command.index("-r") + 1], "60")

    def test_gsr_auto_selects_working_nvenc_and_respects_explicit_encoder(self):
        with patch("katman.windows_capture.subprocess.run") as run:
            self.assertEqual(gsr_h264_encoder({"video_codec": "h264_software"}, "h264_nvenc"), "libx264")
            self.assertEqual(gsr_h264_encoder({"video_codec": "auto"}, "h264_qsv"), "h264_qsv")
            run.assert_not_called()
            run.return_value.returncode = 0
            self.assertEqual(gsr_h264_encoder({"video_codec": "auto"}, "libx264"), "h264_nvenc")
            self.assertIn("h264_nvenc", run.call_args.args[0])
            run.return_value.returncode = 1
            self.assertEqual(gsr_h264_encoder({"video_codec": "auto"}, "libx264"), "libx264")

    def test_left_monitor_capture_uses_signed_origin(self):
        args = capture_command(Display("Left", -1280, -50, 1280, 720), 30, "libx264")
        self.assertEqual(args[args.index("-offset_x") + 1], "-1280")
        self.assertEqual(args[args.index("-offset_y") + 1], "-50")

    def test_desktop_duplication_uses_gpu_frames_and_constant_output_rate(self):
        args = desktop_duplication_command(Display("Primary", 0, 0, 1920, 1080, True), 60)
        self.assertIn("ddagrab=output_idx=0:framerate=60:output_fmt=bgra", args)
        self.assertEqual(args[args.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(args[args.index("-fps_mode") + 1], "cfr")
        self.assertNotIn("-vf", args)
        self.assertEqual(args[args.index("-maxrate") + 1], "11M")


def synthetic_video(display, fps, encoder):
    cmd = capture_command(display, fps, encoder)
    cmd[4:cmd.index("-an")] = [
        "-re", "-f", "lavfi", "-i", f"testsrc2=s={display.width}x{display.height}:r={fps}",
    ]
    return cmd


def synthetic_audio(_source, codec):
    command = audio_encoder_command(codec)
    command[command.index("-f"):command.index("-c:a")] = [
        "-re", "-f", "lavfi", "-i", "sine=sample_rate=48000",
    ]
    return command


def wait_fragments(test, capture, track, count=1):
    deadline = time.monotonic() + 20
    while capture._status()[track.id]["count"] < count:
        if error := capture.failure():
            test.fail(error)
        test.assertLess(time.monotonic(), deadline, "Capture did not produce fragments")
        time.sleep(.1)


@unittest.skipUnless(capture_binary().is_file() and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "The Rust capture engine and FFmpeg are required")
class ProjectTests(unittest.TestCase):
    def test_content_mode_retains_motion_and_static_replay_heartbeat(self):
        display = Display("Fixture", 0, 0, 320, 240)
        with tempfile.TemporaryDirectory() as directory:
            for pattern, expected in (("color", 4), ("testsrc2", 120)):
                with self.subTest(pattern=pattern):
                    output = Path(directory) / f"{pattern}.mp4"
                    command = capture_command(display, 30, "libx264")
                    command[4:command.index("-an")] = ["-f", "lavfi", "-i", f"{pattern}=s=320x240:r=30"]
                    set_video_framerate_mode(command, video_framerate_mode({"framerate_mode": "content"}), 30)
                    subprocess.run(command[:-1] + ["-t", "4", str(output)], check=True, timeout=15)
                    count = subprocess.check_output([
                        "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(output),
                    ], timeout=10)
                    self.assertEqual(int(count), expected)

    def test_two_tracks_record_replay_and_export(self):
        monitor = Display("Primary", 0, 0, 96, 64, True)
        second = Display("Left", -96, 0, 96, 64)

        with tempfile.TemporaryDirectory(prefix="Layer Türkçe ' ") as directory:
            library = Path(directory)
            with patch("katman.windows_capture.capture_command", synthetic_video):
                capture = Capture(library, [monitor, second], fps=10, seconds=3, record=True)
            try:
                for track in capture.tracks:
                    wait_fragments(self, capture, track, 3)
                path, replay = load_project(capture.save_replay(1.5))
                self.assertAlmostEqual(replay["duration"], 1.5, places=2)
                self.assertEqual(len(replay["sources"]), 2)
                for source in replay["sources"]:
                    stream = probe(path.parent / source["file"])["streams"][0]
                    self.assertAlmostEqual(float(stream["start_time"]), 0, places=2)
                    self.assertGreater(float(stream["duration"]), 0)
                result = export_project(path, library / "Dışa aktarım.mp4", preview=True)
                self.assertAlmostEqual(float(probe(result)["format"]["duration"]), 1.5, delta=.15)
                _, recording = load_project(capture.finish_recording())
                self.assertEqual(len(recording["sources"]), 2)
                self.assertGreater(recording["duration"], 2)
            finally:
                capture.stop()

    def test_nvenc_falls_back_when_desktop_duplication_fails(self):
        import sys
        monitor = Display("Primary", 0, 0, 96, 64, True)
        with (tempfile.TemporaryDirectory() as directory,
              patch("katman.windows_capture.displays", return_value=[monitor]),
              patch("katman.windows_capture.dxgi_targets", return_value={"primary": (0, 0)}),
              patch("katman.windows_capture.desktop_duplication_command",
                    return_value=[sys.executable, "-c", "raise SystemExit(1)"]),
              patch("katman.windows_capture.capture_command",
                    side_effect=lambda display, fps, _: synthetic_video(display, fps, "libx264"))):
            capture = Capture(directory, [monitor], fps=10, encoder="h264_nvenc")
            try:
                deadline = time.monotonic() + 15
                while capture.active[monitor.key].backend == "ddagrab":
                    self.assertLess(time.monotonic(), deadline)
                    capture.reconcile([monitor])
                    time.sleep(.1)
                wait_fragments(self, capture, capture.active[monitor.key])
                seconds, used, backends = capture.replay_status()
                self.assertGreater(seconds, 0)
                self.assertGreater(used, 0)
                self.assertEqual(backends, ["gdigrab"])
                _, replay = load_project(capture.save_replay(1))
                self.assertEqual(replay["sources"][0]["backend"], "gdigrab")
                self.assertTrue(any("Desktop Duplication failed" in w for w in replay["warnings"]))
            finally:
                capture.stop()

    def test_display_reconnect_preserves_source_generations_and_independent_audio(self):
        first = Display("DISPLAY1", 0, 0, 96, 64, True, "monitor-a")
        changed = Display("DISPLAY2", 0, 0, 128, 72, True, "monitor-a")
        microphone = AudioSource("microphone:fixture", "Test microphone", "microphone")
        application = AudioSource("app:123", "Test app", "application", pid=123)
        with (tempfile.TemporaryDirectory() as directory,
              patch("katman.windows_capture.capture_command", synthetic_video),
              patch("katman.windows_audio.capture_audio_command", synthetic_audio)):
            capture = Capture(directory, [first], fps=10, seconds=10, record=True,
                              audio=[microphone], follow_application_audio=True, audio_codec="opus")
            try:
                wait_fragments(self, capture, capture.active[first.key], 2)
                self.assertEqual(capture.reconcile([], [microphone, application]), 0)
                self.assertTrue(capture.tracks[0].stopped)
                self.assertEqual(capture.reconcile([changed], [microphone, application]), 1)
                wait_fragments(self, capture, capture.active[changed.key], 2)
                wait_fragments(self, capture, capture.active[application.key], 2)
                replay_path, replay = load_project(capture.save_replay(10))
                self.assertEqual({s["width"] for s in replay["sources"] if s["type"] == "video"}, {96, 128})
                self.assertEqual({s["kind"] for s in replay["sources"] if s["type"] == "audio"},
                                 {"microphone", "application"})
                for source in replay["sources"]:
                    info = probe(replay_path.parent / source["file"])
                    self.assertEqual(info["streams"][0]["codec_type"], source["type"])
                    if source["type"] == "audio":
                        self.assertEqual(info["streams"][0]["codec_name"], "opus")
                recording_path, recording = load_project(capture.finish_recording())
                self.assertEqual(len(recording["sources"]), 4)
                for source in recording["sources"]:
                    if source["type"] == "audio":
                        self.assertEqual(probe(recording_path.parent / source["file"])["streams"][0]["codec_name"],
                                         "opus")
                clips = [track["clips"][0] for track in recording["studio"]["tracks"]]
                self.assertGreater(clips[2]["start"], 0)
            finally:
                capture.stop()


@unittest.skipUnless(os.name == "nt", "Win32 hotkeys")
class HotkeyTests(unittest.TestCase):
    def test_message_routing_and_registration_conflicts(self):
        from katman.windows_hotkeys import ADDITIONAL_HOTKEYS, GlobalHotkeys, HOTKEYS, WM_HOTKEY
        actions = {key: action for key, (action, _) in HOTKEYS.items()}
        actions.update({key: action for key, (_, action, _) in ADDITIONAL_HOTKEYS.items()})
        first = GlobalHotkeys()
        try:
            if first.conflicts:
                self.skipTest("Another desktop process has one of Layer's hotkeys.")
            second = GlobalHotkeys()
            try:
                self.assertEqual(len(second.conflicts), len(actions))
                for key, action in actions.items():
                    self.assertTrue(first.user32.PostThreadMessageW(first.thread_id, WM_HOTKEY, key, 0))
                    self.assertEqual(first.events.get(timeout=2), action)
            finally:
                second.close()
        finally:
            first.close()
        self.assertFalse(first.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
