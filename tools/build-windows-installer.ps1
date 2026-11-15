param(
    [Parameter(Mandatory)][string]$PythonRoot,
    [Parameter(Mandatory)][string]$Stage,
    [Parameter(Mandatory)][string]$Output,
    [string]$Compiler = 'ISCC.exe'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$PythonRoot = (Resolve-Path $PythonRoot).Path
New-Item -ItemType Directory -Path $Stage,$Output -Force | Out-Null
$Stage = (Resolve-Path $Stage).Path
$Output = (Resolve-Path $Output).Path
$runtime = Join-Path $Stage 'runtime'
New-Item -ItemType Directory -Path "$Stage/bin","$runtime/Lib","$runtime/Lib/site-packages" -Force | Out-Null
foreach ($name in 'python.exe','pythonw.exe','python3.dll','python312.dll','vcruntime140.dll','vcruntime140_1.dll','LICENSE.txt','DLLs','tcl') {
    Copy-Item -LiteralPath (Join-Path $PythonRoot $name) -Destination $runtime -Recurse -Force
}
foreach ($entry in Get-ChildItem "$PythonRoot/Lib") {
    if ($entry.Name -in @('site-packages','__pycache__','test','tests','idlelib','turtledemo','ensurepip')) { continue }
    Copy-Item -LiteralPath $entry.FullName -Destination "$runtime/Lib" -Recurse -Force
}
$runtimePackages = @(
    'Pillow==12.3.0', 'numpy==2.5.4', 'opencv-python-headless==4.14.0.94', 'pystray==0.19.5', 'six==1.17.0'
    'faster-whisper==1.2.1', 'sounddevice==0.5.6', 'webrtcvad-wheels==2.0.14.post1'
)
& "$PythonRoot/python.exe" -m pip install --only-binary=:all: --no-compile --target "$runtime/Lib/site-packages" @runtimePackages
if ($LASTEXITCODE -ne 0) { throw 'Could not assemble the private runtime.' }
Copy-Item -Recurse (Join-Path $root 'katman'),(Join-Path $root 'licenses') -Destination $Stage -Force
Copy-Item (Join-Path $root 'LICENSE'),(Join-Path $root 'NOTICE.md') -Destination $Stage -Force
Copy-Item (Join-Path $root 'bin/layer-studio.exe'),(Join-Path $root 'bin/layer-capture.exe') -Destination "$Stage/bin" -Force
Copy-Item (Join-Path $root 'bin/layer.exe') -Destination $Stage -Force
Copy-Item -LiteralPath (Join-Path $root 'build/native/windows-overlay-package/overlay') -Destination $Stage -Recurse -Force
& git -C $root archive --format=zip --prefix=layer-recorder-source/ -o "$Stage/Layer-source.zip" HEAD
if ($LASTEXITCODE -ne 0) { throw 'Could not archive the corresponding source.' }
& $Compiler "/DStage=$Stage" "/DOutDir=$Output" (Join-Path $root 'packaging/windows/Layer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }
$setup = Join-Path $Output 'Layer-Setup-x64.exe'
(Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant() + '  Layer-Setup-x64.exe' |
    Set-Content -LiteralPath "$setup.sha256" -Encoding ascii
