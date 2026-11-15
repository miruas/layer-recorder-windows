# SPDX-License-Identifier: GPL-3.0-only
"""Voice-control settings dialog."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import windows_voice
from .voice_control import VoiceCommand, VoiceConfig, validate_commands, validate_config

ACTIONS = (
    ("start_recording", "Start recording"),
    ("stop_recording", "Stop recording"),
    ("toggle_recording", "Toggle recording"),
    ("save_replay", "Save replay"),
)
ACTION_LABELS = dict(ACTIONS)
ACTION_VALUES = {label: action for action, label in ACTIONS}
DEFAULT_MICROPHONE = "(System default)"


def _microphone_choices(stored, status):
    """Return combobox labels and their microphone values."""
    values = [DEFAULT_MICROPHONE]
    labels = {DEFAULT_MICROPHONE: ""}
    try:
        names = list(windows_voice.microphones())
    except (RuntimeError, OSError, ImportError) as error:
        names = []
        status.set(f"Could not list microphones: {error}")
    for raw in names:
        name = raw
        if not name or name in labels.values():
            continue
        label = name
        while label in labels:
            label += " "
        labels[label] = name
        values.append(label)
    if stored and stored not in labels.values():
        label = f"{stored} (not available)"
        while label in labels:
            label += " "
        labels[label] = stored
        values.insert(1, label)
    return values, labels


def show_voice_settings(root: tk.Misc, config: VoiceConfig,
                        on_apply: Callable[[VoiceConfig], None],
                        status: tk.StringVar) -> None:
    window = tk.Toplevel(root)
    window.title("Voice Control")
    window.transient(root)
    window.minsize(560, 480)
    window.columnconfigure(0, weight=1)
    window.rowconfigure(0, weight=1)

    panel = ttk.Frame(window, padding=12)
    panel.grid(row=0, column=0, sticky="nsew")
    panel.columnconfigure(1, weight=1)
    panel.rowconfigure(4, weight=1)

    commands = list(config.commands)
    install_queue = queue.Queue()
    installing = False
    install_after = None

    enabled = tk.BooleanVar(master=window, value=bool(config.enabled))
    wake = tk.StringVar(master=window, value=config.wake_phrase)
    microphone_values, microphone_labels = _microphone_choices(config.microphone, status)
    microphone = tk.StringVar(master=window, value=next(
        (label for label, value in microphone_labels.items() if value == config.microphone),
        DEFAULT_MICROPHONE))
    validation = tk.StringVar(master=window)

    def current_config():
        return VoiceConfig(
            bool(enabled.get()),
            microphone_labels.get(microphone.get(), config.microphone),
            wake.get().strip(),
            tuple(commands),
        )

    def refresh_commands(select=None):
        listbox.delete(0, "end")
        for command in commands:
            label = ACTION_LABELS.get(command.action, command.action)
            listbox.insert("end", f"{label}: {command.template}")
        if select is not None and 0 <= select < len(commands):
            listbox.selection_set(select)
            listbox.see(select)

    def update_apply_state():
        try:
            validate_config(current_config())
        except ValueError as error:
            validation.set(str(error))
            valid = False
        else:
            validation.set("")
            valid = True
        apply_button.state(["!disabled"] if valid and not installing else ["disabled"])

    def edit_command(index=None):
        existing = commands[index] if index is not None else None
        dialog = tk.Toplevel(window)
        dialog.title("Edit voice command" if existing is not None else "Add voice command")
        dialog.transient(window)
        dialog.resizable(False, False)
        form = ttk.Frame(dialog, padding=12)
        form.pack(fill="both", expand=True)
        form.columnconfigure(1, weight=1)

        phrase = tk.StringVar(master=dialog, value=existing.template if existing is not None else "")
        action_labels = list(ACTION_LABELS.values())
        selected_label = ACTION_LABELS[existing.action] if existing is not None else action_labels[0]
        action = tk.StringVar(master=dialog, value=selected_label)
        error = tk.StringVar(master=dialog)

        ttk.Label(form, text="Spoken phrase").grid(row=0, column=0, sticky="w")
        phrase_field = ttk.Entry(form, textvariable=phrase, width=44)
        phrase_field.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(form, text="Action").grid(row=1, column=0, sticky="w", pady=(8, 0))
        action_field = ttk.Combobox(form, textvariable=action, values=action_labels,
                                    state="readonly", width=30)
        action_field.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Label(form, text=(
            "Use {duration} in a replay template, for example “save the last {duration}”. "
            "Say “save the last two minutes and fifteen seconds” after your wake phrase."
        ), wraplength=400, foreground="#555").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(form, textvariable=error, wraplength=400, foreground="#a00000").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(form)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))

        def save_command():
            template = phrase.get().strip()
            if not template:
                error.set("Enter the phrase a person will say.")
                phrase_field.focus_set()
                return
            draft = commands.copy()
            selected = len(draft) if index is None else index
            draft[selected:selected + 1] = [VoiceCommand(template, ACTION_VALUES[action.get()])]
            try:
                validate_commands(draft)
            except ValueError as exc:
                error.set(str(exc))
                return
            commands[:] = draft
            refresh_commands(selected)
            update_apply_state()
            close_editor()

        def close_editor():
            dialog.destroy()
            window.grab_set()

        ttk.Button(buttons, text="Cancel", command=close_editor).pack(side="right")
        ttk.Button(buttons, text="Save", command=save_command).pack(side="right", padx=(0, 6))
        dialog.bind("<Return>", lambda _event: save_command())
        dialog.bind("<Escape>", lambda _event: close_editor())
        dialog.protocol("WM_DELETE_WINDOW", close_editor)
        dialog.wait_visibility()
        dialog.grab_set()
        phrase_field.focus_set()

    def edit_selected():
        selection = listbox.curselection()
        if not selection:
            status.set("Select a voice command to edit.")
            return
        edit_command(selection[0])

    def add_command():
        edit_command()

    def delete_selected():
        selection = listbox.curselection()
        if not selection:
            status.set("Select a voice command to delete.")
            return
        index = selection[0]
        del commands[index]
        refresh_commands(min(index, len(commands) - 1) if commands else None)
        update_apply_state()

    def install_worker():
        # This runs off the Tk thread and never touches widgets or Tk variables.
        try:
            windows_voice.install_model()
        except Exception as error:
            install_queue.put(f"Could not install the speech model: {error}")
        else:
            install_queue.put("English speech model installed.")

    def poll_install():
        nonlocal installing, install_after
        install_after = None
        try:
            message = install_queue.get_nowait()
        except queue.Empty:
            install_after = window.after(100, poll_install)
            return
        installing = False
        status.set(message)
        install_button.state(["!disabled"])
        update_apply_state()

    def start_install():
        nonlocal installing, install_after
        if installing:
            return
        installing = True
        install_button.state(["disabled"])
        apply_button.state(["disabled"])
        status.set("Installing the speech model… This may take several minutes.")
        install_after = window.after(100, poll_install)
        threading.Thread(target=install_worker, name="voice-model-install", daemon=True).start()

    def apply():
        try:
            candidate = current_config()
            validate_config(candidate)
        except ValueError as error:
            status.set(str(error))
            return
        try:
            on_apply(candidate)
        except (OSError, ValueError) as error:
            status.set(str(error) or "Could not save voice control settings.")
            return
        status.set("Voice control settings saved.")
        close()

    def close():
        if install_after is not None:
            window.after_cancel(install_after)
        window.destroy()

    ttk.Checkbutton(panel, text="Enable voice control", variable=enabled).grid(
        row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(panel, text="Wake phrase").grid(row=1, column=0, sticky="w", pady=(10, 0))
    wake_field = ttk.Entry(panel, textvariable=wake)
    wake_field.grid(row=1, column=1, sticky="ew", pady=(10, 0))
    ttk.Label(panel, text="Microphone").grid(row=2, column=0, sticky="w", pady=(8, 0))
    microphone_field = ttk.Combobox(panel, textvariable=microphone, values=microphone_values,
                                     state="readonly")
    microphone_field.grid(row=2, column=1, sticky="ew", pady=(8, 0))
    ttk.Label(panel, text="Voice commands").grid(
        row=3, column=0, columnspan=2, sticky="w", pady=(12, 4))

    command_panel = ttk.Frame(panel)
    command_panel.grid(row=4, column=0, columnspan=2, sticky="nsew")
    command_panel.columnconfigure(0, weight=1)
    command_panel.rowconfigure(0, weight=1)
    listbox = tk.Listbox(command_panel, height=8, exportselection=False)
    scrollbar = ttk.Scrollbar(command_panel, orient="vertical", command=listbox.yview)
    listbox.configure(yscrollcommand=scrollbar.set)
    listbox.grid(row=0, column=0, sticky="nsew")
    scrollbar.grid(row=0, column=1, sticky="ns")
    command_buttons = ttk.Frame(command_panel)
    command_buttons.grid(row=0, column=2, sticky="ns", padx=(8, 0))
    for text, command in (("Add", add_command), ("Edit", edit_selected),
                          ("Delete", delete_selected)):
        ttk.Button(command_buttons, text=text, command=command).pack(fill="x", pady=(0, 4))
    listbox.bind("<Double-Button-1>", lambda _event: edit_selected())

    ttk.Label(panel, text=(
        "Say your wake phrase followed by an English command. Replay templates use {duration}."
    ), wraplength=540, foreground="#555").grid(
        row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

    model_panel = ttk.LabelFrame(panel, text="Speech recognition model", padding=8)
    model_panel.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 0))
    install_button = ttk.Button(model_panel, text="Install voice model…", command=start_install)
    install_button.pack(side="left")
    ttk.Label(model_panel, text="One-time download (78 MB). Recognition runs offline.",
              wraplength=330).pack(side="left", padx=(8, 0))

    ttk.Label(panel, textvariable=status, wraplength=540).grid(
        row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
    ttk.Label(panel, textvariable=validation, wraplength=540, foreground="#a00000").grid(
        row=8, column=0, columnspan=2, sticky="w")

    footer = ttk.Frame(panel)
    footer.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(12, 0))
    footer.columnconfigure(0, weight=1)
    apply_button = ttk.Button(footer, text="Apply", command=apply)
    apply_button.grid(row=0, column=2)
    ttk.Button(footer, text="Close", command=close).grid(row=0, column=1, padx=(0, 6))

    refresh_commands()
    enabled.trace_add("write", lambda *_args: update_apply_state())
    wake.trace_add("write", lambda *_args: update_apply_state())
    microphone.trace_add("write", lambda *_args: update_apply_state())
    window.protocol("WM_DELETE_WINDOW", close)
    window.bind("<Escape>", lambda _event: close())
    update_apply_state()
    window.wait_visibility()
    window.grab_set()
    wake_field.focus_set()
