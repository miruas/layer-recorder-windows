// SPDX-License-Identifier: GPL-3.0-only
#include <windows.h>
#include "GsrInfo.hpp"
#include "Utils.hpp"
#include <filesystem>
#include <cstdlib>
#include <sstream>
#include <algorithm>

namespace gsr {
std::string GsrVersion::to_string() const {
    return std::to_string(major) + "." + std::to_string(minor) + "." + std::to_string(patch);
}

static constexpr auto run_key = L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";

bool is_windows_autostart_enabled() {
    return RegGetValueW(HKEY_CURRENT_USER, run_key, L"LayerRecorder", RRF_RT_REG_SZ,
                        nullptr, nullptr, nullptr) == ERROR_SUCCESS;
}

int set_windows_autostart(bool enabled) {
    wchar_t module[32768];
    const auto length = GetModuleFileNameW(nullptr, module, std::size(module));
    if (!length || length == std::size(module)) return ERROR_BAD_PATHNAME;
    const auto launcher = std::filesystem::path(module).parent_path().parent_path() / L"Layer.exe";
    if (enabled && !std::filesystem::is_regular_file(launcher)) return ERROR_FILE_NOT_FOUND;
    HKEY key;
    const auto result = RegCreateKeyExW(HKEY_CURRENT_USER, run_key, 0, nullptr, 0,
                                      KEY_SET_VALUE, nullptr, &key, nullptr);
    if (result != ERROR_SUCCESS) return result;
    LSTATUS status;
    if (enabled) {
        const auto command = L"\"" + launcher.wstring() + L"\" --background";
        status = RegSetValueExW(key, L"LayerRecorder", 0, REG_SZ,
                                reinterpret_cast<const BYTE *>(command.c_str()),
                                static_cast<DWORD>((command.size() + 1) * sizeof(wchar_t)));
    } else {
        status = RegDeleteValueW(key, L"LayerRecorder");
        if (status == ERROR_FILE_NOT_FOUND) status = ERROR_SUCCESS;
    }
    RegCloseKey(key);
    return status;
}
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

std::string windows_monitor_target(const std::string &name) {
    if (!starts_with(name, R"(\\.\)")) return name;
    const auto device_name = std::filesystem::u8path(name).wstring();
    DISPLAY_DEVICEW device{};
    device.cb = sizeof(device);
    if (!EnumDisplayDevicesW(device_name.c_str(), 0, &device, EDD_GET_DEVICE_INTERFACE_NAME) ||
        !device.DeviceID[0])
        return name;
    return std::filesystem::path(device.DeviceID).u8string();
}

std::string get_parent_directory(std::string_view directory) {
    const auto path = std::filesystem::u8path(directory);
    const auto parent = path.parent_path();
    return parent.empty() ? path.u8string() : parent.u8string();
}
}
