param([string]$Toolchain = 'C:/msys64/ucrt64/bin')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:Path = "$Toolchain;$env:Path"
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

$exe = Join-Path $root 'build/native/gsr-windows/layer-gsr-overlay.exe'
$source = Join-Path $root 'build/native/gsr-ui/source'
$process = Start-Process -FilePath $exe -ArgumentList ('"' + $source + '"') -PassThru -WindowStyle Hidden
try {
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
    if (![GsrOverlayTest]::PostMessage($window, 0x100, [IntPtr]27, [IntPtr]1)) { throw 'Could not send Escape.' }
    if (!$process.WaitForExit(5000) -or $process.ExitCode -ne 0) { throw 'Escape did not close the overlay cleanly.' }
    Write-Host 'The native GSR overlay covered the cursor monitor and closed on Escape.'
} finally {
    if (!$process.HasExited) { $process.Kill() }
    $process.Dispose()
}
