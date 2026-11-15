// SPDX-License-Identifier: GPL-3.0-only
#include <windows.h>
#include <mglpp/mglpp.hpp>
#include <mglpp/window/Window.hpp>
#include <mglpp/window/Event.hpp>
#include "Config.hpp"
#include "GsrInfo.hpp"
#include "Theme.hpp"
#include "Utils.hpp"
#include "gui/DropdownButton.hpp"
#include "gui/ScreenshotSettingsPage.hpp"
#include "front_page.hpp"
#include <cstdio>
#include <filesystem>
#include <string>
#include <thread>
#include <vector>

static void drain(mgl::Window &window, gsr::DropdownButton &button) {
    mgl::Event event;
    while (window.poll_event(event)) button.on_event(event, window, {0, 0});
}

static bool test_settings_write_during_read() {
    const auto path = std::filesystem::u8path(gsr::get_config_dir()) / "config_ui";
    const auto utf8_path = path.u8string();
    std::string original;
    if (!gsr::file_get_content(utf8_path.c_str(), original)) return false;
    const auto reader = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ,
                                    nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (reader == INVALID_HANDLE_VALUE) return false;
    std::thread release_reader([reader] {
        Sleep(35);
        CloseHandle(reader);
    });
    const auto updated = original + "\n";
    const bool saved = gsr::file_overwrite(utf8_path.c_str(), updated);
    release_reader.join();
    std::string content;
    return saved && gsr::file_get_content(utf8_path.c_str(), content) && content == updated;
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
    page.resize({480, 360});
    send(WM_LBUTTONDOWN, 403, 114);
    send(WM_LBUTTONUP, 403, 114);
    if (!page.back() || page.back()) return false;

    // Exercise the original settings constructors and save hooks with the
    // same platform adapters used by the running overlay.
    _wputenv_s(L"LAYER_GSR_AUDIO_INVENTORY",
        L"device:default_output\tDefault output\ndevice:mic\tMikrofon \u00c7\napp:game.exe\tGame");
    const auto devices = gsr::get_audio_devices();
    if (devices.size() != 2 || devices[1].description != "Mikrofon \xC3\x87" ||
        gsr::get_application_audio() != std::vector<std::string>{"game.exe"})
        return false;
    info.system_info.supports_app_audio = true;
    const std::vector<std::string> saved_inputs = {"device:system:disconnected.monitor", "app:offline.exe"};
    const std::string disconnected_monitor = R"(\\.\DISPLAY99)";
    const std::string disconnected_identity = R"(\\?\DISPLAY#DISCONNECTED#instance)";
    config.record_config.record_options.audio_tracks_list = {{"", saved_inputs, false}};
    config.record_config.record_options.record_area_option = disconnected_monitor;
    config.screenshot_config.record_area_option = disconnected_identity;
    gsr::PageStack stack;
    using Type = gsr::SettingsPage::Type;
    for (auto type : {Type::RECORD, Type::REPLAY, Type::STREAM}) {
        stack.push(std::make_unique<gsr::SettingsPage>(type, &info, config, &stack, false));
        stack.draw(window, {0, 0});
        stack.pop();
    }
    stack.push(std::make_unique<gsr::ScreenshotSettingsPage>(&info, config, &stack, false, true));
    stack.draw(window, {0, 0});
    stack.pop();
    const auto &record_options = config.record_config.record_options;
    const auto &saved_tracks = record_options.audio_tracks_list;
    if (!stack.empty() || saved_tracks.size() != 1 || saved_tracks.front().audio_inputs != saved_inputs ||
        record_options.record_area_option != disconnected_monitor ||
        config.screenshot_config.record_area_option != disconnected_identity)
        return false;

    auto restored = gsr::read_config(gsr::get_supported_capture_options(info));
    return restored && restored->record_config.record_options.record_area_option == disconnected_monitor &&
        restored->screenshot_config.record_area_option == disconnected_identity &&
        restored->record_config.record_options.audio_tracks_list == saved_tracks;
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
        // The replay sprite has a transparent corner and an opaque center.
        // A flat white rectangle here means the GL texture state is missing.
        if (read) {
            unsigned char corner[4]{}, center[4]{};
            read(93, 360 - 72, 1, 1, 0x1908, 0x1401, corner);
            read(122, 360 - 102, 1, 1, 0x1908, 0x1401, center);
            if (corner[0] >= 32 || center[0] <= 224) result = 7;
        }
        PostMessageW(hwnd, WM_MOUSEMOVE, 0, MAKELPARAM(60, 175));
        drain(window, button);
        button.draw(window, {0, 0});
        PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, MAKELPARAM(60, 175));
        drain(window, button);
        if (clicked != "start") result = 5;
    }
    if (!result && !test_front_page(window, config, info)) result = 6;
    if (!result && !test_settings_write_during_read()) result = 8;
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
