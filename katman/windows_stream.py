# SPDX-License-Identifier: GPL-3.0-only
"""Publish a Windows capture using the GSR streaming destination."""
import subprocess
import threading
import time
from queue import Empty, Full, Queue
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .windows_capture import (
    capture_command, desktop_duplication_command, displays, dxgi_targets, scale_video,
    set_capture_cursor, set_video_color_range,
    set_video_framerate_mode, set_video_rate_control,
)
from .windows_audio import AUDIO_TIMESCALE, CHANNELS, FRAME_BYTES, capture_pcm, gsr_audio_selection


@dataclass(frozen=True)
class WhipDestination:
    url: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class CustomDestination:
    url: str = field(repr=False)
    container: str


def stream_destination(settings):
    service = settings.get("service", "custom")
    providers = {
        "twitch": "rtmp://live.twitch.tv/app",
        "youtube": "rtmp://a.rtmp.youtube.com/live2",
        "rumble": "rtmp://rtmp.rumble.com/live",
    }
    options = settings.get(service, {}) if isinstance(service, str) else {}
    if not isinstance(options, dict):
        raise ValueError("Invalid GSR livestream settings.")
    if service == "whip":
        url, token = options.get("url", ""), options.get("bearer_token", "")
        if not isinstance(url, str) or not isinstance(token, str) or "\r" in token or "\n" in token:
            raise ValueError("Invalid GSR WHIP URL or bearer token.")
        url = url.strip()
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.fragment:
            raise ValueError("Enter a valid HTTP or HTTPS WHIP endpoint in the GSR livestream settings.")
        return WhipDestination(url, token.strip())
    if service in providers:
        base = providers[service]
    elif service in ("custom", "kick"):
        base = options.get("url", "")
    else:
        raise ValueError("Select an RTMP or RTMPS service in the GSR livestream settings.")
    key = options.get("key", "")
    if not isinstance(base, str) or not isinstance(key, str):
        raise ValueError("Invalid GSR livestream URL or key.")
    base, key = base.strip(), key.strip()
    if base and service == "custom" and "://" not in base:
        base = "rtmp://" + base
    parsed = urlsplit(base)
    if not parsed.hostname:
        raise ValueError("Enter a valid livestream URL.")
    if service == "custom":
        container = options.get("container", "flv")
        protocols = {
            "flv": ("rtmp", "rtmps", "tcp", "http", "https"),
            "mp4": ("tcp", "http", "https"),
            "mpegts": ("srt", "tcp", "udp", "http", "https"),
            "hls": ("http", "https"),
        }
        if not isinstance(container, str) or parsed.scheme not in protocols.get(container, ()):
            raise ValueError("The selected GSR container does not support this publishing URL.")
        if key:
            base += ("" if base.endswith(("/", "=")) else "/") + key
        return CustomDestination(base, container)
    if parsed.scheme not in ("rtmp", "rtmps"):
        raise ValueError("Enter a valid RTMP or RTMPS URL in the GSR livestream settings.")
    if not key:
        raise ValueError("Enter the stream key in the GSR livestream settings.")
    return base.rstrip("/") + "/" + key if key else base


def stream_frame_rate(settings):
    options = settings.get("record_options")
    rate = options.get("fps") if isinstance(options, dict) else None
    return rate if type(rate) is int and 1 <= rate <= 500 else 30


def stream_audio_sources(settings, inventory):
    options = settings.get("record_options") or {}
    tracks = options.get("audio_track_item", [{"audio_inputs": ["default_output"]}])
    return gsr_audio_selection(tracks, inventory)


def mix_audio(sources, output, stop):
    import numpy as np

    block_size = AUDIO_TIMESCALE // 50 * FRAME_BYTES
    errors = Queue()

    class Input:
        def __init__(self):
            self.blocks = Queue(maxsize=25)
            self.pending = bytearray()

        def write(self, data):
            self.pending.extend(data)
            while len(self.pending) >= block_size and not stop.is_set():
                try:
                    self.blocks.put(bytes(self.pending[:block_size]), timeout=.1)
                except Full:
                    continue
                del self.pending[:block_size]

    def read(source, sink):
        try:
            capture_pcm(source, sink, stop)
            if not stop.is_set():
                raise RuntimeError(f"{source.name}: Windows audio capture ended.")
        except Exception as error:
            errors.put(error)
            stop.set()

    inputs = [Input() for _ in sources]
    readers = [threading.Thread(target=read, args=(source, sink), daemon=True)
               for source, sink in zip(sources, inputs)]
    for reader in readers:
        reader.start()
    try:
        while not stop.is_set():
            mixed = np.zeros(block_size // 2, dtype=np.int32)
            for source, sink in zip(sources, inputs):
                deadline = time.monotonic() + 3
                while not stop.is_set():
                    try:
                        block = sink.blocks.get(timeout=.1)
                    except Empty:
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f"{source.name}: Windows audio capture stopped responding.")
                        continue
                    mixed += np.frombuffer(block, dtype="<i2")
                    break
            if not stop.is_set():
                output.write(mixed.clip(-32768, 32767).astype("<i2").tobytes())
        if not errors.empty():
            raise errors.get()
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=2)


def stream_video_command(display, fps, encoder, resolution, cursor, color_range, rate_control, framerate_mode):
    if encoder == "h264_nvenc" and resolution is None and color_range != "pc" and (
            framerate_mode != "content" and not display.window and display.width % 2 == display.height % 2 == 0):
        try:
            monitor = next((monitor for monitor in displays() if display == monitor or (
                display.name == "Selected region" and monitor.x <= display.x and monitor.y <= display.y and
                display.x + display.width <= monitor.x + monitor.width and
                display.y + display.height <= monitor.y + monitor.height)), None)
            target = dxgi_targets().get(monitor.name.casefold()) if monitor else None
            if target is not None:
                command = desktop_duplication_command(display, fps, monitor, target)
                set_capture_cursor(command, cursor)
                set_video_rate_control(command, rate_control)
                set_video_framerate_mode(command, framerate_mode, fps)
                # Check capture and encoding before opening a connection to the publisher.
                probe = command[:command.index("-video_track_timescale")]
                subprocess.run(probe + ["-frames:v", "1", "-f", "null", "-"], check=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                return command
        except (OSError, subprocess.SubprocessError):
            pass
    command = scale_video(capture_command(display, fps, encoder), resolution)
    set_capture_cursor(command, cursor)
    set_video_color_range(command, color_range)
    set_video_rate_control(command, rate_control)
    set_video_framerate_mode(command, framerate_mode, fps)
    return command


class Livestream:
    def __init__(self, display, fps, encoder, destination, audio=(), *, resolution=None, cursor=None,
                 color_range=None, rate_control=None, framerate_mode=None):
        whip = isinstance(destination, WhipDestination)
        command = stream_video_command(display, fps, encoder, resolution, cursor, color_range, rate_control,
                                       framerate_mode)
        # Keep the existing capture and encoding options and replace MP4 output.
        command = command[:command.index("-video_track_timescale")]
        if audio:
            maps = ["-map", "0:v:0", "-map", "1:a:0"]
            if "-filter_complex" in command:
                graph = command.index("-filter_complex") + 1
                command[graph] += ",setpts=PTS-STARTPTS[video]"
                maps = ["-map", "[video]", "-map", "0:a:0"]
            index = command.index("-an")
            command[index:index + 1] = [
                "-thread_queue_size", "64", "-analyzeduration", "1", "-probesize", "4096",
                "-f", "s16le", "-ar", str(AUDIO_TIMESCALE),
                "-ac", str(CHANNELS), "-i", "pipe:0", *maps,
                "-c:a", "libopus" if whip else "aac", "-b:a", "160k", "-shortest",
            ]
            if "-filter_complex" in command:
                # FFmpeg's shortest-stream queue stalls this source-only video graph.
                command.remove("-shortest")
            command.insert(1, "-nostdin")
        if whip:
            command.extend(["-f", "whip", "-handshake_timeout", "5000", "-timeout", "5s"])
            if destination.token:
                command.extend(["-authorization", destination.token])
            command.append(destination.url)
        elif isinstance(destination, CustomDestination):
            command.extend(["-rw_timeout", "5000000"])
            if destination.container == "mp4":
                command.extend(["-movflags", "empty_moov+default_base_moof+frag_keyframe"])
            elif destination.container == "hls":
                command.extend(["-method", "PUT", "-hls_time", "2"])
            command.extend(["-f", destination.container, destination.url])
        else:
            command.extend(["-rw_timeout", "5000000", "-f", "flv", destination])
        self.audio_stop = threading.Event()
        self.audio_thread = None
        self.audio_error = None
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if audio:
            self.audio_thread = threading.Thread(target=self._feed_audio, args=(audio,), daemon=True)
            self.audio_thread.start()

    def _feed_audio(self, sources):
        try:
            with self.process.stdin as output:
                if len(sources) == 1:
                    capture_pcm(sources[0], output, self.audio_stop)
                else:
                    mix_audio(sources, output, self.audio_stop)
        except BrokenPipeError:
            pass
        except Exception as error:
            self.audio_error = str(error)

    def poll(self):
        return self.process.poll()

    def stop(self):
        try:
            if self.audio_thread:
                self.audio_stop.set()
            if self.poll() is None:
                if not self.audio_thread:
                    self.process.stdin.write(b"q\n")
                    self.process.stdin.flush()
                self.process.wait(timeout=6)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            if self.poll() is None:
                self.process.kill()
            self.process.wait()
        finally:
            if self.audio_thread:
                self.audio_thread.join(timeout=2)
            else:
                self.process.stdin.close()
