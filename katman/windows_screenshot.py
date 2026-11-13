# SPDX-License-Identifier: GPL-3.0-only
"""Capture a single frame using the settings saved by the native GSR overlay."""
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .windows_capture import displays


def screenshot_settings():
    folder = Path(os.environ["APPDATA"]) / "Layer" / "GSR"
    config = Path(os.environ.get("LAYER_GSR_CONFIG") or folder / "config_ui")
    try:
        settings = json.loads(config.read_text(encoding="utf-8")).get("screenshot", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return settings if isinstance(settings, dict) else {}


def screenshot_source(settings, target):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    source = settings.get("record_area_option", "focused_monitor")
    if target == "screenshot_window" or source == "window":
        window = user32.GetForegroundWindow()
        if not window:
            raise RuntimeError("No active window to capture.")
        return ["-i", f"hwnd={window}"]

    monitors = displays()
    if not monitors:
        raise RuntimeError("No active monitor to capture.")
    monitor = next((item for item in monitors if source == "\\\\.\\" + item.name), None)
    if monitor is None:
        point = wintypes.POINT()
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not user32.GetCursorPos(ctypes.byref(point)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            user32.SetThreadDpiAwarenessContext(previous)
        monitor = next((item for item in monitors if item.x <= point.x < item.x + item.width
                        and item.y <= point.y < item.y + item.height), monitors[0])
    return ["-offset_x", str(monitor.x), "-offset_y", str(monitor.y),
            "-video_size", f"{monitor.width}x{monitor.height}", "-i", "desktop"]


def take_screenshot(source, settings):
    if not settings.get("save_screenshot_to_disk", True):
        raise RuntimeError("Enable Save screenshot to disk in the GSR screenshot settings.")
    extension = "png" if settings.get("image_format") == "png" else "jpg"
    quality = {"medium": "6", "high": "4", "very_high": "2", "ultra": "1"}
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "gdigrab",
               "-draw_mouse", "1" if settings.get("record_cursor", True) and source[-1] == "desktop" else "0",
               "-framerate", "1", *source, "-frames:v", "1"]
    if settings.get("change_image_resolution"):
        width, height = settings.get("image_width"), settings.get("image_height")
        if type(width) is int and type(height) is int and 32 <= min(width, height) <= max(width, height) <= 32768:
            command.extend(["-vf", f"scale={width}:{height}"])
    if extension == "jpg":
        selected = settings.get("image_quality")
        command.extend(["-q:v", quality.get(selected, "2") if isinstance(selected, str) else "2"])
    command.extend(["-threads", "1", "-f", "image2pipe", "-c:v",
                    "png" if extension == "png" else "mjpeg", "pipe:1"])
    result = subprocess.run(command, capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not result.stdout:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or "Screenshot capture failed.")
    configured = settings.get("save_directory")
    directory = Path(configured) if isinstance(configured, str) and configured else Path.home() / "Pictures"
    directory.mkdir(parents=True, exist_ok=True)
    name = f"Screenshot_{datetime.now():%Y-%m-%d_%H-%M-%S_%f}.{extension}"
    path = directory / name
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
        pending = Path(temporary.name)
        try:
            temporary.write(result.stdout)
            temporary.close()
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
    return path
