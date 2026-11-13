// SPDX-License-Identifier: GPL-3.0-only
#include <windows.h>
#include <mglpp/mglpp.hpp>
#include <mglpp/window/Window.hpp>
#include <mglpp/window/Event.hpp>
#include "Config.hpp"
#include "GsrInfo.hpp"
#include "Theme.hpp"
#include "gui/DropdownButton.hpp"
#include <cstdio>
#include <string>

static void drain(mgl::Window &window, gsr::DropdownButton &button) {
    mgl::Event event;
    while (window.poll_event(event)) button.on_event(event, window, {0, 0});
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
    gsr::deinit_color_theme();
    gsr::deinit_theme();
    return result;
}

int main(int argc, char **argv) {
    if (argc != 2) return 1;
    mgl::Init platform(mgl::WindowSystem::NATIVE);
    const int result = test(argv[1]);
    if (!result) std::puts("Original GSR theme, replay icon and dropdown draw and dispatch on Windows.");
    return result;
}
