// SPDX-License-Identifier: GPL-3.0-only
#include <windows.h>
#include "GsrInfo.hpp"
#include "Utils.hpp"
#include <filesystem>
#include <cstdlib>
#include <sstream>
#include <algorithm>

namespace gsr {
static std::string audio_inventory() {
    const auto *inventory = _wgetenv(L"LAYER_GSR_AUDIO_INVENTORY");
    if (!inventory) return {};
    const int length = WideCharToMultiByte(CP_UTF8, 0, inventory, -1, nullptr, 0, nullptr, nullptr);
    if (length <= 1) return {};
    std::string text(length, '\0');
    WideCharToMultiByte(CP_UTF8, 0, inventory, -1, text.data(), length, nullptr, nullptr);
    text.pop_back();
    return text;
}

std::vector<AudioDevice> get_audio_devices() {
    std::vector<AudioDevice> devices;
    std::istringstream rows(audio_inventory());
    for (std::string row; std::getline(rows, row);) {
        const auto split = row.find('\t');
        if (split != std::string::npos && starts_with(row, "device:"))
            devices.push_back({row.substr(7, split - 7), row.substr(split + 1)});
    }
    return devices;
}

std::vector<std::string> get_application_audio() {
    std::vector<std::string> applications;
    std::istringstream rows(audio_inventory());
    for (std::string row; std::getline(rows, row);) {
        const auto split = row.find('\t');
        if (split == std::string::npos || !starts_with(row, "app:")) continue;
        auto name = row.substr(4, split - 4);
        if (std::find(applications.begin(), applications.end(), name) == applications.end())
            applications.push_back(std::move(name));
    }
    return applications;
}

SupportedCaptureOptions get_supported_capture_options(const GsrInfo &) {
    SupportedCaptureOptions options;
    options.window = true;
    options.region = true;
    EnumDisplayMonitors(nullptr, nullptr, [](HMONITOR monitor, HDC, LPRECT, LPARAM data) -> BOOL {
        MONITORINFOEXW info{};
        info.cbSize = sizeof(info);
        if (GetMonitorInfoW(monitor, reinterpret_cast<MONITORINFO *>(&info))) {
            auto &monitors = *reinterpret_cast<std::vector<GsrMonitor> *>(data);
            const auto &bounds = info.rcMonitor;
            monitors.push_back({std::filesystem::path(info.szDevice).u8string(),
                                {static_cast<int>(bounds.right - bounds.left),
                                 static_cast<int>(bounds.bottom - bounds.top)}});
        }
        return TRUE;
    }, reinterpret_cast<LPARAM>(&options.monitors));
    return options;
}

std::string get_parent_directory(std::string_view directory) {
    const auto path = std::filesystem::u8path(directory);
    const auto parent = path.parent_path();
    return parent.empty() ? path.u8string() : parent.u8string();
}
}
