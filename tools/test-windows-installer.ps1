# SPDX-License-Identifier: GPL-3.0-only
param(
    [Parameter(Mandatory)][string]$Installer,
    [Parameter(Mandatory)][string]$TestDirectory
)

$ErrorActionPreference = 'Stop'
$setup = (Resolve-Path -LiteralPath $Installer).Path
$root = (New-Item -ItemType Directory -Path $TestDirectory).FullName
$install = Join-Path $root 'Layer Recorder'
$launcher = Join-Path $install 'Layer.exe'
$runtime = Join-Path $install 'runtime/pythonw.exe'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{1BAA861B-B01E-4458-89EC-F93E9B68D6B1}_is1'
$startupKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$projectKey = 'HKCU:\Software\Classes\LayerRecorder.Project'
$startup = '"' + $launcher + '" --background'
$mutexName = 'Local\LayerRecorder.Instance'
$programs = Join-Path ([Environment]::GetFolderPath('Programs')) 'Layer Recorder'
$desktopShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Layer Recorder.lnk'

function Read-Startup {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Run')
    if ($key) {
        try { $key.GetValue('LayerRecorder') } finally { $key.Dispose() }
    }
}

function Run-Setup($name, [string[]]$options = @()) {
    $log = Join-Path $root "$name.log"
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$install`"", "/LOG=`"$log`"") + $options
    $process = Start-Process -FilePath $setup -ArgumentList $arguments -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        Get-Content -LiteralPath $log -Tail 50 -ErrorAction SilentlyContinue
        throw "$name exited with code $($process.ExitCode)."
    }
}

function Recorder-Processes {
    Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $runtime }
}

function Wait-Recorder {
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $running = @(Recorder-Processes)
        if ($running.Count -eq 1) {
            $mutex = $null
            if ([Threading.Mutex]::TryOpenExisting($mutexName, [ref]$mutex)) {
                $mutex.Dispose()
                return $running[0]
            }
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'The installed launcher did not start a recorder instance.'
}

function Launch-Recorder([string[]]$arguments) {
    $options = @{}
    if ($arguments.Count) { $options.ArgumentList = $arguments }
    $process = Start-Process -FilePath $launcher -PassThru -WindowStyle Hidden @options
    if (!$process.WaitForExit(10000) -or $process.ExitCode -ne 0) {
        throw 'The installed launcher failed to hand off to the recorder.'
    }
}

function Run-Uninstall {
    $uninstaller = Get-ChildItem -LiteralPath $install -Filter 'unins*.exe' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (!$uninstaller) { throw 'The installed uninstaller is missing.' }
    $log = Join-Path $root 'uninstall.log'
    $process = Start-Process -FilePath $uninstaller.FullName -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$log`""
    ) -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        Get-Content -LiteralPath $log -Tail 50 -ErrorAction SilentlyContinue
        throw "Uninstall exited with code $($process.ExitCode)."
    }
}

$existingMutex = $null
if ([Threading.Mutex]::TryOpenExisting($mutexName, [ref]$existingMutex)) {
    $existingMutex.Dispose()
    throw 'Use an isolated Windows test account with no Layer instance running.'
}
if ((Test-Path -LiteralPath $uninstallKey) -or (Read-Startup) -or (Test-Path -LiteralPath $projectKey) -or
    (Test-Path -LiteralPath $programs) -or (Test-Path -LiteralPath $desktopShortcut)) {
    throw 'Use an isolated Windows test account with no existing Layer installation.'
}
$expectedHash = (Get-Content -LiteralPath "$setup.sha256" -Raw).Trim().Split(' ')[0]
if ((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash -ne $expectedHash) {
    throw 'Installer checksum mismatch.'
}

$removed = $false
try {
    Run-Setup 'install' -options @('/TASKS=startup,desktopicon,projects')
    foreach ($file in @($launcher, $runtime, "$install/bin/layer-capture.exe", "$install/bin/layer-studio.exe",
                       "$install/overlay/layer-gsr-overlay.exe", "$install/Layer-source.zip",
                       "$programs/Layer Recorder.lnk", "$programs/Layer Studio.lnk", $desktopShortcut)) {
        if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing installed file: $file" }
    }
    if ((Read-Startup) -ne $startup) { throw 'Setup did not write the expected sign-in command.' }
    $association = (Get-Item -LiteralPath "$projectKey/shell/open/command").GetValue('')
    if ($association -ne ('"' + $launcher + '" "%1"')) { throw 'The project file association is incorrect.' }

    $previousPath = $env:Path
    Push-Location $install
    try {
        $media = Join-Path $install 'media-tools/ffmpeg-9.0.2-full_build/bin'
        foreach ($file in 'ffmpeg.exe', 'ffprobe.exe') {
            if (!(Test-Path -LiteralPath (Join-Path $media $file))) { throw "Missing bundled tool: $file" }
        }
        $env:Path = "$media;$previousPath"
        & "$install/runtime/python.exe" -m katman doctor
        if ($LASTEXITCODE -ne 0) { throw 'The installed runtime did not pass doctor.' }
        & "$install/runtime/python.exe" -c 'import sounddevice, webrtcvad; from faster_whisper import WhisperModel'
        if ($LASTEXITCODE -ne 0) { throw 'The installed Voice Control dependencies could not load.' }
    } finally {
        $env:Path = $previousPath
        Pop-Location
    }

    Launch-Recorder @('--background')
    $first = Wait-Recorder
    Start-Sleep -Milliseconds 1000
    $first.Refresh()
    if ($first.HasExited -or $first.MainWindowHandle -ne 0) {
        throw 'The sign-in command did not leave the recorder in the background.'
    }
    Launch-Recorder @()
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 100
        $running = @(Recorder-Processes)
        if ($running.Count -eq 1 -and $running[0].Id -eq $first.Id -and $running[0].MainWindowHandle -ne 0) {
            break
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($running.Count -ne 1 -or $running[0].Id -ne $first.Id -or $running[0].MainWindowHandle -eq 0) {
        throw 'A second launch did not show the existing recorder window.'
    }
    $first | Stop-Process -ErrorAction Stop
    $first.WaitForExit()

    Run-Setup 'upgrade-startup-enabled'
    if ((Read-Startup) -ne $startup) { throw 'Upgrade lost the enabled sign-in choice.' }
    Remove-ItemProperty -LiteralPath $startupKey -Name LayerRecorder
    Run-Setup 'upgrade-startup-disabled'
    if (Read-Startup) { throw 'Upgrade re-enabled the disabled sign-in choice.' }

    Run-Uninstall
    $removed = $true
    foreach ($item in @($launcher, $runtime, $uninstallKey, $projectKey, $desktopShortcut,
                        "$programs/Layer Recorder.lnk", "$programs/Layer Studio.lnk")) {
        if (Test-Path -LiteralPath $item) { throw "Uninstall left an installed item: $item" }
    }
    if (Read-Startup) { throw 'Uninstall left the sign-in command behind.' }
    Write-Host 'Setup, installed launcher, single instance, upgrade and uninstall passed.'
} finally {
    Recorder-Processes | Stop-Process -ErrorAction SilentlyContinue
    if (!$removed -and (Test-Path -LiteralPath $uninstallKey)) { Run-Uninstall }
}
