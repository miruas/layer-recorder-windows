# Layer Recorder for Windows

The Windows port of [Layer Recorder](https://github.com/miruas/layer-recorder) saves each selected display and audio source as independent media in a version 1 `project.katman`. A Rust capture engine supervises the producers, keeps encoded replay fragments in RAM and writes normal recordings. FFmpeg encodes display tracks, and WASAPI supplies audio.

## Install

Use 64-bit Windows with Python 3.12 and FFmpeg. Install both with WinGet:

```powershell
winget install --id Python.Python.3.12 --exact
winget install --id Gyan.FFmpeg --exact
```

Open a new PowerShell window so it sees the updated PATH. Download **Layer-Windows-x64** from a successful [Windows workflow run](https://github.com/miruas/layer-recorder-windows/actions/workflows/windows.yml). Extract the artifact and the inner ZIP, then run:

```powershell
.\tools\install-windows.ps1
.\Layer.cmd
```

The installer creates a Python environment under local app data. If you move the extracted directory, rerun the installer.

## Capture and replay

Choose the displays to capture. Select output devices for a system mix, microphones, or independent applications in the audio list. Use **Refresh audio** after an application starts playing. Enable **Include newly active applications** before recording to follow applications automatically. Choose either system audio or independent applications to avoid capturing the same playback twice. Application capture uses WASAPI process loopback and requires Windows build 20348 or later (Windows 11 on desktop).

A selected display resumes after reconnecting or changing mode. Each capture generation has its own source and timeline interval. Enable **Include newly connected displays** to also capture other monitors that appear mid-session. The recorder rediscovers selected audio endpoints during capture.

Replay can save up to five minutes of completed fragments and uses a shared 512 MiB RAM budget. The effective history depends on bitrate and the number of sources. The recorder shows the available replay duration, encoded RAM use, and active video backend. Normal recordings keep replay available. The recorder window keeps the capture session alive.

Enable **global shortcuts** to use these while Layer is in the background:

| Shortcut | Action |
| --- | --- |
| Ctrl+Alt+F8 | Start or stop normal recording |
| Ctrl+Alt+F9 | Start or stop replay |
| Ctrl+Alt+F10 | Save up to the last minute |
| Ctrl+Alt+F11 | Save up to the last five minutes |

## Edit and export

Projects default to `~/Videos/Layer`. Keep `project.katman` and its `media` directory together. Choose **Open editor** to browse the library or open an existing project. Studio mixes enabled audio tracks during timeline playback. **Record voiceover** captures the selected microphone and inserts a separate audio clip at the playhead.

Use **Export project…** or run:

```powershell
.\Layer.cmd export 'C:\Clips\My project\project.katman' --output 'C:\Clips\My video.mp4'
```

## Build from source

Install stable Rust and a Windows linker. For the default MSVC toolchain, install Visual Studio Build Tools with **Desktop development with C++**.

```powershell
git clone https://github.com/miruas/layer-recorder-windows.git
cd layer-recorder-windows
.\tools\build-windows.ps1
.\tools\install-windows.ps1
.\Layer.cmd editor
```

## Compatibility

- With NVIDIA NVENC and one connected, even-sized primary display, Layer uses Desktop Duplication and passes D3D11 frames directly to the encoder. Other layouts and encoders use Windows GDI. Layer falls back to GDI if Desktop Duplication stops on an unchanged display. Software H.264 is the default. NVENC, AMF and Quick Sync require a matching build and driver. `layer doctor` lists the encoders compiled into FFmpeg. Capture verifies whether the selected encoder can start. For 1080p60 on a single NVIDIA display, select **60** and **h264_nvenc** in the recorder. The Desktop Duplication profile caps peak video bitrate at 11 Mb/s to leave room in the default RAM buffer. Actual capture cadence depends on the desktop, driver and workload.
- Physical 1920×1080 capture, software H.264, NVIDIA NVENC and real WASAPI system, application and microphone sources were exercised. Automated tests cover display reconnects and independent sources with synthetic video. Physical multi-monitor, mixed-DPI, AMD and Intel testing is pending. Independent producers have approximate startup alignment.
- Linux-specific GSR overlay controls, portal, KMS and PipeWire roles are described in the [original repository](https://github.com/miruas/layer-recorder#readme).

[FFmpeg Windows capture](https://ffmpeg.org/ffmpeg-devices.html#gdigrab) · [Windows process-loopback capture](https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/)
