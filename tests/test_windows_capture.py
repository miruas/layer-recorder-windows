# SPDX-License-Identifier: GPL-3.0-only
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from katman.common import load_project, probe
from katman.export import export_project
from katman.windows_capture import Capture, Display, capture_binary, capture_command, desktop_duplication_command
from katman.windows_audio import AudioSource


class ReplayTests(unittest.TestCase):
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


def synthetic_video(display, fps, encoder):
    cmd = capture_command(display, fps, encoder)
    cmd[4:cmd.index("-an")] = [
        "-re", "-f", "lavfi", "-i", f"testsrc2=s={display.width}x{display.height}:r={fps}",
    ]
    return cmd


def synthetic_audio(_source):
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
            "-i", "sine=sample_rate=48000", "-c:a", "aac", "-frag_duration", "1000000",
            "-movflags", "empty_moov+default_base_moof+skip_trailer", "-f", "mp4", "pipe:1"]


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
                              audio=[microphone], follow_application_audio=True)
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
                _, recording = load_project(capture.finish_recording())
                self.assertEqual(len(recording["sources"]), 4)
                clips = [track["clips"][0] for track in recording["studio"]["tracks"]]
                self.assertGreater(clips[2]["start"], 0)
            finally:
                capture.stop()


@unittest.skipUnless(os.name == "nt", "Win32 hotkeys")
class HotkeyTests(unittest.TestCase):
    def test_message_routing_and_registration_conflicts(self):
        from katman.windows_hotkeys import GlobalHotkeys, HOTKEYS, WM_HOTKEY
        first = GlobalHotkeys()
        try:
            if first.conflicts:
                self.skipTest("Another desktop process has one of Layer's hotkeys.")
            second = GlobalHotkeys()
            try:
                self.assertEqual(len(second.conflicts), len(HOTKEYS))
                for key, (action, _) in HOTKEYS.items():
                    self.assertTrue(first.user32.PostThreadMessageW(first.thread_id, WM_HOTKEY, key, 0))
                    self.assertEqual(first.events.get(timeout=2), action)
            finally:
                second.close()
        finally:
            first.close()
        self.assertFalse(first.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
