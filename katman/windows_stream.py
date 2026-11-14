# SPDX-License-Identifier: GPL-3.0-only
"""Publish a Windows capture using the GSR streaming destination."""
import subprocess
from urllib.parse import urlsplit

from .windows_capture import capture_command


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
    parsed = urlsplit(base)
    if parsed.scheme not in ("rtmp", "rtmps") or not parsed.hostname:
        raise ValueError("Enter a valid RTMP or RTMPS URL in the GSR livestream settings.")
    if not key and service != "custom":
        raise ValueError("Enter the stream key in the GSR livestream settings.")
    return base.rstrip("/") + "/" + key if key else base


def stream_frame_rate(settings):
    options = settings.get("record_options")
    rate = options.get("fps") if isinstance(options, dict) else None
    return rate if type(rate) is int and 1 <= rate <= 500 else 30


class Livestream:
    def __init__(self, display, fps, encoder, destination):
        command = capture_command(display, fps, encoder)
        # Keep the existing capture and encoding options and replace MP4 output.
        command = command[:command.index("-video_track_timescale")]
        command.extend(["-rw_timeout", "5000000", "-f", "flv", destination])
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def poll(self):
        return self.process.poll()

    def stop(self):
        try:
            if self.poll() is None:
                self.process.stdin.write(b"q\n")
                self.process.stdin.flush()
                self.process.wait(timeout=6)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            if self.poll() is None:
                self.process.kill()
            self.process.wait()
        finally:
            self.process.stdin.close()
