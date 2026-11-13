# Layer Recorder for Windows

This repository carries the Git history of [Layer Recorder](https://github.com/miruas/layer-recorder) and adds a Windows capture preview. Layer saves each selected monitor as a separate video source in a version 1 `project.katman`. The replay buffer holds encoded fragments in memory. Ordinary recordings write their own media while replay stays available in the same session.

## Run on Windows

Use a desktop session on 64-bit Windows with Python 3.12+ (including Tcl/Tk) and an FFmpeg build that provides `gdigrab`, `libx264` and FFprobe. For example, in PowerShell:

```powershell
winget install --id Python.Python.3.12 --exact
winget install --id Gyan.FFmpeg --exact
```

Open a new PowerShell window so it sees the updated PATH.

```powershell
git clone https://github.com/miruas/layer-recorder-windows.git
cd layer-recorder-windows
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\layer.exe doctor
.\.venv\Scripts\layer.exe
```

Select the monitors to capture, then start a recording or replay. Replay can save up to one or five minutes of available footage. Its default 512 MiB budget is divided among the selected monitors, so high bitrates may shorten the available history. Replay saves use the most recent complete fragments, normally about a second behind the live desktop. Recording and replay start only when requested in the app.

Projects default to `~/Videos/Layer`. Keep `project.katman` and its `media` directory together. Use **Export project…** in the app or run:

```powershell
.\.venv\Scripts\layer.exe export 'C:\Clips\My project\project.katman' --output 'C:\Clips\My video.mp4'
```

## Native timeline editor

Install stable Rust and a Windows linker. For the default MSVC toolchain, install Visual Studio Build Tools with **Desktop development with C++**. From the checkout:

```powershell
.\tools\build-windows.ps1
.\.venv\Scripts\layer.exe editor
.\.venv\Scripts\layer.exe editor 'C:\Clips\My project\project.katman'
```

The capture window also has an **Open editor** button. Studio uses the same project files and library as the recorder. The file picker accepts existing projects, including recordings made on Linux when the source media is present. Studio plays timeline video and preserves audio tracks in MP4 exports. Audible timeline playback and voiceover capture are pending on Windows.

## Preview scope

- Screen capture uses FFmpeg's Windows GDI input. Software H.264 is the default. NVENC, AMF and Quick Sync are selectable when the FFmpeg build and driver support them. `doctor` lists compiled encoders. Actual encoder availability is determined when capture starts.
- The controls, Studio and MP4 export run on Windows. Application/system audio and microphone capture, global shortcuts, and live display hotplug are pending. Keep the display layout fixed during capture.
- One physical 1920×1080 display was tested with software H.264 and NVIDIA NVENC. The Windows regression test exercises independent video sources, replay trimming, and MP4 export with two synthetic displays. Physical multi-monitor, mixed-DPI, AMD and Intel coverage is pending. Separate producers have approximate startup alignment.
- Linux build and feature documentation lives in the [upstream README](https://github.com/miruas/layer-recorder#readme).

FFmpeg reference for [Windows capture](https://ffmpeg.org/ffmpeg-devices.html#gdigrab) and [fragmented MP4](https://ffmpeg.org/ffmpeg-formats.html#Fragmentation).
