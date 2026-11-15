# SPDX-License-Identifier: GPL-3.0-only
"""DirectShow webcam selection and FFmpeg picture-in-picture composition."""
import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Webcam:
    device: str
    video_format: str
    mode: tuple[int, int, int]
    position: tuple[int, int]
    size: tuple[int, int]
    flip: bool


def dshow_log(*options):
    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", *options],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=4,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # FFmpeg lists devices and modes on stderr and then exits nonzero.
        return result.stderr.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def camera_inventory():
    listing = dshow_log("-list_devices", "true", "-f", "dshow", "-i", "dummy")
    rows = []
    name = ""
    for line in listing.splitlines():
        if match := re.search(r'"(.*)" \((video|audio|none)\)$', line):
            name = match[1] if match[2] == "video" else ""
        elif name and (match := re.search(r'Alternative name "(.*)"$', line)):
            device = match[1]
            if "\t" not in device:
                # Unknown formats can still be opened using DirectShow defaults.
                rows.append(f"dshow:{device}\t{' '.join(name.split())}")
                modes = dshow_log("-list_options", "true", "-f", "dshow", "-i", f"video={device}")
                seen = set()
                for mode in modes.splitlines():
                    fmt = ("yuyv" if "pixel_format=yuyv422" in mode else
                           "mjpeg" if "vcodec=mjpeg" in mode else "")
                    if not fmt:
                        continue
                    for width, height, fps in re.findall(r"\bs=(\d+)x(\d+)\s+fps=(\d+)(?=\s|$)", mode):
                        # The pinned GSR widgets store whole-number frame rates.
                        row = f"{fmt}\t{width}\t{height}\t{fps}"
                        if row not in seen:
                            seen.add(row)
                            rows.append(row)
            name = ""
    return "\n".join(rows)


def gsr_webcam(options):
    if not isinstance(options, dict) or not (source := options.get("webcam_source")):
        return None
    if not isinstance(source, str) or not source.startswith("dshow:") or not source[6:]:
        raise ValueError("Choose a Windows camera in the GSR capture settings.")
    video_format = options.get("webcam_video_format", "auto")
    if video_format not in ("auto", "yuyv", "mjpeg"):
        raise ValueError("The configured Windows webcam format is unsupported.")

    def setting(name, default, maximum):
        value = options.get(f"webcam_{name}", default)
        return max(0, min(maximum, value)) if type(value) is int else default

    return Webcam(
        source[6:], video_format,
        (setting("camera_width", 0, 16384), setting("camera_height", 0, 16384), setting("camera_fps", 0, 500)),
        (setting("x", 0, 100), setting("y", 0, 100)),
        (setting("width", 30, 100), setting("height", 30, 100)),
        options.get("webcam_flip_horizontally") is True,
    )


def webcam_input(camera):
    width, height, fps = camera.mode
    args = ["-thread_queue_size", "8", "-f", "dshow"]
    if width and height:
        args.extend(["-video_size", f"{width}x{height}"])
    if fps:
        args.extend(["-framerate", str(fps)])
    if camera.video_format == "yuyv":
        args.extend(["-pixel_format", "yuyv422"])
    elif camera.video_format == "mjpeg":
        args.extend(["-c:v", "mjpeg"])
    return [*args, "-i", f"video={camera.device}"]


def add_webcam(command, camera, canvas):
    """Composite the camera after the desktop's existing resolution and range filters."""
    filters = command.index("-vf")
    base = command[filters + 1]
    width, height = canvas
    cap_width, cap_height = camera.size
    cap_width = max(2, width * cap_width // 100) if cap_width else width
    cap_height = max(2, height * cap_height // 100) if cap_height else height
    scale = (f"scale=w='min(iw,{cap_width})':h='min(ih,{cap_height})'" if not any(camera.size)
             else f"scale={cap_width}:{cap_height}")
    camera_filters = (
        "setpts=PTS-STARTPTS," + ("hflip," if camera.flip else "") + scale
        + ":force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1"
    )
    x, y = camera.position
    graph = (
        f"[0:v]{base},setpts=PTS-STARTPTS[desktop];[1:v]{camera_filters}[camera];"
        f"[desktop][camera]overlay=x='min(W*{x}/100,W-w)':y='min(H*{y}/100,H-h)'"
        ":format=auto:eof_action=pass[webcam_video]"
    )
    command[filters:filters + 2] = ["-filter_complex", graph, "-map", "[webcam_video]"]
    insertion = command.index("-an")
    command[insertion:insertion] = webcam_input(camera)
