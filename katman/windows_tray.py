# SPDX-License-Identifier: GPL-3.0-only
"""Single recorder instance and Windows notification-area controls."""
import ctypes
from ctypes import wintypes
import threading

INSTANCE_EVENT = r"Local\LayerRecorder.Show"
INSTANCE_MUTEX = r"Local\LayerRecorder.Instance"


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

    def item(label, action, default=False):
        return MenuItem(label, lambda _icon, _item: events.put(action), default=default)

    with Image.open(icon_file) as source:
        image = source.convert("RGBA")
    icon = Icon("LayerRecorder", image, "Layer Recorder", Menu(
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

    icon.run_detached(setup=setup)
    return icon, ready
