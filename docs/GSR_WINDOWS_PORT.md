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
The backend provides the foundation for the original GSR widgets. The GSR page hierarchy and transparent desktop compositing still need integration.

## Original GSR dropdown on Windows

Install the MSYS2 UCRT64 GCC, pkgconf and Pango packages, then run
`tools/test-gsr-windows-widgets.ps1` in PowerShell. The test builds the pinned
MGL and MGL++ graphics and Pango text code, and the original GSR Theme,
Widget, Tooltip and DropdownButton classes. It loads the upstream image
resources and verifies the NVIDIA accent pixel and the Start action of the
Instant Replay dropdown. The test uses a hidden Win32 window and sends its
input messages directly to that window.
