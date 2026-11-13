param([string]$Python = 'py')

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$editor = Join-Path $root 'bin/layer-studio.exe'
if (!(Test-Path -LiteralPath $editor -PathType Leaf)) {
    throw 'Layer Studio is missing. Run tools/build-windows.ps1 or download the Windows build from GitHub Actions.'
}
foreach ($dependency in @('ffmpeg', 'ffprobe')) {
    if (!(Get-Command $dependency -ErrorAction SilentlyContinue)) {
        throw 'Install FFmpeg with winget install --id Gyan.FFmpeg --exact, then open a new PowerShell window.'
    }
}
$venv = Join-Path $root '.venv'
$venvPython = Join-Path $venv 'Scripts/python.exe'
if (!(Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $version = if ($Python -eq 'py') { @('-3.12') } else { @() }
    & $Python @version -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Layer Python environment.' }
}
& $venvPython -m pip install --editable $root
if ($LASTEXITCODE -ne 0) { throw 'Layer installation failed.' }
& $venvPython -m katman doctor
if ($LASTEXITCODE -ne 0) { throw 'Layer could not verify the capture environment.' }
Write-Output "Start Layer with: & '$(Join-Path $venv 'Scripts/layer.exe')'"
