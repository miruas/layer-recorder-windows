// SPDX-License-Identifier: GPL-3.0-only
#include <windows.h>
#include <mglpp/mglpp.hpp>
#include <mglpp/window/Window.hpp>
#include <mglpp/window/Event.hpp>
#include "Config.hpp"
#include "GsrInfo.hpp"
#include "Theme.hpp"
#include "gui/DropdownButton.hpp"
#include "front_page.hpp"
#include <cstdio>
#include <string>
#include <vector>

static void drain(mgl::Window &window, gsr::DropdownButton &button) {
    mgl::Event event;
    while (window.poll_event(event)) button.on_event(event, window, {0, 0});
}

static bool test_front_page(mgl::Window &window, gsr::Config &config, gsr::GsrInfo &info) {
    using Action = gsr::FrontPageAction;
    std::vector<Action> actions;
    gsr::WindowsFrontPage page(config, info, [&](Action action) { actions.push_back(action); });
    page.resize({480, 360});
    const auto hwnd = reinterpret_cast<HWND>(window.get_system_handle());
    const auto send = [&](UINT message, int x, int y) {
        PostMessageW(hwnd, message, message == WM_LBUTTONDOWN ? MK_LBUTTON : 0, MAKELPARAM(x, y));
        bool open = true;
        mgl::Event event;
        while (window.poll_event(event)) open = page.on_event(event, window) && open;
        page.draw(window);
        return open;
    };
    page.draw(window);
    // Original four-column layout at 480x360: 72px cards centered at y=90.
    for (const int x : {132, 204, 276, 348}) {
        send(WM_MOUSEMOVE, x, 90);
        send(WM_LBUTTONDOWN, x, 90);
        send(WM_LBUTTONUP, x, 90);
        send(WM_MOUSEMOVE, x, 130);
        send(WM_LBUTTONDOWN, x, 130);
        send(WM_LBUTTONUP, x, 130);
    }
    send(WM_MOUSEMOVE, 470, 10);
    send(WM_LBUTTONDOWN, 470, 10);
    const bool closed = !send(WM_LBUTTONUP, 470, 10);
    if (!closed || actions != std::vector<Action>{
        Action::replay_toggle, Action::record_toggle, Action::stream_toggle, Action::recordings})
        return false;

    page.resize({480, 360});
    page.draw(window);
    // The screenshot gear is above the main settings gear, right of Recordings.
    send(WM_MOUSEMOVE, 403, 90);
    send(WM_LBUTTONDOWN, 403, 90);
    send(WM_LBUTTONUP, 403, 90);
    // Going back must return to the home page, then close the home page.
    if (!page.back() || page.back()) return false;

    // Exercise the original settings constructors and save hooks with the
    // same platform adapters used by the running overlay.
    gsr::PageStack stack;
    using Type = gsr::SettingsPage::Type;
    for (auto type : {Type::RECORD, Type::REPLAY, Type::STREAM}) {
        stack.push(std::make_unique<gsr::SettingsPage>(type, &info, config, &stack, false));
        stack.draw(window, {0, 0});
        stack.pop();
    }
    return stack.empty();
}

static int test(const std::string &resources) {
    mgl::Window window;
    mgl::Window::CreateParams params;
    params.size = {480, 360};
    params.hidden = true;
    if (!window.create("Original GSR dropdown on Win32", params)) return 2;
    if (!gsr::init_theme(resources + "/")) return 3;
    gsr::Config config(gsr::SupportedCaptureOptions{});
    gsr::GsrInfo info{};
    info.gpu_info.vendor = gsr::GpuVendor::NVIDIA;
    gsr::init_color_theme(config, info);
    gsr::get_theme().set_window_size({480, 360});
    int result = 0;
    {
        const auto &theme = gsr::get_theme();
        gsr::DropdownButton button(theme.title_font_desc.c_str(), theme.body_font_desc.c_str(),
                                   "Instant Replay", "Hazır", &gsr::get_theme().replay_button_texture, {180, 140});
        button.set_position({32, 32});
        button.add_item("Start", "start");
        std::string clicked;
        button.on_click = [&](const std::string &id) { clicked = id; };
        auto hwnd = reinterpret_cast<HWND>(window.get_system_handle());
        PostMessageW(hwnd, WM_MOUSEMOVE, 0, MAKELPARAM(60, 60));
        PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, MAKELPARAM(60, 60));
        drain(window, button);
        window.clear(mgl::Color(0, 0, 0));
        button.draw(window, {0, 0});

        // Sample the upstream widget's NVIDIA accent before swapping buffers.
        using ReadPixels = void (WINAPI *)(int, int, int, int, unsigned, unsigned, void *);
        auto read = reinterpret_cast<ReadPixels>(GetProcAddress(GetModuleHandleW(L"opengl32.dll"), "glReadPixels"));
        unsigned char pixel[4]{};
        if (read) read(60, 360 - 33, 1, 1, 0x1908, 0x1401, pixel);
        if (pixel[0] != 118 || pixel[1] != 185 || pixel[2]) result = 4;
        PostMessageW(hwnd, WM_MOUSEMOVE, 0, MAKELPARAM(60, 175));
        drain(window, button);
        button.draw(window, {0, 0});
        PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, MAKELPARAM(60, 175));
        drain(window, button);
        if (clicked != "start") result = 5;
    }
    if (!result && !test_front_page(window, config, info)) result = 6;
    gsr::deinit_color_theme();
    gsr::deinit_theme();
    return result;
}

int main(int argc, char **argv) {
    if (argc != 2) return 1;
    mgl::Init platform(mgl::WindowSystem::NATIVE);
    const int result = test(argv[1]);
    if (!result) std::puts("Original GSR front page renders and all four Layer cards dispatch on Windows.");
    return result;
}
