param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$header = Get-Content -LiteralPath (Join-Path $Source 'include/gui/GlobalSettingsPage.hpp') -Raw
$code = Get-Content -LiteralPath (Join-Path $Source 'src/gui/GlobalSettingsPage.cpp') -Raw
$header = $header.Replace('../GsrInfo.hpp', 'GsrInfo.hpp').Replace('../Config.hpp', 'Config.hpp')
$header = $header.Replace('"StaticPage.hpp"', '"gui/StaticPage.hpp"').Replace('class Overlay;', 'class WindowsFrontPage;').Replace('Overlay *', 'WindowsFrontPage *')
$header = $header.Replace('        void load();', '        bool editing_hotkey() const { return configure_hotkey_type != ConfigureHotkeyType::NONE; }' + "`n        void load();")
$code = $code.Replace('../../include/', '').Replace('"gui/GlobalSettingsPage.hpp"', '"global_settings_generated.hpp"')
$code = $code.Replace('"Overlay.hpp"', '"front_page.hpp"').Replace('#include <X11/Xlib.h>', '#include "GlobalHotkeys/GlobalHotkeys.hpp"')
$code = $code.Replace('Overlay *', 'WindowsFrontPage *').Replace('DisplayServer::X11', 'DisplayServer::UNKNOWN')
$code = $code.Replace('set_xdg_autostart', 'set_windows_autostart').Replace('is_xdg_autostart_enabled', 'is_windows_autostart_enabled')
$code = $code.Replace('overlay->set_notification_speed(NotificationSpeed::NORMAL);', '(void)0;').Replace('overlay->set_notification_speed(NotificationSpeed::FAST);', '(void)0;')
$code = $code.Replace('overlay->show_notification(error_msg.c_str(), 3.0, mgl::Color(255, 0, 0, 255), mgl::Color(255, 0, 0, 255), NotificationType::NONE);', 'overlay->show_hotkey_error(error_msg);')
Set-Content -LiteralPath (Join-Path $OutputDirectory 'global_settings_generated.hpp') -Value $header -Encoding utf8
Set-Content -LiteralPath (Join-Path $OutputDirectory 'global_settings_generated.cpp') -Value $code -Encoding utf8
