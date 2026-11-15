param(
    [string]$Toolchain = 'C:/msys64/ucrt64/bin',
    [string]$OverlayDirectory = '',
    [switch]$SoftwareOpenGL
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
$fileChooser = Join-Path $build 'file_chooser_generated.cpp'
& (Join-Path $PSScriptRoot 'Get-GsrWindowsFrontPage.ps1') -Source $source -OutputFile $frontPage
& (Join-Path $PSScriptRoot 'Get-GsrWindowsFileChooser.ps1') -Source $source -OutputFile $fileChooser
& (Join-Path $PSScriptRoot 'Get-GsrWindowsGlobalSettings.ps1') -Source $source -OutputDirectory $build
$settingsPage = Join-Path $build 'settings_page_generated.cpp'
$settingsCode = Get-Content -LiteralPath "$source/src/gui/SettingsPage.cpp" -Raw
$settingsCode = $settingsCode.Replace('../../include/', '')
$settingsCode = $settingsCode.Replace('    static std::optional<std::string> webcam_get_name(std::string_view webcam_path) {',
    "    std::string windows_webcam_name(std::string_view device);"+[Environment]::NewLine+
    "    static std::optional<std::string> webcam_get_name(std::string_view webcam_path) {"+[Environment]::NewLine+
    '        if(starts_with(webcam_path, "dshow:")) return windows_webcam_name(webcam_path);')
$settingsCode = $settingsCode.Replace('        webcam_sources_box_ptr->set_selected_item(record_options.webcam_source);',
    "        if(!record_options.webcam_source.empty()) webcam_sources_box_ptr->add_item(windows_webcam_name(record_options.webcam_source), record_options.webcam_source, false);"+[Environment]::NewLine+
    '        webcam_sources_box_ptr->set_selected_item(record_options.webcam_source);')
$settingsCode = $settingsCode.Replace('Sync to content (Only X11 or desktop portal capture)', 'Sync to content')
foreach ($codec in 'hevc', 'av1', 'hevc_10bit', 'av1_10bit') {
    $settingsCode = $settingsCode.Replace("if(gsr_info->supported_video_codecs.$codec)",
        "if(type != Type::STREAM && gsr_info->supported_video_codecs.$codec)")
}
foreach ($device in 'device_name', 'audio_input') {
    $selection = "audio_device_box->set_selected_item($device);"
    $settingsCode = $settingsCode.Replace($selection,
        "audio_device_box->add_item(TR(`"Disconnected audio device`"), $device, false);`n                    $selection")
}
$screenshotSettingsPage = Join-Path $build 'screenshot_settings_page_generated.cpp'
$screenshotSettingsCode = (Get-Content -LiteralPath "$source/src/gui/ScreenshotSettingsPage.cpp" -Raw).Replace('../../include/', '')
foreach ($page in @(
    @{ Path = $settingsPage; Code = $settingsCode; Target = 'record_options.record_area_option' }
    @{ Path = $screenshotSettingsPage; Code = $screenshotSettingsCode; Target = 'config.screenshot_config.record_area_option' }
)) {
    $target = $page.Target
    $selection = "record_area_box_ptr->set_selected_item($target);"
    $replacement = @"
const auto monitor_target = windows_monitor_target($target);
        if(starts_with(monitor_target, R"(\\.\)") || starts_with(monitor_target, R"(\\?\)"))
            record_area_box_ptr->add_item(TR("Disconnected monitor"), monitor_target, false);
        record_area_box_ptr->set_selected_item(monitor_target);
"@
    $code = $page.Code.Replace($selection, $replacement)
    $code = $code.Replace('namespace gsr {', 'namespace gsr {' + [Environment]::NewLine +
        '    std::string windows_monitor_target(const std::string &name);')
    $code = $code.Replace('record_area_box->add_item(name, monitor.name);',
        'record_area_box->add_item(name, windows_monitor_target(monitor.name));')
    Set-Content -LiteralPath $page.Path -Value $code -Encoding utf8
}
$includes = @("-I$mgl/include", "-I$pp/include", "-I$source/include", "-I$port", "-I$build")
$pango = (& $pkg --cflags pangoft2 gio-2.0) -split ' ' | Where-Object { $_ }
if ($LASTEXITCODE -ne 0) { throw 'Pango and GIO headers are required.' }
$libs = (& $pkg --libs pangoft2 gio-2.0) -split ' ' | Where-Object { $_ }
if ($LASTEXITCODE -ne 0) { throw 'Pango and GIO libraries are required.' }
$cSources = @("$port/mgl_win32.c", "$port/mgl_files_win32.c", "$build/window_api.c", "$build/gl_loader.c",
    "$mgl/src/system/clock.c", "$mgl/src/system/utf8.c")
$cSources += (Get-ChildItem "$mgl/src/graphics" -Filter '*.c').FullName
$cppSources = @("$port/config.cpp", "$build/key_names.cpp",
    "$source/src/Config.cpp", "$source/src/Theme.cpp", "$source/src/Translation.cpp",
    "$port/front_page.cpp", "$port/settings_platform.cpp", $frontPage, $fileChooser, $settingsPage,
    $screenshotSettingsPage, "$build/global_settings_generated.cpp")
$cppSources += @('Widget', 'Tooltip', 'DropdownButton', 'Utils', 'Page', 'PageStack', 'StaticPage',
    'List', 'CustomRendererWidget', 'Label', 'Image', 'Button',
    'GsrPage', 'ScrollablePage', 'Subsection', 'RadioButton', 'LineSeparator', 'ComboBox', 'CheckBox',
    'Entry') | ForEach-Object { "$source/src/gui/$_.cpp" }
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
$testConfig = Join-Path $build 'widget-config-test.o'
& $cxx -std=c++17 -O2 -DLAYER_GSR_CONFIG_LIBRARY -DLAYER_GSR_CONFIG_TEST @includes -c "$port/config.cpp" -o $testConfig
if ($LASTEXITCODE -ne 0) { throw 'Could not compile the isolated GSR test configuration.' }
# The first object in the C++ group is the production config adapter.
$testObjects = $objects.Clone()
$testObjects[$cSources.Count] = $testConfig
& $cxx -std=c++17 -O2 @includes "$port/gsr_widget_smoke.cpp" @testObjects @libs @windowsLibs -o $exe
if ($LASTEXITCODE -ne 0) { throw 'Could not link the GSR widget test.' }
$env:LAYER_GSR_TEST_CONFIG_DIR = Join-Path $build 'widget-test-config'
$softwareDll = Join-Path $build 'opengl32.dll'
try {
    if ($SoftwareOpenGL) {
        Copy-Item -LiteralPath (Join-Path $Toolchain 'opengl32.dll') -Destination $softwareDll
    }
    & $exe $source
    if ($LASTEXITCODE -ne 0) { throw "GSR widget test failed with exit code $LASTEXITCODE." }
    if (!(Test-Path -LiteralPath "$env:LAYER_GSR_TEST_CONFIG_DIR/config_ui")) {
        throw 'The screenshot settings page did not save its configuration.'
    }
} finally {
    if ($SoftwareOpenGL -and (Test-Path -LiteralPath $softwareDll)) {
        Remove-Item -LiteralPath $softwareDll
    }
    Remove-Item Env:LAYER_GSR_TEST_CONFIG_DIR
}
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
            # The shipped overlay uses the system OpenGL driver.
            if ($dll -ieq 'opengl32.dll') { continue }
            $file = Join-Path $Toolchain $dll
            if ((Test-Path -LiteralPath $file) -and $copied.Add($dll)) {
                Copy-Item -LiteralPath $file -Destination $OverlayDirectory -Force
                $pending.Enqueue($file)
            }
        }
    }
}
