$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$manifest = Join-Path $root 'native/editor/Cargo.toml'
$metadata = & cargo metadata --locked --no-deps --format-version 1 --manifest-path $manifest
if ($LASTEXITCODE -ne 0) { throw 'Could not read the Studio build configuration.' }
$target = ($metadata | ConvertFrom-Json).target_directory
& cargo build --release --locked --manifest-path $manifest
if ($LASTEXITCODE -ne 0) { throw 'Layer Studio build failed.' }
$bin = Join-Path $root 'bin'
New-Item -ItemType Directory -Path $bin -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $target 'release/layer-studio.exe') -Destination $bin
