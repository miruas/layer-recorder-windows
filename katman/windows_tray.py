# SPDX-License-Identifier: GPL-3.0-only
"""Single recorder instance and Windows notification-area controls."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import threading

INSTANCE_EVENT = r"Local\LayerRecorder.Show"
INSTANCE_MUTEX = r"Local\LayerRecorder.Instance"


def capture_notification(action, value, previous_mode, read_settings):
    """Describe successful capture actions using the active GSR profile's preference."""
    messages = {
        "record": ("record", "Recording started"),
        "record_in_replay": ("record", "Recording started"),
        "pause": ("record", "Recording paused" if value else "Recording resumed"),
        "replay": ("replay", "Instant Replay started"),
        "automatic_replay": ("replay", "Instant Replay started"),
        "save": ("replay", "Instant Replay saved"),
        "stream_start": ("streaming", "Livestream started"),
        "stream_stop": ("streaming", "Livestream stopped"),
        "screenshot": ("screenshot", "Screenshot saved" if value else "Screenshot copied to clipboard"),
    }
    if action in {"stop", "stop_record_in_replay"}:
        if previous_mode == "record":
            section, message = "record", "Recording saved"
        elif previous_mode == "replay":
            section, message = "replay", "Instant Replay stopped"
        else:
            return None
    elif action in messages:
        section, message = messages[action]
    else:
        return None

    settings = read_settings(section)
    options = settings if section == "screenshot" else settings.get("record_options")
    if isinstance(options, dict) and options.get("show_notifications") is False:
        return None
    if action in {"save", "screenshot", "stop", "stop_record_in_replay"} and isinstance(value, (str, Path)):
        path = Path(value)
        label = path.parent.name if path.name == "project.katman" else path.name
        if label:
            message = f"{message}\n{label[:105]}"
    return message


class RecorderInstance:
    def __init__(self):
        api = self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        api.CreateEventW.restype = wintypes.HANDLE
        api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        api.CreateMutexW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.SetEvent.argtypes = [wintypes.HANDLE]
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.event = api.CreateEventW(None, False, False, INSTANCE_EVENT)
        if not self.event:
            raise ctypes.WinError(ctypes.get_last_error())
        self.mutex = api.CreateMutexW(None, False, INSTANCE_MUTEX)
        error = ctypes.get_last_error()
        if not self.mutex:
            api.CloseHandle(self.event)
            raise ctypes.WinError(error)
        self.primary = error != 183  # ERROR_ALREADY_EXISTS
        if not self.primary:
            api.SetEvent(self.event)

    def show_requested(self):
        return self.api.WaitForSingleObject(self.event, 0) == 0

    def close(self):
        self.api.CloseHandle(self.mutex)
        self.api.CloseHandle(self.event)


def create_tray(events, icon_file):
    from PIL import Image
    from pystray import Icon, Menu, MenuItem
    from pystray._util import win32

    class WindowsTray(Icon):
        registered = False

        def _message(self, code, flags, **kwargs):
            if code == win32.NIM_ADD:
                # The pinned pystray backend discards Shell_NotifyIcon's result.
                data = win32.NOTIFYICONDATAW(cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW),
                                            hWnd=self._hwnd, hID=id(self), uFlags=flags, **kwargs)
                self.registered = bool(win32.Shell_NotifyIcon(code, data))
            else:
                super()._message(code, flags, **kwargs)
                if code == win32.NIM_DELETE:
                    self.registered = False

        def _on_taskbarcreated(self, wparam, lparam):
            super()._on_taskbarcreated(wparam, lparam)
            events.put("tray_ready")

    def item(label, action, default=False):
        return MenuItem(label, lambda _icon, _item: events.put(action), default=default)

    with Image.open(icon_file) as source:
        image = source.convert("RGBA")
    icon = WindowsTray("LayerRecorder", image, "Layer Recorder", Menu(
        item("Open Layer", "show", True),
        item("Open Studio", "editor"),
        item("Toggle overlay", "toggle_overlay"),
        Menu.SEPARATOR,
        item("Start or stop recording", "toggle_record"),
        item("Start or stop replay", "toggle_replay"),
        item("Save last minute", "save_short"),
        item("Save last five minutes", "save_long"),
        Menu.SEPARATOR,
        item("Exit Layer", "exit"),
    ))
    ready = threading.Event()

    def setup(tray):
        try:
            tray.visible = True
        finally:
            ready.set()
            events.put("tray_ready")

    icon.run_detached(setup=setup)
    return icon, ready
