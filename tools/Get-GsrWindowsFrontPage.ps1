param(
    [Parameter(Mandatory)][string]$Source,
    [Parameter(Mandatory)][string]$OutputFile
)

$ErrorActionPreference = 'Stop'
$overlay = (Get-Content -LiteralPath (Join-Path $Source 'src/Overlay.cpp') -Raw).Replace("`r`n", "`n")
$start = $overlay.IndexOf('    static const char* get_first_usable_hardware_video_codec_name(')
$end = $overlay.IndexOf('        if(gpu_screen_recorder_process > 0) {', $overlay.IndexOf('    void Overlay::recreate_frontpage_ui_components()'))
if ($start -lt 0 -or $end -le $start) { throw 'Could not find the pinned GSR front page.' }
$frontPage = $overlay.Substring($start, $end - $start)
$recordings = @'
            button->on_click = [this](const std::string &id) {
                const char *katman_args[] = {"layer", id.c_str(), nullptr};
                hide();
                exec_program_daemonized(katman_args);
            };
'@
$replacements = [ordered]@{
    'Overlay::recreate_frontpage_ui_components' = 'WindowsFrontPage::recreate_frontpage_ui_components'
    'on_press_start_replay(false, false, true)' = 'on_press_start_replay()'
    'on_press_start_record(false, RecordForceType::NONE)' = 'on_press_start_record()'
    'on_press_start_stream(false)' = 'on_press_start_stream()'
    $recordings = '            button->on_click = [this](const std::string &) { action(FrontPageAction::recordings); };'
}
foreach ($replacement in $replacements.GetEnumerator()) {
    if (!$frontPage.Contains($replacement.Key)) { throw "Pinned GSR front page changed at $($replacement.Key)." }
    $frontPage = $frontPage.Replace($replacement.Key, $replacement.Value)
}

$prefix = @'
// Generated from the pinned Layer-patched GSR Overlay.cpp.
#include "front_page.hpp"
#include "Theme.hpp"
#include "Translation.hpp"
#include "gui/Button.hpp"
#include "gui/DropdownButton.hpp"
#include "gui/Image.hpp"
#include "gui/Label.hpp"
#include "gui/List.hpp"
#include "gui/StaticPage.hpp"
#include "gui/Utils.hpp"
#include <mglpp/window/Event.hpp>
#include <mglpp/window/Window.hpp>

namespace gsr {
static const mgl::Color bg_color(0, 0, 0, 100);
'@
Set-Content -LiteralPath $OutputFile -Value ($prefix + "`n" + $frontPage + "    }`n}") -Encoding utf8
