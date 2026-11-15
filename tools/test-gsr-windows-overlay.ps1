param(
    [string]$Toolchain = 'C:/msys64/ucrt64/bin',
    [string]$OverlayDirectory = '',
    [switch]$SelectRecordings,
    [switch]$SelectRegion,
    [switch]$CloseSettings,
    [switch]$LiveCaptureState,
    [switch]$SoftwareOpenGL
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (!$OverlayDirectory) { $env:Path = "$Toolchain;$env:Path" }
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class GsrOverlayTest {
    [StructLayout(LayoutKind.Sequential)]
    public struct Point { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)]
    public struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)]
    public struct MonitorInfo { public int Size; public Rect Bounds, Work; public int Flags; }
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")] public static extern IntPtr MonitorFromPoint(Point point, uint flags);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool GetMonitorInfo(IntPtr monitor, ref MonitorInfo info);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr window, out Rect rect);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr window, uint message, IntPtr wparam, IntPtr lparam);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern uint RegisterWindowMessage(string name);
    [DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr window, uint message,
        IntPtr wparam, IntPtr lparam, uint flags, uint timeout, out IntPtr result);
    private delegate bool EnumCallback(IntPtr window, IntPtr param);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumCallback callback, IntPtr param);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    public static IntPtr FindWindow(uint pid) {
        IntPtr result = IntPtr.Zero;
        EnumWindows((window, param) => {
            uint owner;
            GetWindowThreadProcessId(window, out owner);
            if (owner != pid || !IsWindowVisible(window)) return true;
            result = window;
            return false;
        }, IntPtr.Zero);
        return result;
    }
}
'@

[void][GsrOverlayTest]::SetProcessDpiAwarenessContext([IntPtr](-4))
$cursor = [GsrOverlayTest+Point]::new()
if (![GsrOverlayTest]::GetCursorPos([ref]$cursor)) { throw 'Could not query the cursor monitor.' }
$monitor = [GsrOverlayTest]::MonitorFromPoint($cursor, 2)
$info = [GsrOverlayTest+MonitorInfo]::new()
$info.Size = 40
if (![GsrOverlayTest]::GetMonitorInfo($monitor, [ref]$info)) { throw 'Could not query monitor bounds.' }

if ($OverlayDirectory) {
    $exe = Join-Path $OverlayDirectory 'layer-gsr-overlay.exe'
    $source = (Resolve-Path (Join-Path $OverlayDirectory 'resources')).Path
} else {
    $exe = Join-Path $root 'build/native/gsr-windows/layer-gsr-overlay.exe'
    $source = Join-Path $root 'build/native/gsr-ui/source'
}
$launch = [System.Diagnostics.ProcessStartInfo]::new($exe)
$launch.ArgumentList.Add($source)
if ($SelectRegion) { $launch.ArgumentList.Add('region') }
$launch.UseShellExecute = $false
$launch.RedirectStandardOutput = $true
if ($LiveCaptureState) { $launch.Environment['LAYER_GSR_STATE_MESSAGES'] = '1' }
$configDirectory = $null
if ($CloseSettings) {
    $configDirectory = [IO.Directory]::CreateTempSubdirectory('layer-overlay-')
    $configFile = Join-Path $configDirectory.FullName 'config_ui'
    $launch.Environment['LAYER_GSR_CONFIG'] = $configFile
}
$process = $null
$softwareDll = Join-Path (Split-Path -Parent $exe) 'opengl32.dll'
try {
    if ($SoftwareOpenGL) {
        Copy-Item -LiteralPath (Join-Path $Toolchain 'opengl32.dll') -Destination $softwareDll
        $launch.Environment['Path'] = "$Toolchain;$env:Path"
    }
    $process = [System.Diagnostics.Process]::Start($launch)
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 100
        $process.Refresh()
        if ($process.HasExited) { throw "Overlay exited early with code $($process.ExitCode)." }
        $window = [GsrOverlayTest]::FindWindow($process.Id)
    } until ($window -ne [IntPtr]::Zero -or [DateTime]::UtcNow -ge $deadline)
    if ($window -eq [IntPtr]::Zero) { throw 'The GSR overlay did not show a window.' }
    $bounds = [GsrOverlayTest+Rect]::new()
    if (![GsrOverlayTest]::GetWindowRect($window, [ref]$bounds) -or
        $bounds.Left -ne $info.Bounds.Left -or $bounds.Top -ne $info.Bounds.Top -or
        $bounds.Right -ne $info.Bounds.Right -or $bounds.Bottom -ne $info.Bounds.Bottom) {
        throw 'The overlay did not cover the cursor monitor.'
    }
    if ($LiveCaptureState) {
        $message = [GsrOverlayTest]::RegisterWindowMessage('Layer.Overlay.CaptureState.v1')
        $ack = [IntPtr]::Zero
        if ([GsrOverlayTest]::SendMessageTimeout($window, $message, [IntPtr]2, [IntPtr]0, 2, 3000,
                                              [ref]$ack) -eq [IntPtr]::Zero -or $ack -ne [IntPtr]1) {
            throw 'The native overlay did not receive a live replay state.'
        }
        Start-Sleep -Milliseconds 100
        $height = $bounds.Bottom - $bounds.Top
        $card = [int][Math]::Floor($height / 5)
        $x = [int](($bounds.Right - $bounds.Left) / 2 - 1.5 * $card)
        $row = [Math]::Max(13, .015 * $height) * .8 + 2 * [Math]::Floor(.00694 * $height)
        foreach ($y in @([int]($height / 4), [int]($height / 4 + $card / 2 + 1.4 * $row))) {
            $point = [IntPtr](($y -shl 16) -bor $x)
            [void][GsrOverlayTest]::PostMessage($window, 0x200, [IntPtr]0, $point)
            [void][GsrOverlayTest]::PostMessage($window, 0x201, [IntPtr]1, $point)
            [void][GsrOverlayTest]::PostMessage($window, 0x202, [IntPtr]0, $point)
        }
    } elseif ($CloseSettings) {
        $height = $bounds.Bottom - $bounds.Top
        $card = [int][Math]::Floor($height / 5)
        $side = [int][Math]::Floor($card * .33)
        $x = [int](($bounds.Right - $bounds.Left) / 2 + 2 * $card + .83 * $side)
        $y = [int]($height / 4 + $card / 2 - $side / 2)
        $point = [IntPtr](($y -shl 16) -bor $x)
        if (![GsrOverlayTest]::PostMessage($window, 0x201, [IntPtr]1, $point) -or
            ![GsrOverlayTest]::PostMessage($window, 0x202, [IntPtr]0, $point) -or
            ![GsrOverlayTest]::PostMessage($window, 0x10, [IntPtr]0, [IntPtr]0)) {
            throw 'Could not open and close the GSR settings.'
        }
    } elseif ($SelectRegion) {
        $start = [IntPtr]((250 -shl 16) -bor 300)
        $end = [IntPtr]((70 -shl 16) -bor 100)
        if (![GsrOverlayTest]::PostMessage($window, 0x201, [IntPtr]1, $start) -or
            ![GsrOverlayTest]::PostMessage($window, 0x200, [IntPtr]1, $end) -or
            ![GsrOverlayTest]::PostMessage($window, 0x202, [IntPtr]0, $end)) {
            throw 'Could not drag the screenshot region.'
        }
    } elseif ($SelectRecordings) {
        $height = $bounds.Bottom - $bounds.Top
        $width = $bounds.Right - $bounds.Left
        $cardSize = [int][Math]::Floor($height / 5)
        $x = [int]($width / 2 + 1.5 * $cardSize)
        foreach ($y in @([int]($height / 4), [int]($height / 4 + $cardSize / 2 + $height / 90))) {
            $position = [IntPtr](($y -shl 16) -bor $x)
            if (![GsrOverlayTest]::PostMessage($window, 0x200, [IntPtr]0, $position) -or
                ![GsrOverlayTest]::PostMessage($window, 0x201, [IntPtr]1, $position) -or
                ![GsrOverlayTest]::PostMessage($window, 0x202, [IntPtr]0, $position)) {
                throw 'Could not click the Recordings card.'
            }
        }
    } elseif (![GsrOverlayTest]::PostMessage($window, 0x100, [IntPtr]27, [IntPtr]1)) {
        throw 'Could not send Escape.'
    }
    if (!$process.WaitForExit(5000) -or $process.ExitCode -ne 0) { throw 'The overlay did not close cleanly.' }
    $action = $process.StandardOutput.ReadToEnd().Trim()
    if ($LiveCaptureState) {
        if ($action -notmatch '\Aoverlay_ready [0-9]+\r?\nsave_replay\z') {
            throw "Live replay state did not enable the Save action: '$action'."
        }
        Write-Host 'The open overlay enabled Replay Save after a live state update.'
    } elseif ($CloseSettings) {
        $config = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
        if (!$config.main -or $action -ne 'resume_hotkeys') { throw 'Closing settings did not save the GSR configuration.' }
        Write-Host 'The overlay saved its settings before closing.'
    } elseif ($SelectRegion) {
        $expected = "region $($bounds.Left + 100) $($bounds.Top + 70) 200 180"
        if ($action -ne $expected) { throw "Expected '$expected', received '$action'." }
        Write-Host 'The native overlay returned the selected desktop region.'
    } elseif ($SelectRecordings) {
        if ($action -ne 'recordings') { throw "Unexpected overlay action '$action'." }
        Write-Host 'The native overlay returned the Recordings action.'
    } else {
        if ($action) { throw "Escape returned an unexpected overlay action '$action'." }
        Write-Host 'The native GSR overlay covered the cursor monitor and closed on Escape.'
    }
} finally {
    if ($process) {
        if (!$process.HasExited) {
            $process.Kill()
            $process.WaitForExit()
        }
        $process.Dispose()
    }
    if ($SoftwareOpenGL -and (Test-Path -LiteralPath $softwareDll)) {
        Remove-Item -LiteralPath $softwareDll
    }
    if ($configDirectory) {
        if (Test-Path -LiteralPath $configFile) { Remove-Item -LiteralPath $configFile }
        $configDirectory.Delete()
    }
}
