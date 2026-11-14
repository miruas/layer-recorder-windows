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
    if 1 <= key <= 36:
        code = 0x41 + key - 1 if key <= 26 else 0x30 + key - 27
        return code, chr(code)
    if 86 <= key <= 100 or 116 <= key <= 124:
        number = key - 85 if key <= 100 else key - 100
        return 0x6F + number, f"F{number}"
    if 76 <= key <= 85:
        number = key - 76
        return 0x60 + number, f"Numpad {number}"
    return {
        37: (0x1B, "Esc"), 46: (0x5D, "Menu"),
        47: (0xDB, "["), 48: (0xDD, "]"), 49: (0xBA, ";"),
        50: (0xBC, ","), 51: (0xBE, "."), 52: (0xDE, "'"),
        53: (0xBF, "/"), 54: (0xDC, "\\"), 55: (0xC0, "`"),
        56: (0xBB, "="), 57: (0xBD, "-"), 58: (0x20, "Space"),
        59: (0x0D, "Enter"), 60: (0x08, "Backspace"), 61: (0x09, "Tab"),
        62: (0x21, "Page Up"), 63: (0x22, "Page Down"),
        64: (0x23, "End"), 65: (0x24, "Home"), 66: (0x2D, "Insert"), 67: (0x2E, "Delete"),
        68: (0x6B, "Numpad +"), 69: (0x6D, "Numpad -"),
        70: (0x6A, "Numpad *"), 71: (0x6F, "Numpad /"),
        72: (0x25, "Left"), 73: (0x27, "Right"), 74: (0x26, "Up"), 75: (0x28, "Down"),
        101: (0x13, "Pause"), 102: (0x2C, "Print Screen"), 103: (0x0D, "Enter"),
        104: (0xAE, "Volume Down"), 105: (0xAF, "Volume Up"),
        106: (0xB3, "Play/Pause"), 107: (0xB2, "Media Stop"), 108: (0xB3, "Play/Pause"),
        109: (0xAD, "Mute"), 110: (0xB1, "Previous Track"), 111: (0xB0, "Next Track"),
        115: (0xDE, "'"),
    }.get(key)


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
