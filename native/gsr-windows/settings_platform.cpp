// SPDX-License-Identifier: GPL-3.0-only
#include <windows.h>
#include "GsrInfo.hpp"
#include "Utils.hpp"
#include <filesystem>

namespace gsr {
SupportedCaptureOptions get_supported_capture_options(const GsrInfo &) {
    SupportedCaptureOptions options;
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
