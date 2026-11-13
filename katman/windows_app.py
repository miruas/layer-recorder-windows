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
import time

from .common import APP, ASSETS, SETTINGS_FILE
from .windows_capture import Capture, capture_binary, displays
from .windows_hotkeys import GlobalHotkeys

ENCODERS = ("libx264", "h264_nvenc", "h264_amf", "h264_qsv")


def default_library():
    try:
        configured = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("library")
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser()
    except (OSError, ValueError, AttributeError):
        pass
    return Path.home() / "Videos" / "Layer"


def export(path, output):
    from .export import export_project
    return export_project(path, output)


def console_python():
    executable = Path(sys.executable)
    return str(executable.with_name("python.exe")) if executable.name.lower() == "pythonw.exe" else str(executable)


def editor_command(project=None, library=None):
    editor = APP / "bin/layer-studio.exe"
    if not editor.is_file():
        raise RuntimeError("Layer Studio is missing. Build it with tools/build-windows.ps1.")
    command = [str(editor), "--root", str(APP)]
    if project is not None:
        command.extend(("--project", str(Path(project).expanduser().resolve())))
    if library is not None:
        command.extend(("--library", str(library)))
    return command


def file_dialog(mode, kind):
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        extensions = {"open": ("Layer project", "*.katman"), "export": ("Video", "*.mp4"),
                      "import_srt": ("SubRip subtitles", "*.srt"),
                      "export_srt": ("SubRip subtitles", "*.srt"), "frame": ("PNG image", "*.png"),
                      "lut": ("LUT", "*.cube")}
        if mode == "directory":
            path = filedialog.askdirectory(parent=root)
        else:
            options = {"parent": root}
            if kind in extensions:
                options["filetypes"] = [extensions[kind], ("All files", "*")]
            if mode == "save":
                options["defaultextension"] = extensions.get(kind, ("", ""))[1][1:]
                path = filedialog.asksaveasfilename(**options)
            else:
                path = filedialog.askopenfilename(**options)
        print(json.dumps(path or None))
    finally:
        root.destroy()


def capture_controls(library, *, background=False):
    import tkinter as tk
    from tkinter import filedialog, ttk

    from .windows_tray import RecorderInstance, create_tray

    instance = RecorderInstance()
    if not instance.primary:
        instance.close()
        return
    try:
        root = tk.Tk()
    except Exception:
        instance.close()
        raise
    root.title("Layer Recorder")
    icon_file = ASSETS / "layer.ico"
    if icon_file.is_file():
        root.iconbitmap(default=str(icon_file))
    root.minsize(540, 520)
    panel = ttk.Frame(root, padding=20)
    panel.pack(fill="both", expand=True)
    status = tk.StringVar(value="Select the displays you want to capture.")
    replay_status = tk.StringVar()
    options = []
    audio_options = []
    last_audio_inventory = None
    capture = None
    mode = None
    hotkeys = None
    next_inventory = 0.0
    next_audio_inventory = 0.0
    active_display_count = None
    busy = False
    closing = False
    results = queue.SimpleQueue()
    tray_events = queue.SimpleQueue()
    tray = tray_ready = None
    if icon_file.is_file():
        try:
            tray, tray_ready = create_tray(tray_events, icon_file)
        except (ImportError, OSError):
            pass

    def show():
        root.deiconify()
        root.state("normal")
        root.lift()

    def hide():
        if tray_ready and tray_ready.is_set() and tray.visible:
            root.withdraw()
        else:
            close()

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
        selected = {display.key: flag.get() for display, flag in options}
        options.clear()
        for display in found:
            flag = tk.BooleanVar(value=selected.get(display.key, display.primary))
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

    follow_displays = tk.BooleanVar(value=False)
    follow_apps = tk.BooleanVar(value=False)
    capture_all_displays = ttk.Checkbutton(panel, text="Include newly connected displays", variable=follow_displays)
    capture_all_displays.grid(row=9, column=0, columnspan=3, sticky="w", pady=(12, 0))

    audio_panel = ttk.LabelFrame(panel, text="Independent audio sources", padding=6)
    audio_panel.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    audio_select = tk.Listbox(audio_panel, selectmode="multiple", exportselection=False, height=5)
    audio_select.pack(side="left", fill="both", expand=True)
    scrollbar = ttk.Scrollbar(audio_panel, orient="vertical", command=audio_select.yview)
    scrollbar.pack(side="right", fill="y")
    audio_select.configure(yscrollcommand=scrollbar.set)
    capture_all_apps = ttk.Checkbutton(panel, text="Include newly active applications", variable=follow_apps)
    capture_all_apps.grid(row=11, column=0, columnspan=3, sticky="w")
    audio_message = tk.StringVar()
    ttk.Label(panel, textvariable=audio_message, wraplength=490).grid(
        row=12, column=0, columnspan=3, sticky="w")

    def read_audio_inventory():
        from .windows_audio import audio_sources
        return audio_sources()

    def update_audio_inventory(found):
        nonlocal last_audio_inventory
        last_audio_inventory = found
        audio_select.configure(state="normal")
        chosen = {audio_options[index].key for index in audio_select.curselection()}
        audio_options[:] = found
        audio_select.delete(0, "end")
        for index, source in enumerate(found):
            audio_select.insert("end", f"{source.name}  ·  {source.kind}")
            if source.key in chosen:
                audio_select.selection_set(index)
        audio_message.set("" if found else "Windows returned no audio sources.")

    def refresh_audio():
        submit("audio_inventory", read_audio_inventory)

    audio_refresh = ttk.Button(panel, text="Refresh audio", command=refresh_audio)
    audio_refresh.grid(row=11, column=2, sticky="e")

    def toggle_hotkeys():
        nonlocal hotkeys
        if hotkeys:
            hotkeys.close()
            hotkeys = None
        else:
            try:
                hotkeys = GlobalHotkeys()
                if hotkeys.conflicts:
                    status.set("Unavailable shortcuts: " + ", ".join(hotkeys.conflicts))
            except OSError as error:
                hotkey_setting.set(False)
                status.set(str(error))

    hotkey_setting = tk.BooleanVar(value=False)
    ttk.Checkbutton(panel, text="Enable global shortcuts", variable=hotkey_setting,
                    command=toggle_hotkeys).grid(row=13, column=0, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Label(panel, text="Ctrl+Alt+F8 Record  ·  F9 Replay  ·  F10 Save 1 min  ·  F11 Save 5 min",
              wraplength=490).grid(row=14, column=0, columnspan=3, sticky="w")

    def sync_sources(session, inspect_audio):
        inventory = None
        audio_error = None
        if inspect_audio:
            try:
                inventory = read_audio_inventory()
            except (ImportError, OSError, RuntimeError) as error:
                audio_error = str(error)
        count = session.reconcile(displays(), inventory)
        seconds, used, backends = session.replay_status()
        return count, audio_error, seconds, used, backends

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
        sources = [audio_options[index] for index in audio_select.curselection()]
        if any(source.kind == "system" for source in sources) and (
                follow_apps.get() or any(source.kind == "application" for source in sources)):
            status.set("Choose the system mix or separate applications to avoid duplicating playback audio.")
            return
        if follow_apps.get() and last_audio_inventory:
            sources = list({source.key: source for source in
                            [*sources, *(item for item in last_audio_inventory if item.kind == "application")]}.values())
        follow_screens, follow_audio = follow_displays.get(), follow_apps.get()
        status.set("Starting capture…")
        submit(kind, lambda: Capture(library, selected, rate, codec, record=kind == "record",
                                     audio=sources, follow_displays=follow_screens,
                                     follow_application_audio=follow_audio))

    def stop():
        status.set("Finishing capture…")
        submit("stop", capture.finish_recording if mode == "record" else capture.stop)

    def save(seconds):
        status.set("Saving replay…")
        submit("save", lambda: capture.save_replay(seconds))

    def handle_hotkey(action):
        if action == "toggle_record":
            if capture is None:
                start("record")
            elif mode == "record":
                stop()
            else:
                status.set("Stop replay before starting a normal recording.")
        elif action == "toggle_replay":
            if capture is None:
                start("replay")
            elif mode == "replay":
                stop()
            else:
                status.set("Replay is already active with this recording.")
        elif capture is not None:
            save(60 if action == "save_short" else 300)
        else:
            status.set("Start replay or recording before saving a replay.")

    def open_library():
        try:
            library.mkdir(parents=True, exist_ok=True)
            os.startfile(library)
        except OSError as error:
            status.set(str(error))

    def open_editor():
        try:
            command = editor_command(library=library)
            subprocess.Popen(command, env={**os.environ, "LAYER_PYTHON": console_python()},
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, RuntimeError) as error:
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
    ttk.Button(panel, text="Open editor", command=open_editor).grid(row=7, column=1, sticky="w", pady=8)
    ttk.Label(panel, textvariable=status, wraplength=430).grid(row=8, column=0, columnspan=3, sticky="w")

    ttk.Label(panel, textvariable=replay_status, wraplength=490).grid(
        row=15, column=0, columnspan=3, sticky="w", pady=(6, 0))

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
        for control in (capture_all_displays, capture_all_apps, audio_select, audio_refresh):
            control.configure(state="normal" if idle else "disabled")

    def close():
        nonlocal closing
        closing = True
        if not busy:
            if capture is None:
                root.destroy()
            else:
                stop()

    def poll():
        nonlocal capture, mode, busy, closing, next_inventory, next_audio_inventory, active_display_count
        if instance.show_requested():
            show()
        try:
            tray_action = tray_events.get_nowait()
        except queue.Empty:
            pass
        else:
            if tray_action == "show":
                show()
            elif tray_action == "editor":
                open_editor()
            elif tray_action == "exit":
                close()
                if not busy:
                    return
            elif not busy and not closing:
                handle_hotkey(tray_action)
        try:
            action, value, error = results.get_nowait()
        except queue.Empty:
            pass
        else:
            busy = False
            if action in {"record", "replay"} and error is None:
                capture, mode = value, action
                next_inventory = next_audio_inventory = 0.0
                active_display_count = None
                status.set("Recording · Replay is also active" if mode == "record" else "Replay active")
            if action == "stop":
                capture, mode = None, None
                replay_status.set("")
            if action == "audio_inventory":
                if error:
                    audio_message.set(error)
                else:
                    update_audio_inventory(value)
            elif action == "reconcile":
                if error:
                    status.set(error)
                else:
                    count, audio_error, seconds, used, backends = value
                    backend_names = {"ddagrab": "Desktop Duplication", "gdigrab": "GDI"}
                    active_backends = ", ".join(backend_names[name] for name in backends)
                    if seconds:
                        replay_status.set(f"Replay available: {int(seconds)} s  ·  RAM: {used / 2**20:.0f} MiB"
                                          + (f"  ·  {active_backends}" if active_backends else ""))
                    else:
                        replay_status.set("Replay is warming up." if count else "Waiting for video.")
                    if audio_error:
                        audio_message.set(audio_error)
                    if not count:
                        warning = capture.warnings[-1] if capture.warnings else "Selected displays are disconnected."
                        status.set(f"{warning} Waiting for a display.")
                    elif active_display_count == 0:
                        status.set("Display capture resumed.")
                    active_display_count = count
                    if capture.warnings:
                        audio_message.set(capture.warnings[-1])
            elif error:
                status.set(error)
                if action == "stop":
                    closing = False
            elif isinstance(value, (str, Path)):
                status.set(f"Saved {value}")
            elif action == "stop":
                status.set("Capture stopped.")
            if closing:
                close()
                if not busy:
                    return
            update_buttons()

        if hotkeys and not busy and not closing:
            try:
                handle_hotkey(hotkeys.events.get_nowait())
            except queue.Empty:
                pass

        now = time.monotonic()
        if capture is not None and not busy and now >= next_inventory:
            next_inventory = now + 1
            inspect_audio = (capture.audio or capture.follow_application_audio) and now >= next_audio_inventory
            if inspect_audio:
                next_audio_inventory = now + 3
            session = capture
            submit("reconcile", lambda: sync_sources(session, inspect_audio))
        root.after(120, poll)

    root.protocol("WM_DELETE_WINDOW", hide)
    if background:
        root.after(400, lambda: root.withdraw()
                   if tray_ready and tray_ready.is_set() and tray.visible else None)
    refresh()
    update_buttons()
    root.after(150, poll)
    root.after(200, lambda: refresh_audio() if not busy and capture is None else None)
    try:
        root.mainloop()
    finally:
        if tray:
            tray.stop()
        if hotkeys:
            hotkeys.close()
        instance.close()


def main():
    if sys.argv[1:2] == ["--file-dialog"]:
        return file_dialog(*sys.argv[2:])
    parser = argparse.ArgumentParser(prog="layer", description="Layer Recorder for Windows")
    parser.add_argument("command", nargs="?", default="ui", choices=("ui", "editor", "displays", "audio", "doctor", "export"))
    parser.add_argument("project", nargs="?")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--library", type=Path, default=default_library())
    args = parser.parse_args()
    try:
        if args.command == "ui":
            capture_controls(args.library.expanduser().resolve(), background=args.background)
        elif args.command == "editor":
            result = subprocess.run(editor_command(args.project, args.library.expanduser().resolve()),
                                    env={**os.environ, "LAYER_PYTHON": console_python()},
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            raise SystemExit(result.returncode)
        elif args.command == "export":
            if args.project is None or args.output is None:
                parser.error("export requires a project and --output")
            print(export(args.project, args.output))
        elif args.command == "audio":
            from .windows_audio import audio_sources
            print(json.dumps([asdict(source) for source in audio_sources()], indent=2))
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
            engine = capture_binary()
            print(json.dumps({**paths, "capture_engine": str(engine) if engine.is_file() else None,
                              "gdigrab": capture_available, "encoders": available,
                              "displays": [asdict(display) for display in displays()]}, indent=2))
            if not engine.is_file() or not all(paths.values()) or not capture_available or "libx264" not in available:
                raise SystemExit(1)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if sys.stderr is None:
            from tkinter.messagebox import showerror
            showerror("Layer Recorder", str(error))
        else:
            print(error, file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
