# SPDX-License-Identifier: GPL-3.0-only
"""Windows launcher and capture controls."""
import argparse
import ctypes
from ctypes import wintypes
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

from .common import APP, ASSETS, CONFIG, SETTINGS_FILE, atomic_json
from .windows_capture import (
    Capture, Display, ReplayAutostart, capture_binary, displays, dxgi_targets, foreground_window, recording_container,
    video_color_range, ac_power_connected, gsr_h264_encoder, replay_autostart_trigger, replay_durations,
    running_steam_game, video_cursor, video_rate_control,
    GSR_NVENC_RECORDING_CODECS, H264_HARDWARE_ENCODERS, gsr_frame_rate, gsr_recording_encoder, gsr_ten_bit,
    video_encoder_available, video_framerate_mode,
    video_resolution, window_source,
)
from .windows_hotkeys import GlobalHotkeys, hotkey_bindings
from .windows_screenshot import capture_monitor, gsr_settings, is_monitor_target
from .windows_output import capture_output
from .windows_webcam import camera_inventory, gsr_webcam

ENCODERS = ("libx264", "h264_nvenc", "h264_amf", "h264_qsv")


def default_library():
    try:
        configured = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("library")
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser()
    except (OSError, ValueError, AttributeError):
        pass
    return Path.home() / "Videos" / "Layer"


def gsr_save_directory(settings, library):
    directory = settings.get("save_directory")
    return Path(directory).expanduser().resolve() if isinstance(directory, str) and directory.strip() else library


def export(path, output):
    from .export import export_project
    return export_project(path, output)


def console_python():
    executable = Path(sys.executable)
    return str(executable.with_name("python.exe")) if executable.name.lower() == "pythonw.exe" else str(executable)


def request_overlay_close(process):
    user32 = ctypes.WinDLL("user32")
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    @callback
    def close_window(window, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == process.pid:
            user32.PostMessageW(window, 0x0010, 0, 0)  # WM_CLOSE
        return True

    user32.EnumWindows(close_window, 0)


def terminate_overlay(process):
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass


def editor_command(project=None, library=None):
    editor = APP / "bin/layer-studio.exe"
    if not editor.is_file():
        raise RuntimeError("Layer Studio is missing. Build it with tools/build-windows.ps1.")
    command = [str(editor), "--root", str(APP)]
    if project is not None:
        command.extend(("--project", str(Path(project).expanduser().resolve())))
    if library is not None:
        command.extend(("--library", str(library)))
        directories = dict.fromkeys(gsr_save_directory(gsr_settings(kind), library) for kind in ("record", "replay"))
        directories.pop(library, None)
        for directory in directories:
            command.extend(("--recording-directory", str(directory)))
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
    from .voice_control import VoiceConfig, load_config, save_config
    from .windows_voice import VoiceListener, local_model
    from .windows_voice_settings import show_voice_settings

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
    voice_status = tk.StringVar(value="Voice Control is off.")
    voice = VoiceListener()
    voice_pending = None
    cameras = ""
    camera_scan = None

    def refresh_cameras():
        nonlocal camera_scan
        if camera_scan is not None and camera_scan.is_alive():
            return

        def scan():
            nonlocal cameras
            cameras = camera_inventory()

        camera_scan = threading.Thread(target=scan, name="Layer camera discovery", daemon=True)
        camera_scan.start()
    try:
        voice_config = load_config()
    except (OSError, ValueError) as error:
        voice_config = VoiceConfig()
        voice_status.set(f"Could not load Voice Control settings: {error}")
    replay_status = tk.StringVar()
    options = []
    audio_options = []
    last_audio_inventory = None
    capture = None
    stream = None
    replay_save_lengths = {}
    mode = None
    overlay = None
    overlay_messages = None
    overlay_action = b""
    pending_screenshot = None
    overlay_hotkeys_suspended = False
    overlay_dismissed = False
    overlay_region_capture = None
    overlay_window = None
    hotkeys = None
    next_inventory = 0.0
    next_power_check = 0.0
    replay_autostart = ReplayAutostart()
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

    hotkey_file = CONFIG / "windows-shortcuts.json"
    try:
        enabled = json.loads(hotkey_file.read_text(encoding="utf-8")) is True
    except (OSError, ValueError):
        enabled = True

    def apply_hotkeys():
        nonlocal hotkeys
        if (not overlay_hotkeys_suspended and hotkey_setting.get()
                and gsr_settings("main").get("hotkeys_enable_option") != "disable_hotkeys"):
            bindings, unsupported = hotkey_bindings(gsr_settings)
            if hotkeys:
                if (hotkeys.bindings, hotkeys.unsupported) == (bindings, unsupported):
                    return
                hotkeys.close()
                hotkeys = None
            try:
                hotkeys = GlobalHotkeys(gsr_settings)
                if hotkeys.conflicts:
                    status.set("Unavailable shortcuts: " + ", ".join(hotkeys.conflicts))
            except OSError as error:
                status.set(str(error))
        elif hotkeys:
            hotkeys.close()
            hotkeys = None

    def toggle_hotkeys():
        apply_hotkeys()
        try:
            atomic_json(hotkey_file, hotkey_setting.get())
        except OSError as error:
            status.set(f"Could not save shortcut preference: {error}")

    hotkey_setting = tk.BooleanVar(value=enabled)
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

    def start(kind, *, use_gsr=False, window_handle=None, region=None, force_window=False, automatic=False):
        nonlocal replay_save_lengths
        selected = [display for display, flag in options if flag.get()]
        rate, codec = int(fps.get()), encoder.get()
        seconds = 300
        resolution = None
        cursor = None
        color_range = None
        rate_control = None
        container = "mp4"
        replay_container = None
        audio_codec = "aac"
        save_lengths = {}
        recording_directory = replay_directory = library
        configured_tracks = None
        record_options = None
        chosen_monitor = False
        if use_gsr:
            settings = gsr_settings(kind)
            replay_settings = settings if kind == "replay" else gsr_settings("replay")
            recording_directory = gsr_save_directory(settings, library)
            replay_directory = gsr_save_directory(replay_settings, library)
            record_options = settings.get("record_options")
            audio_codec = record_options.get("audio_codec", "opus") if isinstance(record_options, dict) else "opus"
            cursor = video_cursor(record_options)
            color_range = video_color_range(record_options)
            try:
                resolution = video_resolution(record_options)
                rate_control = video_rate_control(record_options)
                container = recording_container(settings)
                replay_container = recording_container(replay_settings)
            except ValueError as error:
                status.set(str(error))
                show()
                return
            if isinstance(record_options, dict) and "audio_track_item" in record_options:
                configured_tracks = record_options["audio_track_item"]
            if not force_window and region is None:
                target = (record_options.get("record_area_option", "focused_monitor")
                          if isinstance(record_options, dict) else "focused_monitor")
                if target == "region":
                    launch_overlay("region", region_capture=kind)
                    return
                if target == "focused_monitor" or is_monitor_target(target):
                    try:
                        selected = [capture_monitor(target)]
                        chosen_monitor = True
                    except (OSError, RuntimeError) as error:
                        status.set(str(error))
                        show()
                        return
            rate = gsr_frame_rate(record_options)
            seconds, short, long = replay_durations(replay_settings)
            save_lengths = {"save_short": short, "save_long": long}
        if force_window or (use_gsr and isinstance(record_options, dict)
                            and record_options.get("record_area_option") == "window"):
            try:
                selected = [window_source(window_handle if window_handle is not None else foreground_window())]
                chosen_monitor = True
            except RuntimeError as error:
                status.set(str(error))
                show()
                return
        sources = [audio_options[index] for index in audio_select.curselection()]
        follow_audio = follow_apps.get() and not use_gsr
        if use_gsr:
            from .windows_audio import gsr_recording_audio

            try:
                inventory = read_audio_inventory()
                if configured_tracks is None:
                    configured_tracks = [{"audio_inputs": ["default_output"]}]
                sources = gsr_recording_audio(configured_tracks, inventory)
            except (OSError, ValueError) as error:
                status.set(str(error))
                show()
                return
        if not use_gsr and any(source.kind == "system" for source in sources) and (
                follow_audio or any(source.kind == "application" for source in sources)):
            status.set("Choose the system mix or separate applications to avoid duplicating playback audio.")
            return
        if follow_audio and last_audio_inventory:
            sources = list({source.key: source for source in
                            [*sources, *(item for item in last_audio_inventory if item.kind == "application")]}.values())
        if region is not None:
            selected = [region]
        follow_screens = follow_displays.get() and not chosen_monitor and region is None
        replay_save_lengths = save_lengths
        status.set("Starting capture…")
        recording_output = (capture_output(settings, recording_directory, "Layer-recording-",
                                           foreground_window() if window_handle is None else window_handle)
                            if use_gsr and kind == "record" else None)
        submit("automatic_replay" if automatic else kind, lambda: Capture(recording_directory, selected, rate,
                                     gsr_recording_encoder(record_options, codec, container, replay_container)
                                     if use_gsr else codec,
                                     seconds=seconds, record=kind == "record",
                                     audio=sources, follow_displays=follow_screens,
                                     follow_application_audio=follow_audio, fixed_region=region,
                                     resolution=resolution, cursor=cursor, color_range=color_range,
                                     rate_control=rate_control, container=container, replay_directory=replay_directory,
                                     replay_container=replay_container, audio_codec=audio_codec,
                                     framerate_mode=video_framerate_mode(record_options),
                                     ten_bit=gsr_ten_bit(record_options),
                                     gsr_audio_tracks=configured_tracks, webcam=gsr_webcam(record_options),
                                     recording_output=recording_output))

    def stop():
        status.set("Finishing capture…")
        submit("stop", capture.finish_recording if mode == "record" else capture.stop)

    def save(seconds, *, require_full_duration=False, window_handle=None):
        session = capture
        output = (capture_output(gsr_settings("replay"), session.replay_directory, "Layer-replay-",
                                 foreground_window() if window_handle is None else window_handle)
                  if session.gsr_audio_tracks is not None else None)
        status.set("Saving replay…")
        submit("save", lambda: session.save_replay(seconds, require_full_duration=require_full_duration, output=output))

    def apply_voice_config(config):
        nonlocal voice_config, voice_pending
        if config.enabled:
            try:
                local_model()
            except RuntimeError as error:
                raise ValueError(str(error)) from error
        save_config(config)
        voice_config = config
        voice_pending = None
        voice.configure(config)

    def open_voice_settings():
        show_voice_settings(root, voice_config, apply_voice_config, voice_status)

    def handle_voice(command):
        if command.action == "save_replay":
            if capture is None:
                status.set("Start replay or recording before saving a replay.")
            else:
                save(command.seconds, require_full_duration=True)
        elif command.action == "toggle_recording":
            handle_hotkey("gsr_toggle_record")
        elif command.action == "start_recording":
            if mode != "record":
                handle_hotkey("gsr_toggle_record")
        elif command.action == "stop_recording" and mode == "record":
            stop()

    def toggle_stream(*, from_overlay=False):
        if stream is not None:
            submit("stream_stop", stream.stop)
            return
        from .windows_stream import stream_destination, stream_frame_rate

        try:
            settings = gsr_settings("streaming")
            destination = stream_destination(settings)
            options = settings.get("record_options") or {}
            target = options.get("record_area_option", "focused_monitor")
            if target == "region":
                launch_overlay("region", region_capture="stream")
                return
            source = (window_source(overlay_window if from_overlay else foreground_window())
                      if target == "window" else capture_monitor(target))
            launch_stream(source, stream_frame_rate(settings), destination, settings)
        except (OSError, RuntimeError, ValueError, AttributeError) as error:
            status.set(str(error))
            show()

    def launch_stream(source, rate, destination, settings):
        from .windows_stream import Livestream, stream_audio_sources, stream_audio_tracks

        codec = encoder.get()
        status.set("Starting livestream…")

        def publish():
            tracks = stream_audio_tracks(settings)
            inventory = read_audio_inventory()
            # Validate the configuration before inspecting its track fields.
            audio = stream_audio_sources(tracks, inventory, allow_missing=True)
            follow = any(track.get("application_audio_invert") or any(
                isinstance(item, str) and item.casefold().startswith("app:") for item in track["audio_inputs"]
            ) for track in tracks)
            if not follow:
                audio = stream_audio_sources(tracks, inventory)

            def discover_audio():
                return stream_audio_sources(tracks, read_audio_inventory(), allow_missing=True)

            options = settings.get("record_options")
            return Livestream(source, rate, gsr_h264_encoder(options, codec), destination, audio,
                              resolution=video_resolution(options), cursor=video_cursor(options),
                              color_range=video_color_range(options), rate_control=video_rate_control(options),
                              framerate_mode=video_framerate_mode(options), webcam=gsr_webcam(options),
                              discover_audio=discover_audio if follow else None)

        submit("stream_start", publish)

    def stop_overlay():
        nonlocal overlay_dismissed
        if overlay is not None and overlay.poll() is None:
            overlay_dismissed = True
            process = overlay
            request_overlay_close(process)
            root.after(1500, lambda: terminate_overlay(process))

    def toggle_overlay():
        if overlay is not None:
            stop_overlay()
        else:
            state = "paused" if capture and capture.paused else mode or "idle"
            launch_overlay(state + ("+stream" if stream is not None else ""))

    def launch_overlay(overlay_mode, *, region_capture=None):
        nonlocal overlay, overlay_dismissed, overlay_region_capture, overlay_window
        nonlocal overlay_messages, overlay_action
        if overlay is not None:
            return
        executable = APP / "overlay/layer-gsr-overlay.exe"
        resources = APP / "overlay/resources"
        try:
            overlay_dismissed = False
            overlay_region_capture = region_capture
            overlay_window = foreground_window()
            from .windows_audio import gsr_audio_inventory

            inventory = gsr_audio_inventory(last_audio_inventory or ())
            refresh_cameras()
            overlay = subprocess.Popen(
                [str(executable), str(resources), overlay_mode],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env={**os.environ, "LAYER_GSR_AUDIO_INVENTORY": inventory, "LAYER_GSR_CAMERA_INVENTORY": cameras,
                     **{f"LAYER_GSR_{codec.upper()}_NVENC": str(int(available))
                        for codec, available in nvenc_recording_support.items()}},
            )
            overlay_action = b""
            overlay_messages = queue.SimpleQueue()

            def read_messages(pipe, messages):
                try:
                    with pipe:
                        for line in pipe:
                            messages.put(line.strip())
                finally:
                    messages.put(None)

            threading.Thread(target=read_messages, args=(overlay.stdout, overlay_messages), daemon=True).start()
        except OSError as error:
            status.set(f"Could not open the GSR overlay: {error}")
            show()

    def poll_overlay():
        nonlocal overlay, overlay_action, overlay_hotkeys_suspended, pending_screenshot
        if overlay is None:
            return
        while True:
            try:
                message = overlay_messages.get_nowait()
            except queue.Empty:
                return
            if message is None:
                break
            if message in (b"suspend_hotkeys", b"resume_hotkeys"):
                overlay_hotkeys_suspended = message == b"suspend_hotkeys"
                apply_hotkeys()
            else:
                overlay_action = message
        finished, overlay = overlay, None
        finished.wait()
        action = overlay_action
        overlay_hotkeys_suspended = False
        apply_hotkeys()
        if overlay_dismissed:
            if pending_screenshot is not None:
                action, window_handle = pending_screenshot
                pending_screenshot = None
                if not busy and not closing:
                    start_screenshot(action, window_handle)
            return
        if finished.returncode:
            status.set(f"GSR overlay exited with code {finished.returncode}.")
            show()
        elif action == b"recordings":
            open_editor()
        elif action == b"settings":
            show()
        elif action == b"exit":
            close()
        elif not busy and action.startswith(b"region "):
            from .windows_screenshot import desktop_source, screenshot_settings, take_screenshot

            bounds = action.split()[1:]
            if len(bounds) == 4:
                try:
                    x, y, width, height = (int(value) for value in bounds)
                    if min(width, height) < 2:
                        raise ValueError("Invalid region dimensions")
                except ValueError:
                    status.set("The GSR region coordinates were invalid.")
                    return
                if overlay_region_capture:
                    region = Display("Selected region", x, y, width, height)
                    if overlay_region_capture == "stream":
                        from .windows_stream import stream_destination, stream_frame_rate

                        try:
                            settings = gsr_settings("streaming")
                            launch_stream(region, stream_frame_rate(settings), stream_destination(settings), settings)
                        except (ValueError, AttributeError) as error:
                            status.set(str(error))
                            show()
                        return
                    start(overlay_region_capture, use_gsr=True, region=region, window_handle=overlay_window)
                    return
                source = desktop_source(x, y, width, height)
                settings = screenshot_settings()
                clipboard_owner = root.winfo_id()
                submit("screenshot", lambda: take_screenshot(source, settings, clipboard_owner, window=overlay_window))
        elif not busy and action in {b"toggle_record", b"toggle_replay", b"toggle_pause", b"save_replay", b"save_short", b"save_long"}:
            handle_hotkey(action.decode("ascii"), from_overlay=True)
        elif not busy and action == b"toggle_stream":
            toggle_stream(from_overlay=True)

    def start_screenshot(action, window_handle=None):
        from .windows_screenshot import screenshot_settings, screenshot_source, take_screenshot

        try:
            settings = screenshot_settings()
            window_handle = foreground_window() if window_handle is None else window_handle
            if action == "screenshot_region" or (
                action == "screenshot" and settings.get("record_area_option") == "region"
            ):
                launch_overlay("region")
                return
            source = screenshot_source(settings, action, window_handle=window_handle)
            clipboard_owner = root.winfo_id()
            submit("screenshot", lambda: take_screenshot(source, settings, clipboard_owner, window=window_handle))
        except (OSError, RuntimeError) as error:
            status.set(str(error))

    def handle_hotkey(action, *, from_overlay=False):
        nonlocal overlay_dismissed, pending_screenshot
        if action == "toggle_overlay":
            toggle_overlay()
        elif action == "toggle_stream":
            toggle_stream()
        elif action == "toggle_pause":
            if capture is not None and mode == "record":
                submit("pause", capture.toggle_pause)
        elif action in {"screenshot", "screenshot_window", "screenshot_region"}:
            if overlay is None:
                start_screenshot(action)
            else:
                pending_screenshot = action, overlay_window or 0
                overlay_dismissed = True
                stop_overlay()
        elif action == "editor":
            open_editor()
        elif action in {"toggle_record", "gsr_toggle_record", "toggle_window_record", "toggle_region_record"}:
            if capture is None:
                if action == "toggle_region_record":
                    if overlay is None:
                        launch_overlay("region", region_capture="record")
                else:
                    start("record", use_gsr=from_overlay or action != "toggle_record",
                          window_handle=overlay_window if from_overlay else None,
                          force_window=action == "toggle_window_record")
            elif mode == "record":
                stop()
            else:
                status.set("Stop replay before starting a normal recording.")
        elif action in {"toggle_replay", "gsr_toggle_replay"}:
            if capture is None:
                start("replay", use_gsr=from_overlay or action == "gsr_toggle_replay",
                      window_handle=overlay_window if from_overlay else None)
            elif mode == "replay":
                stop()
            else:
                status.set("Replay is already active with this recording.")
        elif capture is not None:
            default = min(60, capture.seconds) if action == "save_short" else capture.seconds
            save(replay_save_lengths.get(action, default), window_handle=overlay_window if from_overlay else None)
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

    ttk.Label(panel, text="Alt+Z  Open overlay").grid(row=14, column=2, sticky="e")
    ttk.Label(panel, textvariable=replay_status, wraplength=490).grid(
        row=15, column=0, columnspan=3, sticky="w", pady=(6, 0))
    ttk.Button(panel, text="Voice Control…", command=open_voice_settings).grid(
        row=16, column=0, sticky="w", pady=(8, 0))
    ttk.Label(panel, textvariable=voice_status, wraplength=490).grid(
        row=17, column=0, columnspan=3, sticky="w")

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
            if stream is not None:
                submit("stream_stop", stream.stop)
            elif capture is None:
                root.destroy()
            else:
                stop()

    def poll():
        nonlocal voice_pending
        nonlocal capture, stream, mode, busy, closing, next_inventory, next_audio_inventory, active_display_count
        nonlocal next_power_check
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
            elif not closing and (tray_action == "toggle_overlay" or not busy):
                handle_hotkey(tray_action)
        try:
            action, value, error = results.get_nowait()
        except queue.Empty:
            pass
        else:
            busy = False
            if action == "stream_start" and error is None:
                stream = value
                status.set("Livestream running.")
            elif action == "stream_stop":
                stream = None
                if error is None:
                    status.set("Livestream stopped.")
            if action in {"record", "replay", "automatic_replay"} and error is None:
                capture, mode = value, "record" if action == "record" else "replay"
                replay_autostart.owns_replay = action == "automatic_replay"
                next_inventory = next_audio_inventory = 0.0
                active_display_count = None
                status.set("Recording · Replay is also active" if mode == "record" else "Replay active")
            if action == "stop":
                capture, mode = None, None
                replay_autostart.owns_replay = False
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
                if action in {"record", "replay", "stream_start"}:
                    show()
                if action == "stop":
                    closing = False
            elif action == "pause":
                status.set("Recording paused." if value else "Recording resumed.")
            elif action == "screenshot" and value is None:
                status.set("Screenshot copied to the clipboard.")
            elif isinstance(value, (str, Path)):
                status.set(f"Saved {value}")
            elif action == "stop":
                status.set("Capture stopped.")
            if closing:
                close()
                if not busy:
                    return
            update_buttons()

        if not closing:
            poll_overlay()
            if closing and not busy:
                return
        if stream is not None and not busy and stream.poll() is not None:
            error = stream.audio_error
            stream.stop()
            stream = None
            status.set(error or "Livestream ended. Check the destination and stream key.")
            show()

        if hotkeys and not closing:
            try:
                action = hotkeys.events.get_nowait()
                if action == "toggle_overlay" or not busy:
                    handle_hotkey(action)
            except queue.Empty:
                pass

        now = time.monotonic()
        if not closing:
            for kind, value in voice.read():
                if kind == "status":
                    voice_status.set(value)
                else:
                    voice_pending = (value, now)
            if voice_pending is not None:
                command, received = voice_pending
                if now - received >= 2:
                    voice_pending = None
                    status.set("Voice command expired while capture was busy.")
                elif not busy:
                    voice_pending = None
                    handle_voice(command)
        if not busy and not closing and now >= next_power_check:
            next_power_check = now + 2
            check_replay_startup()
        if capture is not None and not capture.paused and not busy and now >= next_inventory:
            next_inventory = now + 1
            inspect_audio = (capture.audio or capture.follow_application_audio or
                             capture.gsr_audio_tracks) and now >= next_audio_inventory
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
    apply_hotkeys()
    update_buttons()
    root.after(150, poll)
    nvenc_recording_support = dict.fromkeys(
        (f"{codec}{suffix}" for codec in GSR_NVENC_RECORDING_CODECS for suffix in ("", "_10bit")), False
    )

    def detect_recording_codecs():
        for codec, encoder in GSR_NVENC_RECORDING_CODECS.items():
            nvenc_recording_support[codec] = video_encoder_available(encoder)
            nvenc_recording_support[f"{codec}_10bit"] = video_encoder_available(encoder, ten_bit=True)

    threading.Thread(target=detect_recording_codecs, daemon=True).start()

    def check_replay_startup():
        if overlay is not None and capture is None and stream is None:
            return
        settings = gsr_settings("replay")
        trigger = replay_autostart_trigger(settings, running_steam_game, ac_power_connected)
        action = replay_autostart.update(trigger, idle=capture is None and stream is None)
        if action == "stop":
            stop()
        elif action == "start":
            options = settings.get("record_options")
            target = options.get("record_area_option") if isinstance(options, dict) else None
            if target not in ("window", "region"):
                start("replay", use_gsr=True, automatic=True)
                return
            status.set("Select a monitor in GSR to start replay automatically.")

    def initialize_capture():
        if busy or closing or capture is not None:
            return
        refresh_audio()

    root.after(200, initialize_capture)
    refresh_cameras()
    if voice_config.enabled:
        voice.configure(voice_config)
    try:
        root.mainloop()
    finally:
        voice.close()
        stop_overlay()
        if overlay is not None:
            try:
                overlay.wait(timeout=2)
            except subprocess.TimeoutExpired:
                terminate_overlay(overlay)
                overlay.wait()
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
            compiled = set()
            capture_available = False
            if paths["ffmpeg"]:
                for option, token in (("-encoders", None), ("-devices", "gdigrab")):
                    result = subprocess.run([paths["ffmpeg"], "-hide_banner", option],
                                            capture_output=True, text=True, check=True, timeout=10)
                    names = {parts[1] for line in result.stdout.splitlines() if len(parts := line.split()) > 1}
                    if token:
                        capture_available = token in names
                    else:
                        compiled = names
                        available = [encoder for encoder in ENCODERS if encoder in names]
            engine = capture_binary()
            hardware_h264 = {encoder: encoder in compiled and video_encoder_available(encoder)
                             for encoder in H264_HARDWARE_ENCODERS}
            nvenc = {"h264": hardware_h264["h264_nvenc"]}
            for codec, encoder in GSR_NVENC_RECORDING_CODECS.items():
                nvenc[codec] = encoder in compiled and video_encoder_available(encoder)
                nvenc[f"{codec}_10bit"] = nvenc[codec] and video_encoder_available(encoder, ten_bit=True)
            print(json.dumps({**paths, "capture_engine": str(engine) if engine.is_file() else None,
                              "gdigrab": capture_available, "encoders": available,
                              "hardware_h264": hardware_h264,
                              "nvenc": nvenc, "dxgi_outputs": dxgi_targets() if engine.is_file() else {},
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
