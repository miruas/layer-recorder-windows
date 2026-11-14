# SPDX-License-Identifier: GPL-3.0-only
"""Windows display inventory, FFmpeg capture, and memory-only replay."""
import ctypes
from collections import deque
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

from .common import APP, atomic_json, probe

TIMESCALE = 90_000
RECORDING_CONTAINERS = {"mp4": ".mp4", "matroska": ".mkv", "mov": ".mov", "flv": ".flv"}


def recording_container(settings):
    container = settings.get("container", "mp4")
    if not isinstance(container, str) or container not in RECORDING_CONTAINERS:
        raise ValueError("Select MP4, MKV, MOV or FLV in the GSR recording settings.")
    return container


def replay_durations(settings, default_seconds=300):
    seconds = settings.get("time", default_seconds)
    seconds = max(1, min(seconds, 86400)) if type(seconds) is int else default_seconds

    def duration(key, default):
        value = settings.get(key)
        return min(value if type(value) is int and value >= 1 else default, seconds)

    short = duration("short_save_duration", 60)
    return seconds, short, max(short, duration("long_save_duration", 300))


def ac_power_connected():
    class PowerStatus(ctypes.Structure):
        _fields_ = [("ac", wintypes.BYTE), ("battery", wintypes.BYTE),
                    ("percentage", wintypes.BYTE), ("saver", wintypes.BYTE),
                    ("remaining", wintypes.DWORD), ("full", wintypes.DWORD)]

    status = PowerStatus()
    query = ctypes.WinDLL("kernel32").GetSystemPowerStatus
    query.argtypes = [ctypes.POINTER(PowerStatus)]
    return bool(query(ctypes.byref(status)) and status.ac == 1)


def running_steam_game():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            app_id, kind = winreg.QueryValueEx(key, "RunningAppID")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as key:
            pid, pid_kind = winreg.QueryValueEx(key, "pid")
    except OSError:
        return None
    if kind != winreg.REG_DWORD or pid_kind != winreg.REG_DWORD or not app_id or not pid:
        return None
    from .windows_audio import process_name
    name = process_name(pid)
    return app_id if name and name.casefold() == "steam" else None



@dataclass(frozen=True)
class Display:
    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False
    device_id: str = ""
    window: int = 0

    @property
    def key(self):
        return self.device_id or self.name


def displays():
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD),
                    ("device", wintypes.WCHAR * 32)]

    class DisplayDevice(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("name", wintypes.WCHAR * 32),
                    ("description", wintypes.WCHAR * 128), ("flags", wintypes.DWORD),
                    ("device_id", wintypes.WCHAR * 128), ("registry_key", wintypes.WCHAR * 128)]

    user32.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                          ctypes.POINTER(DisplayDevice), wintypes.DWORD]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p,
                                           callback_type, wintypes.LPARAM]
    result = []

    @callback_type
    def visit(handle, _dc, _rect, _data):
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return False
        r = info.monitor
        device = DisplayDevice()
        device.size = ctypes.sizeof(device)
        device_id = device.device_id if user32.EnumDisplayDevicesW(
            info.device, 0, ctypes.byref(device), 1) else ""
        result.append(Display(info.device.removeprefix("\\\\.\\"), r.left, r.top,
                              r.right - r.left, r.bottom - r.top, bool(info.flags & 1), device_id))
        return True

    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not previous:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not user32.EnumDisplayMonitors(None, None, visit, 0):
            raise OSError("Windows could not enumerate the display layout.")
    finally:
        user32.SetThreadDpiAwarenessContext(previous)
    return sorted(result, key=lambda item: (not item.primary, item.name))


def foreground_window():
    user32 = ctypes.WinDLL("user32")
    user32.GetForegroundWindow.restype = wintypes.HWND
    return user32.GetForegroundWindow()


def window_source(handle):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    bounds = wintypes.RECT()
    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        if not handle or user32.IsIconic(handle) or not user32.GetClientRect(handle, ctypes.byref(bounds)):
            raise RuntimeError("The selected window is unavailable or minimized.")
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(previous)
    if min(bounds.right, bounds.bottom) < 2:
        raise RuntimeError("The selected window has no visible client area.")
    return Display("Selected window", 0, 0, bounds.right, bounds.bottom, window=handle)


def capture_command(display, fps, encoder):
    source = ["-f", "gdigrab", "-framerate", str(fps)]
    if display.window:
        source.extend(["-draw_mouse", "0", "-i", f"hwnd={display.window}"])
    else:
        source.extend(["-offset_x", str(display.x),
              "-offset_y", str(display.y), "-video_size", f"{display.width}x{display.height}",
              "-i", "desktop"])
    quality = {
        "libx264": ["-preset", "ultrafast", "-tune", "zerolatency", "-crf", "21"],
        "h264_nvenc": ["-preset", "p3", "-rc", "vbr", "-cq", "21", "-b:v", "0"],
        "h264_amf": ["-quality", "speed", "-b:v", "12M"],
        "h264_qsv": ["-preset", "veryfast", "-global_quality", "21"],
    }
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", *source, "-an",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p",
            "-c:v", encoder, *quality[encoder], "-bf", "0", "-g", str(fps),
            "-force_key_frames", "expr:gte(t,n_forced*1)",
            "-video_track_timescale", str(TIMESCALE), "-movflags",
            "empty_moov+default_base_moof+frag_keyframe+skip_trailer", "-f", "mp4", "pipe:1"]


def desktop_duplication_command(display, fps):
    command = capture_command(display, fps, "h264_nvenc")
    # Pass D3D11 surfaces directly to NVENC. CFR normalizes DXGI timestamps
    # to one frame per output tick before the fragmented MP4 muxer.
    command[4:command.index("-an")] = [
        "-f", "lavfi", "-i", f"ddagrab=output_idx=0:framerate={fps}:output_fmt=bgra",
    ]
    # Leave headroom in the default 512 MiB ring for five minutes of video,
    # independent AAC tracks and container overhead.
    rate = command.index("-b:v")
    command[rate + 2:rate + 2] = ["-maxrate", "11M", "-bufsize", "11M"]
    start = command.index("-vf")
    command[start:command.index("-c:v")] = ["-fps_mode", "cfr", "-r", str(fps)]
    return command


def video_resolution(options):
    if not isinstance(options, dict) or not options.get("change_video_resolution"):
        return None
    width, height = options.get("video_width"), options.get("video_height")
    if not all(type(value) is int and 32 <= value <= 32768 for value in (width, height)):
        raise ValueError("Set a valid output width and height in the GSR capture settings.")
    # H.264 with 4:2:0 chroma requires even coded dimensions.
    return width + width % 2, height + height % 2


def scale_video(command, resolution):
    if resolution is not None:
        width, height = resolution
        command[command.index("-vf") + 1] = f"scale={width}:{height},setsar=1"
    return command


def set_capture_cursor(command, enabled):
    if enabled is None:
        return command
    if "-draw_mouse" in command:
        command[command.index("-draw_mouse") + 1] = str(int(enabled))
    elif "gdigrab" in command:
        index = command.index("-i")
        command[index:index] = ["-draw_mouse", str(int(enabled))]
    elif "lavfi" in command:
        index = command.index("-i") + 1
        command[index] += f":draw_mouse={int(enabled)}"
    return command


def video_cursor(options):
    value = options.get("record_cursor") if isinstance(options, dict) else None
    return value if type(value) is bool else None


def video_color_range(options):
    value = options.get("color_range") if isinstance(options, dict) else None
    return {"full": "pc", "limited": "tv"}.get(value) if isinstance(value, str) else None


def video_rate_control(options):
    if not isinstance(options, dict):
        return None
    quality = options.get("video_quality")
    if quality == "custom":
        bitrate = options.get("video_bitrate")
        if type(bitrate) is not int or not 1 <= bitrate <= 500000:
            raise ValueError("Set a video bitrate between 1 and 500000 Kbps in GSR.")
        return ("cbr", bitrate)
    levels = {"medium": 30, "high": 26, "very_high": 21, "ultra": 16}
    if isinstance(quality, str) and quality in levels:
        return ("quality", levels[quality])
    return None


def set_video_rate_control(command, settings):
    if settings is None:
        return
    mode, value = settings
    codec_pos = command.index("-c:v")
    encoder = command[codec_pos + 1]
    if mode == "cbr":
        end = command.index("-bf", codec_pos)
        preserved = []
        for index in range(codec_pos + 2, end, 2):
            if command[index] in ("-preset", "-tune", "-quality"):
                preserved.extend(command[index:index + 2])
        bitrate = f"{value}k"
        controls = {
            "libx264": ["-x264-params", "nal-hrd=cbr"],
            "h264_nvenc": ["-rc", "cbr"],
            "h264_amf": ["-rc", "cbr"],
            "h264_qsv": [],
        }
        command[codec_pos + 2:end] = preserved + controls[encoder] + [
            "-b:v", bitrate, "-minrate", bitrate, "-maxrate", bitrate, "-bufsize", f"{value * 2}k",
        ]
    elif encoder == "h264_amf":
        index = command.index("-b:v", codec_pos)
        command[index:index + 2] = ["-rc", "cqp", "-qp_i", str(value), "-qp_p", str(value)]
    else:
        flag = {"libx264": "-crf", "h264_nvenc": "-cq", "h264_qsv": "-global_quality"}[encoder]
        command[command.index(flag, codec_pos) + 1] = str(value)


def set_video_color_range(command, color_range):
    if color_range is None:
        return
    index = command.index("-vf") + 1
    filters = command[index].split(",")
    if filters[0].startswith("scale="):
        filters[0] += f":out_range={color_range}"
    else:
        filters.insert(0, f"scale=out_range={color_range}")
    command[index] = ",".join(filters)
    index = command.index("-c:v")
    command[index:index] = ["-color_range", color_range]


def capture_binary():
    return Path(os.environ.get("LAYER_CAPTURE_BINARY", APP / "bin/layer-capture.exe"))


class CaptureEngine:
    def __init__(self, seconds, memory_bytes):
        binary = capture_binary()
        if not binary.is_file():
            raise RuntimeError("Layer capture engine is missing. Run tools/build-windows.ps1 or install the Windows package.")
        self.process = subprocess.Popen([binary, str(seconds), str(memory_bytes)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, encoding="utf-8",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def request(self, op, **args):
        try:
            self.process.stdin.write(json.dumps({"op": op, **args}) + "\n")
            self.process.stdin.flush()
            raw = self.process.stdout.readline()
        except OSError as error:
            raise RuntimeError("Layer capture engine disconnected.") from error
        if not raw:
            raise RuntimeError("Layer capture engine stopped unexpectedly.")
        reply = json.loads(raw)
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["data"]

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        finally:
            self.process.stdout.close()


@dataclass
class Track:
    id: int
    source: object
    fps: int
    media: Path | None
    stopped: bool = False
    backend: str = "gdigrab"

    @property
    def kind(self):
        return "video" if isinstance(self.source, Display) else "audio"


def project_data(folder, kind, tracks, cutoff=None, end=None, warnings=()):
    sources = []
    warnings = list(warnings)
    for track, media, start in tracks:
        try:
            info = probe(media)
            stream = next(s for s in info["streams"] if s["codec_type"] == track.kind)
            available = float(stream.get("duration", info["format"]["duration"]))
        except (OSError, subprocess.SubprocessError, KeyError, ValueError, StopIteration) as error:
            warnings.append(f"{track.source.name}: saved media could not be read ({error}).")
            continue
        if available <= 0:
            continue
        if track.kind == "video":
            display = track.source
            metadata = {"name": display.name, "x": display.x, "y": display.y,
                        "backend": track.backend, "logical_width": display.width,
                        "logical_height": display.height, "width": stream["width"],
                        "height": stream["height"]}
        else:
            metadata = track.source.metadata
        sources.append({**metadata, "id": media.stem, "type": track.kind,
                        "file": media.relative_to(folder).as_posix(), "enabled": True,
                        "gain": 1, "media_duration": available, "timeline_start": start,
                        "trim_in": 0})
    if not sources:
        raise RuntimeError("No complete media was available to save.")
    if end is None:
        end = max(s["timeline_start"] + s["media_duration"] for s in sources)
    if cutoff is None:
        cutoff = min(s["timeline_start"] for s in sources)
    sources = [s for s in sources if s["timeline_start"] < end and
               s["timeline_start"] + s["media_duration"] > cutoff]
    for source in sources:
        relative_start = source["timeline_start"] - cutoff
        source["trim_in"] = max(0, -relative_start)
        source["timeline_start"] = max(0, relative_start)
    if not sources or end <= cutoff:
        raise RuntimeError("No sources overlap the saved interval.")
    project = {"format": "katman", "version": 1, "id": uuid.uuid4().hex, "name": "Desktop",
               "application": "Desktop", "kind": kind, "created": datetime.now().astimezone().isoformat(),
               "fps": tracks[0][0].fps, "duration": end - cutoff, "sources": sources,
               "sections": [], "warnings": list(warnings)}
    # The timeline compositor stops drawing a retired source when its clip ends.
    from .studio_model import from_project
    project["studio"] = from_project(project)
    return project


class Capture:
    def __init__(self, library, selected, fps=30, encoder="libx264", seconds=300,
                 memory_mb=512, record=False, *, audio=(), follow_displays=False,
                 follow_application_audio=False, fixed_region=None, resolution=None, cursor=None,
                 color_range=None, rate_control=None, container="mp4", replay_directory=None,
                 replay_container=None):
        if not selected:
            raise ValueError("Select at least one display.")
        if fps < 1 or seconds <= 0 or memory_mb <= 0:
            raise ValueError("Frame rate, replay duration and memory budget must be positive.")
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("Install FFmpeg and add both ffmpeg and ffprobe to PATH.")
        self.library = Path(library).expanduser().resolve()
        self.replay_directory = Path(replay_directory).expanduser().resolve() if replay_directory else self.library
        self.library.mkdir(parents=True, exist_ok=True)
        self.record_folder = None
        self.fps = fps
        self.encoder = encoder
        self.seconds = seconds
        self.memory_bytes = memory_mb * 1024**2
        self.container = recording_container({"container": container})
        self.replay_container = self.container if replay_container is None else recording_container(
            {"container": replay_container})
        self.engine = CaptureEngine(seconds, self.memory_bytes)
        self.selected = {display.key for display in selected}
        self.audio = {source.key: source for source in audio}
        self.follow_displays = follow_displays
        self.fixed_region = fixed_region
        self.resolution = resolution
        self.cursor = cursor
        self.color_range = color_range
        self.rate_control = rate_control
        self.fixed_windows = [source for source in selected if source.window]
        self.follow_application_audio = follow_application_audio
        self.tracks = []
        self.active = {}
        self.serial = 0
        self.retry_at = {}
        self.warnings = deque(maxlen=20)
        self.final_project = None
        self.paused = False
        self.duplication_failed = set()
        self.single_display = None
        if encoder == "h264_nvenc":
            try:
                self._update_duplication_layout(displays())
            except OSError:
                pass
        try:
            if record:
                self.record_folder = Path(tempfile.mkdtemp(prefix="Layer-recording-", dir=self.library))
                (self.record_folder / "media").mkdir()
            for source in selected:
                self._start_track(source)
            for source in audio:
                self._try_start(source)
        except Exception:
            self.stop()
            if self.record_folder:
                shutil.rmtree(self.record_folder)
            raise

    def _update_duplication_layout(self, current):
        self.single_display = current[0] if len(current) == 1 and current[0].primary else None

    def _backend(self, source):
        if (self.resolution is None and self.color_range != "pc" and
                self.encoder == "h264_nvenc" and source == self.single_display and
                source.key not in self.duplication_failed and source.width % 2 == source.height % 2 == 0):
            return "ddagrab"
        return "gdigrab"

    def _start_track(self, source):
        self.serial += 1
        if isinstance(source, Display):
            backend = self._backend(source)
            command = (desktop_duplication_command(source, self.fps) if backend == "ddagrab"
                       else scale_video(capture_command(source, self.fps, self.encoder), self.resolution))
            set_capture_cursor(command, self.cursor)
            set_video_rate_control(command, self.rate_control)
            if backend == "gdigrab":
                set_video_color_range(command, self.color_range)
            stem, timescale = "display", TIMESCALE
        else:
            from .windows_audio import AUDIO_TIMESCALE, capture_audio_command
            command = capture_audio_command(source)
            stem, timescale = "audio", AUDIO_TIMESCALE
            backend = "wasapi"
        path = self.record_folder / "media" / f"{stem}-{self.serial}.mp4" if self.record_folder else None
        try:
            self.engine.request("start", id=self.serial, command=command, scale=timescale,
                                media=str(path) if path else None)
        except (OSError, RuntimeError):
            if path:
                path.unlink(missing_ok=True)
            raise
        track = Track(self.serial, source, self.fps, path, backend=backend)
        self.tracks.append(track)
        self.active[source.key] = track

    def _try_start(self, source):
        try:
            self._start_track(source)
        except (OSError, RuntimeError, ValueError) as error:
            self.warnings.append(f"{source.name}: {error}")
            self.retry_at[source.key] = time.monotonic() + 5

    def _retire(self, key):
        track = self.active.pop(key)
        track.stopped = True
        error = self.engine.request("stop", id=track.id)
        if error:
            self.warnings.append(f"{track.source.name}: {error}")

    def reconcile(self, current, current_audio=None):
        """Reconcile device generations on the capture-control worker."""
        if self.paused:
            return 0
        self._update_duplication_layout(current)
        desired = {display.key: display for display in current
                   if self.follow_displays or display.key in self.selected}
        if self.fixed_region is not None:
            region = self.fixed_region
            # Keep the region only while a monitor contains its full rectangle.
            if any(display.x <= region.x and display.y <= region.y and
                   region.x + region.width <= display.x + display.width and
                   region.y + region.height <= display.y + display.height for display in current):
                desired[region.key] = region
        for window in self.fixed_windows:
            try:
                desired[window.key] = window_source(window.window)
            except RuntimeError:
                continue
        if current_audio is None:
            desired.update(self.audio)
            desired.update((key, track.source) for key, track in self.active.items()
                           if track.kind == "audio")
        else:
            desired.update((source.key, source) for source in current_audio
                           if source.key in self.audio or
                           (self.follow_application_audio and source.kind == "application"))
        now = time.monotonic()
        for key, track in tuple(self.active.items()):
            source = desired.get(key)
            failure = self.engine.request("failure", id=track.id)
            if failure and track.backend == "ddagrab" and source == track.source:
                self.duplication_failed.add(key)
                self.warnings.append(f"{source.name}: Desktop Duplication failed. Using GDI with NVENC.")
                self._retire(key)
            elif (source != track.source or failure or
                  (track.kind == "video" and track.backend != self._backend(track.source))):
                self._retire(key)
                if failure and source == track.source:
                    self.retry_at[key] = now + 5
        for key, source in desired.items():
            if key not in self.active and now >= self.retry_at.get(key, 0):
                self._try_start(source)
        self.retry_at = {key: value for key, value in self.retry_at.items() if key in desired}
        self._prune()
        return sum(track.kind == "video" for track in self.active.values())

    def _status(self):
        return {row["id"]: row for row in self.engine.request("status")["tracks"]}

    def replay_status(self):
        state = self.engine.request("status")
        tracks = {track.id: track for track in self.tracks}
        intervals = []
        live_ends = []
        backends = set()
        for row in state["tracks"]:
            track = tracks[row["id"]]
            if track.kind != "video" or not row["count"]:
                continue
            start = row["started"]
            end = start + row["last"] + 1
            intervals.append((start + row["first"], end))
            if not track.stopped:
                live_ends.append(end)
                backends.add(track.backend)
        seconds = 0
        if intervals:
            end = min(live_ends) if live_ends else max(last for _, last in intervals)
            seconds = max(0, min(self.seconds, end - min(first for first, _ in intervals)))
        return seconds, state["bytes"], sorted(backends)

    def _prune(self):
        if self.record_folder:
            return
        state = self._status()
        for track in self.tracks[:]:
            if track.stopped and not state[track.id]["count"]:
                self.engine.request("forget", id=track.id)
                self.tracks.remove(track)

    def failure(self):
        for track in self.active.values():
            if error := self.engine.request("failure", id=track.id):
                return f"{track.source.name}: {error}"
        return None

    def _stop_tracks(self):
        for key in tuple(self.active):
            try:
                self._retire(key)
            except (OSError, RuntimeError) as error:
                self.warnings.append(str(error))

    def toggle_pause(self):
        if not self.record_folder:
            raise RuntimeError("No ordinary recording is active.")
        if self.paused:
            current = displays()
            current_audio = None
            if self.audio or self.follow_application_audio:
                from .windows_audio import audio_sources
                current_audio = audio_sources()
            self.engine.request("resume_clock")
            self.paused = False
            self.retry_at.clear()
            self.reconcile(current, current_audio)
        else:
            self.engine.request("pause_clock")
            self.paused = True
            self._stop_tracks()
        return self.paused

    def stop(self):
        self._stop_tracks()
        if self.engine:
            self.engine.close()
            self.engine = None
        return list(self.warnings)

    def finish_recording(self):
        if not self.record_folder:
            raise RuntimeError("No ordinary recording is active.")
        if self.final_project:
            return self.final_project
        self._stop_tracks()
        try:
            state = self._status()
            tracks = [(track, track.media, state[track.id]["started"]) for track in self.tracks
                      if state[track.id]["started"] is not None]
            if not tracks:
                raise RuntimeError(f"No media was completed. Recording folder: {self.record_folder}. "
                                   + " ".join(self.warnings))
            path = self.record_folder / "project.katman"
            self._write_project(path, "recording", tracks)
            self.final_project = path
            return path
        finally:
            self.stop()

    def save_replay(self, seconds):
        if not 0 < seconds <= self.seconds:
            raise ValueError("Replay duration must fit inside the configured buffer.")
        state = self._status()
        snapshots = [(track, state[track.id]) for track in self.tracks if state[track.id]["count"]]
        if not snapshots:
            raise RuntimeError("Replay is still warming up, or the memory budget is too small.")
        bounds = [(row["started"] + row["last"] + 1, track) for track, row in snapshots]
        live_video = [end for end, track in bounds if not track.stopped and track.kind == "video"]
        end = min(live_video) if live_video else max(end for end, _ in bounds)
        cutoff = max(min(row["started"] + row["first"] for _, row in snapshots), end - seconds)
        self.replay_directory.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="Layer-replay-", dir=self.replay_directory))
        try:
            (folder / "media").mkdir()
            tracks = []
            for track, row in snapshots:
                start = row["started"]
                media = folder / "media" / f"{track.kind}-{track.id}.mp4"
                first = self.engine.request("snapshot", id=track.id, path=str(media),
                                            **{"from": cutoff - start, "to": end - start})
                if first is not None:
                    tracks.append((track, media, start + first))
            path = folder / "project.katman"
            self._write_project(path, "replay", tracks, cutoff, end)
            return path
        except Exception:
            shutil.rmtree(folder)
            raise

    def _write_project(self, path, kind, tracks, cutoff=None, end=None):
        container = self.replay_container if kind == "replay" else self.container
        extension = RECORDING_CONTAINERS[container]
        converted = []
        cleanup = []
        warnings = list(self.warnings)
        for track, media, start in tracks:
            if extension == media.suffix:
                converted.append((track, media, start))
                continue
            output = media.with_suffix(extension)
            pending = media.with_suffix(".pending" + extension)
            try:
                subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(media),
                                "-map", "0", "-c", "copy", "-f", container, str(pending)],
                               check=True, capture_output=True, timeout=120,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                pending.replace(output)
                converted.append((track, output, start))
                cleanup.append((media, output))
            except (OSError, subprocess.SubprocessError):
                warnings.append(f"{media.name}: could not save as {extension}; kept the original MP4.")
                converted.append((track, media, start))
            finally:
                pending.unlink(missing_ok=True)
        project = project_data(path.parent, kind, converted, cutoff, end, warnings)
        atomic_json(path, project)
        used = {source["file"] for source in project["sources"]}
        for original, output in cleanup:
            if output.relative_to(path.parent).as_posix() in used:
                try:
                    original.unlink(missing_ok=True)
                except OSError:
                    pass
