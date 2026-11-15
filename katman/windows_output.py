# SPDX-License-Identifier: GPL-3.0-only
"""Map GSR's naming preferences to Windows project and screenshot paths."""
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import re

from .windows_audio import process_path


@dataclass(frozen=True)
class CaptureOutput:
    directory: Path
    prefix: str
    name: str = "Desktop"
    application: str = "Desktop"


def safe_component(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:80].rstrip(" .")
    if not name:
        return "Desktop"
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|(?:COM|LPT)[1-9¹²³])", name.split(".", 1)[0], re.I):
        return "_" + name
    return name


def window_application(window):
    from ctypes import wintypes

    pid = wintypes.DWORD()
    get_pid = ctypes.WinDLL("user32").GetWindowThreadProcessId
    get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_pid(window, ctypes.byref(pid))
    path = process_path(pid.value) if pid.value and pid.value != os.getpid() else None
    if path is None or path.stem.casefold() in {"explorer", "layer", "layer-gsr-overlay", "layer-studio"}:
        return "Desktop", False
    parts = [part.casefold() for part in path.parent.parts]
    is_game = any((first, second) == ("steamapps", "common") for first, second in zip(parts, parts[1:]))
    return path.stem, is_game


def capture_output(settings, directory, prefix, window):
    organize = settings.get("organize_into_folders")
    name_after = settings.get("name_after_captured_application")
    choices = ("yes", "yes_only_games")
    if organize not in choices and name_after not in choices:
        return CaptureOutput(directory, prefix)
    application, game = window_application(window) if window else ("Desktop", False)
    component = safe_component(application)
    if organize == "yes" or (organize == "yes_only_games" and game):
        directory = directory / component
    named = name_after == "yes" or (name_after == "yes_only_games" and game)
    return CaptureOutput(directory, f"{component}-{prefix}" if named else prefix,
                         application if named else "Desktop", application)
