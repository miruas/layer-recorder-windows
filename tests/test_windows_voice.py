# SPDX-License-Identifier: GPL-3.0-only
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from katman.voice_control import (
    CommandMatcher, VoiceCommand, VoiceConfig, VoiceMatch, duration_seconds, load_config, save_config,
    validate_config, words,
)
from katman.windows_capture import Capture
from katman.windows_voice import SpeechGate, VoiceListener


class VoiceCommandTests(unittest.TestCase):
    def test_wake_phrase_and_examples(self):
        matcher = CommandMatcher(VoiceConfig(wake_phrase="Kivo"))
        for utterance, expected in {
            "Kivo, start recording": VoiceMatch("start_recording"),
            "Kivo, stop recording": VoiceMatch("stop_recording"),
            "Kivo, toggle recording": VoiceMatch("toggle_recording"),
            "Kivo, save the last three minutes": VoiceMatch("save_replay", 180),
            "Kivo, last thirty seconds": VoiceMatch("save_replay", 30),
            "Kivo, save the last two minutes and fifteen seconds": VoiceMatch("save_replay", 135),
            "KIVO, save the last 2 minutes and 15 seconds.": VoiceMatch("save_replay", 135),
        }.items():
            with self.subTest(utterance=utterance):
                self.assertEqual(matcher.match(utterance), expected)
        for utterance in ("start recording", "Kivon start recording", "hey Kivo start recording",
                          "Kivo please start recording", "Kivo, start recording now", "Kivo, last three",
                          "Kivo, last -30 seconds", "Kivo, last 3.5 minutes"):
            self.assertIsNone(matcher.match(utterance), utterance)

    def test_dynamic_numbers_and_invalid_values(self):
        for spoken, expected in {
            "an hour and twenty-one minutes and a second": 4861,
            "three hundred and fifty seconds": 350,
            "one thousand and five seconds": 1005,
            "1,200 seconds": 1200,
            "zero seconds": None,
            "thirty twenty seconds": None,
            "five seconds two minutes": None,
            "two minutes three minutes": None,
            "two minutes and": None,
        }.items():
            self.assertEqual(duration_seconds(words(spoken)), expected, spoken)

    def test_custom_template_and_persistence(self):
        commands = (VoiceCommand("grab {duration} of that", "save_replay"),)
        config = VoiceConfig(True, "Mic 1", "Hey Jarvis", commands)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "voice.json"
            self.assertFalse(load_config(path).enabled)
            save_config(config, path)
            self.assertEqual(load_config(path), config)
        matcher = CommandMatcher(config)
        self.assertEqual(matcher.match("Hey Jarvis, grab one minute of that."), VoiceMatch("save_replay", 60))

    def test_invalid_config(self):
        for commands in (
            (VoiceCommand("save replay", "save_replay"),),
            (VoiceCommand("{duration} and {duration}", "save_replay"),),
            (VoiceCommand("{duration}", "start_recording"),),
            (VoiceCommand("start recording", "unknown"),),
            (VoiceCommand("start recording", "start_recording"), VoiceCommand("START RECORDING!", "stop_recording")),
        ):
            with self.assertRaises(ValueError):
                validate_config(VoiceConfig(commands=commands))

    def test_strict_replay_validates_same_snapshot_and_preserves_existing_behavior(self):
        capture = Capture.__new__(Capture)
        capture.seconds = 180
        capture._status = Mock(return_value={})
        capture.tracks = []
        with tempfile.TemporaryDirectory() as folder:
            capture.replay_directory = Path(folder)
            with patch("katman.windows_capture.replay_video_bounds", return_value=(15, 150)), \
                    patch.object(capture, "_write_project") as write:
                with self.assertRaisesRegex(ValueError, "configured buffer"):
                    capture.save_replay(181, require_full_duration=True)
                with self.assertRaisesRegex(ValueError, "only 135 s"):
                    capture.save_replay(136, require_full_duration=True)
                self.assertEqual(list(Path(folder).iterdir()), [])
                capture.save_replay(135, require_full_duration=True)
                self.assertEqual(write.call_args.args[-2:], (15, 150))
                capture.save_replay(180)
                self.assertEqual(write.call_args.args[-2:], (15, 150))


class SpeechGateTests(unittest.TestCase):
    def test_idle_noise_and_utterance_endpoint(self):
        gate = SpeechGate()
        for _ in range(500):
            self.assertIsNone(gate.push(b"q", False))
        for _ in range(4):
            self.assertIsNone(gate.push(b"s", True))
        for _ in range(18):
            self.assertIsNone(gate.push(b"q", False))
        for _ in range(10):
            self.assertIsNone(gate.push(b"s", True))
        for _ in range(17):
            self.assertIsNone(gate.push(b"q", False))
        self.assertIn(b"s" * 10, gate.push(b"q", False))

    def test_continuous_speech_is_not_split_into_commands(self):
        gate = SpeechGate()
        for _ in range(650):
            self.assertIsNone(gate.push(b"s", True))
        for _ in range(18):
            self.assertIsNone(gate.push(b"q", False))
        self.assertFalse(gate.discarding)

    def test_disable_and_reconfigure_drops_old_commands(self):
        listener = VoiceListener()
        starting = threading.Event()

        def listen(config, stop, emit):
            emit("command", VoiceMatch("start_recording"))
            starting.set()
            stop.wait(2)

        with patch.object(listener, "_listen", side_effect=listen):
            listener.configure(VoiceConfig(enabled=True))
            try:
                self.assertTrue(starting.wait(2))
                listener.configure(VoiceConfig())
                listener._thread.join(2)
                self.assertFalse(listener._thread.is_alive())
                events = list(listener.read())
                self.assertEqual(events[-1], ("status", "Voice Control is off."))
                self.assertFalse(any(kind == "command" for kind, _ in events))
            finally:
                listener.close()
