# SPDX-License-Identifier: GPL-3.0-only
"""WASAPI endpoint and process-loopback audio for the Windows recorder."""
from array import array
from contextlib import ExitStack, contextmanager
import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass, replace
import json
from pathlib import Path
from queue import Empty, Full, Queue
import os
import subprocess
import sys
import threading
import time
import uuid

AUDIO_TIMESCALE = 48_000
CHANNELS = 2
FRAME_BYTES = CHANNELS * 2
PROCESS_LOOPBACK_BUILD = 20348


@dataclass(frozen=True)
class AudioSource:
    key: str
    name: str
    kind: str
    endpoint: str = ""
    pid: int = 0
    default: bool = False
    track_name: str = ""
    inputs: tuple["AudioSource", ...] = ()

    @property
    def metadata(self):
        return dict(type="audio", kind=self.kind, name=self.track_name or self.name, backend="wasapi")


class GUID(c.Structure):
    _fields_ = [("data", c.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__((c.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


class WaveFormat(c.Structure):
    _pack_ = 1
    _fields_ = [("tag", w.WORD), ("channels", w.WORD), ("rate", w.DWORD),
                ("bytes_per_second", w.DWORD), ("align", w.WORD),
                ("bits", w.WORD), ("extra", w.WORD)]


class Blob(c.Structure):
    _fields_ = [("size", w.ULONG), ("data", c.c_void_p)]


class Variant(c.Structure):
    _fields_ = [("type", w.WORD), ("reserved", w.WORD * 3), ("blob", Blob)]


class PropertyKey(c.Structure):
    _fields_ = [("category", GUID), ("pid", w.DWORD)]


PTR = c.c_void_p
HR = c.c_long
U32 = w.DWORD
U64 = c.c_uint64
REFERENCE_TIME = c.c_int64
CLSCTX_ALL = 23
RENDER = 0
CAPTURE = 1
MULTIMEDIA = 1
ACTIVE = 1
LOOPBACK = 0x00020000
AUTOCONVERT_PCM = 0x80000000
SRC_QUALITY = 0x08000000
SILENT = 2
TIMESTAMP_ERROR = 4

DEVICE_ENUMERATOR = GUID("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_DEVICE_ENUMERATOR = GUID("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_AUDIO_CLIENT = GUID("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
IID_CAPTURE_CLIENT = GUID("C8ADBD64-E71E-48a0-A4DE-185C395CD317")
IID_SESSION_MANAGER = GUID("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_SESSION_CONTROL = GUID("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
IID_UNKNOWN = GUID("00000000-0000-0000-C000-000000000046")
IID_AGILE = GUID("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")
IID_COMPLETION = GUID("41D949AB-9862-444A-80F6-C261334DA5EB")
FRIENDLY_NAME = PropertyKey(GUID("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14)


def check(status, operation):
    if status < 0:
        raise OSError(f"{operation} failed (0x{status & 0xffffffff:08X}).")
    return status


def call(obj, index, result, types, *args):
    table = c.cast(obj, c.POINTER(c.POINTER(PTR))).contents
    return c.WINFUNCTYPE(result, PTR, *types)(table[index])(obj, *args)


def release(obj):
    call(obj, 2, w.ULONG, ())


def object_result(stack, obj, index, types, args, operation):
    result = PTR()
    status = call(obj, index, HR, (*types, c.POINTER(PTR)), *args, c.byref(result))
    check(status, operation)
    stack.callback(release, result)
    return result


@contextmanager
def com():
    ole = c.WinDLL("ole32")
    check(ole.CoInitializeEx(None, 0), "Initialize WASAPI")
    try:
        with ExitStack() as stack:
            yield ole, stack
    finally:
        ole.CoUninitialize()


def enumerator(ole, stack):
    result = PTR()
    check(ole.CoCreateInstance(c.byref(DEVICE_ENUMERATOR), None, CLSCTX_ALL,
                               c.byref(IID_DEVICE_ENUMERATOR), c.byref(result)),
          "Open Windows audio devices")
    stack.callback(release, result)
    return result


def activate(stack, endpoint, iid, name):
    return object_result(stack, endpoint, 3, (c.POINTER(GUID), U32, PTR),
                         (c.byref(iid), CLSCTX_ALL, None), name)


def device_id(ole, endpoint):
    result = PTR()
    check(call(endpoint, 5, HR, (c.POINTER(PTR),), c.byref(result)), "Read audio device ID")
    try:
        return c.wstring_at(result)
    finally:
        ole.CoTaskMemFree(result)


def device_name(ole, endpoint, default):
    with ExitStack() as stack:
        store = object_result(stack, endpoint, 4, (U32,), (0,), "Open audio properties")
        value = Variant()
        check(call(store, 5, HR, (c.POINTER(PropertyKey), c.POINTER(Variant)),
                   c.byref(FRIENDLY_NAME), c.byref(value)), "Read audio device name")
        try:
            name_ptr = c.cast(c.byref(value, Variant.blob.offset), c.POINTER(PTR)).contents
            return c.wstring_at(name_ptr) if value.type == 31 and name_ptr.value else default
        finally:
            ole.PropVariantClear(c.byref(value))


def endpoints(stack, devices, flow):
    collection = object_result(stack, devices, 3, (c.c_int, U32), (flow, ACTIVE),
                               "List audio endpoints")
    count = U32()
    check(call(collection, 3, HR, (c.POINTER(U32),), c.byref(count)), "Count audio endpoints")
    for index in range(count.value):
        with ExitStack() as current:
            yield object_result(current, collection, 4, (U32,), (index,), "Read audio endpoint")


def process_name(pid):
    kernel = c.WinDLL("kernel32")
    kernel.OpenProcess.argtypes = [U32, w.BOOL, U32]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, U32, w.LPWSTR, c.POINTER(U32)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        path = c.create_unicode_buffer(32768)
        length = U32(len(path))
        if kernel.QueryFullProcessImageNameW(handle, 0, path, c.byref(length)):
            return Path(path.value).stem
        return None
    finally:
        kernel.CloseHandle(handle)


def process_snapshot():
    class ProcessEntry(c.Structure):
        _fields_ = [("size", U32), ("usage", U32), ("pid", U32),
                    ("heap", c.c_size_t), ("module", U32), ("threads", U32),
                    ("parent", U32), ("priority", c.c_long), ("flags", U32),
                    ("executable", w.WCHAR * 260)]

    kernel = c.WinDLL("kernel32")
    kernel.CreateToolhelp32Snapshot.argtypes = [U32, U32]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    processes = {}
    if snapshot == c.c_void_p(-1).value:
        return processes
    try:
        entry = ProcessEntry()
        entry.size = c.sizeof(entry)
        first, next_process = kernel.Process32FirstW, kernel.Process32NextW
        first.argtypes = next_process.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]
        found = first(snapshot, c.byref(entry))
        while found:
            processes[entry.pid] = (entry.parent, entry.executable)
            found = next_process(snapshot, c.byref(entry))
        return processes
    finally:
        kernel.CloseHandle(snapshot)


def process_ancestors(pid, processes):
    seen = {pid}
    while pid in processes:
        parent, _ = processes[pid]
        if not parent or parent in seen:
            return
        yield parent
        seen.add(parent)
        pid = parent


def own_audio_processes(processes):
    excluded = {os.getpid()}
    excluded.update(pid for pid, (_, name) in processes.items() if name.casefold() == "layer-studio.exe")
    return excluded | {pid for pid in processes if excluded.intersection(process_ancestors(pid, processes))}


def sessions(stack, endpoint):
    manager = activate(stack, endpoint, IID_SESSION_MANAGER, "Open Windows audio sessions")
    session_list = object_result(stack, manager, 5, (), (), "List Windows audio sessions")
    count = c.c_int()
    check(call(session_list, 3, HR, (c.POINTER(c.c_int),), c.byref(count)), "Count Windows audio sessions")
    for index in range(count.value):
        with ExitStack() as current:
            control = object_result(current, session_list, 4, (c.c_int,), (index,), "Read audio session")
            control2 = object_result(current, control, 0, (c.POINTER(GUID),),
                                     (c.byref(IID_SESSION_CONTROL),), "Inspect audio session")
            state = c.c_int()
            if call(control, 3, HR, (c.POINTER(c.c_int),), c.byref(state)) < 0 or state.value == 2:
                continue
            pid = U32()
            if call(control2, 14, HR, (c.POINTER(U32),), c.byref(pid)) == 0 and pid.value:
                yield pid.value


def audio_sources():
    if os.name != "nt":
        return []
    sources = []
    applications = {}
    excluded = own_audio_processes(process_snapshot())
    with com() as (ole, stack):
        devices = enumerator(ole, stack)
        default_output = ""
        try:
            endpoint = object_result(stack, devices, 4, (c.c_int, c.c_int), (RENDER, 1),
                                     "Find default Windows output")
            default_output = device_id(ole, endpoint)
        except OSError:
            pass
        for flow, kind, label in [(RENDER, "system", "System audio"), (CAPTURE, "microphone", "Microphone")]:
            for endpoint in endpoints(stack, devices, flow):
                identity = device_id(ole, endpoint)
                name = device_name(ole, endpoint, label)
                sources.append(AudioSource(f"{kind}:{identity}", f"{label} · {name}", kind, identity,
                                           default=kind == "system" and identity == default_output))
                if flow == RENDER and sys.getwindowsversion().build >= PROCESS_LOOPBACK_BUILD:
                    try:
                        with ExitStack() as current:
                            for pid in sessions(current, endpoint):
                                if pid not in excluded and pid not in applications and (app := process_name(pid)):
                                    applications[pid] = AudioSource(f"app:{pid}", f"{app} (PID {pid})",
                                                                    "application", pid=pid)
                    except OSError:
                        # A device can disappear while its session list is being read.
                        continue
    return sources + sorted(applications.values(), key=lambda source: source.name.casefold())


def gsr_audio_id(source):
    if source.kind == "system":
        return f"device:{source.key}.monitor"
    if source.kind == "application":
        return f"app:{source.name.rsplit(' (PID ', 1)[0]}"
    return f"device:{source.key}"


def gsr_audio_inventory(sources):
    rows = ["device:default_output\tDefault output"] if any(source.default for source in sources) else []
    for source in sources:
        label = " ".join(source.name.split())
        rows.append(f"{gsr_audio_id(source)}\t{label}")
    return "\n".join(rows)


def gsr_audio_selection(tracks, sources, *, allow_missing=False):
    if not isinstance(tracks, list):
        raise ValueError("Invalid audio tracks in the GSR configuration.")
    wanted = {}
    inverse = {}
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get("audio_inputs"), list):
            raise ValueError("Invalid audio track in the GSR configuration.")
        invert = bool(track.get("application_audio_invert"))
        name = track.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if invert:
            exclusions = {item[4:].casefold().removesuffix(".exe") for item in track["audio_inputs"]
                          if isinstance(item, str) and item.casefold().startswith("app:")}
            for source in inverse_application_audio(sources, exclusions):
                inverse.setdefault(source.key, replace(source, track_name=name))
        for item in track["audio_inputs"]:
            if isinstance(item, str):
                identity = item.casefold()
                if invert and identity.startswith("app:"):
                    continue
                if invert and (identity == "default_output" or identity.startswith("device:default_output")
                               or identity.startswith("device:system:") or identity.endswith(".monitor")):
                    raise ValueError("Remove system output from an inverted application audio track.")
                wanted[identity] = wanted.get(identity) or name
    if "default_output" in wanted or "device:default_output" in wanted:
        default = next((source for source in sources if source.default), None)
        if default:
            name = wanted.pop("default_output", "")
            alias_name = wanted.pop("device:default_output", "")
            identity = gsr_audio_id(default).casefold()
            wanted[identity] = wanted.get(identity) or name or alias_name
    selected = [replace(source, track_name=wanted[identity]) for source in sources
                if (identity := gsr_audio_id(source).casefold()) in wanted]
    missing = wanted.keys() - {gsr_audio_id(source).casefold() for source in selected}
    if missing and not allow_missing:
        raise ValueError("Some audio sources selected in GSR are unavailable. Review the capture settings.")
    return list({**inverse, **{source.key: source for source in selected}}.values())


def inverse_application_audio(sources, exclusions):
    if not hasattr(sys, "getwindowsversion") or sys.getwindowsversion().build < PROCESS_LOOPBACK_BUILD:
        raise ValueError("Inverted application audio requires Windows build 20348 or later.")
    processes = process_snapshot()
    blocked = own_audio_processes(processes)
    blocked.update(pid for pid, (_, name) in processes.items()
                   if name.casefold().removesuffix(".exe") in exclusions)
    # WASAPI includes descendants of each selected process. Do not open a
    # process tree that contains one of the excluded applications.
    blocked_roots = {parent for pid in blocked for parent in process_ancestors(pid, processes)}
    allowed = {source.pid: source for source in sources if source.kind == "application"
               and source.pid in processes and source.pid not in blocked_roots and source.pid not in blocked
               and not blocked.intersection(process_ancestors(source.pid, processes))
               and gsr_audio_id(source)[4:].casefold().removesuffix(".exe") not in exclusions}
    return [source for pid, source in allowed.items()
            if not any(parent in allowed for parent in process_ancestors(pid, processes))]


def gsr_recording_audio(tracks, sources):
    """Resolve each GSR track independently and keep its key across input changes."""
    if not isinstance(tracks, list):
        raise ValueError("Invalid audio tracks in the GSR configuration.")
    selected = {}
    for index, track in enumerate(tracks):
        inputs = gsr_audio_selection([track], sources, allow_missing=True)
        if not inputs:
            continue
        if any(source.kind == "system" for source in inputs):
            # The system mix already contains the selected application's audio.
            inputs = [source for source in inputs if source.kind != "application"]
        if not track.get("application_audio_invert") and len(track["audio_inputs"]) == 1 and len(inputs) == 1:
            source = inputs[0]
            if source.key not in selected:
                selected[source.key] = source
                continue
        name = inputs[0].track_name or " + ".join(source.name for source in inputs)
        source = AudioSource(f"gsr-track:{index}", name, "mix", inputs=tuple(inputs))
        selected[source.key] = source
    return list(selected.values())


def process_audio_client(stack, pid):
    callback = c.WINFUNCTYPE(HR, PTR, PTR)
    query = c.WINFUNCTYPE(HR, PTR, c.POINTER(GUID), c.POINTER(PTR))
    ref = c.WINFUNCTYPE(w.ULONG, PTR)
    completed = threading.Event()
    refs = 1
    refs_lock = threading.Lock()

    @ref
    def add_ref(_):
        nonlocal refs
        with refs_lock:
            refs += 1
            return refs

    @ref
    def release_ref(_):
        nonlocal refs
        with refs_lock:
            refs -= 1
            return refs

    @query
    def query_interface(this, iid, output):
        if bytes(iid.contents) not in {bytes(IID_UNKNOWN), bytes(IID_AGILE), bytes(IID_COMPLETION)}:
            output[0] = None
            return -2147467262  # E_NOINTERFACE
        output[0] = this
        add_ref(this)
        return 0

    @callback
    def activate_completed(_, _operation):
        completed.set()
        return 0

    functions = (query_interface, add_ref, release_ref, activate_completed)
    table = (PTR * len(functions))(*(c.cast(fn, PTR) for fn in functions))
    handler = c.pointer(c.cast(table, PTR))
    params = (U32 * 3)(1, pid, 0)  # process loopback, include target and descendants
    variant = Variant(65, (w.WORD * 3)(), Blob(c.sizeof(params), c.cast(params, PTR)))
    operation = PTR()
    mmdevapi = c.WinDLL("mmdevapi")
    activate_async = mmdevapi.ActivateAudioInterfaceAsync
    activate_async.argtypes = [w.LPCWSTR, c.POINTER(GUID), c.POINTER(Variant), PTR, c.POINTER(PTR)]
    activate_async.restype = HR
    check(activate_async("VAD\\Process_Loopback", c.byref(IID_AUDIO_CLIENT), c.byref(variant),
                         c.cast(handler, PTR), c.byref(operation)), "Start application audio activation")
    stack.callback(release, operation)
    # Keep the Python callback alive until Windows releases its reference,
    # including when activation completes after the timeout.
    _active_handlers.append((functions, table, handler, variant, params))
    if not completed.wait(5):
        raise OSError("Windows did not open the selected application audio in time.")
    status, result = HR(), PTR()
    check(call(operation, 3, HR, (c.POINTER(HR), c.POINTER(PTR)),
               c.byref(status), c.byref(result)), "Read application audio activation")
    check(status.value, "Open application audio")
    stack.callback(release, result)
    return result


_active_handlers = []


def audio_client(ole, stack, source):
    if source.kind == "application":
        return process_audio_client(stack, source.pid)
    devices = enumerator(ole, stack)
    if source.endpoint:
        endpoint = object_result(stack, devices, 5, (w.LPCWSTR,), (source.endpoint,), "Open selected audio device")
    else:
        flow = RENDER if source.kind == "system" else CAPTURE
        endpoint = object_result(stack, devices, 4, (c.c_int, c.c_int), (flow, MULTIMEDIA),
                                 "Open default audio device")
    return activate(stack, endpoint, IID_AUDIO_CLIENT, "Activate selected audio device")


def capture_pcm(source, output, stop):
    """Write stereo 48 kHz s16le and keep silence on the timeline across idle gaps."""
    with com() as (ole, stack):
        client = audio_client(ole, stack, source)
        flags = AUTOCONVERT_PCM | SRC_QUALITY
        if source.kind != "microphone":
            flags |= LOOPBACK
        wave = WaveFormat(1, CHANNELS, AUDIO_TIMESCALE, AUDIO_TIMESCALE * FRAME_BYTES,
                          FRAME_BYTES, 16, 0)
        check(call(client, 3, HR, (c.c_int, U32, REFERENCE_TIME, REFERENCE_TIME,
                                  c.POINTER(WaveFormat), PTR),
                   0, flags, 1_000_000, 0, c.byref(wave), None), "Initialize Windows audio capture")
        recorder = object_result(stack, client, 14, (c.POINTER(GUID),),
                                 (c.byref(IID_CAPTURE_CLIENT),), "Open WASAPI capture stream")
        check(call(client, 10, HR, ()), "Start WASAPI capture stream")
        stack.callback(call, client, 11, HR, ())
        start = time.perf_counter()
        cursor = 0
        sample = PTR()
        frames, status = U32(), U32()
        timestamp = U64()

        def silence(count):
            while count > 0:
                block = min(count, AUDIO_TIMESCALE // 2)
                output.write(bytes(block * FRAME_BYTES))
                count -= block

        while not stop.is_set():
            pending = U32()
            check(call(recorder, 5, HR, (c.POINTER(U32),), c.byref(pending)), "Read audio packet size")
            while pending.value:
                check(call(recorder, 3, HR, (c.POINTER(PTR), c.POINTER(U32), c.POINTER(U32),
                                            c.POINTER(U64), c.POINTER(U64)),
                           c.byref(sample), c.byref(frames), c.byref(status), None,
                           c.byref(timestamp)), "Read WASAPI audio")
                size = frames.value
                try:
                    packet = bytes(size * FRAME_BYTES) if status.value & SILENT else c.string_at(sample, size * FRAME_BYTES)
                finally:
                    check(call(recorder, 4, HR, (U32,), size), "Release WASAPI audio packet")
                if timestamp.value and not status.value & TIMESTAMP_ERROR:
                    position = round((timestamp.value / 10_000_000 - start) * AUDIO_TIMESCALE)
                else:
                    position = cursor
                if position > cursor:
                    silence(position - cursor)
                    cursor = position
                clipped = min(size, max(0, cursor - position))
                output.write(packet[clipped * FRAME_BYTES:])
                cursor += size - clipped
                check(call(recorder, 5, HR, (c.POINTER(U32),), c.byref(pending)), "Read audio packet size")
            # Allow the audio engine to deliver late packets before filling gaps.
            target = round((time.perf_counter() - start - .12) * AUDIO_TIMESCALE)
            if target > cursor:
                silence(target - cursor)
                cursor = target
            stop.wait(.01)


class WaveHeader(c.Structure):
    _fields_ = [("data", PTR), ("size", U32), ("recorded", U32),
                ("user", c.c_size_t), ("flags", U32), ("loops", U32),
                ("next", PTR), ("reserved", c.c_size_t)]


def play_pcm(stream, ready, closed):
    """Play decoded 48 kHz stereo PCM with at most 40 ms queued."""
    api = c.WinDLL("winmm")
    handle = PTR()
    wave = WaveFormat(1, CHANNELS, AUDIO_TIMESCALE, AUDIO_TIMESCALE * FRAME_BYTES,
                      FRAME_BYTES, 16, 0)

    def result(code, operation):
        if code:
            raise OSError(f"{operation} failed (MMRESULT {code}).")

    result(api.waveOutOpen(c.byref(handle), U32(0xffffffff), c.byref(wave),
                           c.c_size_t(0), c.c_size_t(0), U32(0)), "Open default audio output")
    chunk_bytes = AUDIO_TIMESCALE // 50 * FRAME_BYTES
    buffers = [c.create_string_buffer(chunk_bytes) for _ in range(2)]
    headers = [WaveHeader(c.cast(buffer, PTR), chunk_bytes) for buffer in buffers]
    prepared = []
    try:
        # Decode one block before synchronizing with video. This gives FFmpeg
        # time to seek and start the audio graph while the video graph starts.
        chunk = stream.read(chunk_bytes)
        ready.wait()
        index = 0
        while chunk and not closed.is_set():
            header = headers[index]
            if index in prepared:
                while not header.flags & 1:  # WHDR_DONE
                    if closed.wait(.002):
                        return
                result(api.waveOutUnprepareHeader(handle, c.byref(header), c.sizeof(header)),
                       "Release playback buffer")
                prepared.remove(index)
            c.memmove(header.data, chunk, len(chunk))
            header.size = len(chunk)
            result(api.waveOutPrepareHeader(handle, c.byref(header), c.sizeof(header)),
                   "Prepare playback buffer")
            prepared.append(index)
            result(api.waveOutWrite(handle, c.byref(header), c.sizeof(header)), "Play preview audio")
            index = (index + 1) % len(headers)
            chunk = stream.read(chunk_bytes)
        while any(not headers[index].flags & 1 for index in prepared):
            if closed.wait(.002):
                break
    finally:
        api.waveOutReset(handle)
        for index in prepared:
            api.waveOutUnprepareHeader(handle, c.byref(headers[index]), c.sizeof(headers[index]))
        api.waveOutClose(handle)


AUDIO_ENCODERS = {"aac": "aac", "opus": "libopus"}


def mix_audio(sources, output, stop):
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
            mixed = [0] * (block_size // 2)
            for source, sink in zip(sources, inputs):
                deadline = time.monotonic() + 3
                while not stop.is_set():
                    try:
                        block = sink.blocks.get(timeout=.1)
                    except Empty:
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f"{source.name}: Windows audio capture stopped responding.")
                        continue
                    # Both WASAPI and the Windows encoder use little-endian PCM.
                    for index, sample in enumerate(array("h", block)):
                        mixed[index] += sample
                    break
            if not stop.is_set():
                output.write(array("h", (max(-32768, min(32767, sample)) for sample in mixed)).tobytes())
        if not errors.empty():
            raise errors.get()
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=2)


def capture_audio_command(source, codec="aac"):
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    if source.inputs:
        inputs = [[item.kind, item.endpoint, item.pid] for item in source.inputs]
        return [str(executable), "-m", "katman.windows_audio", "mix", json.dumps(inputs), codec]
    return [str(executable), "-m", "katman.windows_audio", "capture",
            source.kind, source.endpoint, str(source.pid), codec]


def watch_stop(stop):
    try:
        for line in iter(sys.stdin.buffer.raw.readline, b""):
            if line.strip().lower() == b"q":
                break
    finally:
        stop.set()


@contextmanager
def child_job():
    """Terminate the encoder if the audio worker is force-closed."""
    class Limits(c.Structure):
        _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", U32),
                    ("minimum", c.c_size_t), ("maximum", c.c_size_t), ("active", U32),
                    ("affinity", c.c_size_t), ("priority", U32), ("scheduling", U32)]

    class ExtendedLimits(c.Structure):
        _fields_ = [("basic", Limits), ("io_counts", c.c_uint64 * 6),
                    ("memory_limits_and_peaks", c.c_size_t * 4)]

    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [PTR, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, PTR, U32]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.OpenProcess.argtypes = [U32, w.BOOL, U32]
    kernel.OpenProcess.restype = w.HANDLE

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise c.WinError(c.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)):
            raise c.WinError(c.get_last_error())

        def attach(pid):
            handle = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not handle:
                raise c.WinError(c.get_last_error())
            try:
                if not kernel.AssignProcessToJobObject(job, handle):
                    raise c.WinError(c.get_last_error())
            finally:
                kernel.CloseHandle(handle)

        yield attach
    finally:
        kernel.CloseHandle(job)


def audio_encoder_command(codec):
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "s16le", "-ar", str(AUDIO_TIMESCALE), "-ac", str(CHANNELS),
               "-i", "pipe:0", "-c:a", AUDIO_ENCODERS[codec], "-b:a", "192k",
               "-frag_duration", "1000000", "-movflags",
               "empty_moov+default_base_moof+skip_trailer", "-f", "mp4", "pipe:1"]


def capture(source, codec="aac"):
    command = audio_encoder_command(codec)
    stop = threading.Event()
    threading.Thread(target=watch_stop, args=(stop,), daemon=True).start()
    with child_job() as attach:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=sys.stdout.buffer,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            attach(process.pid)
            with process.stdin as output:
                if len(source.inputs) > 1:
                    mix_audio(source.inputs, output, stop)
                else:
                    capture_pcm(source.inputs[0] if source.inputs else source, output, stop)
            return process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def main():
    command, *args = sys.argv[1:]
    if command == "capture":
        kind, endpoint, pid, *codec = args
        return capture(AudioSource("", "", kind, endpoint, int(pid)), *codec)
    if command == "mix":
        inputs, codec = args
        sources = tuple(AudioSource("", kind, kind, endpoint, pid) for kind, endpoint, pid in json.loads(inputs))
        return capture(AudioSource("", "", "mix", inputs=sources), codec)
    if command == "devices":
        print(json.dumps([{"key": source.key, "name": source.name, "endpoint": source.endpoint}
                          for source in audio_sources() if source.kind == "microphone"]))
        return 0
    raise ValueError("Unknown Windows audio command.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
