param(
    [Parameter(Mandatory)][string]$Output,
    [string]$OverlayDirectory
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (!$OverlayDirectory) {
    $OverlayDirectory = Join-Path $root 'build/native/windows-overlay-package/overlay'
}
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$destination = (Resolve-Path -LiteralPath $Output).Path
$temporary = [IO.Directory]::CreateTempSubdirectory('layer-portable-')
$package = Join-Path $temporary.FullName 'Layer-Windows-x64'
try {
    New-Item -ItemType Directory -Path "$package/bin", "$package/docs", "$package/tools" | Out-Null
    Copy-Item -Recurse (Join-Path $root 'katman'), (Join-Path $root 'licenses'), $OverlayDirectory -Destination $package
    foreach ($file in 'pyproject.toml', 'MANIFEST.in', 'README.md', 'LICENSE', 'NOTICE.md', 'Layer.cmd',
                      'docs/WINDOWS.md', 'tools/install-windows.ps1', 'bin/layer-studio.exe', 'bin/layer-capture.exe') {
        Copy-Item -LiteralPath (Join-Path $root $file) -Destination (Join-Path $package $file)
    }
    git -C $root archive --format=zip --prefix=layer-recorder-source/ -o "$package/Layer-source.zip" HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Could not package the corresponding source.' }
    $archive = Join-Path $temporary.FullName 'Layer-Windows-x64.zip'
    Compress-Archive -LiteralPath $package -DestinationPath $archive
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  Layer-Windows-x64.zip" | Set-Content -LiteralPath "$archive.sha256" -Encoding ascii
    Copy-Item -LiteralPath $archive, "$archive.sha256" -Destination $destination -Force
} finally {
    $temporary.Delete($true)
}
