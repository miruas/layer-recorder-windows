# Porting Layer's patched GPU Screen Recorder to Windows

The reference implementation is GPU Screen Recorder 6.1.7 under
`upstream/gpu-screen-recorder` and the exact GSR UI revision in
`packaging/native-lock.json` with `patches/gsr-ui-layer.patch` applied. The
existing Windows capture and Studio binaries are usable components of the
Windows distribution. The small Windows Alt+Z panel is provisional.

| Component | Windows port work | Acceptance |
| --- | --- | --- |
| GSR configuration | Compile the patched upstream config implementation against Win32 filesystem operations, then connect it to the Windows UI and recorder. | Open existing JSON and legacy configurations, preserve unfamiliar fields on save, preserve separate capture profiles and validate Layer replay durations. |
| MGL and GSR UI | Provide a Win32 window/event/monitor backend and use the original GSR widget hierarchy, resources, pages and Layer patch. | Alt+Z opens the patched interface with Replay, Record, Livestream and Recordings. Menu transitions, settings and input match the reference. |
| Capture and recording | Port the GSR platform boundaries for displays, windows, regions, cameras, audio discovery and supported encoder reporting. Connect the Windows producers and Layer archive integration to the shared actions. | Capture each supported source type and compare normal recording, pause, replay, timing, source retirement and save behavior against Linux. |
| Livestream and screenshots | Connect the original action and configuration paths to Windows capture, transport, clipboard and file operations. | Exercise the stream modes with a local receiver and verify full-screen, region and window screenshots, clipboard and disk outputs. |
| Desktop integration | Replace Linux helper responsibilities with Windows equivalents for shortcut registration, focus/identity, game detection, notifications and startup. | Test foreground and background paths, shortcut conflicts, clean shutdown and behavior over borderless and exclusive fullscreen applications. |
| Distribution | Bundle the Windows build of the patched UI and test it as the installed application's entry point. | Clean install, upgrade, launch at sign-in, uninstall and preservation of projects and settings. |

## Verified upstream configuration build

From PowerShell with a MinGW-w64 C++ compiler on PATH:

```powershell
.\tools\build-gsr-windows-config.ps1
.\tools\test-gsr-windows-config.ps1
```

The tool compiles the original patched `Config.cpp` and pinned MGL key name
table. Its Windows filesystem adapter supports Unicode paths. The command is
a development check. The installer does not contain or launch this tool.

## Win32 MGL window backend

After preparing the pinned sources, run:

```powershell
.\tools\test-mgl-windows.ps1
```

The test links the pinned MGL window API and rectangle renderer against a
Win32/WGL backend. It checks rendered pixels, monitor enumeration, resize,
Unicode input, key translation and window recreation on a Windows desktop.
The backend provides the foundation for the original GSR widgets. Transparent
desktop compositing still needs integration.

## Original Layer-patched GSR front page on Windows

Install the MSYS2 UCRT64 GCC, pkgconf and Pango packages, then run
`tools/test-gsr-windows-widgets.ps1` in PowerShell. The test builds the pinned
MGL and MGL++ graphics and Pango text code, and the original GSR Theme,
widget and page classes. The build takes the main page from the pinned,
Layer-patched `Overlay.cpp` and maps its callbacks to Windows actions.
The test loads upstream image resources, verifies the NVIDIA accent pixel,
clicks the first action on Instant Replay, Record, Livestream and Recordings,
and checks the header close button in a hidden Win32 window.

The same build produces `layer-gsr-overlay.exe`. The executable takes the
GSR resource directory as its argument. It captures one frame of the cursor
monitor into memory before showing its borderless window, then draws the
original GSR dim layer and controls over that frame. Run
`tools/test-gsr-windows-overlay.ps1` after building to verify monitor coverage
and Escape dismissal on the interactive Windows desktop. The backdrop is
static while open. Build with `-OverlayDirectory build/native/windows-overlay-package/overlay`
to stage the resources and runtime dependencies for the Windows packages.
The Layer tray and Alt+Z launch that overlay. Record, Replay, Replay Save
and Recordings dispatch to the existing Windows capture controller and Studio.
The main gear opens the Windows controls. The screenshot gear opens the
original GSR screenshot settings and saves them to the GSR configuration.
The Windows directory picker backend preserves Unicode paths. With global
shortcuts enabled, Print Screen saves the configured monitor or window and
Shift+Print Screen saves the foreground window. PNG and JPEG use the
configured directory and resolution. Clipboard output can be enabled
independently of disk output. Ctrl+Print Screen opens a drag selector on the
cursor monitor. Escape cancels selection. Record, Instant Replay and Livestream
open their original GSR settings pages. Record and Replay launched from the
overlay use the saved frame rate and monitor. Replay also uses its buffer and save lengths.
When Region is selected, Record and Replay open the same drag selector and
capture only the selected rectangle.
The original settings list Windows audio endpoints and active applications
from WASAPI. Record and Replay use the selected sources as independent Layer
tracks. Inverted application audio, mixing multiple sources into one GSR
track, other capture options, pause and livestream still need integration.
