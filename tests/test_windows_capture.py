# SPDX-License-Identifier: GPL-3.0-only
import io
from pathlib import Path
import shutil
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from katman.common import load_project, probe
from katman.export import export_project
from katman.windows_capture import Capture, Display, Fragment, ReplayBuffer, capture_command, read_boxes


class ReplayTests(unittest.TestCase):
    def test_replay_evicts_by_time_and_size(self):
        ring = ReplayBuffer(2, 6)
        for start in range(4):
            ring.add(Fragment(start, b"a", b"b"))
        self.assertEqual([f.start for f in ring.fragments], [1, 2, 3])
        ring.add(Fragment(4, b"aaa", b"bb"))
        self.assertEqual([f.start for f in ring.fragments], [4])
        self.assertEqual(ring.bytes, 5)

    def test_fragment_reader_rejects_partial_payload(self):
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            list(read_boxes(io.BytesIO(struct.pack(">I4s", 10, b"mdat") + b"x")))

    def test_left_monitor_capture_uses_signed_origin(self):
        args = capture_command(Display("Left", -1280, -50, 1280, 720), 30, "libx264")
        self.assertEqual(args[args.index("-offset_x") + 1], "-1280")
        self.assertEqual(args[args.index("-offset_y") + 1], "-50")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class ProjectTests(unittest.TestCase):
    def test_two_tracks_record_replay_and_export(self):
        monitor = Display("Primary", 0, 0, 96, 64, True)
        second = Display("Left", -96, 0, 96, 64)

        def synthetic_command(display, fps, encoder):
            cmd = capture_command(display, fps, encoder)
            cmd[4:cmd.index("-an")] = [
                "-re", "-f", "lavfi", "-i", f"testsrc2=s=96x64:r={fps}",
            ]
            return cmd

        with tempfile.TemporaryDirectory(prefix="Layer Türkçe ' ") as directory:
            library = Path(directory)
            with patch("katman.windows_capture.capture_command", synthetic_command):
                capture = Capture(library, [monitor, second], fps=10, seconds=3, record=True)
            try:
                deadline = time.monotonic() + 20
                while not all(track.snapshot()[2] and track.snapshot()[2][-1].start >= 2
                              for track in capture.tracks):
                    if error := capture.failure():
                        self.fail(error)
                    self.assertLess(time.monotonic(), deadline, "Capture did not produce fragments")
                    time.sleep(.1)
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


if __name__ == "__main__":
    unittest.main()
