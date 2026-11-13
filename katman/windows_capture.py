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


def displays():
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD),
                    ("device", wintypes.WCHAR * 32)]

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
        result.append(Display(info.device.removeprefix("\\\\.\\"), r.left, r.top,
                              r.right - r.left, r.bottom - r.top, bool(info.flags & 1)))
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
        while (self.bytes > self.byte_limit or
               (len(self.fragments) > 1 and
                self.fragments[1].start <= fragment.start - self.seconds)):
            self.bytes -= self.fragments.popleft().size


class Track:
    def __init__(self, display, command, fps, seconds, byte_limit, recording=None):
        self.display = display
        self.command = command
        self.fps = fps
        self.replay = ReplayBuffer(seconds, byte_limit)
        self.recording = recording
        self.lock = threading.Lock()
        self.init = []
        self.started = None
        self.error = None
        self.process = None
        self.stopped = False

    def start(self):
        self.log = deque(maxlen=8)
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
                    fragment = Fragment(fragment_timestamp(pending)[2] / TIMESCALE, pending, box)
                    with self.lock:
                        if self.started is None:
                            # The first complete fragment is flushed at the next keyframe.
                            self.started = time.monotonic() - 1.0
                        self.replay.add(fragment)
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


def project_data(folder, kind, tracks, requested_seconds=None):
    sources = []
    for track, media, first, offset in tracks:
        info = probe(media)
        stream = next(s for s in info["streams"] if s["codec_type"] == "video")
        available = float(stream.get("duration", info["format"]["duration"]))
        if available <= 0:
            raise ValueError(f"No video frames were saved for {track.display.name}.")
        source = {**asdict(track.display), "id": media.stem, "type": "video",
                  "file": media.relative_to(folder).as_posix(), "enabled": True, "gain": 1,
                  "backend": "gdigrab", "media_duration": available,
                  "logical_width": track.display.width, "logical_height": track.display.height,
                  "width": stream["width"], "height": stream["height"],
                  "timeline_start": offset + first, "trim_in": 0}
        sources.append(source)
    end_of_source = [s["timeline_start"] + s["media_duration"] for s in sources]
    end = min(end_of_source) if kind == "replay" else max(end_of_source)
    cut = max(0, end - requested_seconds) if requested_seconds is not None else 0
    sources = [s for s in sources if s["timeline_start"] + s["media_duration"] > cut]
    for source in sources:
        relative_start = source["timeline_start"] - cut
        source["trim_in"] = max(0, -relative_start)
        source["timeline_start"] = max(0, relative_start)
    return {"format": "katman", "version": 1, "id": uuid.uuid4().hex, "name": "Desktop",
            "application": "Desktop", "kind": kind, "created": datetime.now().astimezone().isoformat(),
            "fps": tracks[0][0].fps, "duration": end - cut, "sources": sources, "sections": []}


class Capture:
    def __init__(self, library, selected, fps=30, encoder="libx264", seconds=300,
                 memory_mb=512, record=False):
        if not selected:
            raise ValueError("Select at least one display.")
        if fps < 1 or seconds <= 0 or memory_mb <= 0:
            raise ValueError("Frame rate, replay duration and memory budget must be positive.")
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("Install FFmpeg and add both ffmpeg and ffprobe to PATH.")
        self.library = Path(library).expanduser().resolve()
        self.library.mkdir(parents=True, exist_ok=True)
        self.record_folder = Path(tempfile.mkdtemp(prefix="Layer-recording-", dir=self.library)) if record else None
        self.tracks = []
        if self.record_folder:
            (self.record_folder / "media").mkdir()
        try:
            for i, display in enumerate(selected, 1):
                command = capture_command(display, fps, encoder)
                path = self.record_folder / "media" / f"display-{i}.mp4" if self.record_folder else None
                track = Track(display, command, fps, seconds, memory_mb * 1024**2 // len(selected),
                              path.open("wb") if path else None)
                try:
                    track.start()
                except OSError:
                    if track.recording:
                        track.recording.close()
                    raise
                self.tracks.append(track)
        except Exception:
            self.stop()
            if self.record_folder:
                shutil.rmtree(self.record_folder)
            raise

    def failure(self):
        for track in self.tracks:
            error = track.failure()
            if error:
                return f"{track.display.name}: {error}"
            if track.process.poll() is not None:
                return f"{track.display.name}: capture stopped unexpectedly."
        return None

    def stop(self):
        failures = []
        for track in self.tracks:
            error = track.stop()
            if error:
                failures.append(f"{track.display.name}: {error}")
        return failures

    def finish_recording(self):
        if not self.record_folder:
            raise RuntimeError("No ordinary recording is active.")
        failures = self.stop()
        snapshots = [(track, track.snapshot()[0]) for track in self.tracks]
        if not all(start is not None for _, start in snapshots):
            raise RuntimeError(f"Recording is too short or a source failed. Media: {self.record_folder}. "
                               + " ".join(failures))
        origin = min(start for _, start in snapshots)
        tracks = [(track, self.record_folder / "media" / f"display-{i}.mp4", 0, start - origin)
                  for i, (track, start) in enumerate(snapshots, 1)]
        path = self.record_folder / "project.katman"
        project = project_data(self.record_folder, "recording", tracks)
        project["warnings"] = failures
        atomic_json(path, project)
        return path

    def save_replay(self, seconds):
        snapshots = [(track, *track.snapshot()) for track in self.tracks]
        if not all(start is not None and fragments for _, start, _, fragments in snapshots):
            raise RuntimeError("Replay is still warming up, or the memory budget is too small.")
        origin = min(start for _, start, _, _ in snapshots)
        if not 0 < seconds <= self.tracks[0].replay.seconds:
            raise ValueError("Replay duration must fit inside the configured buffer.")
        cutoff = min(start - origin + fragments[-1].start + 1
                     for _, start, _, fragments in snapshots) - seconds
        folder = Path(tempfile.mkdtemp(prefix="Layer-replay-", dir=self.library))
        try:
            (folder / "media").mkdir()
            tracks = []
            for i, (track, start, init, fragments) in enumerate(snapshots, 1):
                offset = start - origin
                first = max((j for j, frag in enumerate(fragments) if offset + frag.start <= cutoff),
                            default=0)
                media = folder / "media" / f"display-{i}.mp4"
                with media.open("wb") as output:
                    output.writelines(init)
                    base = fragment_timestamp(fragments[first].moof)[2]
                    for fragment in fragments[first:]:
                        fmt, position, timestamp = fragment_timestamp(fragment.moof)
                        moof = bytearray(fragment.moof)
                        struct.pack_into(fmt, moof, position, timestamp - base)
                        output.write(moof)
                        output.write(fragment.mdat)
                tracks.append((track, media, fragments[first].start, offset))
            path = folder / "project.katman"
            atomic_json(path, project_data(folder, "replay", tracks, seconds))
            return path
        except Exception:
            shutil.rmtree(folder)
            raise
