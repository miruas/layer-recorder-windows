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
ADDITIONAL_HOTKEYS = {
    0x2C: (0, "screenshot", "Print Screen"),
    0x12C: (0x0004, "screenshot_window", "Shift+Print Screen"),
    0x22C: (0x0002, "screenshot_region", "Ctrl+Print Screen"),
    0x178: (0x0004, "toggle_window_record", "Shift+F9"),
    0x176: (0x0001, "toggle_pause", "Alt+F7"),
    0x177: (0x0001, "toggle_stream", "Alt+F8"),
    0x278: (0x0001, "gsr_toggle_record", "Alt+F9"),
    0x378: (0x0002, "toggle_region_record", "Ctrl+F9"),
    0x179: (0x0005, "gsr_toggle_replay", "Shift+Alt+F10"),
    0x279: (0x0001, "save_replay", "Alt+F10"),
    0x17A: (0x0001, "save_short", "Alt+F11"),
    0x17B: (0x0001, "save_long", "Alt+F12"),
    0x15A: (0x0009, "editor", "Win+Alt+Z"),
}
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
GSR_BINDINGS = {
    OVERLAY_KEY: ("main", "show_hide_hotkey"),
    0x15A: ("main", "layer_editor_hotkey"),
    0x176: ("record", "pause_unpause_hotkey"),
    0x177: ("streaming", "start_stop_hotkey"),
    0x278: ("record", "start_stop_hotkey"),
    0x378: ("record", "start_stop_region_hotkey"),
    0x178: ("record", "start_stop_window_hotkey"),
    0x179: ("replay", "start_stop_hotkey"),
    0x279: ("replay", "save_hotkey"),
    0x17A: ("replay", "save_1_min_hotkey"),
    0x17B: ("replay", "save_10_min_hotkey"),
    0x2C: ("screenshot", "take_screenshot_hotkey"),
    0x12C: ("screenshot", "take_screenshot_window_hotkey"),
    0x22C: ("screenshot", "take_screenshot_region_hotkey"),
}


def gsr_key(key):
    # Serialized values in the pinned MGL key enum.
    for start, end, vk, name in ((1, 26, 0x41, "letter"), (27, 36, 0x30, "digit"),
                                 (86, 100, 0x70, "function"), (116, 124, 0x7F, "function")):
        if start <= key <= end:
            code = vk + key - start
            return code, f"F{code - 0x6F}" if name == "function" else chr(code)
    return {37: (0x1B, "Esc"), 58: (0x20, "Space"), 59: (0x0D, "Enter"),
            61: (0x09, "Tab"), 102: (0x2C, "Print Screen")}.get(key)


def hotkey_bindings(settings=None):
    result = {identifier: (identifier, 1 if identifier == OVERLAY_KEY else 3, action, label)
              for identifier, (action, label) in HOTKEYS.items()}
    result.update({identifier: (identifier & 0xFF, mods, action, label)
                   for identifier, (mods, action, label) in ADDITIONAL_HOTKEYS.items()})
    if settings is None:
        return result, []
    unsupported = []
    for identifier, (section, name) in GSR_BINDINGS.items():
        config = settings(section).get(name)
        if not isinstance(config, dict):
            continue
        key, mods = config.get("key"), config.get("modifiers")
        _, _, action, label = result[identifier]
        if key == 0:
            del result[identifier]
            continue
        translated = gsr_key(key) if type(key) is int else None
        if translated is None or type(mods) is not int or not 0 <= mods <= 255:
            unsupported.append(label)
            del result[identifier]
            continue
        vk, key_name = translated
        flags, names = 0, []
        for mask, flag, prefix in ((0xC0, 8, "Win"), (0x0C, 2, "Ctrl"),
                                   (0x30, 1, "Alt"), (0x03, 4, "Shift")):
            if mods & mask:
                flags |= flag
                names.append(prefix)
        result[identifier] = vk, flags, action, "+".join([*names, key_name])
    return result, unsupported


class GlobalHotkeys:
    def __init__(self, settings=None):
        self.events = queue.SimpleQueue()
        self.ready = queue.Queue(maxsize=1)
        self.bindings, self.unsupported = hotkey_bindings(settings)
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
        conflicts = list(self.unsupported)
        actions = {}
        try:
            for identifier, (vk, modifiers, action, label) in self.bindings.items():
                if self.user32.RegisterHotKey(None, identifier, modifiers | 0x4000, vk):
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
