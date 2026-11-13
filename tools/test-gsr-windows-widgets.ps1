param(
    [string]$Toolchain = 'C:/msys64/ucrt64/bin',
    [string]$OverlayDirectory = ''
)
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
$frontPage = Join-Path $build 'front_page_generated.cpp'
& (Join-Path $PSScriptRoot 'Get-GsrWindowsFrontPage.ps1') -Source $source -OutputFile $frontPage
$includes = @("-I$mgl/include", "-I$pp/include", "-I$source/include", "-I$port")
$pango = (& $pkg --cflags pangoft2 gio-2.0) -split ' ' | Where-Object { $_ }
if ($LASTEXITCODE -ne 0) { throw 'Pango and GIO headers are required.' }
$libs = (& $pkg --libs pangoft2 gio-2.0) -split ' ' | Where-Object { $_ }
if ($LASTEXITCODE -ne 0) { throw 'Pango and GIO libraries are required.' }
$cSources = @("$port/mgl_win32.c", "$port/mgl_files_win32.c", "$build/window_api.c", "$build/gl_loader.c",
    "$mgl/src/system/clock.c", "$mgl/src/system/utf8.c")
$cSources += (Get-ChildItem "$mgl/src/graphics" -Filter '*.c').FullName
$cppSources = @("$port/config.cpp", "$build/key_names.cpp",
    "$source/src/Config.cpp", "$source/src/Theme.cpp", "$source/src/Translation.cpp",
    "$port/front_page.cpp", $frontPage)
$cppSources += @('Widget', 'Tooltip', 'DropdownButton', 'Utils', 'Page', 'PageStack', 'StaticPage',
    'List', 'CustomRendererWidget', 'Label', 'Image', 'Button') | ForEach-Object { "$source/src/gui/$_.cpp" }
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
$windowsLibs = @('-lopengl32', '-lgdi32', '-luser32', '-lshell32', '-lole32', '-luuid')
& $cxx -std=c++17 -O2 @includes "$port/gsr_widget_smoke.cpp" @objects @libs @windowsLibs -o $exe
if ($LASTEXITCODE -ne 0) { throw 'Could not link the GSR widget test.' }
& $exe $source
if ($LASTEXITCODE -ne 0) { throw "GSR widget test failed with exit code $LASTEXITCODE." }
$overlay = Join-Path $build 'layer-gsr-overlay.exe'
& $cxx -std=c++17 -O2 -municode -mwindows @includes "$port/overlay_main.cpp" @objects @libs @windowsLibs -o $overlay
if ($LASTEXITCODE -ne 0) { throw 'Could not link the native GSR overlay.' }

if ($OverlayDirectory) {
    $resources = Join-Path $OverlayDirectory 'resources'
    New-Item -ItemType Directory -Force -Path $resources | Out-Null
    Copy-Item -LiteralPath $overlay -Destination $OverlayDirectory -Force
    Copy-Item -LiteralPath "$source/images","$source/translations" -Destination $resources -Recurse -Force
    $toolchainRoot = Split-Path -Parent $Toolchain
    Copy-Item -LiteralPath "$toolchainRoot/etc/fonts" -Destination $resources -Recurse -Force
    foreach ($component in @{ 'gsr-ui' = $source; mglpp = $pp; mgl = $mgl }.GetEnumerator()) {
        $archive = Join-Path $OverlayDirectory "$($component.Key)-source.zip"
        & git -c core.longpaths=true -C $component.Value archive --format=zip "--prefix=$($component.Key)/" -o ([IO.Path]::GetFullPath($archive)) HEAD
        if ($LASTEXITCODE -ne 0) { throw "Could not archive $($component.Key) source." }
    }

    $objdump = Join-Path $Toolchain 'objdump.exe'
    $pending = [System.Collections.Generic.Queue[string]]::new()
    $copied = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $pending.Enqueue($overlay)
    while ($pending.Count) {
        $imports = & $objdump -p $pending.Dequeue()
        if ($LASTEXITCODE -ne 0) { throw 'Could not inspect native overlay dependencies.' }
        foreach ($line in $imports) {
            if ($line -notmatch 'DLL Name:\s+(\S+\.dll)$') { continue }
            $dll = $Matches[1]
            $file = Join-Path $Toolchain $dll
            if ((Test-Path -LiteralPath $file) -and $copied.Add($dll)) {
                Copy-Item -LiteralPath $file -Destination $OverlayDirectory -Force
                $pending.Enqueue($file)
            }
        }
    }
}
