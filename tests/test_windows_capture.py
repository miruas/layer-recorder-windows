# SPDX-License-Identifier: GPL-3.0-only
import os
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

from katman.common import load_project, probe
from katman.export import export_project
from katman.windows_capture import (
    Capture, Display, capture_binary, capture_command, desktop_duplication_command, dxgi_targets, gsr_h264_encoder,
    gsr_recording_encoder, set_video_framerate_mode, set_video_rate_control, video_framerate_mode,
)
from katman.windows_audio import (
    AudioSource, audio_encoder_command, capture_audio_command, gsr_audio_selection, gsr_recording_audio, mix_audio,
    mix_live_audio,
)
from katman.windows_stream import CustomDestination, Livestream, stream_frame_rate, stream_video_command
from katman.windows_screenshot import capture_monitor, screenshot_source
from katman.windows_webcam import add_webcam, camera_inventory, gsr_webcam


class ReplayTests(unittest.TestCase):
    def test_live_audio_keeps_pcm_running_through_empty_and_replaced_sessions(self):
        from struct import pack

        first = AudioSource("app:101", "Game", "application", pid=101)
        second = AudioSource("app:102", "Game", "application", pid=102)
        stop = threading.Event()
        received = []
        sources = []

        def capture(source, sink, halted):
            while not halted.is_set():
                sink.write(pack("<h", source.pid) * 1920)
                halted.wait(.020)

        def discover():
            return list(sources)

        class Output:
            def write(self, data):
                received.append(int.from_bytes(data[:2], "little"))
                if len(received) == 3:
                    sources[:] = [first]
                if received.count(101) == 3:
                    sources[:] = [second]
                if received.count(102) == 3 or len(received) >= 60:
                    stop.set()

        with patch("katman.windows_audio.capture_pcm", side_effect=capture):
            mix_live_audio([], discover, Output(), stop, self.fail, interval=.05)
        self.assertEqual(received[:3], [0, 0, 0])
        self.assertIn(101, received)
        self.assertIn(102, received)
        self.assertNotIn(101, received[received.index(102):])

    def test_live_application_stream_includes_audio_even_without_an_initial_session(self):
        with (patch.object(subprocess, "CREATE_NO_WINDOW", 0, create=True),
              patch("katman.windows_stream.subprocess.Popen") as start,
              patch("katman.windows_stream.threading.Thread", return_value=Mock()) as thread):
            discover = Mock(return_value=[])
            Livestream(Display("Fixture", 0, 0, 320, 240), 30, "libx264",
                       CustomDestination("test.mp4", "mp4"), discover_audio=discover)
        args = start.call_args.args[0]
        self.assertIn("1:a:0", args)
        self.assertIn("pipe:0", args)
        self.assertEqual(thread.call_args.kwargs["args"], ((), discover))

    def test_inverted_gsr_audio_excludes_named_process_trees_without_duplicate_mixes(self):
        from katman.windows_audio import inverse_application_audio

        inventory = [AudioSource(f"app:{pid}", f"{name} (PID {pid})", "application", pid=pid)
                     for pid, name in [(101, "Launcher"), (102, "Chat"), (103, "ChatAudio"),
                                       (200, "Game"), (201, "GameAudio"), (202, "Browser")]]
        processes = {101: (0, "Launcher.exe"), 102: (101, "Chat.exe"), 103: (102, "ChatAudio.exe"),
                     200: (0, "Game.exe"), 201: (200, "GameAudio.exe"), 202: (0, "Browser.exe")}
        with (patch("katman.windows_audio.process_snapshot", return_value=processes),
              patch("katman.windows_audio.sys.getwindowsversion", create=True,
                    return_value=Mock(build=22000))):
            self.assertEqual([s.pid for s in inverse_application_audio(inventory, {"chat"})], [200, 202])
            track = {"name": "Without chat", "audio_inputs": ["app:Chat.exe"], "application_audio_invert": True}
            mixed, = gsr_recording_audio([track], inventory)
            self.assertEqual(mixed.metadata["name"], "Without chat")
            self.assertEqual([s.pid for s in mixed.inputs], [200, 202])
            self.assertEqual(gsr_recording_audio([track], inventory[:3]), [])
            new, = gsr_recording_audio([track], inventory[3:4])
            self.assertEqual(new.key, mixed.key)
            track["audio_inputs"].append("device:default_output")
            with self.assertRaisesRegex(ValueError, "Remove system output"):
                gsr_recording_audio([track], inventory)

    def test_directshow_inventory_uses_video_identity_and_known_modes(self):
        listing = ('[dshow] "Caméra HD" (video)\n'
                   '[dshow]   Alternative name "@device_pnp_camera"\n'
                   '[dshow] "Microphone" (audio)\n'
                   '[dshow]   Alternative name "@device_cm_mic"')
        modes = ('[dshow] pixel_format=yuyv422 min s=640x480 fps=15 max s=640x480 fps=30\n'
                 '[dshow] vcodec=mjpeg min s=1920x1080 fps=30 max s=1920x1080 fps=30')
        with patch("katman.windows_webcam.dshow_log", side_effect=[listing, modes]) as run:
            self.assertEqual(camera_inventory(), "dshow:@device_pnp_camera\tCaméra HD\n"
                             "yuyv\t640\t480\t15\nyuyv\t640\t480\t30\nmjpeg\t1920\t1080\t30")
            self.assertEqual(run.call_args.args[-1], "video=@device_pnp_camera")

    def test_webcam_livestream_maps_pipe_audio_after_desktop_and_camera(self):
        camera = gsr_webcam({"webcam_source": "dshow:Camera", "webcam_video_format": "mjpeg"})
        with (patch.object(subprocess, "CREATE_NO_WINDOW", 0, create=True),
              patch("katman.windows_stream.subprocess.Popen") as start,
              patch("katman.windows_stream.threading.Thread", return_value=Mock())):
            Livestream(Display("Fixture", 0, 0, 320, 240), 30, "libx264",
                       CustomDestination("test.mp4", "mp4"), audio=[object()], webcam=camera)
        args = start.call_args.args[0]
        self.assertEqual([args[i + 1] for i, arg in enumerate(args) if arg == "-i"],
                         ["desktop", "video=Camera", "pipe:0"])
        self.assertEqual([args[i + 1] for i, arg in enumerate(args) if arg == "-map"],
                         ["2:a:0", "[webcam_video]"])

    def test_gsr_mixes_inputs_per_track_and_keeps_the_track_key_on_disconnect(self):
        mic = AudioSource("microphone:mic", "Microphone", "microphone", "mic")
        speaker = AudioSource("system:speaker", "Speakers", "system", "speaker", default=True)
        app = AudioSource("app:7", "game.exe (PID 7)", "application", pid=7)
        tracks = [
            {"audio_inputs": ["device:default_output", "device:microphone:mic"], "name": "Commentary"},
            {"audio_inputs": ["app:game.exe"], "name": "Game"},
        ]
        mixed, game = gsr_recording_audio(tracks, [mic, speaker, app])
        self.assertEqual(mixed.metadata["name"], "Commentary")
        self.assertEqual(game.key, app.key)
        command = capture_audio_command(mixed)
        self.assertEqual(command[-3], "mix")
        self.assertEqual(json.loads(command[-2]), [["microphone", "mic", 0], ["system", "speaker", 0]])
        shrunk, = gsr_recording_audio(tracks, [speaker])
        self.assertEqual(shrunk.key, mixed.key)
        self.assertEqual([source.key for source in shrunk.inputs], [speaker.key])

    def test_pcm_mixer_sums_sources_and_clips_instead_of_wrapping(self):
        from struct import pack

        sources = [AudioSource("first", "First", "microphone"), AudioSource("second", "Second", "microphone")]
        stop = threading.Event()

        class Output(io.BytesIO):
            def write(self, data):
                super().write(data)
                stop.set()

        def capture_block(source, sink, halted):
            sample = 20000 if source.key == "first" else 16000
            sink.write(pack("<hh", sample, -sample) * 960)
            halted.wait(2)

        output = Output()
        with patch("katman.windows_audio.capture_pcm", side_effect=capture_block):
            mix_audio(sources, output, stop)
        self.assertEqual(output.getvalue(), pack("<hh", 32767, -32768) * 960)

    def test_gsr_unsupported_codec_does_not_silently_encode_h264(self):
        with patch("katman.windows_capture.video_encoder_available") as available:
            with self.assertRaisesRegex(ValueError, "configured video codec is unavailable"):
                gsr_recording_encoder({"video_codec": "hevc_hdr"}, "libx264", "mp4", "mp4")
            with self.assertRaisesRegex(ValueError, "Windows livestreaming"):
                gsr_h264_encoder({"video_codec": "av1"}, "h264_nvenc")
            available.assert_not_called()

    def test_gsr_custom_audio_name_preserves_source_identity(self):
        speaker = AudioSource("system:speaker", "Speakers", "system", default=True)
        selected = gsr_audio_selection(
            [{"audio_inputs": ["device:default_output"], "name": "Masaüstü sesi"}], [speaker],
        )[0]
        self.assertEqual(selected.key, speaker.key)
        self.assertEqual(selected.name, speaker.name)
        self.assertEqual(selected.metadata["name"], "Masaüstü sesi")

    def test_saved_windows_monitor_id_survives_display_number_changes(self):
        identity = r"\\?\DISPLAY#MONITOR_A#instance"
        replacement = Display("DISPLAY1", 0, 0, 1920, 1080, True, r"\\?\DISPLAY#MONITOR_B#instance")
        original = Display("DISPLAY3", -1920, 0, 1920, 1080, False, identity)
        with patch("katman.windows_screenshot.displays", return_value=[replacement, original]) as monitors:
            self.assertEqual(capture_monitor(identity.lower()), original)
            self.assertEqual(capture_monitor(r"\\.\DISPLAY1"), replacement)
            monitors.return_value = [replacement]
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                capture_monitor(identity)

    def test_gsr_audio_tracks_follow_selected_app_when_its_pid_changes(self):
        display = Display("DISPLAY1", 0, 0, 320, 240)
        first = AudioSource("app:123", "game.exe (PID 123)", "application", pid=123)
        restarted = AudioSource("app:456", "game.exe (PID 456)", "application", pid=456)
        other = AudioSource("app:789", "other.exe (PID 789)", "application", pid=789)
        tracks = [{"audio_inputs": ["app:game.exe"], "name": "Game audio"}]
        with (tempfile.TemporaryDirectory() as folder,
              patch("katman.windows_capture.shutil.which", return_value="ffmpeg"),
              patch("katman.windows_capture.CaptureEngine") as engine):
            engine.return_value.request.return_value = None
            capture = Capture(folder, [display], record=True, gsr_audio_tracks=tracks)
            try:
                capture.reconcile([display], [other])
                self.assertEqual(set(capture.active), {display.key})
                capture.reconcile([display], [first, other])
                self.assertEqual(set(capture.active), {display.key, first.key})
                capture.reconcile([display], [restarted, other])
                self.assertEqual(set(capture.active), {display.key, restarted.key})
                self.assertTrue(next(track for track in capture.tracks if track.source.key == first.key).stopped)
                self.assertEqual(capture.active[restarted.key].source.metadata["name"], "Game audio")
                capture.reconcile([display], [other])
                capture.reconcile([display], None)
                self.assertEqual(set(capture.active), {display.key})
            finally:
                capture.stop()

    def test_gsr_frame_rate_defaults_to_60_for_missing_or_legacy_settings(self):
        self.assertEqual(stream_frame_rate({}), 60)
        self.assertEqual(stream_frame_rate({"record_options": {"fps": 0}}), 60)
        self.assertEqual(stream_frame_rate({"record_options": {"fps": 144}}), 144)

    def test_window_screenshot_uses_window_that_preceded_overlay(self):
        with patch("katman.windows_screenshot.foreground_window", return_value=999) as foreground:
            self.assertEqual(screenshot_source({}, "screenshot_window", window_handle=321),
                             ["-i", "hwnd=321"])
            foreground.assert_not_called()
            self.assertEqual(screenshot_source({}, "screenshot_window"), ["-i", "hwnd=999"])

    def test_replay_after_display_disconnect_ends_at_last_video_fragment(self):
        display = Display("DISPLAY1", 0, 0, 320, 240)
        audio = AudioSource("audio", "Test microphone", "microphone")
        state = {"tracks": [
            {"id": 1, "started": 0, "first": 0, "last": 4, "end": 4.25, "count": 5},
            {"id": 2, "started": 0, "first": 0, "last": 14, "count": 15},
        ], "bytes": 1000}
        with (tempfile.TemporaryDirectory() as folder,
              patch("katman.windows_capture.shutil.which", return_value="ffmpeg"),
              patch("katman.windows_capture.CaptureEngine") as engine,
              patch.object(Capture, "_write_project") as write):
            request = engine.return_value.request
            request.side_effect = lambda op, **_: {"status": state, "snapshot": 2}.get(op)
            capture = Capture(folder, [display], audio=[audio], record=True)
            try:
                capture._retire(display.key)
                self.assertEqual(capture.replay_status()[0], 4.25)
                capture.save_replay(3)
                self.assertEqual(write.call_args.args[-2:], (1.25, 4.25))
                for call in request.call_args_list:
                    if call.args == ("snapshot",):
                        self.assertEqual((call.kwargs["from"], call.kwargs["to"]), (1.25, 4.25))
            finally:
                capture.stop()

    def test_region_follows_original_monitor_after_reorder_or_disconnect(self):
        original = Display("DISPLAY1", 0, 0, 1920, 1080, True, "original")
        replacement = Display("DISPLAY2", 0, 0, 1920, 1080, True, "other")
        moved = Display("DISPLAY3", -1920, 0, 1920, 1080, False, "original")
        region = Display("Selected region", 64, 48, 640, 360)
        with (tempfile.TemporaryDirectory() as folder,
              patch("katman.windows_capture.displays", return_value=[original]),
              patch("katman.windows_capture.shutil.which", return_value="ffmpeg"),
              patch("katman.windows_capture.CaptureEngine") as engine):
            engine.return_value.request.return_value = None
            capture = Capture(folder, [region], fixed_region=region, record=True)
            try:
                first = capture.active[region.key]
                self.assertEqual(capture.reconcile([replacement]), 0)
                self.assertTrue(first.stopped)
                self.assertEqual(capture.reconcile([replacement, moved]), 1)
                track = capture.active[region.key]
                self.assertNotEqual(first.id, track.id)
                self.assertEqual((track.source.x, track.source.y), (-1856, 48))
            finally:
                capture.stop()

    def test_rotated_outputs_are_not_used_for_desktop_duplication(self):
        with patch("katman.windows_capture.subprocess.run") as run:
            run.return_value.stdout = b"""[
                {"name":"DISPLAY1","adapter":0,"output":0,"rotated":false},
                {"name":"DISPLAY2","adapter":0,"output":1,"rotated":true},
                {"name":"DISPLAY3","adapter":1,"output":0}
            ]"""
            self.assertEqual(dxgi_targets(), {"display1": (0, 0)})

    def test_10_bit_desktop_duplication_requires_10_bit_surfaces(self):
        display = Display("Primary", 0, 0, 1920, 1080, True)
        command = desktop_duplication_command(display, 60, encoder="hevc_nvenc", ten_bit=True)
        self.assertIn("ddagrab=output_idx=0:framerate=60:output_fmt=x2bgr10:force_fmt=1", command)
        self.assertNotIn("-pix_fmt", command)

    def test_ten_bit_gsr_codecs_probe_the_requested_pixel_format(self):
        for codec in ("hevc", "av1"):
            with (self.subTest(codec=codec),
                  patch("katman.windows_capture.subprocess.run") as run):
                run.return_value.returncode = 0
                encoder = gsr_recording_encoder({"video_codec": f"{codec}_10bit"}, "libx264", "mp4", "matroska")
                self.assertEqual(encoder, f"{codec}_nvenc")
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("-pix_fmt") + 1], "p010le")

    def test_av1_uses_nvenc_for_mp4_and_mkv(self):
        options = {"video_codec": "av1"}
        with patch("katman.windows_capture.video_encoder_available", return_value=True) as available:
            self.assertEqual(gsr_recording_encoder(options, "libx264", "mp4", "matroska"), "av1_nvenc")
            available.assert_called_once_with("av1_nvenc", ten_bit=False)
            for container in ("mov", "flv"):
                with self.assertRaisesRegex(ValueError, "MP4 or MKV"):
                    gsr_recording_encoder(options, "libx264", "mp4", container)
        display = Display("Primary", 0, 0, 1920, 1080, True)
        args = desktop_duplication_command(display, 60, encoder="av1_nvenc")
        set_video_rate_control(args, ("cbr", 8000))
        self.assertEqual(args[args.index("-c:v") + 1], "av1_nvenc")
        self.assertEqual(args[args.index("-rc") + 1], "cbr")

    def test_hevc_requires_nvenc_and_a_compatible_container(self):
        options = {"video_codec": "hevc"}
        with patch("katman.windows_capture.video_encoder_available", return_value=True) as available:
            self.assertEqual(gsr_recording_encoder(options, "libx264", "mp4", "matroska"), "hevc_nvenc")
            available.assert_called_once_with("hevc_nvenc", ten_bit=False)
            with self.assertRaisesRegex(ValueError, "MP4, MKV or MOV"):
                gsr_recording_encoder(options, "libx264", "mp4", "flv")
            available.return_value = False
            with self.assertRaisesRegex(RuntimeError, "HEVC NVENC"):
                gsr_recording_encoder(options, "libx264", "mp4", "mov")
        display = Display("Primary", 0, 0, 1920, 1080, True)
        command = desktop_duplication_command(display, 60, encoder="hevc_nvenc")
        set_video_rate_control(command, ("quality", 25))
        self.assertEqual(command[command.index("-c:v") + 1], "hevc_nvenc")
        self.assertEqual(command[command.index("-cq") + 1], "25")

    def test_dxgi_stream_maps_pcm_input_without_shortest_queue(self):
        display = Display("DISPLAY1", 0, 0, 1920, 1080, True)
        command = desktop_duplication_command(display, 30, display, (0, 0))
        with (patch("katman.windows_stream.stream_video_command", return_value=command),
              patch.object(subprocess, "CREATE_NO_WINDOW", 0, create=True),
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
              patch.object(subprocess, "CREATE_NO_WINDOW", 0, create=True),
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
            capture.color_range = "pc"
            self.assertEqual(capture._duplication_target(region), (secondary, (1, 2)))
            capture.color_range = "tv"
            self.assertIsNone(capture._duplication_target(region))

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

    def test_gsr_auto_selects_amf_then_quick_sync_before_software(self):
        order = [call("h264_nvenc"), call("h264_amf"), call("h264_qsv")]
        for encoder, results in (("h264_amf", [False, True]), ("h264_qsv", [False, False, True]),
                                 ("libx264", [False, False, False])):
            with (self.subTest(encoder=encoder),
                  patch("katman.windows_capture.video_encoder_available", side_effect=results) as available):
                self.assertEqual(gsr_h264_encoder({"video_codec": "auto"}, "libx264"), encoder)
                self.assertEqual(available.call_args_list, order[:len(results)])

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
    def test_webcam_composition_places_camera_in_output_frame(self):
        camera = gsr_webcam({"webcam_source": "dshow:Fixture", "webcam_x": 75, "webcam_y": 75,
                             "webcam_width": 25, "webcam_height": 25,
                             "webcam_flip_horizontally": True})
        command = capture_command(Display("Fixture", 0, 0, 320, 240), 30, "libx264")
        command[4:command.index("-an")] = ["-f", "lavfi", "-i", "color=black:s=320x240:r=30"]
        with patch("katman.windows_webcam.webcam_input",
                   return_value=["-f", "lavfi", "-i", "color=white:s=160x120:r=30"]):
            add_webcam(command, camera, (320, 240))
        output = subprocess.check_output(
            command[:command.index("-c:v")] + ["-frames:v", "1", "-f", "rawvideo", "pipe:1"], timeout=10)
        self.assertEqual(len(output), 320 * 240 * 3 // 2)
        self.assertEqual(output[20 * 320 + 20], 16)
        self.assertEqual(output[210 * 320 + 280], 235)

    def test_pause_trims_old_generations_before_resuming_recording(self):
        monitor = Display("DISPLAY1", 0, 0, 96, 64, True)
        microphone = AudioSource("mic:fixture", "Test microphone", "microphone")
        with (tempfile.TemporaryDirectory() as directory,
              patch("katman.windows_capture.displays", return_value=[monitor]),
              patch("katman.windows_capture.capture_command", synthetic_video),
              patch("katman.windows_audio.audio_sources", return_value=[microphone]),
              patch("katman.windows_audio.capture_audio_command", synthetic_audio)):
            capture = Capture(directory, [monitor], fps=10, audio=[microphone], record=True)
            try:
                for track in capture.tracks:
                    wait_fragments(self, capture, track)
                self.assertTrue(capture.toggle_pause())
                video_before, audio_before = capture.tracks
                self.assertIsInstance(video_before.paused_at, float)
                self.assertEqual(video_before.paused_at, audio_before.paused_at)
                time.sleep(0.5)
                self.assertFalse(capture.toggle_pause())
                for track in capture.active.values():
                    wait_fragments(self, capture, track)
                _, project = load_project(capture.finish_recording())
                for kind in ("video", "audio"):
                    before, after = [source for source in project["sources"] if source["type"] == kind]
                    self.assertLessEqual(before["timeline_start"] + before["media_duration"],
                                         after["timeline_start"] + 0.000001)
            finally:
                capture.stop()

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
