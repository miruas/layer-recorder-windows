# SPDX-License-Identifier: GPL-3.0-only
"""Opt-in Windows global shortcuts using a dedicated Win32 message queue."""
import ctypes
from ctypes import wintypes
import queue
import threading

OVERLAY_KEY = 0x5A
HOTKEYS = {
    OVERLAY_KEY: ("toggle_overlay", "Alt+Z"),
    0x77: ("toggle_record", "Ctrl+Alt+F8"),
    0x78: ("toggle_replay", "Ctrl+Alt+F9"),
    0x79: ("save_short", "Ctrl+Alt+F10"),
    0x7A: ("save_long", "Ctrl+Alt+F11"),
}
SCREENSHOT_HOTKEYS = {
    0x2C: (0, "screenshot", "Print Screen"),
    0x12C: (0x0004, "screenshot_window", "Shift+Print Screen"),
}
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MODIFIERS = 0x0001 | 0x0002 | 0x4000


class GlobalHotkeys:
    def __init__(self):
        self.events = queue.SimpleQueue()
        self.ready = queue.Queue(maxsize=1)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                               wintypes.UINT, wintypes.UINT]
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                             wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                            wintypes.UINT, wintypes.UINT]
        self.user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                                   wintypes.WPARAM, wintypes.LPARAM]
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()
        self.thread_id, self.conflicts = self.ready.get()

    def _listen(self):
        message = wintypes.MSG()
        self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        registered = []
        conflicts = []
        actions = {key: action for key, (action, _) in HOTKEYS.items()}
        try:
            for key, (_, label) in HOTKEYS.items():
                modifiers = MODIFIERS & ~0x0002 if key == OVERLAY_KEY else MODIFIERS
                if self.user32.RegisterHotKey(None, key, modifiers, key):
                    registered.append(key)
                else:
                    conflicts.append(label)
            for identifier, (modifiers, action, label) in SCREENSHOT_HOTKEYS.items():
                if self.user32.RegisterHotKey(None, identifier, modifiers | 0x4000, 0x2C):
                    registered.append(identifier)
                    actions[identifier] = action
                else:
                    conflicts.append(label)
            self.ready.put((threading.get_native_id(), conflicts))
            while self.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY and message.wParam in registered:
                    self.events.put(actions[message.wParam])
        finally:
            for key in registered:
                self.user32.UnregisterHotKey(None, key)

    def close(self):
        if self.thread.is_alive():
            self.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
            self.thread.join(timeout=2)
