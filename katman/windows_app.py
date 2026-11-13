# SPDX-License-Identifier: GPL-3.0-only
"""Windows launcher and capture controls."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading

from .windows_capture import Capture, displays

ENCODERS = ("libx264", "h264_nvenc", "h264_amf", "h264_qsv")


def default_library():
    return Path.home() / "Videos" / "Layer"


def export(path, output):
    from .export import export_project
    return export_project(path, output)


def capture_controls(library):
    import tkinter as tk
    from tkinter import filedialog, ttk

    root = tk.Tk()
    root.title("Layer Recorder")
    root.minsize(470, 360)
    panel = ttk.Frame(root, padding=20)
    panel.pack(fill="both", expand=True)
    status = tk.StringVar(value="Select the displays you want to capture.")
    options = []
    capture = None
    mode = None
    capture_failure = None
    busy = False
    closing = False
    results = queue.SimpleQueue()

    ttk.Label(panel, text="Displays").grid(row=0, column=0, sticky="w")
    monitor_panel = ttk.Frame(panel)
    monitor_panel.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 12))

    def refresh():
        try:
            found = displays()
        except OSError as error:
            status.set(str(error))
            return
        for control in monitor_panel.winfo_children():
            control.destroy()
        selected = {display.name: flag.get() for display, flag in options}
        options.clear()
        for display in found:
            flag = tk.BooleanVar(value=selected.get(display.name, display.primary))
            caption = f"{display.name} · {display.width} × {display.height}"
            if display.primary:
                caption += " · Primary"
            ttk.Checkbutton(monitor_panel, text=caption, variable=flag).pack(anchor="w")
            options.append((display, flag))
        if not found:
            status.set("No active displays were found.")

    fps = tk.StringVar(value="30")
    encoder = tk.StringVar(value=ENCODERS[0])
    ttk.Label(panel, text="Frame rate").grid(row=2, column=0, sticky="w")
    ttk.Label(panel, text="H.264 encoder").grid(row=2, column=1, sticky="w")
    fps_field = ttk.Combobox(panel, textvariable=fps, values=("15", "30", "60"), width=8, state="readonly")
    encoder_field = ttk.Combobox(panel, textvariable=encoder, values=ENCODERS, width=16, state="readonly")
    fps_field.grid(row=3, column=0, sticky="w", padx=(0, 10))
    encoder_field.grid(row=3, column=1, sticky="w")
    refresh_button = ttk.Button(panel, text="Refresh displays", command=refresh)
    refresh_button.grid(row=3, column=2, sticky="ew", padx=(10, 0))
    ttk.Separator(panel).grid(row=4, column=0, columnspan=3, sticky="ew", pady=14)

    def submit(action, task):
        nonlocal busy
        busy = True
        update_buttons()

        def run():
            try:
                results.put((action, task(), None))
            except Exception as error:
                results.put((action, None, str(error)))

        threading.Thread(target=run, daemon=True).start()

    def start(kind):
        selected = [display for display, flag in options if flag.get()]
        rate, codec = int(fps.get()), encoder.get()
        status.set("Starting capture…")
        submit(kind, lambda: Capture(library, selected, rate, codec, record=kind == "record"))

    def stop():
        status.set("Finishing capture…")
        submit("stop", capture.finish_recording if mode == "record" else capture.stop)

    def save(seconds):
        status.set("Saving replay…")
        submit("save", lambda: capture.save_replay(seconds))

    def open_library():
        try:
            library.mkdir(parents=True, exist_ok=True)
            os.startfile(library)
        except OSError as error:
            status.set(str(error))

    def export_dialog():
        path = filedialog.askopenfilename(initialdir=library, filetypes=[("Layer project", "*.katman")])
        if not path:
            return
        output = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")])
        if output:
            status.set("Exporting video…")
            submit("export", lambda: export(path, output))

    record_button = ttk.Button(panel, text="Start recording", command=lambda: start("record"))
    replay_button = ttk.Button(panel, text="Start replay", command=lambda: start("replay"))
    stop_button = ttk.Button(panel, text="Stop", command=stop)
    short_button = ttk.Button(panel, text="Save last 1 min", command=lambda: save(60))
    long_button = ttk.Button(panel, text="Save last 5 min", command=lambda: save(300))
    export_button = ttk.Button(panel, text="Export project…", command=export_dialog)
    for column, control in enumerate((record_button, replay_button, stop_button)):
        control.grid(row=5, column=column, sticky="ew", padx=2, pady=2)
    for column, control in enumerate((short_button, long_button, export_button)):
        control.grid(row=6, column=column, sticky="ew", padx=2, pady=2)
    ttk.Button(panel, text="Open recordings", command=open_library).grid(row=7, column=0, sticky="w", pady=8)
    ttk.Label(panel, textvariable=status, wraplength=430).grid(row=8, column=0, columnspan=3, sticky="w")

    def update_buttons():
        idle = not busy and capture is None
        active = not busy and capture is not None
        for control in (record_button, replay_button, refresh_button, *monitor_panel.winfo_children()):
            control.configure(state="normal" if idle else "disabled")
        for control in (fps_field, encoder_field):
            control.configure(state="readonly" if idle else "disabled")
        for control in (stop_button, short_button, long_button):
            control.configure(state="normal" if active else "disabled")
        export_button.configure(state="disabled" if busy else "normal")

    def close():
        nonlocal closing
        closing = True
        if not busy:
            if capture is None:
                root.destroy()
            else:
                stop()

    def poll():
        nonlocal capture, mode, busy, closing, capture_failure
        try:
            action, value, error = results.get_nowait()
        except queue.Empty:
            if capture is not None and not busy and (failure := capture.failure()):
                capture_failure = failure
                status.set(failure)
                submit("failure", capture.finish_recording if mode == "record" else capture.stop)
        else:
            busy = False
            if action in {"record", "replay"} and error is None:
                capture, mode = value, action
                status.set("Recording · Replay is also active" if mode == "record" else "Replay active")
            if action in {"stop", "failure"}:
                capture, mode = None, None
            if error:
                status.set(error)
                if action in {"stop", "failure"}:
                    closing = False
            elif isinstance(value, (str, Path)):
                status.set(f"Saved {value}")
            elif action == "stop":
                status.set("Capture stopped.")
            if action == "failure" and capture_failure:
                status.set(f"{capture_failure}\n{status.get()}" if status.get() != capture_failure else capture_failure)
                capture_failure = None
            if closing:
                close()
                if not busy:
                    return
            update_buttons()
        root.after(150, poll)

    root.protocol("WM_DELETE_WINDOW", close)
    refresh()
    update_buttons()
    root.after(150, poll)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(prog="layer", description="Layer Recorder for Windows")
    parser.add_argument("command", nargs="?", default="ui", choices=("ui", "displays", "doctor", "export"))
    parser.add_argument("project", nargs="?")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--library", type=Path, default=default_library())
    args = parser.parse_args()
    try:
        if args.command == "ui":
            capture_controls(args.library.expanduser().resolve())
        elif args.command == "export":
            if args.project is None or args.output is None:
                parser.error("export requires a project and --output")
            print(export(args.project, args.output))
        elif args.command == "displays":
            print(json.dumps([asdict(display) for display in displays()], indent=2))
        else:
            paths = {program: shutil.which(program) for program in ("ffmpeg", "ffprobe")}
            available = []
            capture_available = False
            if paths["ffmpeg"]:
                for option, token in (("-encoders", None), ("-devices", "gdigrab")):
                    result = subprocess.run([paths["ffmpeg"], "-hide_banner", option],
                                            capture_output=True, text=True, check=True, timeout=10)
                    names = {parts[1] for line in result.stdout.splitlines() if len(parts := line.split()) > 1}
                    if token:
                        capture_available = token in names
                    else:
                        available = [encoder for encoder in ENCODERS if encoder in names]
            print(json.dumps({**paths, "gdigrab": capture_available, "encoders": available,
                              "displays": [asdict(display) for display in displays()]}, indent=2))
            if not all(paths.values()) or not capture_available or "libx264" not in available:
                raise SystemExit(1)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
