// SPDX-License-Identifier: GPL-3.0-only
#include <windows.h>
#include "front_page.hpp"
#include "Theme.hpp"
#include "Translation.hpp"
#include <mglpp/graphics/Texture.hpp>
#include <mglpp/mglpp.hpp>
#include <mglpp/window/Event.hpp>
#include <mglpp/window/Window.hpp>
#include <filesystem>
#include <cstdio>
#include <cstdlib>
#include <utility>
#include <string_view>

namespace {
bool capture_backdrop(const RECT &bounds, mgl::Texture &texture) {
    const int width = bounds.right - bounds.left;
    const int height = bounds.bottom - bounds.top;
    BITMAPINFO info{};
    info.bmiHeader.biSize = sizeof(info.bmiHeader);
    info.bmiHeader.biWidth = width;
    info.bmiHeader.biHeight = -height;
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    info.bmiHeader.biCompression = BI_RGB;
    HDC screen = GetDC(nullptr);
    if (!screen) return false;
    HDC memory = CreateCompatibleDC(screen);
    void *pixels = nullptr;
    HBITMAP bitmap = memory ? CreateDIBSection(screen, &info, DIB_RGB_COLORS, &pixels, nullptr, 0) : nullptr;
    HGDIOBJ previous = bitmap ? SelectObject(memory, bitmap) : nullptr;
    bool loaded = false;
    if (previous && previous != HGDI_ERROR &&
        BitBlt(memory, 0, 0, width, height, screen, bounds.left, bounds.top, SRCCOPY | CAPTUREBLT)) {
        GdiFlush();
        auto *pixel = static_cast<unsigned char *>(pixels);
        const auto count = static_cast<size_t>(width) * height;
        for (size_t i = 0; i < count; ++i, pixel += 4) {
            std::swap(pixel[0], pixel[2]);
            pixel[3] = 255;
        }
        loaded = texture.load_from_memory(static_cast<unsigned char *>(pixels), width, height, MGL_IMAGE_FORMAT_RGBA);
    }
    if (previous && previous != HGDI_ERROR) SelectObject(memory, previous);
    if (bitmap) DeleteObject(bitmap);
    if (memory) DeleteDC(memory);
    ReleaseDC(nullptr, screen);
    return loaded;
}

int show_overlay(const std::string &resources, std::wstring_view mode) {
    mgl::Window window;
    mgl::Window::CreateParams params;
    params.hidden = true;
    params.size = {0, 0};
    params.window_type = MGL_WINDOW_TYPE_OVERLAY;
    if (!window.create("GPU Screen Recorder", params)) return 2;
    const auto hwnd = reinterpret_cast<HWND>(window.get_system_handle());
    RECT bounds{};
    if (!GetWindowRect(hwnd, &bounds)) return 3;
    mgl::Texture backdrop;
    if (!capture_backdrop(bounds, backdrop)) return 4;
    if (!gsr::init_theme(resources)) return 5;

    gsr::GsrInfo info{};
    const gsr::SupportedCaptureOptions options{};
    auto config = gsr::read_config(options).value_or(gsr::Config(options));
    gsr::Translation::instance().init((resources + "translations/").c_str(), config.main_config.language.c_str());
    gsr::init_color_theme(config, info);
    {
        bool open = true;
        gsr::WindowsFrontPage page(config, info, [&](gsr::FrontPageAction action) {
            using Action = gsr::FrontPageAction;
            const char *name = nullptr;
            switch (action) {
            case Action::replay_toggle: name = "toggle_replay"; break;
            case Action::replay_save: name = "save_replay"; break;
            case Action::replay_save_short: name = "save_short"; break;
            case Action::replay_save_long: name = "save_long"; break;
            case Action::record_toggle: name = "toggle_record"; break;
            case Action::recordings: name = "recordings"; break;
            case Action::settings: name = "settings"; break;
            default: break;
            }
            if (name) {
                std::puts(name);
                std::fflush(stdout);
                open = false;
            }
        });
        page.resize(window.get_size());
        page.set_capture_state(mode == L"record", mode == L"record" || mode == L"replay");
        mgl::Sprite desktop(&backdrop);
        auto draw = [&] {
            window.clear();
            window.draw(desktop);
            page.draw(window);
            window.display();
        };
        draw();
        window.set_visible(true);
        while (open && window.is_open()) {
            mgl::Event event;
            while (open && window.poll_event(event)) {
                if (event.type == mgl::Event::Closed ||
                    (event.type == mgl::Event::KeyPressed && event.key.code == mgl::Keyboard::Escape)) {
                    open = false;
                } else {
                    const bool page_open = page.on_event(event, window);
                    open = open && page_open;
                }
                if (open) draw();
            }
            if (open && window.is_open()) MsgWaitForMultipleObjectsEx(0, nullptr, INFINITE, QS_ALLINPUT, MWMO_INPUTAVAILABLE);
        }
        window.set_visible(false);
    }
    gsr::deinit_color_theme();
    gsr::deinit_theme();
    return 0;
}
}

int wmain(int argc, wchar_t **argv) {
    if (argc != 2 && argc != 3) return 1;
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    const std::filesystem::path resources(argv[1]);
    const auto fonts = resources / L"fonts";
    if (GetFileAttributesW((fonts / L"fonts.conf").c_str()) != INVALID_FILE_ATTRIBUTES) {
        _putenv_s("FONTCONFIG_PATH", fonts.u8string().c_str());
        _putenv_s("FONTCONFIG_FILE", "fonts.conf");
    }
    mgl::Init platform(mgl::WindowSystem::NATIVE);
    return show_overlay(resources.u8string() + "/", argc == 3 ? argv[2] : L"idle");
}
