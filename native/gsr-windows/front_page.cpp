// SPDX-License-Identifier: GPL-3.0-only
#include "front_page.hpp"
#include "Theme.hpp"
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
