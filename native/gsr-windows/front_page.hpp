// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include "Config.hpp"
#include "GsrInfo.hpp"
#include "gui/CustomRendererWidget.hpp"
#include "gui/PageStack.hpp"
#include "gui/SettingsPage.hpp"
#include <mglpp/graphics/Rectangle.hpp>
#include <mglpp/graphics/Sprite.hpp>
#include <mglpp/graphics/Text.hpp>

namespace gsr {
class DropdownButton;

enum class FrontPageAction {
    replay_toggle, replay_save, replay_save_short, replay_save_long, replay_settings,
    record_toggle, record_pause, record_settings,
    stream_toggle, stream_settings, recordings, settings
};

class WindowsFrontPage {
public:
    WindowsFrontPage(Config &config, GsrInfo &info, std::function<void(FrontPageAction)> action);
    void resize(mgl::vec2i size);
    void set_capture_state(bool recording, bool replay);
    bool on_event(mgl::Event &event, mgl::Window &window);
    bool back();
    void draw(mgl::Window &window);

private:
    void recreate_frontpage_ui_components();
    void open_replay_settings_page() { open_capture_settings(SettingsPage::Type::REPLAY); }
    void on_press_save_replay() { action(FrontPageAction::replay_save); }
    void on_press_save_replay_1_min_replay() { action(FrontPageAction::replay_save_short); }
    void on_press_save_replay_10_min_replay() { action(FrontPageAction::replay_save_long); }
    void on_press_start_replay() { action(FrontPageAction::replay_toggle); }
    void open_record_settings_page() { open_capture_settings(SettingsPage::Type::RECORD); }
    void toggle_pause() { action(FrontPageAction::record_pause); }
    void on_press_start_record() { action(FrontPageAction::record_toggle); }
    void open_stream_settings_page() { open_capture_settings(SettingsPage::Type::STREAM); }
    void open_capture_settings(SettingsPage::Type type);
    void on_press_start_stream() { action(FrontPageAction::stream_toggle); }
    void open_settings_page() { action(FrontPageAction::settings); }
    void open_screenshot_settings_page();

    Config &config;
    GsrInfo &gsr_info;
    std::function<void(FrontPageAction)> action;
    mgl::vec2i window_size;
    mgl::Rectangle bg_screenshot_overlay{{0, 0}};
    mgl::Rectangle top_bar_background{{0, 0}};
    mgl::Text top_bar_text;
    mgl::Sprite logo_sprite;
    CustomRendererWidget close_button_widget{{0, 0}};
    PageStack page_stack;
    DropdownButton *replay_dropdown_button_ptr = nullptr;
    DropdownButton *record_dropdown_button_ptr = nullptr;
    DropdownButton *stream_dropdown_button_ptr = nullptr;
    bool close_button_pressed_inside = false;
};
}
