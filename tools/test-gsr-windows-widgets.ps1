param([string]$Toolchain = 'C:/msys64/ucrt64/bin')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root 'build/native/gsr-ui/source'
$pp = Join-Path $source 'depends/mglpp'
$mgl = Join-Path $pp 'depends/mgl'
$port = Join-Path $root 'native/gsr-windows'
$build = Join-Path $root 'build/native/gsr-windows'
$env:Path = "$Toolchain;$env:Path"
$env:PKG_CONFIG_PATH = Join-Path (Split-Path -Parent $Toolchain) 'lib/pkgconfig'
$cc = Join-Path $Toolchain 'gcc.exe'
$cxx = Join-Path $Toolchain 'g++.exe'
$pkg = Join-Path $Toolchain 'pkgconf.exe'
if (!(Test-Path $pkg)) { throw 'Install the MSYS2 UCRT64 gcc, pkgconf and pango packages.' }
& (Join-Path $PSScriptRoot 'build-gsr-windows-config.ps1') -Compiler $cxx
& (Join-Path $PSScriptRoot 'test-mgl-windows.ps1') -GenerateOnly
$includes = @("-I$mgl/include", "-I$pp/include", "-I$source/include")
$pango = (& $pkg --cflags pangoft2 gio-2.0) -split ' ' | Where-Object { $_ }
if ($LASTEXITCODE -ne 0) { throw 'Pango and GIO headers are required.' }
$libs = (& $pkg --libs pangoft2 gio-2.0) -split ' ' | Where-Object { $_ }
if ($LASTEXITCODE -ne 0) { throw 'Pango and GIO libraries are required.' }
$cSources = @("$port/mgl_win32.c", "$port/mgl_files_win32.c", "$build/window_api.c", "$build/gl_loader.c",
    "$mgl/src/system/clock.c", "$mgl/src/system/utf8.c")
$cSources += (Get-ChildItem "$mgl/src/graphics" -Filter '*.c').FullName
$cppSources = @("$port/gsr_widget_smoke.cpp", "$port/config.cpp", "$build/key_names.cpp",
    "$source/src/Config.cpp", "$source/src/Theme.cpp")
$cppSources += @('Widget', 'Tooltip', 'DropdownButton', 'Utils') | ForEach-Object { "$source/src/gui/$_.cpp" }
$cppSources += (Get-ChildItem "$pp/src" -Recurse -Filter '*.cpp' |
    Where-Object { $_.Name -ne 'Keyboard.cpp' }).FullName
$objects = @()
foreach ($file in $cSources + $cppSources) {
    $object = Join-Path $build ("widget-" + $objects.Count + ".o")
    if ($file.EndsWith('.cpp')) {
        & $cxx -std=c++17 -O2 -DLAYER_GSR_CONFIG_LIBRARY @includes @pango -c $file -o $object
    } else {
        & $cc -std=gnu11 -O2 -DSTBI_WINDOWS_UTF8 @includes @pango -c $file -o $object
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not compile $file." }
    $objects += $object
}
$exe = Join-Path $build 'gsr-widget-win32-smoke.exe'
& $cxx @objects @libs -o $exe -lopengl32 -lgdi32 -luser32 -lshell32 -lole32 -luuid
if ($LASTEXITCODE -ne 0) { throw 'Could not link the GSR widget test.' }
& $exe $source
if ($LASTEXITCODE -ne 0) { throw "GSR widget test failed with exit code $LASTEXITCODE." }
