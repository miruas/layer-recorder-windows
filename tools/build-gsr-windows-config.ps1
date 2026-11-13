param([string]$Compiler = 'g++', [string]$Checkout)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$spec = (Get-Content (Join-Path $root 'packaging/native-lock.json') -Raw | ConvertFrom-Json).'gsr-ui'
$patch = Join-Path $root $spec.patch
if (!$Checkout) { $Checkout = Join-Path $root 'build/native/gsr-ui/source' }
if (!(Test-Path -LiteralPath $Checkout)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $Checkout) -Force | Out-Null
    git -c core.longpaths=true clone --no-checkout $spec.url $Checkout
    if ($LASTEXITCODE -ne 0) { throw 'Could not clone GSR UI.' }
    git -c core.longpaths=true -C $Checkout checkout --detach $spec.revision
    if ($LASTEXITCODE -ne 0) { throw 'Could not check out the pinned GSR UI revision.' }
}
$revision = git -C $Checkout rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $revision -ne $spec.revision) { throw 'Incorrect GSR UI revision.' }
git -c core.longpaths=true -C $Checkout submodule update --init --recursive
if ($LASTEXITCODE -ne 0) { throw 'Could not fetch the pinned MGL dependencies.' }
git -C $Checkout apply --reverse --check $patch 2>$null
if ($LASTEXITCODE -ne 0) {
    git -C $Checkout apply $patch
    if ($LASTEXITCODE -ne 0) { throw 'Could not apply the Layer GSR UI patch.' }
}
$mglpp = Join-Path $Checkout 'depends/mglpp'
$mgl = Join-Path $mglpp 'depends/mgl'
$build = Join-Path $root 'build/native/gsr-windows'
New-Item -ItemType Directory -Path $build -Force | Out-Null

# Keep the original MGL key names without the X11 keysym conversion.
$keySource = Get-Content (Join-Path $mgl 'src/window/key.c') -Raw
$start = $keySource.IndexOf('const char* mgl_key_to_string')
$end = $keySource.IndexOf('bool mgl_key_is_modifier', $start)
if ($start -lt 0 -or $end -le $start) { throw 'Could not locate the pinned MGL key name table.' }
$lines = @(
    '#include <mglpp/window/Keyboard.hpp>'
    '#include <mgl/window/key.h>'
    $keySource.Substring($start, $end - $start)
    'const char *mgl::Keyboard::key_to_string(Key key) {'
    '    return mgl_key_to_string(static_cast<mgl_key>(key));'
    '}'
)
$keyFile = Join-Path $build 'key_names.cpp'
Set-Content -LiteralPath $keyFile -Value $lines -Encoding utf8
$output = Join-Path $build 'layer-gsr-config.exe'
& $Compiler -std=c++17 -O2 -static -municode "-I$Checkout/include" "-I$mglpp/include" "-I$mgl/include" (Join-Path $root 'native/gsr-windows/config.cpp') (Join-Path $Checkout 'src/Config.cpp') $keyFile -o $output -lshell32 -lole32 -luuid
if ($LASTEXITCODE -ne 0) { throw 'Windows GSR configuration build failed.' }
Write-Output $output
