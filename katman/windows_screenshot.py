# SPDX-License-Identifier: GPL-3.0-only
"""Capture a single frame using the settings saved by the native GSR overlay."""
import ctypes
from ctypes import wintypes
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .windows_capture import displays, foreground_window


def gsr_settings(section):
    folder = Path(os.environ["APPDATA"]) / "Layer" / "GSR"
    config = Path(os.environ.get("LAYER_GSR_CONFIG") or folder / "config_ui")
    try:
        settings = json.loads(config.read_text(encoding="utf-8")).get(section, {})
    except (OSError, ValueError, AttributeError):
        return {}
    return settings if isinstance(settings, dict) else {}


def screenshot_settings():
    return gsr_settings("screenshot")


def screenshot_source(settings, target, *, window_handle=None):
    source = settings.get("record_area_option", "focused_monitor")
    if target == "screenshot_window" or source == "window":
        if window_handle is None:
            window_handle = foreground_window()
        if not window_handle:
            raise RuntimeError("No active window to capture.")
        return ["-i", f"hwnd={window_handle}"]

    monitor = capture_monitor(source)
    return desktop_source(monitor.x, monitor.y, monitor.width, monitor.height)


def is_monitor_target(source):
    return isinstance(source, str) and source.startswith(("\\\\.\\", "\\\\?\\"))


def capture_monitor(source):
    monitors = displays()
    if not monitors:
        raise RuntimeError("No active monitor to capture.")
    monitor = next((item for item in monitors if is_monitor_target(source) and source.casefold() in (
        ("\\\\.\\" + item.name).casefold(), item.device_id.casefold()
    )), None)
    if monitor is None:
        if is_monitor_target(source):
            raise RuntimeError("The monitor selected in GSR is disconnected.")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
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
    return monitor


def desktop_source(x, y, width, height):
    return ["-offset_x", str(x), "-offset_y", str(y),
            "-video_size", f"{width}x{height}", "-i", "desktop"]


def copy_image_to_clipboard(image_bytes, owner):
    from PIL import Image

    bmp = BytesIO()
    with Image.open(BytesIO(image_bytes)) as image:
        image.convert("RGB").save(bmp, format="BMP")
    dib = bmp.getvalue()[14:]  # CF_DIB starts at the DIB header.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    handle = kernel32.GlobalAlloc(0x0002, len(dib))  # GMEM_MOVEABLE
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        address = kernel32.GlobalLock(handle)
        if not address:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(address, dib, len(dib))
        finally:
            kernel32.GlobalUnlock(handle)

        for attempt in range(6):
            if user32.OpenClipboard(owner):
                break
            if attempt == 5:
                raise ctypes.WinError(ctypes.get_last_error())
            time.sleep(0.025)
        try:
            if not user32.EmptyClipboard() or not user32.SetClipboardData(8, handle):  # CF_DIB
                raise ctypes.WinError(ctypes.get_last_error())
            handle = None  # Windows owns the allocation after SetClipboardData.
        finally:
            user32.CloseClipboard()
    finally:
        if handle:
            kernel32.GlobalFree(handle)


def take_screenshot(source, settings, clipboard_owner=None, *, window=None):
    save_to_disk = settings.get("save_screenshot_to_disk", True)
    save_to_clipboard = settings.get("save_screenshot_to_clipboard", False)
    if not save_to_disk and not save_to_clipboard:
        raise RuntimeError("Enable a screenshot destination in the GSR screenshot settings.")
    if save_to_clipboard and not clipboard_owner:
        raise RuntimeError("The Windows clipboard owner is unavailable.")
    extension = "png" if settings.get("image_format") == "png" else "jpg"
    quality = {"medium": "6", "high": "4", "very_high": "2", "ultra": "1"}
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "gdigrab",
               "-draw_mouse", "1" if settings.get("record_cursor", True) else "0",
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
    path = save_image(result.stdout, settings, extension, window=window) if save_to_disk else None
    if save_to_clipboard:
        try:
            copy_image_to_clipboard(result.stdout, clipboard_owner)
        except OSError as error:
            if path is not None:
                raise RuntimeError(f"Saved {path}. Could not copy to the clipboard: {error}") from error
            raise
    return path


def save_image(data, settings, extension, *, window=None):
    from .windows_output import capture_output

    configured = settings.get("save_directory")
    directory = Path(configured) if isinstance(configured, str) and configured else Path.home() / "Pictures"
    output = capture_output(settings, directory, "Screenshot_", window)
    directory = output.directory
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{output.prefix}{datetime.now():%Y-%m-%d_%H-%M-%S_%f}.{extension}"
    path = directory / name
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
        pending = Path(temporary.name)
        try:
            temporary.write(data)
            temporary.close()
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
    return path
