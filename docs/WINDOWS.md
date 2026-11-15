# Layer Recorder for Windows

Layer Recorder brings the Layer-patched GPU Screen Recorder (GSR) overlay and Layer Studio to Windows. The Rust capture engine supervises FFmpeg video and WASAPI audio producers, retains encoded Instant Replay fragments in RAM, and writes independent media tracks into Layer projects.

- [Install and open Layer](#install-and-open-layer)
- [Record and save Instant Replay](#record-and-save-instant-replay)
- [Voice Control](#voice-control)
- [Livestream and take screenshots](#livestream-and-take-screenshots)
- [Edit and export](#edit-and-export)
- [Default shortcuts](#default-shortcuts)
- [Troubleshoot](#troubleshoot)
- [Implementation and verification status](#implementation-and-verification-status)
- [Build from source](#build-from-source)

## Install and open Layer

1. Open the **Windows capture and editor** workflow for the branch you want. For the build that introduced Voice Control, use the [verified Voice Control workflow run](https://github.com/miruas/layer-recorder-windows/actions/runs/43735470674).
2. Download the **Layer-Setup-x64** artifact, extract the downloaded ZIP, and run **Layer-Setup-x64.exe**. The artifact includes a SHA-256 checksum. Setup installs for the current Windows user and downloads a checksum-verified FFmpeg build on first installation if needed. Python and the media workers run from Layer's private installation directory.
3. Choose whether to add a desktop shortcut, start Layer at sign-in, or add Studio to **Open with** for `.katman` projects. Open **Layer Recorder** or **Layer Studio** from Start.
4. Press **Alt+Z** to open the GSR overlay. If the shortcut is occupied, right-click the Layer icon in the notification area and choose **Toggle overlay**. The overlay's main gear opens the Windows recorder controls. Each capture card has its own GSR settings.

Closing the recorder window leaves Layer in the notification area when the tray is available. A second launch opens the existing window. To finish a capture and close the background process, use **Exit Layer** in the tray menu. Before upgrading, exit the running instance and run the new Setup. Setup reads the current launch-at-sign-in preference when upgrading. Remove the application through **Settings → Apps → Installed apps**. Projects and preferences are stored separately from the installed program.

The workflow also produces a **Layer-Windows-x64** portable artifact for source-style use. Its startup and dependencies are covered under [Build from source](#build-from-source).

## Record and save Instant Replay

### Choose the capture profile

- **GSR overlay and GSR recording shortcuts.** Open **Alt+Z → Record → Settings** or **Instant Replay → Settings**. Choose a display, a window, or **Region**, plus the frame rate, audio inputs, output location, and supported video and audio options. Start the action from its card. Region capture opens a drag selector. For window capture, focus the target window before opening the overlay. **Shift+F9** starts or stops foreground-window recording directly.
- **Windows recorder controls and tray.** Select displays, FPS, encoder and audio sources in the main recorder window. Use **Start recording** or **Start replay**. The tray and the recorder's **Ctrl+Alt** start/stop shortcuts use this profile. Enable **Include newly connected displays** or **Include newly active applications** before starting when you want to follow those sources.

For game audio, choose the system mix or individual applications. Selecting both can duplicate playback. Per-application audio uses WASAPI process loopback and requires Windows build 20348 or later. If an application is missing, start its audio playback and use **Refresh audio**. A selected capture microphone is a recording track. Set the Voice Control microphone separately.

### Make a recording

1. Start **Record** in the overlay or click **Start recording** in the recorder.
2. Use **Alt+F7** or the overlay's **Pause** action to pause and resume. The paused interval is omitted from the project timeline.
3. Stop from the same card, press **Alt+F9** while recording, or use **Stop** in the recorder. Let Layer finish writing the project before moving its files.

Normal recording keeps replay available. If a standalone Instant Replay session is already running, stop that session before starting a normal recording. Saved replay clips are separate projects.

### Save a replay

1. Start **Instant Replay**, or use an active normal recording. Let the buffer fill.
2. Check the recorder's available replay seconds and RAM use. The Windows controls default to 300 seconds. The GSR profile can set a different buffer duration and separate short and long save lengths. The default shared RAM budget is 512 MiB. Effective history can be shorter at high bitrates or with multiple outputs.
3. Use **Alt+F10** for the configured replay duration, **Alt+F11** for the short save, or **Alt+F12** for the long save. Keyboard and UI saves can use the shorter history already available. Voice saves require the full requested duration.
4. Open **Recordings** in the GSR overlay or **Open editor** in the recorder to find the new project.

### Choose a video encoder

For GSR **Auto** and **H.264**, Layer tries NVIDIA NVENC, AMD AMF and Intel Quick Sync in that order, then falls back to software H.264. The probe encodes a test frame using the same settings as the default capture path. An explicit compatible encoder choice in the Windows controls takes precedence over automatic selection. Use **H.264 Software** to force software encoding. Each hardware encoder requires a compatible FFmpeg build, hardware and driver. In the output of `.\Layer.cmd doctor`, `hardware_h264` reports which hardware encoders passed the probe and `encoders` lists the ones compiled into FFmpeg.

| GSR record or replay format | Requirements |
| --- | --- |
| H.264 | Software or a supported H.264 hardware encoder |
| HEVC | Working NVIDIA HEVC NVENC; MP4, MKV or MOV |
| AV1 | Working NVIDIA AV1 NVENC; MP4 or MKV |
| HEVC or AV1 10-bit | Working matching NVIDIA 10-bit encoder; SDR capture in 10-bit 4:2:0 |

Layer tries Desktop Duplication for compatible NVENC display or region capture. It uses GDI for cases including rotated displays, resizing, and explicit limited-range conversion, or when Desktop Duplication is unavailable. NVENC can still encode GDI frames. **Sync to content** uses GDI and keeps a low-rate heartbeat on static screens so replay time continues advancing. The Desktop Duplication profile caps video peak bitrate at 11 Mb/s per track.

GSR audio codec selection applies to recording and replay audio tracks. Add multiple inputs to a single GSR audio track to mix them into one project track. Add separate GSR tracks to keep their audio independently editable. If one track contains both a system mix and an application, Layer uses the system mix to avoid doubling the application audio. A replay made during a recording shares its encoded tracks. MOV and FLV output convert Opus audio to AAC when needed.

## Voice Control

### Initial setup

1. In the Windows recorder, click **Voice Control…**.
2. Use **Install voice model…** to download the approximately 78 MB English model. The download is a one-time setup step. Speech recognition works offline thereafter.
3. Choose a microphone or **System default**. Set a wake phrase, such as `Kivo`, `Jarvis`, `Computer`, or the default `Layer`. Use English letters, spaces or hyphens.
4. Check **Enable voice control** and click **Apply**. Wait for the recorder to show that it is listening.
5. Say the wake phrase followed by the command in one utterance. Voice Control keeps running in the notification area. To release the microphone and loaded recognition model, clear **Enable voice control** and apply.

Voice Control starts disabled. It analyzes microphone samples in memory and uses a voice activity gate before running the local CPU decoder. GPU encoding remains available to the capture engine. The microphone audio used for recognizing commands stays off disk and is never sent to a recognition service. A microphone you independently enable as a recording track is saved with the recording.

### Built-in commands

| Say this with wake phrase `Kivo` | Result |
| --- | --- |
| “Kivo, start recording” | Start normal recording with the GSR Record profile |
| “Kivo, stop recording” | Stop an active normal recording |
| “Kivo, toggle recording” | Start or stop normal recording |
| “Kivo, save the last three minutes” | Request 180 seconds of Instant Replay |
| “Kivo, last thirty seconds” | Request 30 seconds of Instant Replay |
| “Kivo, save the last two minutes and fifteen seconds” | Request 135 seconds of Instant Replay |

Voice start and toggle use the same GSR action as **Alt+F9**. For example, a **Region** profile still opens the region picker. The voice stop action leaves a standalone replay session running.

### Add, edit or delete commands

1. Click **Add**, or select a command and click **Edit**. Enter the phrase you want to speak after your wake phrase and select its action. To remove an entry, select it and click **Delete**.
2. Use a fixed phrase for start, stop or toggle, such as `roll capture`. Each replay template needs exactly one `{duration}`, such as `clip {duration} of that`. The wake phrase goes in its own field.
3. Save the command and click **Apply** to persist your changes. Command matching expects the whole phrase. Equivalent templates that differ only in capitalization or punctuation cannot be added twice.

For `clip {duration} of that`, say “Kivo, clip one minute and ten seconds of that.” Positive whole-number durations and English hours, minutes, and seconds are supported. You can combine units from largest to smallest. Decimal durations are unsupported. Before saving, Layer checks both the configured buffer length and the actual retained video history. If the requested history is unavailable, wait for more footage or ask for a shorter clip. Immediate repetitions of the same recognized action and duration are suppressed.

## Livestream and take screenshots

### Livestream

1. Open **Alt+Z → Livestream → Settings**. Choose a capture source, supported H.264 encoder, frame rate, audio inputs and destination.
2. Set the service's RTMP or RTMPS URL and key where applicable. A WHIP receiver needs its HTTP or HTTPS endpoint and may need a bearer token. Keep keys and tokens private.
3. Start or stop from the card or with **Alt+F8**. RTMP/RTMPS output mixes selected WASAPI inputs into AAC. WHIP publishes H.264 with Opus. The custom container must match the publishing URL:

| Custom container | Accepted URL schemes |
| --- | --- |
| FLV | RTMP, RTMPS, TCP, HTTP, HTTPS |
| Fragmented MP4 | TCP, HTTP, HTTPS |
| MPEG-TS | SRT, TCP, UDP, HTTP, HTTPS |
| HLS | HTTP, HTTPS |

### Screenshots

Open the overlay's screenshot settings to select the target, PNG or JPEG, save directory, optional resize, and disk or clipboard output. At least one destination must be enabled. **Print Screen** captures the configured target. **Shift+Print Screen** captures the foreground window. **Ctrl+Print Screen** opens a region selector on the cursor monitor. Drag to select or press **Escape** to cancel.

## Edit and export

1. Open Layer Studio from the Start menu, the overlay's **Recordings** card, the tray, or **Open editor** in the recorder.
2. Open a project. The default library is `%USERPROFILE%\Videos\Layer`. GSR Record and Instant Replay profiles can save elsewhere. Keep `project.katman` with its `media` directory when moving or backing up a project.
3. Work with separate video and audio sources in the timeline. Studio mixes the enabled audio tracks during playback. **Record voiceover** adds a separate audio clip from the selected microphone at the playhead.
4. Use Studio export or **Export project…** in the recorder to render an MP4. From a source or portable checkout:

```powershell
.\Layer.cmd export 'C:\Clips\My project\project.katman' --output 'C:\Clips\My video.mp4'
```

For editor controls and workflows, see [Studio interaction](https://github.com/miruas/layer-recorder-windows/blob/voice-control/docs/EDITOR_INTERACTION.md) and [Fast export](https://github.com/miruas/layer-recorder-windows/blob/voice-control/docs/FAST_EXPORT.md).

## Default shortcuts

Global shortcuts work while Layer runs, including in the tray. The recorder can disable them. The GSR hotkey settings can override supported GSR bindings.

| Shortcut | Action |
| --- | --- |
| Alt+Z | Open or close the GSR overlay |
| Alt+F7 | Pause or resume recording |
| Alt+F8 | Start or stop livestreaming |
| Alt+F9 | Start or stop recording with the GSR profile |
| Shift+F9 | Start or stop recording the foreground window |
| Ctrl+F9 | Select a recording region or stop a region recording |
| Shift+Alt+F10 | Start or stop Instant Replay with the GSR profile |
| Alt+F10 | Save the configured replay duration |
| Alt+F11 | Save up to the short replay limit |
| Alt+F12 | Save up to the long replay limit |
| Print Screen | Take a screenshot of the configured target |
| Shift+Print Screen | Screenshot the foreground window |
| Ctrl+Print Screen | Screenshot a selected region |
| Win+Alt+Z | Open Layer Studio |
| Ctrl+Alt+F8 | Start or stop normal recording with the Windows recorder profile |
| Ctrl+Alt+F9 | Start or stop replay with the Windows recorder profile |
| Ctrl+Alt+F10 | Save the short replay, one minute by default |
| Ctrl+Alt+F11 | Save the long replay, five minutes by default |

## Troubleshoot

| Symptom | What to check |
| --- | --- |
| Alt+Z is unavailable | Try the tray's **Toggle overlay**. Check the recorder's shortcut status and release the binding in the conflicting app or select another supported binding. For exclusive fullscreen, switch to borderless mode for the overlay, or use capture shortcuts. |
| Recording fails to start | Check that the selected display or window exists and that the output location is writable. Try H.264 Software to isolate encoder errors. For a source or portable build, use `.\Layer.cmd doctor` and check that both FFmpeg and FFprobe are available. |
| Expected microphone or app audio is missing | Check Windows microphone access, the selected GSR audio inputs, and whether the app is producing sound. Refresh the recorder's audio list. For Voice Control, choose the input in its own dialog and reapply settings after a microphone disconnects. |
| Voice Control will not listen | Install the English speech model, choose an available microphone, then enable and apply. If the native recognition library reports missing runtime DLLs, install or repair the [Microsoft Visual C++ x64 Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist). |
| The listener is active but ignores a command | Speak the wake phrase and full configured command together. Check the selected microphone, Windows input level and template. Try a distinct wake phrase if recognition is unreliable. If the recorder was busy, retry once it is ready. |
| Voice replay asks for more history | Verify that Instant Replay or recording is active. Check the available seconds in the recorder, wait for the buffer to fill, or request a shorter clip. |
| Stream will not connect | Check service URL, key or bearer token, container/protocol pairing and whether the receiver is accepting connections. For WHIP use H.264 and an HTTP or HTTPS WHIP endpoint. |

## Implementation and verification status

### Completed

| Area | Available on Windows |
| --- | --- |
| Distribution | x64 Setup, portable ZIP, Start menu entries, tray operation, single instance, optional launch at sign-in and desktop shortcut, optional project file association, and preservation of the sign-in choice during upgrade |
| Overlay and settings | Original Layer-patched GSR widgets and assets on Win32, live desktop dimming with snapshot fallback, settings pages, Unicode directory picker, saved monitor and audio identities, and configurable supported global shortcuts |
| Capture and replay | Display, window and region capture, normal record/pause/resume, bounded encoded replay, independent WASAPI tracks, selected source rediscovery, monitor identity across reorders, and reconnect handling |
| Video encoding | Software H.264 and supported NVENC H.264, HEVC and AV1; NVENC 10-bit SDR where available; explicit AMF/QSV H.264 choices; Desktop Duplication and GDI paths |
| Streaming and screenshots | RTMP/RTMPS, WHIP, supported custom container/protocol combinations, and monitor, window and region screenshots with disk or clipboard destinations |
| Studio | Layer project browsing, independent source tracks, playback, voiceover, and MP4 export |
| Voice Control | Offline English recognition, selectable microphone, custom wake phrase, editable command list, dynamic replay length parsing, available history validation, repeat suppression and listener shutdown |

### Verified

- The [Windows workflow for the original Voice Control commit](https://github.com/miruas/layer-recorder-windows/actions/runs/43735470674) passed. It ran capture, Voice Control, native Studio and GSR overlay checks, verified the portable ZIP and built the x64 installer. The matching [test and package workflow](https://github.com/miruas/layer-recorder-windows/actions/runs/43735470613) also passed.
- On the development PC, 34 Windows tests passed and one shortcut test was skipped because a running Layer instance already owned a binding. Native Windows capture testing exercised physical 1920×1080 capture, software H.264, NVIDIA NVENC and WASAPI system, microphone and application audio. Synthetic tests cover independent tracks, reconnects and timeline behavior.
- The AMD Radeon device passed the AMF H.264 startup probe and produced a recording and a two-second replay through the Rust capture engine, both with synthetic video and while capturing a real 320×240 Windows test window through GDI. Automatic selection chose AMF when the test excluded NVENC. The updated Windows suite passed all 36 tests, including shortcut registration.
- Voice checks covered command add/edit/delete in the dialog, live WASAPI microphone open and release, and synthesized English input for the `Kivo` examples, including 180-, 30- and 135-second replay requests. Recording, replay and export regressions also passed with the listener active. In a quiet 10-second Razer Barracuda X test, the listener used approximately 0.47% of one CPU core and performed no speech decoding. This is an idle sample from one machine.
- Local receiver tests covered WHIP video/audio and session termination, RTMP/FLV, fragmented MP4 and MPEG-TS over TCP, and HLS over HTTP PUT. See the [GSR port notes](https://github.com/miruas/layer-recorder-windows/blob/voice-control/docs/GSR_WINDOWS_PORT.md).
- A Windows test using two live WASAPI output endpoints grouped into one named GSR track produced a stereo AAC track in both a recording and a two-second replay. Mixer tests verify sample addition, clipping, and track identity when an input disconnects.

### Remaining gaps and validation

| Area | Current limitation or open verification |
| --- | --- |
| Overlay coverage | The overlay is available over the desktop and borderless windows. Exclusive fullscreen can bypass desktop composition. Test shortcut ownership and behavior on the target game. |
| Linux parity | Camera capture integration and inverted GSR application audio remain open. Windows uses its own capture and audio APIs. Linux-specific KMS, PipeWire/portal and compositor options have no equivalent workflow in this guide. |
| GPU coverage | AMD AMF recording and replay were tested on a real Windows window and with synthetic video. Full-display and gameplay testing on AMD, physical Intel Quick Sync, multi-monitor and mixed-DPI validation remain open. The documented NVIDIA 10-bit path captures SDR. Full HDR capture and color-management parity are unverified. |
| Audio timing | Independently launched media producers have approximate start alignment. Broad device and workload testing for latency, drift and sync remains pending. |
| Replay transitions | Standalone replay must be stopped before normal recording starts. Transitioning while preserving the existing replay history remains open. Effective duration is constrained by the RAM budget and retained video. |
| Voice field testing | Language support is English. The current wake phrase match follows voice-activity-gated transcription. Live-speaker accuracy across voices, accents, microphones and gameplay noise, along with false-trigger rates and in-game FPS and audio-sync impact, has not been systematically measured. |
| Stream compatibility | Service profiles and the verified receiver combinations are covered above. Every allowed container/protocol pair and production service configuration has not been tested. |
| Distribution coverage | CI builds the installer and verifies the portable package. Fresh-machine installation, upgrade and uninstall across Windows versions and device configurations remain a manual compatibility matrix. |

## Build from source

Use Python 3.12, stable Rust, a Windows linker, FFmpeg and FFprobe. For MSVC, install Visual Studio Build Tools with **Desktop development with C++**. Install MSYS2 and the UCRT64 packages `mingw-w64-ucrt-x86_64-gcc`, `mingw-w64-ucrt-x86_64-pkgconf` and `mingw-w64-ucrt-x86_64-pango` for the original overlay. Use the branch that contains the features you need:

```powershell
git clone --branch voice-control https://github.com/miruas/layer-recorder-windows.git
cd layer-recorder-windows
.\tools\build-windows.ps1
.\tools\test-gsr-windows-widgets.ps1 -OverlayDirectory overlay
.\tools\test-gsr-windows-overlay.ps1 -OverlayDirectory overlay -CloseSettings
.\tools\install-windows.ps1
.\Layer.cmd
```

A portable package already includes the built native components. Extract it and run the Windows install script to prepare the Python environment and launcher. To add Voice Control dependencies to an environment you manage directly from the project root, run `python -m pip install ".[voice]"`. Install the English model from the Voice Control dialog.

Run the Windows test suites from a Python environment with Layer installed. The capture suite uses FFmpeg, FFprobe and the built Rust capture engine.

```powershell
python -m unittest discover -s tests -p 'test_windows*.py' -v
cargo test --locked --manifest-path native/windows-capture/Cargo.toml
```

For the Win32/MGL implementation and overlay tests, see the [GSR Windows port notes](https://github.com/miruas/layer-recorder-windows/blob/voice-control/docs/GSR_WINDOWS_PORT.md). For the upstream Linux behavior, see the [original Layer Recorder repository](https://github.com/miruas/layer-recorder).
