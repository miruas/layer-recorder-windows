$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root 'bin'
New-Item -ItemType Directory -Path $bin -Force | Out-Null
foreach ($component in @{ editor = 'layer-studio'; 'windows-capture' = 'layer-capture' }.GetEnumerator()) {
    $manifest = Join-Path $root "native/$($component.Key)/Cargo.toml"
    $metadata = & cargo metadata --locked --no-deps --format-version 1 --manifest-path $manifest
    if ($LASTEXITCODE -ne 0) { throw "Could not read the $($component.Value) build configuration." }
    $target = ($metadata | ConvertFrom-Json).target_directory
    & cargo build --release --locked --manifest-path $manifest
    if ($LASTEXITCODE -ne 0) { throw "$($component.Value) build failed." }
    Copy-Item -LiteralPath (Join-Path $target "release/$($component.Value).exe") -Destination $bin
}
