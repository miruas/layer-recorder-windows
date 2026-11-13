# SPDX-License-Identifier: GPL-3.0-only
"""Windows display inventory, FFmpeg capture, and memory-only replay."""
import ctypes
from collections import deque
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import uuid

from .common import atomic_json, probe

TIMESCALE = 90_000
MAX_BOX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Display:
    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False
    device_id: str = ""

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


def capture_command(display, fps, encoder):
    source = ["-f", "gdigrab", "-framerate", str(fps), "-offset_x", str(display.x),
              "-offset_y", str(display.y), "-video_size", f"{display.width}x{display.height}",
              "-i", "desktop"]
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


def read_boxes(stream):
    while header := stream.read(8):
        if len(header) != 8:
            raise ValueError("Incomplete MP4 box header.")
        size, kind = struct.unpack(">I4s", header)
        if not 8 <= size <= MAX_BOX_BYTES:
            raise ValueError(f"Unsupported MP4 box size ({size} bytes).")
        data = stream.read(size - 8)
        if len(data) != size - 8:
            raise ValueError("Incomplete MP4 box.")
        yield kind, header + data


def fragment_timestamp(box):
    def children(data):
        position = 0
        while position + 8 <= len(data):
            size, kind = struct.unpack_from(">I4s", data, position)
            if size < 8 or position + size > len(data):
                raise ValueError("Invalid fragmented MP4 structure.")
            yield kind, position + 8, data[position + 8:position + size]
            position += size

    for kind, track_offset, track in children(box[8:]):
        if kind == b"traf":
            for child, offset, body in children(track):
                if child == b"tfdt" and len(body) >= 8:
                    fmt = ">Q" if body[0] == 1 else ">I"
                    return fmt, 8 + track_offset + offset + 4, struct.unpack_from(fmt, body, 4)[0]
    raise ValueError("MP4 fragment is missing its decode timestamp.")


@dataclass(frozen=True)
class Fragment:
    start: float
    moof: bytes
    mdat: bytes

    @property
    def size(self):
        return len(self.moof) + len(self.mdat)


class ReplayBuffer:
    def __init__(self, seconds, byte_limit):
        self.seconds = seconds
        self.byte_limit = byte_limit
        self.fragments = deque()
        self.bytes = 0

    def add(self, fragment):
        self.fragments.append(fragment)
        self.bytes += fragment.size
        while (self.fragments and
               (self.bytes > self.byte_limit or
                (len(self.fragments) > 1 and
                 self.fragments[1].start <= fragment.start - self.seconds))):
            self.evict()

    def evict(self):
        self.bytes -= self.fragments.popleft().size


class ReplayBudget:
    def __init__(self, limit):
        self.limit = limit
        self.lock = threading.RLock()
        self.tracks = []

    def add(self, track, fragment):
        with self.lock:
            track.replay.add(fragment)
            self._trim(time.monotonic())

    def _trim(self, now):
        candidates = []
        total = 0
        for index, track in enumerate(self.tracks):
            ring = track.replay
            while (ring.fragments and
                   track.started + ring.fragments[0].start + 1 < now - ring.seconds):
                ring.evict()
            if ring.fragments:
                candidates.append((track.started + ring.fragments[0].start, index, track))
                total += ring.bytes
        while total > self.limit:
            _, _, track = min(candidates)
            ring = track.replay
            total -= ring.fragments[0].size
            ring.evict()
            candidates = [(item.started + item.replay.fragments[0].start, index, item)
                          for _, index, item in candidates if item.replay.fragments]


class Track:
    def __init__(self, source, command, fps, seconds, budget, media=None, timescale=TIMESCALE):
        self.source = source
        self.media = media
        self.timescale = timescale
        self.kind = "video" if isinstance(source, Display) else "audio"
        self.command = command
        self.fps = fps
        self.budget = budget
        self.replay = ReplayBuffer(seconds, budget.limit)
        self.recording = media.open("wb") if media else None
        self.lock = budget.lock
        self.init = []
        self.started = None
        self.error = None
        self.process = None
        self.stopped = False

    def start(self):
        self.log = deque(maxlen=8)
        self.launched = time.monotonic()
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.log_reader = threading.Thread(target=self.read_log, daemon=True)
        self.log_reader.start()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read_log(self):
        with self.process.stderr as stream:
            while block := stream.read1(4096):
                with self.lock:
                    self.log.append(block)

    def read(self):
        pending = None
        try:
            for kind, box in read_boxes(self.process.stdout):
                if kind == b"moof":
                    pending = box
                elif kind == b"mdat" and pending is not None:
                    fragment = Fragment(fragment_timestamp(pending)[2] / self.timescale, pending, box)
                    with self.lock:
                        if self.started is None:
                            # The first complete fragment usually flushes after one second.
                            self.started = max(self.launched, time.monotonic() - 1.0)
                        self.budget.add(self, fragment)
                    pending = None
                elif kind in {b"ftyp", b"moov"}:
                    with self.lock:
                        self.init.append(box)
                if self.recording is not None:
                    self.recording.write(box)
        except (OSError, ValueError, struct.error) as error:
            self.error = str(error)
            if self.process.poll() is None:
                self.process.kill()
        finally:
            self.process.stdout.close()

    def failure(self):
        code = self.process.poll()
        if self.error:
            return self.error
        if code is not None and code != 0:
            with self.lock:
                details = b"".join(self.log).decode("utf-8", errors="replace").strip()
            return details or f"FFmpeg exited with {code}."
        return None

    def stop(self):
        if self.stopped:
            return self.error
        self.stopped = True
        try:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write(b"q\n")
                    self.process.stdin.flush()
                except OSError:
                    pass
                try:
                    self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.reader.join()
            self.log_reader.join()
            self.error = self.failure()
            return self.error
        finally:
            self.process.stdin.close()
            if self.recording is not None:
                self.recording.close()

    def snapshot(self):
        with self.lock:
            return self.started, tuple(self.init), tuple(self.replay.fragments)


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
                        "backend": "gdigrab", "logical_width": display.width,
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
                 follow_application_audio=False):
        if not selected:
            raise ValueError("Select at least one display.")
        if fps < 1 or seconds <= 0 or memory_mb <= 0:
            raise ValueError("Frame rate, replay duration and memory budget must be positive.")
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("Install FFmpeg and add both ffmpeg and ffprobe to PATH.")
        self.library = Path(library).expanduser().resolve()
        self.library.mkdir(parents=True, exist_ok=True)
        self.record_folder = Path(tempfile.mkdtemp(prefix="Layer-recording-", dir=self.library)) if record else None
        self.fps = fps
        self.encoder = encoder
        self.seconds = seconds
        self.budget = ReplayBudget(memory_mb * 1024**2)
        self.selected = {display.key for display in selected}
        self.audio = {source.key: source for source in audio}
        self.follow_displays = follow_displays
        self.follow_application_audio = follow_application_audio
        self.tracks = self.budget.tracks
        self.active = {}
        self.serial = 0
        self.retry_at = {}
        self.warnings = deque(maxlen=20)
        self.final_project = None
        if self.record_folder:
            (self.record_folder / "media").mkdir()
        try:
            for source in selected:
                self._start_track(source)
            for source in audio:
                self._try_start(source)
        except Exception:
            self.stop()
            if self.record_folder:
                shutil.rmtree(self.record_folder)
            raise

    def _start_track(self, source):
        self.serial += 1
        if isinstance(source, Display):
            command = capture_command(source, self.fps, self.encoder)
            stem, timescale = "display", TIMESCALE
        else:
            from .windows_audio import AUDIO_TIMESCALE, capture_audio_command
            command = capture_audio_command(source)
            stem, timescale = "audio", AUDIO_TIMESCALE
        path = self.record_folder / "media" / f"{stem}-{self.serial}.mp4" if self.record_folder else None
        track = Track(source, command, self.fps, self.seconds, self.budget, path, timescale)
        with self.budget.lock:
            self.tracks.append(track)
        try:
            track.start()
        except OSError:
            with self.budget.lock:
                self.tracks.remove(track)
            if track.recording:
                track.recording.close()
            if path:
                path.unlink(missing_ok=True)
            raise
        self.active[source.key] = track

    def _try_start(self, source):
        try:
            self._start_track(source)
        except (OSError, RuntimeError, ValueError) as error:
            self.warnings.append(f"{source.name}: {error}")
            self.retry_at[source.key] = time.monotonic() + 5

    def _retire(self, key):
        track = self.active.pop(key)
        error = track.stop()
        if error:
            self.warnings.append(f"{track.source.name}: {error}")

    def reconcile(self, current, current_audio=None):
        """Reconcile device generations on the capture-control worker."""
        desired = {display.key: display for display in current
                   if self.follow_displays or display.key in self.selected}
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
            if source is None or source != track.source or track.process.poll() is not None:
                self._retire(key)
                if source == track.source:
                    self.retry_at[key] = now + 5
        for key, source in desired.items():
            if key not in self.active and now >= self.retry_at.get(key, 0):
                self._try_start(source)
        self.retry_at = {key: value for key, value in self.retry_at.items() if key in desired}
        self._prune(time.monotonic())
        return sum(track.kind == "video" for track in self.active.values())

    def _prune(self, now):
        with self.budget.lock:
            self.budget._trim(now)
            if not self.record_folder:
                self.tracks[:] = [track for track in self.tracks
                                  if not track.stopped or track.replay.fragments]

    def failure(self):
        for track in self.active.values():
            error = track.failure()
            if error:
                return f"{track.source.name}: {error}"
            if track.process.poll() is not None:
                return f"{track.source.name}: capture stopped unexpectedly."
        return None

    def stop(self):
        for key in tuple(self.active):
            self._retire(key)
        return list(self.warnings)

    def finish_recording(self):
        if not self.record_folder:
            raise RuntimeError("No ordinary recording is active.")
        if self.final_project:
            return self.final_project
        self.stop()
        tracks = [(track, track.media, start) for track in self.tracks
                  if (start := track.snapshot()[0]) is not None]
        if not tracks:
            raise RuntimeError(f"No media was completed. Recording folder: {self.record_folder}. "
                               + " ".join(self.warnings))
        path = self.record_folder / "project.katman"
        atomic_json(path, project_data(self.record_folder, "recording", tracks,
                                       warnings=self.warnings))
        self.final_project = path
        return path

    def save_replay(self, seconds):
        if not 0 < seconds <= self.seconds:
            raise ValueError("Replay duration must fit inside the configured buffer.")
        with self.budget.lock:
            self._prune(time.monotonic())
            snapshots = []
            for track in self.tracks:
                start, init, fragments = track.snapshot()
                if start is not None and init and fragments:
                    snapshots.append((track, start, init, fragments))
        if not snapshots:
            raise RuntimeError("Replay is still warming up, or the memory budget is too small.")
        bounds = [(start + fragments[-1].start + 1, track) for track, start, _, fragments in snapshots]
        live_video = [end for end, track in bounds if not track.stopped and track.kind == "video"]
        end = min(live_video) if live_video else max(end for end, _ in bounds)
        cutoff = max(min(start + fragments[0].start for _, start, _, fragments in snapshots), end - seconds)
        folder = Path(tempfile.mkdtemp(prefix="Layer-replay-", dir=self.library))
        try:
            (folder / "media").mkdir()
            tracks = []
            for i, (track, start, init, fragments) in enumerate(snapshots, 1):
                if start + fragments[-1].start + 1 <= cutoff or start + fragments[0].start >= end:
                    continue
                first = max((j for j, frag in enumerate(fragments) if start + frag.start <= cutoff),
                            default=0)
                media = folder / "media" / f"{track.kind}-{i}.mp4"
                with media.open("wb") as output:
                    output.writelines(init)
                    base = fragment_timestamp(fragments[first].moof)[2]
                    for fragment in fragments[first:]:
                        if start + fragment.start >= end:
                            break
                        fmt, position, timestamp = fragment_timestamp(fragment.moof)
                        moof = bytearray(fragment.moof)
                        struct.pack_into(fmt, moof, position, timestamp - base)
                        output.write(moof)
                        output.write(fragment.mdat)
                tracks.append((track, media, start + fragments[first].start))
            path = folder / "project.katman"
            atomic_json(path, project_data(folder, "replay", tracks, cutoff, end, self.warnings))
            return path
        except Exception:
            shutil.rmtree(folder)
            raise
