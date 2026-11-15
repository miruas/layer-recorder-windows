// SPDX-License-Identifier: GPL-3.0-only
#include <windows.h>
#include <GL/gl.h>
#include <algorithm>
#include <chrono>
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
class DesktopComposition {
public:
    ~DesktopComposition() {
        if (original) SelectObject(dc, original);
        if (bitmap) DeleteObject(bitmap);
        if (dc) DeleteDC(dc);
    }

    bool initialize(mgl::vec2i size) {
        GLint alpha = 0;
        glGetIntegerv(GL_ALPHA_BITS, &alpha);
        if (alpha < 8) return false;
        this->size = {size.x, size.y};
        BITMAPINFO info{};
        info.bmiHeader.biSize = sizeof(info.bmiHeader);
        info.bmiHeader.biWidth = size.x;
        info.bmiHeader.biHeight = size.y;
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        dc = CreateCompatibleDC(nullptr);
        if (!dc) return false;
        bitmap = CreateDIBSection(dc, &info, DIB_RGB_COLORS, &pixels, nullptr, 0);
        if (!bitmap) return false;
        original = SelectObject(dc, bitmap);
        return original != nullptr;
    }

    bool present(HWND window) {
        glReadPixels(0, 0, size.cx, size.cy, 0x80E1 /* GL_BGRA */, GL_UNSIGNED_BYTE, pixels);
        POINT origin{};
        BLENDFUNCTION blend{AC_SRC_OVER, 0, 255, AC_SRC_ALPHA};
        return UpdateLayeredWindow(window, nullptr, nullptr, &size, dc, &origin, 0, &blend, ULW_ALPHA);
    }

private:
    HDC dc = nullptr;
    HBITMAP bitmap = nullptr;
    HGDIOBJ original = nullptr;
    void *pixels = nullptr;
    SIZE size{};
};

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

void select_region(mgl::Window &window, const RECT &bounds, mgl::Texture &backdrop) {
    const auto hwnd = reinterpret_cast<HWND>(window.get_system_handle());
    const auto size = window.get_size();
    mgl::Sprite desktop(&backdrop);
    mgl::vec2i anchor{}, point{};
    bool dragging = false;
    const auto draw = [&] {
        window.clear();
        window.draw(desktop);
        mgl::Rectangle shade(size.to_vec2f());
        shade.set_color({0, 0, 0, 100});
        window.draw(shade);
        if (dragging) {
            const int x = std::min(anchor.x, point.x);
            const int y = std::min(anchor.y, point.y);
            mgl::Rectangle selection({float(x), float(y)},
                {float(std::abs(point.x - anchor.x)), float(std::abs(point.y - anchor.y))});
            selection.set_color({255, 255, 255, 80});
            window.draw(selection);
        }
        window.display();
    };
    const auto clamp = [&](int x, int y) {
        return mgl::vec2i{std::clamp(x, 0, size.x), std::clamp(y, 0, size.y)};
    };
    draw();
    window.set_visible(true);
    SetClassLongPtrW(hwnd, GCLP_HCURSOR, reinterpret_cast<LONG_PTR>(LoadCursorW(nullptr, MAKEINTRESOURCEW(32515))));
    SetCursor(LoadCursorW(nullptr, MAKEINTRESOURCEW(32515)));
    while (window.is_open()) {
        mgl::Event event;
        while (window.poll_event(event)) {
            if (event.type == mgl::Event::Closed || event.type == mgl::Event::LostFocus ||
                (event.type == mgl::Event::KeyPressed && event.key.code == mgl::Keyboard::Escape))
                return;
            if (event.type == mgl::Event::MouseButtonPressed && event.mouse_button.button == mgl::Mouse::Left) {
                anchor = point = clamp(event.mouse_button.x, event.mouse_button.y);
                dragging = true;
                SetCapture(hwnd);
            } else if (dragging && event.type == mgl::Event::MouseMoved) {
                point = clamp(event.mouse_move.x, event.mouse_move.y);
            } else if (dragging && event.type == mgl::Event::MouseButtonReleased && event.mouse_button.button == mgl::Mouse::Left) {
                point = clamp(event.mouse_button.x, event.mouse_button.y);
                ReleaseCapture();
                dragging = false;
                const int width = std::abs(point.x - anchor.x);
                const int height = std::abs(point.y - anchor.y);
                if (width >= 2 && height >= 2) {
                    window.set_visible(false);
                    std::printf("region %d %d %d %d\n",
                        static_cast<int>(bounds.left) + std::min(anchor.x, point.x),
                        static_cast<int>(bounds.top) + std::min(anchor.y, point.y), width, height);
                    return;
                }
            }
            draw();
        }
        if (window.is_open()) MsgWaitForMultipleObjectsEx(0, nullptr, INFINITE, QS_ALLINPUT, MWMO_INPUTAVAILABLE);
    }
}

int show_overlay(const std::string &resources, std::wstring_view mode) {
    mgl::Window window;
    mgl::Window::CreateParams params;
    params.hidden = true;
    params.support_alpha = mode != L"region";
    params.size = {0, 0};
    params.window_type = MGL_WINDOW_TYPE_OVERLAY;
    if (!window.create("GPU Screen Recorder", params)) return 2;
    const auto hwnd = reinterpret_cast<HWND>(window.get_system_handle());
    RECT bounds{};
    if (!GetWindowRect(hwnd, &bounds)) return 3;
    mgl::Texture backdrop;
    DesktopComposition composition;
    const auto styles = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
    bool live = params.support_alpha && composition.initialize(window.get_size());
    if (live) {
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, styles | WS_EX_LAYERED);
        window.clear({0, 0, 0, 100});
        live = composition.present(hwnd);
        if (!live) SetWindowLongPtrW(hwnd, GWL_EXSTYLE, styles);
    }
    if (!live && !capture_backdrop(bounds, backdrop)) return 4;
    if (mode == L"region") {
        select_region(window, bounds, backdrop);
        return 0;
    }
    if (!gsr::init_theme(resources)) return 5;

    gsr::GsrInfo info{};
    info.supported_video_codecs.h264 = true;
    info.supported_video_codecs.h264_software = true;
    const char *hevc = std::getenv("LAYER_GSR_HEVC_NVENC");
    info.supported_video_codecs.hevc = hevc && hevc[0] == '1';
    const char *av1 = std::getenv("LAYER_GSR_AV1_NVENC");
    info.supported_video_codecs.av1 = av1 && av1[0] == '1';
    info.supported_containers.whip = true;
    info.system_info.supports_app_audio = true;
    info.supported_image_formats.png = true;
    info.supported_image_formats.jpeg = true;
    const auto options = gsr::get_supported_capture_options(info);
    auto config = gsr::read_config(options).value_or(gsr::Config(options));
    gsr::Translation::instance().init((resources + "translations/").c_str(), config.main_config.language.c_str());
    gsr::init_color_theme(config, info);
    {
        bool open = true;
        gsr::WindowsFrontPage page(config, info, [&](gsr::FrontPageAction action) {
            using Action = gsr::FrontPageAction;
            if (action == Action::suspend_hotkeys || action == Action::resume_hotkeys) {
                std::puts(action == Action::suspend_hotkeys ? "suspend_hotkeys" : "resume_hotkeys");
                std::fflush(stdout);
                return;
            }
            const char *name = nullptr;
            switch (action) {
            case Action::replay_toggle: name = "toggle_replay"; break;
            case Action::replay_save: name = "save_replay"; break;
            case Action::replay_save_short: name = "save_short"; break;
            case Action::replay_save_long: name = "save_long"; break;
            case Action::record_toggle: name = "toggle_record"; break;
            case Action::record_pause: name = "toggle_pause"; break;
            case Action::stream_toggle: name = "toggle_stream"; break;
            case Action::recordings: name = "recordings"; break;
            case Action::settings: name = "settings"; break;
            case Action::exit_application: name = "exit"; break;
            default: break;
            }
            if (name) {
                std::puts(name);
                std::fflush(stdout);
                open = false;
            }
        });
        page.resize(window.get_size());
        const auto separator = mode.find(L'+');
        const auto capture = mode.substr(0, separator);
        const bool streaming = separator != mode.npos && mode.substr(separator + 1) == L"stream";
        const bool recording = capture == L"record" || capture == L"paused";
        page.set_capture_state(recording, recording || capture == L"replay", capture == L"paused", streaming);
        mgl::Sprite desktop(&backdrop);
        auto draw = [&] {
            window.clear(live ? mgl::Color(0, 0, 0, 0) : mgl::Color(0, 0, 0, 255));
            if (!live) window.draw(desktop);
            page.draw(window);
            if (live) composition.present(hwnd);
            else window.display();
        };
        draw();
        window.set_visible(true);
        // Upstream GSR advances scroll and hover animations while rendering.
        // Stop scheduling frames once the interactive animation has settled.
        using Clock = std::chrono::steady_clock;
        auto animate_until = Clock::now();
        while (open && window.is_open()) {
            mgl::Event event;
            while (open && window.poll_event(event)) {
                if (event.type == mgl::Event::Closed) {
                    open = false;
                } else if (event.type == mgl::Event::LostFocus &&
                           GetForegroundWindow() != hwnd && !page.editing_hotkey()) {
                    open = false;
                } else if (!page.editing_hotkey() && event.type == mgl::Event::KeyPressed && event.key.code == mgl::Keyboard::Escape) {
                    open = page.back();
                } else {
                    const bool page_open = page.on_event(event, window);
                    open = open && page_open;
                }
                if (open) {
                    draw();
                    animate_until = Clock::now() + std::chrono::milliseconds(650);
                }
            }
            if (open && window.is_open()) {
                const bool animating = Clock::now() < animate_until;
                const auto result = MsgWaitForMultipleObjectsEx(
                    0, nullptr, animating ? 16 : INFINITE, QS_ALLINPUT, MWMO_INPUTAVAILABLE);
                if (result == WAIT_TIMEOUT) draw();
            }
        }
        while (page.back()) {}
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
