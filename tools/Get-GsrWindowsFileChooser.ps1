param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$OutputFile)
$ErrorActionPreference = 'Stop'
$code = Get-Content -LiteralPath (Join-Path $Source 'src/gui/FileChooser.cpp') -Raw
$code = $code.Replace('../../include/', '').Replace('#include <dirent.h>', '#include <filesystem>')
$start = $code.IndexOf('        char path[4096];')
$end = $code.IndexOf('        std::sort(folders.begin()', $start)
if ($start -lt 0 -or $end -le $start) { throw 'Could not find the GSR directory enumeration.' }
$enumeration = @'
        namespace fs = std::filesystem;
        std::error_code error;
        for (fs::directory_iterator entry(fs::u8path(directory), error), end;
             !error && entry != end; entry.increment(error)) {
            std::error_code entry_error;
            const auto name = entry->path().filename().u8string();
            if (name.empty() || name.front() == '.' || !entry->is_directory(entry_error))
                continue;
            const auto modified = entry->last_write_time(entry_error);
            const auto seconds = entry_error ? 0 :
                std::chrono::duration_cast<std::chrono::seconds>(modified.time_since_epoch()).count();
            folders.push_back({mgl::Text(name, get_theme().body_font_desc.c_str()), seconds});
        }

'@
$code = $code.Substring(0, $start) + $enumeration + $code.Substring($end)
$start = $code.IndexOf('        char filepath[PATH_MAX];')
$end = $code.IndexOf('    void FileChooser::open_parent_directory()', $start)
if ($start -lt 0 -or $end -le $start) { throw 'Could not find GSR directory navigation.' }
$navigation = @'
        const auto path = std::filesystem::u8path(current_directory_text.get_string()) / std::filesystem::u8path(name);
        set_current_directory(path.u8string());
    }

'@
$code = $code.Substring(0, $start) + $navigation + "`n" + $code.Substring($end)
Set-Content -LiteralPath $OutputFile -Value $code -Encoding utf8
