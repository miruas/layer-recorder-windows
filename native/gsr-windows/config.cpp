// SPDX-License-Identifier: GPL-3.0-only
// Win32 filesystem adapter for the patched upstream Config.cpp.
#include "Config.hpp"
#include "GsrInfo.hpp"
#include "Utils.hpp"

#include <windows.h>
#include <shlobj.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <cstdlib>
#include <system_error>

namespace fs = std::filesystem;

namespace {
std::string known_folder(const KNOWNFOLDERID &id) {
    PWSTR value = nullptr;
    if (FAILED(SHGetKnownFolderPath(id, 0, nullptr, &value))) {
        CoTaskMemFree(value);
        throw std::runtime_error("Windows known folder is unavailable");
    }
    const fs::path path(value);
    CoTaskMemFree(value);
    return path.u8string();
}
}

namespace gsr {
std::string get_config_dir() {
    return known_folder(FOLDERID_RoamingAppData) + "/Layer/GSR";
}
std::string get_videos_dir() { return known_folder(FOLDERID_Videos); }
std::string get_pictures_dir() { return known_folder(FOLDERID_Pictures); }

int create_directory_recursive(char *name) {
    std::error_code error;
    fs::create_directories(fs::u8path(name), error);
    return error ? -1 : 0;
}
bool file_get_content(const char *name, std::string &content) {
    std::ifstream input(fs::u8path(name), std::ios::binary);
    if (!input) return false;
    content.assign(std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>());
    return !input.bad();
}
bool file_overwrite(const char *name, const std::string &content) {
    const auto path = fs::u8path(name);
    auto temporary = path;
    temporary += L"." + std::to_wstring(GetCurrentProcessId()) + L".tmp";
    std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
    output.write(content.data(), static_cast<std::streamsize>(content.size()));
    output.close();
    if (output && MoveFileExW(temporary.c_str(), path.c_str(),
                            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) return true;
    std::error_code error;
    fs::remove(temporary, error);
    return false;
}
void string_split_char(std::string_view text, char separator, StringSplitCallback callback) {
    while (true) {
        const auto next = text.find(separator);
        if (!callback(text.substr(0, next)) || next == text.npos) return;
        text.remove_prefix(next + 1);
    }
}
bool starts_with(std::string_view text, std::string_view prefix) {
    return text.substr(0, prefix.size()) == prefix;
}
bool starts_with(std::string_view text, const char *prefix) {
    return starts_with(text, std::string_view(prefix));
}
}

#ifndef LAYER_GSR_CONFIG_LIBRARY
int wmain(int argc, wchar_t **argv) {
    if ((argc != 2 && argc != 3) || std::wstring_view(argv[1]) != L"validate") {
        std::cerr << "Usage: layer-gsr-config validate [config_ui]\n";
        return 2;
    }
    try {
        if (argc == 3) {
            const auto path = fs::path(argv[2]).u8string();
            if (_putenv_s("LAYER_GSR_CONFIG", path.c_str()) != 0)
                throw std::runtime_error("Could not set the GSR config path");
        } else if (_wgetenv(L"LAYER_GSR_CONFIG")) {
            const auto path = fs::path(_wgetenv(L"LAYER_GSR_CONFIG")).u8string();
            if (_putenv_s("LAYER_GSR_CONFIG", path.c_str()) != 0)
                throw std::runtime_error("Could not set the GSR config path");
        }
        auto config = gsr::read_config(gsr::SupportedCaptureOptions{});
        if (!config) return 1;
        const auto &replay = config->replay_config;
        std::cout << replay.replay_time << ' ' << replay.short_save_duration << ' '
                  << replay.long_save_duration << '\n';
        return 0;
    } catch (const std::exception &error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
#endif
