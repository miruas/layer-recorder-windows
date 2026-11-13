// SPDX-License-Identifier: GPL-3.0-only
#![cfg_attr(windows, windows_subsystem = "windows")]
use std::{path::PathBuf, process::Command};

fn launch() -> anyhow::Result<()> {
    let root = std::env::current_exe()?.parent().unwrap().to_path_buf();
    let args = std::env::args_os().skip(1).collect::<Vec<_>>();
    let mut command = Command::new(root.join("runtime/pythonw.exe"));
    command.current_dir(&root).args(["-m", "katman"]);
    let path = std::env::join_paths(
        std::iter::once(root.join("media-tools/ffmpeg-9.0.2-full_build/bin")).chain(
            std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()),
        ),
    )?;
    command
        .env("PATH", path)
        .env("PYTHONUTF8", "1")
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env_remove("PYTHONPATH")
        .env_remove("PYTHONHOME");
    if let [file] = args.as_slice()
        && PathBuf::from(file)
            .extension()
            .is_some_and(|ext| ext.eq_ignore_ascii_case("katman"))
    {
        command.arg("editor");
    }
    command.args(args).spawn()?;
    Ok(())
}

fn main() {
    if let Err(error) = launch() {
        #[cfg(windows)]
        {
            let message: Vec<u16> = format!("Layer could not start. {error}")
                .encode_utf16()
                .chain([0])
                .collect();
            let title: Vec<u16> = "Layer Recorder".encode_utf16().chain([0]).collect();
            unsafe {
                windows_sys::Win32::UI::WindowsAndMessaging::MessageBoxW(
                    std::ptr::null_mut(),
                    message.as_ptr(),
                    title.as_ptr(),
                    0x10,
                );
            }
        }
        #[cfg(not(windows))]
        eprintln!("{error}");
    }
}
