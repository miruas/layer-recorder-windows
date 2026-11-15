# SPDX-License-Identifier: GPL-3.0-only
"""Offline microphone listener. Audio and transcripts live only in memory."""

import ctypes
import queue
import threading
import time
from collections import deque

from .common import CACHE
from .voice_control import CommandMatcher

MODEL_ID = "Systran/faster-whisper-tiny.en"
MODEL_REVISION = "0d3d19a32d3338f10357c0889762bd8d64bbdeba"
MODEL_CACHE = CACHE / "voice-models"
MODEL_FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")
RATE = 16000
FRAME = 480  # WebRTC VAD accepts 30 ms of 16-bit mono PCM.
MIN_RMS = 24  # About -63 dBFS. Reject very low-level noise before running VAD.
_MISSING_MODEL = "Install the English model in Voice Control settings before enabling it."


def install_model():
    from huggingface_hub import snapshot_download

    return snapshot_download(MODEL_ID, revision=MODEL_REVISION, cache_dir=MODEL_CACHE,
                             allow_patterns=MODEL_FILES, token=False)


def local_model():
    path = MODEL_CACHE / ("models--" + MODEL_ID.replace("/", "--")) / "snapshots" / MODEL_REVISION
    if not all((path / name).is_file() for name in MODEL_FILES):
        raise RuntimeError(_MISSING_MODEL)
    return path


def _devices(sd):
    for api in sd.query_hostapis():
        if "WASAPI" in api["name"]:
            devices = {index: sd.query_devices(index)["name"] for index in api["devices"]
                       if sd.query_devices(index)["max_input_channels"] > 0}
            return devices, api["default_input_device"]
    raise RuntimeError("Windows WASAPI is unavailable.")


def microphones():
    import sounddevice as sd

    devices, _ = _devices(sd)
    return tuple(dict.fromkeys(devices.values()))


def recognize(model, pcm, wake_phrase):
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    segments, _ = model.transcribe(samples, language="en", beam_size=1, temperature=0.0,
                                   condition_on_previous_text=False, without_timestamps=True,
                                   initial_prompt=f"The wake phrase is {wake_phrase}.",
                                   max_new_tokens=48)
    segments = list(segments)
    if any(segment.avg_logprob < -0.85 or segment.no_speech_prob > 0.6 for segment in segments):
        return ""
    return " ".join(segment.text for segment in segments)


class SpeechGate:
    """Buffer short voiced utterances with pre-roll. Drop interrupted or long speech."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.history = deque(maxlen=10)
        self.frames = []
        self.quiet = self.voiced = 0
        self.discarding = False

    def push(self, pcm, speech):
        if self.discarding:
            self.quiet = 0 if speech else self.quiet + 1
            if self.quiet >= 18:
                self.reset()
            return None

        if not self.frames:
            self.history.append((pcm, speech))
            self.voiced = sum(active for _, active in self.history)
            if speech and self.voiced >= 3:
                self.frames = [frame for frame, _ in self.history]
                self.quiet = 0
            return None

        self.frames.append(pcm)
        self.quiet = 0 if speech else self.quiet + 1
        self.voiced += speech
        if len(self.frames) >= 300:
            self.frames.clear()
            self.discarding = True
            return None
        if self.quiet < 18:
            return None
        audio = b"".join(self.frames[:-12]) if self.voiced >= 6 else None
        self.reset()
        return audio


class VoiceListener:
    """One worker, one microphone. The UI only receives status and matched actions."""

    def __init__(self):
        self.events = queue.SimpleQueue()
        self._thread = None
        self._stop = None
        self._config = None
        self._generation = 0
        self._started = 0

    def configure(self, config):
        self._generation += 1
        self._config = config
        if self._thread is not None and self._thread.is_alive():
            self.events.put((self._generation, "status", "Updating Voice Control…"))
            self._stop.set()
            return
        self._launch()

    def _launch(self):
        self._started = self._generation
        config = self._config
        if config is None or not config.enabled:
            self.events.put((self._generation, "status", "Voice Control is off."))
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(config, self._stop, self._generation),
                                        name="Layer Voice Control", daemon=True)
        self._thread.start()

    def read(self):
        if self._started != self._generation and (self._thread is None or not self._thread.is_alive()):
            self._launch()
        while True:
            try:
                generation, kind, value = self.events.get_nowait()
            except queue.Empty:
                return
            if generation == self._generation:
                yield kind, value

    def close(self):
        self._generation += 1
        self._config = None
        if self._stop is not None:
            self._stop.set()

    def _run(self, config, stop, generation):
        def emit(kind, value):
            if not stop.is_set():
                self.events.put((generation, kind, value))

        try:
            self._listen(config, stop, emit)
        except Exception as error:
            emit("status", f"Voice Control: {error}")

    @staticmethod
    def _listen(config, stop, emit):
        model_path = local_model()
        import numpy as np
        import sounddevice as sd
        import webrtcvad
        from faster_whisper import WhisperModel

        # The microphone callback is managed by PortAudio. Lower the decoder worker priority.
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentThread.restype = ctypes.c_void_p
        kernel.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel.SetThreadPriority(kernel.GetCurrentThread(), -1)

        devices, default = _devices(sd)
        if config.microphone:
            device = next((index for index, name in devices.items() if name == config.microphone), -1)
        else:
            device = default
        if device not in devices:
            raise RuntimeError("Select an available microphone in Voice Control settings.")

        model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=1,
                             num_workers=1, local_files_only=True)
        if stop.is_set():
            return
        matcher = CommandMatcher(config)
        vad, gate = webrtcvad.Vad(3), SpeechGate()
        frames = queue.Queue(maxsize=40)
        interrupted = threading.Event()
        finished = threading.Event()
        last = None
        last_time = 0.0

        def on_audio(indata, frame_count, timestamp, status):
            if stop.is_set():
                raise sd.CallbackStop
            if status:
                interrupted.set()
            try:
                frames.put_nowait(bytes(indata))
            except queue.Full:
                interrupted.set()

        def on_finished():
            finished.set()
            try:
                frames.put_nowait(None)
            except queue.Full:
                pass

        with sd.RawInputStream(device=device, channels=1, samplerate=RATE, blocksize=FRAME,
                               dtype="int16", extra_settings=sd.WasapiSettings(auto_convert=True),
                               callback=on_audio, finished_callback=on_finished):
            emit("status", f"Listening for “{config.wake_phrase}” · {devices[device]}")
            while not stop.is_set():
                pcm = frames.get()
                if finished.is_set() or pcm is None:
                    if not stop.is_set():
                        raise RuntimeError("The microphone stream ended. Check the device and reapply settings.")
                    break
                if interrupted.is_set():
                    interrupted.clear()
                    while True:
                        try:
                            frames.get_nowait()
                        except queue.Empty:
                            break
                    if finished.is_set():
                        if not stop.is_set():
                            raise RuntimeError("The microphone stream ended. Check the device and reapply settings.")
                        break
                    gate.reset()
                    continue
                if len(pcm) != FRAME * 2:
                    gate.reset()
                    continue
                samples = np.frombuffer(pcm, dtype="<i2")
                speech = np.linalg.norm(samples) >= MIN_RMS * FRAME**0.5 and vad.is_speech(pcm, RATE)
                audio = gate.push(pcm, speech)
                if audio is None:
                    continue
                match = matcher.match(recognize(model, audio, config.wake_phrase))
                now = time.monotonic()
                if match is not None and (match != last or now - last_time >= 2.5):
                    last, last_time = match, now
                    emit("command", match)
