# Layer Recorder for Windows

The Windows port of [Layer Recorder](https://github.com/miruas/layer-recorder) saves each selected display and audio source as independent media in a version 1 `project.katman`. A Rust capture engine supervises the producers, keeps encoded replay fragments in RAM and writes normal recordings. FFmpeg encodes display tracks, and WASAPI supplies audio.

## Install

Download **Layer-Setup-x64.exe** from the **Layer-Setup-x64** artifact of a successful [Windows workflow run](https://github.com/miruas/layer-recorder-windows/actions/workflows/windows.yml). Run Setup and open **Layer Recorder** or **Layer Studio** from the Start menu. Setup installs for the current user and downloads a checksum-verified FFmpeg build on first installation. Python and the media workers run from Layer's private installation directory.

Setup offers an optional desktop shortcut, launch at sign-in, and an **Open with** entry for `.katman` projects. Closing the recorder window leaves it in the Windows notification area, where you can start or stop recording and replay, save recent footage, and open Studio. Select **Exit Layer** from that menu to finish capture and close the recorder. A second launch opens the existing recorder window. Use **Settings → Apps → Installed apps** to uninstall. Projects and user preferences are stored separately.

## Capture and replay

Choose the displays to capture. Select output devices for a system mix, microphones, or independent applications in the audio list. Use **Refresh audio** after an application starts playing. Enable **Include newly active applications** before recording to follow applications automatically. Choose either system audio or independent applications to avoid capturing the same playback twice. Application capture uses WASAPI process loopback and requires Windows build 20348 or later (Windows 11 on desktop).

A selected display resumes after reconnecting or changing mode. Each capture generation has its own source and timeline interval. Enable **Include newly connected displays** to also capture other monitors that appear mid-session. The recorder rediscovers selected audio endpoints during capture.

Replay can save up to five minutes of completed fragments and uses a shared 512 MiB RAM budget. The effective history depends on bitrate and the number of sources. The recorder shows the available replay duration, encoded RAM use, and active video backend. Normal recordings keep replay available. The recorder window keeps the capture session alive.

Global shortcuts are enabled by default while Layer runs, including in the notification area. Change **Enable global shortcuts** in the recorder to save your preference:

| Shortcut | Action |
| --- | --- |
| Alt+Z | Open or close the recording overlay |
| Alt+F7 | Pause or resume recording |
| Alt+F8 | Start or stop livestreaming |
| Alt+F9 | Start or stop recording |
| Shift+F9 | Start or stop recording the foreground window |
| Ctrl+F9 | Select a region to record or stop region recording |
| Shift+Alt+F10 | Start or stop instant replay |
| Alt+F10 | Save the configured replay duration |
| Alt+F11 | Save up to the short replay limit |
| Alt+F12 | Save up to the long replay limit |
| Print Screen | Take a screenshot |
| Shift+Print Screen | Take a screenshot of the foreground window |
| Ctrl+Print Screen | Select a region for a screenshot |
| Win+Alt+Z | Open Layer Studio |
| Ctrl+Alt+F8 | Start or stop normal recording |
| Ctrl+Alt+F9 | Start or stop replay |
| Ctrl+Alt+F10 | Save up to the last minute |
| Ctrl+Alt+F11 | Save up to the last five minutes |

The bundled overlay uses the original Layer-patched GSR interface for Replay, Record, Livestream, Recordings and settings. It opens above the desktop and borderless windows. Exclusive fullscreen applications control their own display mode. Use the global recording and replay shortcuts in that mode. Another application may already own Alt+Z, in which case Layer reports the conflict and the tray menu can still open the panel.

## Edit and export

Projects default to `~/Videos/Layer`. Keep `project.katman` and its `media` directory together. Choose **Open editor** to browse the library or open an existing project. Studio mixes enabled audio tracks during timeline playback. **Record voiceover** captures the selected microphone and inserts a separate audio clip at the playhead.

Use **Export project…** or run:

```powershell
.\Layer.cmd export 'C:\Clips\My project\project.katman' --output 'C:\Clips\My video.mp4'
```

## Build from source

Install stable Rust and a Windows linker. For the default MSVC toolchain, install Visual Studio Build Tools with **Desktop development with C++**. Install MSYS2 and the UCRT64 packages `mingw-w64-ucrt-x86_64-gcc`, `mingw-w64-ucrt-x86_64-pkgconf` and `mingw-w64-ucrt-x86_64-pango` to build the original overlay.

```powershell
git clone https://github.com/miruas/layer-recorder-windows.git
cd layer-recorder-windows
.\tools\build-windows.ps1
.\tools\test-gsr-windows-widgets.ps1 -OverlayDirectory overlay
.\tools\test-gsr-windows-overlay.ps1 -OverlayDirectory overlay -CloseSettings
.\tools\install-windows.ps1
.\Layer.cmd editor
```

## Compatibility

- With NVIDIA NVENC and one connected, even-sized primary display, Layer uses Desktop Duplication and passes D3D11 frames directly to the encoder. Other layouts and encoders use Windows GDI. Layer falls back to GDI if Desktop Duplication stops on an unchanged display. Software H.264 is the default. NVENC, AMF and Quick Sync require a matching build and driver. `layer doctor` lists the encoders compiled into FFmpeg. Capture verifies whether the selected encoder can start. For 1080p60 on a single NVIDIA display, select **60** and **h264_nvenc** in the recorder. The Desktop Duplication profile caps peak video bitrate at 11 Mb/s to leave room in the default RAM buffer. Actual capture cadence depends on the desktop, driver and workload.
- Physical 1920×1080 capture, software H.264, NVIDIA NVENC and real WASAPI system, application and microphone sources were exercised. Automated tests cover display reconnects and independent sources with synthetic video. Physical multi-monitor, mixed-DPI, AMD and Intel testing is pending. Independent producers have approximate startup alignment.
- Linux-specific GSR overlay controls, portal, KMS and PipeWire roles are described in the [original repository](https://github.com/miruas/layer-recorder#readme).

[FFmpeg Windows capture](https://ffmpeg.org/ffmpeg-devices.html#gdigrab) · [Windows process-loopback capture](https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/)
