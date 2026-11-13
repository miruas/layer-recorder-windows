// SPDX-License-Identifier: GPL-3.0-only
#include <windows.h>
#include <stdint.h>
#include <stdlib.h>
#include <mgl/system/fileutils.h>

static HANDLE open_utf8(const char *path, DWORD access) {
    int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, NULL, 0);
    if (!count) return INVALID_HANDLE_VALUE;
    WCHAR *name = malloc((size_t)count * sizeof(*name));
    if (!name) return INVALID_HANDLE_VALUE;
    HANDLE file = INVALID_HANDLE_VALUE;
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, name, count))
        file = CreateFileW(name, access, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    free(name);
    return file;
}

int mgl_load_file(const char *path, mgl_filedata *out, const mgl_file_load_options *options) {
    *out = (mgl_filedata){0};
    HANDLE file = open_utf8(path, GENERIC_READ);
    if (file == INVALID_HANDLE_VALUE) return -1;
    LARGE_INTEGER length;
    if (GetFileType(file) != FILE_TYPE_DISK || !GetFileSizeEx(file, &length) ||
        length.QuadPart < 0 || (uint64_t)length.QuadPart >= SIZE_MAX) {
        CloseHandle(file);
        return -1;
    }
    size_t size = (size_t)length.QuadPart;
    unsigned char *bytes = malloc(size + 1);
    if (!bytes) { CloseHandle(file); return -1; }
    for (size_t offset = 0; offset < size;) {
        DWORD block = size - offset > 1024 * 1024 ? 1024 * 1024 : (DWORD)(size - offset);
        DWORD read;
        if (!ReadFile(file, bytes + offset, block, &read, NULL) || !read) {
            free(bytes);
            CloseHandle(file);
            return -1;
        }
        offset += read;
    }
    CloseHandle(file);
    if (options && options->null_terminated) bytes[size] = 0;
    *out = (mgl_filedata){bytes, size};
    return 0;
}

void mgl_filedata_free(mgl_filedata *data) {
    free(data->data);
    *data = (mgl_filedata){0};
}

int mgl_mapped_file_load(const char *path, mgl_memory_mapped_file *out, const mgl_memory_mapped_file_load_options *options) {
    *out = (mgl_memory_mapped_file){.fd=-1};
    bool write = !options || options->writable;
    HANDLE file = open_utf8(path, GENERIC_READ | (write ? GENERIC_WRITE : 0));
    if (file == INVALID_HANDLE_VALUE) return -1;
    LARGE_INTEGER length;
    HANDLE mapping = NULL;
    if (GetFileSizeEx(file, &length) && length.QuadPart > 0 && (uint64_t)length.QuadPart <= SIZE_MAX)
        mapping = CreateFileMappingW(file, NULL, write ? PAGE_READWRITE : PAGE_READONLY, 0, 0, NULL);
    CloseHandle(file);
    if (!mapping) return -1;
    void *data = MapViewOfFile(mapping, write ? FILE_MAP_WRITE : FILE_MAP_READ, 0, 0, 0);
    CloseHandle(mapping);
    if (!data) return -1;
    out->data = data;
    out->size = (size_t)length.QuadPart;
    return 0;
}

void mgl_mapped_file_unload(mgl_memory_mapped_file *file) {
    if (file->data) UnmapViewOfFile(file->data);
    *file = (mgl_memory_mapped_file){.fd=-1};
}
