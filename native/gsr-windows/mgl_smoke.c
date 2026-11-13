// SPDX-License-Identifier: GPL-3.0-only
#include <windows.h>
#include <stdio.h>
#include <mgl/mgl.h>
#include <mgl/window/window.h>
#include <mgl/window/event.h>
#include <mgl/graphics/rectangle.h>

int main(void) {
    if (mgl_init(MGL_WINDOW_SYSTEM_NATIVE)) return 1;
    mgl_window window;
    mgl_window_create_params params = {.size={320, 240}, .position={32, 32}, .hidden=true};
    if (mgl_window_create(&window, "MGL Win32 test", &params)) return 2;
    if (window.num_monitors < 1 || window.monitors[0].size.x <= 0 || !window.monitors[0].name[0])
        return 6;
    mgl_window_set_size(&window, (mgl_vec2i){400, 300});
    mgl_event event;
    while (mgl_window_poll_event(&window, &event)) {}
    if (window.size.x != 400 || window.size.y != 300 || window.view.size.x != 400)
        return 7;
    mgl_window_clear(&window, (mgl_color){0, 0, 0, 255});
    mgl_rectangle rectangle = {.position={32,32}, .size={128,128}, .color={255,0,0,255}};
    mgl_rectangle_draw(mgl_get_context(), &rectangle);

    HMODULE gl = GetModuleHandleW(L"opengl32.dll");
    typedef void (WINAPI *read_fn)(int,int,int,int,unsigned,unsigned,void *);
    read_fn read_pixels = (read_fn)(void *)GetProcAddress(gl, "glReadPixels");
    unsigned char pixel[4] = {0};
    if (!read_pixels) return 3;
    read_pixels(48, window.size.y - 48, 1, 1, 0x1908, 0x1401, pixel);
    if (pixel[0] != 255 || pixel[1] || pixel[2]) return 4;
    mgl_window_display(&window);

    HWND hwnd = mgl_window_get_system_handle(&window);
    PostMessageW(hwnd, WM_CHAR, 0x00fc, 0);
    PostMessageW(hwnd, WM_KEYDOWN, VK_F9, 0);
    PostMessageW(hwnd, WM_CLOSE, 0, 0);
    bool text = false, key = false, close = false;
    while (mgl_window_poll_event(&window, &event)) {
        if (event.type == MGL_EVENT_TEXT_ENTERED)
            text = event.text.size == 2 && event.text.codepoint == 0x00fc;
        if (event.type == MGL_EVENT_KEY_PRESSED)
            key = event.key.code == MGL_KEY_F9;
        if (event.type == MGL_EVENT_CLOSED) close = true;
    }
    mgl_window_close(&window);
    mgl_window_deinit(&window);
    if (mgl_window_create(&window, "MGL Win32 reopen test", &params)) return 8;
    mgl_window_deinit(&window);
    mgl_deinit();
    if (!text || !key || !close) return 5;
    puts("MGL Win32 OpenGL drawing, monitor inventory, resize, Unicode input, key events and reopening passed.");
    return 0;
}
