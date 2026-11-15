// SPDX-License-Identifier: GPL-3.0-only
#include "front_page.hpp"
#include "global_settings_generated.hpp"
#include <windows.h>
#include <filesystem>
#include "Theme.hpp"
#include "Translation.hpp"
#include "gui/DropdownButton.hpp"
#include "gui/ScreenshotSettingsPage.hpp"
#include <mglpp/window/Window.hpp>
#include <utility>

namespace gsr {
WindowsFrontPage::WindowsFrontPage(Config &config, GsrInfo &info,
                                   std::function<void(FrontPageAction)> action)
    : config(config), gsr_info(info), action(std::move(action)) {}

void WindowsFrontPage::resize(mgl::vec2i size) {
    window_size = size;
    get_theme().set_window_size(size);
    recreate_frontpage_ui_components();
}

void WindowsFrontPage::open_screenshot_settings_page() {
    page_stack.push(std::make_unique<ScreenshotSettingsPage>(&gsr_info, config, &page_stack, false, true));
}

void WindowsFrontPage::open_capture_settings(SettingsPage::Type type) {
    page_stack.push(std::make_unique<SettingsPage>(type, &gsr_info, config, &page_stack, false));
}

void WindowsFrontPage::open_settings_page() {
    auto settings = std::make_unique<GlobalSettingsPage>(this, &gsr_info, config, &page_stack);
    settings->on_click_exit_program_button = [this](std::string_view) {
        action(FrontPageAction::exit_application);
    };
    page_stack.push(std::move(settings));
}

void WindowsFrontPage::show_hotkey_error(const std::string &message) {
    MessageBoxW(GetActiveWindow(), std::filesystem::u8path(message).c_str(),
                L"GPU Screen Recorder", MB_OK | MB_ICONWARNING);
}

bool WindowsFrontPage::back() {
    if (page_stack.empty()) return false;
    page_stack.pop();
    return !page_stack.empty();
}

bool WindowsFrontPage::editing_hotkey() {
    const auto *settings = dynamic_cast<const GlobalSettingsPage *>(page_stack.top());
    return settings && settings->editing_hotkey();
}

void WindowsFrontPage::set_capture_state(bool recording, bool replay, bool paused, bool streaming) {
    auto &theme = get_theme();
    stream_dropdown_button_ptr->set_description(streaming ? TR("Streaming") : TR("Not streaming"));
    stream_dropdown_button_ptr->set_activated(streaming);
    stream_dropdown_button_ptr->set_item_label("start", streaming ? TR("Stop") : TR("Start"));
    stream_dropdown_button_ptr->set_item_icon("start", streaming ? &theme.stop_texture : &theme.play_texture);
    record_dropdown_button_ptr->set_description(paused ? TR("Paused") : recording ? TR("Recording") : TR("Not recording"));
    record_dropdown_button_ptr->set_item_enabled("pause", recording);
    record_dropdown_button_ptr->set_item_label("pause", paused ? TR("Unpause") : TR("Pause"));
    record_dropdown_button_ptr->set_item_icon("pause", paused ? &theme.play_texture : &theme.pause_texture);
    record_dropdown_button_ptr->set_activated(recording);
    record_dropdown_button_ptr->set_item_label("start", recording ? TR("Stop and save") : TR("Start"));
    record_dropdown_button_ptr->set_item_icon("start", recording ? &theme.stop_texture : &theme.play_texture);
    replay_dropdown_button_ptr->set_description(replay ? TR("On") : TR("Off"));
    replay_dropdown_button_ptr->set_activated(replay);
    replay_dropdown_button_ptr->set_item_enabled("start", !recording);
    replay_dropdown_button_ptr->set_item_label("start", replay ? TR("Turn off") : TR("Turn on"));
    replay_dropdown_button_ptr->set_item_icon("start", replay ? &theme.stop_texture : &theme.play_texture);
    for (const auto *id : {"save", "save_1_min", "save_10_min"})
        replay_dropdown_button_ptr->set_item_enabled(id, replay);
}

bool WindowsFrontPage::on_event(mgl::Event &event, mgl::Window &window) {
    if (close_button_widget.on_event(event, window, {0, 0}))
        page_stack.on_event(event, window, {0, 0});
    return !page_stack.empty();
}

void WindowsFrontPage::draw(mgl::Window &window) {
    window.draw(bg_screenshot_overlay);
    window.draw(top_bar_background);
    window.draw(top_bar_text);
    window.draw(logo_sprite);
    page_stack.draw(window, {0, 0});
    close_button_widget.draw(window, {0, 0});
}

}
