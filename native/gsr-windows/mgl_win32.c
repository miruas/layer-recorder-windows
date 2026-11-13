// SPDX-License-Identifier: GPL-3.0-only
#include <windows.h>
#include <windowsx.h>
#include <stdint.h>
#include <limits.h>
#include <wchar.h>
#include <stdlib.h>
#include <mgl/mgl.h>
#include <mgl/window/window.h>
#include <mgl/window/event.h>

#define EVENT_CAPACITY 128

typedef struct {
    HWND hwnd;
    HDC dc;
    HGLRC gl;
    mgl_event events[EVENT_CAPACITY];
    unsigned first, count;
    WCHAR surrogate;
    bool cursor_hidden, fullscreen;
    WINDOWPLACEMENT placement;
    LONG_PTR style;
    mgl_vec2i minimum, maximum;
    char monitor_names[MGL_MAX_MONITORS][128];
} win_window;

static mgl_context context;
static unsigned users;

mgl_context *mgl_get_context(void) { return &context; }
int mgl_init(mgl_window_system system) {
    if (system != MGL_WINDOW_SYSTEM_NATIVE) return -1;
    ++users;
    return 0;
}
int mgl_init_with_wayland_display(struct wl_display *display) {
    (void)display;
    return -1;
}
void mgl_deinit(void) {
    if (users && --users == 0) mgl_gl_unload(&context.gl);
}
bool mgl_is_connected_to_display_server(void) { return users != 0; }
void mgl_ping_display_server(void) {}

static void push(win_window *window, mgl_event event) {
    if (window->count == EVENT_CAPACITY) {
        window->first = (window->first + 1) % EVENT_CAPACITY;
        --window->count;
    }
    window->events[(window->first + window->count++) % EVENT_CAPACITY] = event;
}

static mgl_key_states modifiers(void) {
    return (mgl_key_states){
        .alt = GetKeyState(VK_MENU) < 0, .control = GetKeyState(VK_CONTROL) < 0,
        .shift = GetKeyState(VK_SHIFT) < 0,
        .system = GetKeyState(VK_LWIN) < 0 || GetKeyState(VK_RWIN) < 0
    };
}

static const unsigned short keys[__MGL_NUM_KEYS__] = {
    [MGL_KEY_ESCAPE]=VK_ESCAPE, [MGL_KEY_SPACE]=VK_SPACE, [MGL_KEY_ENTER]=VK_RETURN,
    [MGL_KEY_BACKSPACE]=VK_BACK, [MGL_KEY_TAB]=VK_TAB, [MGL_KEY_DELETE]=VK_DELETE,
    [MGL_KEY_INSERT]=VK_INSERT, [MGL_KEY_HOME]=VK_HOME, [MGL_KEY_END]=VK_END,
    [MGL_KEY_PAGEUP]=VK_PRIOR, [MGL_KEY_PAGEDOWN]=VK_NEXT,
    [MGL_KEY_LEFT]=VK_LEFT, [MGL_KEY_RIGHT]=VK_RIGHT, [MGL_KEY_UP]=VK_UP,
    [MGL_KEY_DOWN]=VK_DOWN, [MGL_KEY_PRINTSCREEN]=VK_SNAPSHOT, [MGL_KEY_PAUSE]=VK_PAUSE,
    [MGL_KEY_LCONTROL]=VK_LCONTROL, [MGL_KEY_RCONTROL]=VK_RCONTROL,
    [MGL_KEY_LALT]=VK_LMENU, [MGL_KEY_RALT]=VK_RMENU, [MGL_KEY_LSHIFT]=VK_LSHIFT,
    [MGL_KEY_RSHIFT]=VK_RSHIFT, [MGL_KEY_LSYSTEM]=VK_LWIN, [MGL_KEY_RSYSTEM]=VK_RWIN,
    [MGL_KEY_LBRACKET]=VK_OEM_4, [MGL_KEY_RBRACKET]=VK_OEM_6,
    [MGL_KEY_SEMICOLON]=VK_OEM_1, [MGL_KEY_COMMA]=VK_OEM_COMMA,
    [MGL_KEY_PERIOD]=VK_OEM_PERIOD, [MGL_KEY_QUOTE]=VK_OEM_7, [MGL_KEY_SLASH]=VK_OEM_2,
    [MGL_KEY_BACKSLASH]=VK_OEM_5, [MGL_KEY_TILDE]=VK_OEM_3,
    [MGL_KEY_EQUAL]=VK_OEM_PLUS, [MGL_KEY_HYPHEN]=VK_OEM_MINUS,
};

static unsigned virtual_key(mgl_key key) {
    if (key >= MGL_KEY_A && key <= MGL_KEY_Z) return 'A' + key - MGL_KEY_A;
    if (key >= MGL_KEY_NUM0 && key <= MGL_KEY_NUM9) return '0' + key - MGL_KEY_NUM0;
    if (key >= MGL_KEY_F1 && key <= MGL_KEY_F15) return VK_F1 + key - MGL_KEY_F1;
    if (key >= MGL_KEY_NUMPAD0 && key <= MGL_KEY_NUMPAD9) return VK_NUMPAD0 + key - MGL_KEY_NUMPAD0;
    return keys[key];
}

static mgl_key event_key(WPARAM vk, LPARAM flags) {
    if (vk == VK_SHIFT) vk = MapVirtualKeyW((flags >> 16) & 0xff, MAPVK_VSC_TO_VK_EX);
    if (vk == VK_CONTROL) vk = (flags & (1 << 24)) ? VK_RCONTROL : VK_LCONTROL;
    if (vk == VK_MENU) vk = (flags & (1 << 24)) ? VK_RMENU : VK_LMENU;
    if (vk == VK_RETURN && (flags & (1 << 24))) return MGL_KEY_NUMPAD_ENTER;
    for (int key = 1; key < __MGL_NUM_KEYS__; ++key)
        if (virtual_key(key) == vk) return key;
    return MGL_KEY_UNKNOWN;
}

static void text_event(win_window *window, WCHAR character) {
    if (character >= 0xd800 && character <= 0xdbff) {
        window->surrogate = character;
        return;
    }
    WCHAR text[2] = {character};
    int length = 1;
    uint32_t codepoint = character;
    if (window->surrogate && character >= 0xdc00 && character <= 0xdfff) {
        text[0] = window->surrogate;
        text[1] = character;
        length = 2;
        codepoint = 0x10000 + ((text[0] - 0xd800) << 10) + character - 0xdc00;
    }
    window->surrogate = 0;
    if (codepoint < 32 || codepoint == 127) return;
    mgl_event event = {.type=MGL_EVENT_TEXT_ENTERED, .text={.codepoint=codepoint}};
    event.text.size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text, length, event.text.str, 4, NULL, NULL);
    if (event.text.size) push(window, event);
}

static LRESULT CALLBACK dispatch(HWND hwnd, UINT message, WPARAM wp, LPARAM lp) {
    if (message == WM_NCCREATE) {
        CREATESTRUCTW *params = (CREATESTRUCTW *)lp;
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, (LONG_PTR)params->lpCreateParams);
    }
    mgl_window *self = (mgl_window *)GetWindowLongPtrW(hwnd, GWLP_USERDATA);
    if (!self) return DefWindowProcW(hwnd, message, wp, lp);
    win_window *window = self->impl;
    mgl_event event = {0};
    switch (message) {
    case WM_SETCURSOR:
        if (LOWORD(lp) == HTCLIENT && window->cursor_hidden) { SetCursor(NULL); return TRUE; }
        return DefWindowProcW(hwnd, message, wp, lp);
    case WM_GETMINMAXINFO: {
        MINMAXINFO *info = (MINMAXINFO *)lp;
        RECT inset = {0};
        AdjustWindowRectEx(&inset, (DWORD)GetWindowLongPtrW(hwnd, GWL_STYLE),
                           FALSE, (DWORD)GetWindowLongPtrW(hwnd, GWL_EXSTYLE));
        int width = inset.right - inset.left, height = inset.bottom - inset.top;
        if (window->minimum.x > 0) info->ptMinTrackSize.x = window->minimum.x + width;
        if (window->minimum.y > 0) info->ptMinTrackSize.y = window->minimum.y + height;
        if (window->maximum.x > 0) info->ptMaxTrackSize.x = window->maximum.x + width;
        if (window->maximum.y > 0) info->ptMaxTrackSize.y = window->maximum.y + height;
        return 0;
    }
    case WM_MOVE: {
        POINT point = {0, 0};
        if (ClientToScreen(hwnd, &point)) self->pos = (mgl_vec2i){point.x,point.y};
        return 0;
    }
    case WM_CLOSE: event.type = MGL_EVENT_CLOSED; break;
    case WM_SIZE:
        if (wp == SIZE_MINIMIZED) return 0;
        self->size = (mgl_vec2i){LOWORD(lp), HIWORD(lp)};
        event = (mgl_event){.type=MGL_EVENT_RESIZED, .size={self->size.x, self->size.y}};
        break;
    case WM_SETFOCUS: self->focused = true; event.type = MGL_EVENT_GAINED_FOCUS; break;
    case WM_KILLFOCUS: self->focused = false; window->surrogate = 0; event.type = MGL_EVENT_LOST_FOCUS; break;
    case WM_CHAR:
        if (self->key_repeat_enabled || !(lp & (1L << 30))) text_event(window, (WCHAR)wp);
        return 0;
    case WM_KEYDOWN: case WM_SYSKEYDOWN:
    case WM_KEYUP: case WM_SYSKEYUP:
        if (!self->key_repeat_enabled && !(lp & (1UL << 31)) && (lp & (1UL << 30))) return 0;
        event.type = (lp & (1UL << 31)) ? MGL_EVENT_KEY_RELEASED : MGL_EVENT_KEY_PRESSED;
        event.key = (mgl_key_event){event_key(wp, lp), modifiers()};
        if (message == WM_SYSKEYDOWN && wp == VK_F4) return DefWindowProcW(hwnd, message, wp, lp);
        break;
    case WM_MOUSEMOVE:
        self->cursor_position = (mgl_vec2i){GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
        event = (mgl_event){.type=MGL_EVENT_MOUSE_MOVED,
            .mouse_move={self->cursor_position.x, self->cursor_position.y, modifiers()}};
        break;
    case WM_LBUTTONDOWN: case WM_RBUTTONDOWN: case WM_MBUTTONDOWN:
    case WM_LBUTTONUP: case WM_RBUTTONUP: case WM_MBUTTONUP:
        event.type = (message == WM_LBUTTONDOWN || message == WM_RBUTTONDOWN || message == WM_MBUTTONDOWN)
            ? MGL_EVENT_MOUSE_BUTTON_PRESSED : MGL_EVENT_MOUSE_BUTTON_RELEASED;
        event.mouse_button = (mgl_mouse_button_event){
            (message == WM_LBUTTONDOWN || message == WM_LBUTTONUP) ? MGL_BUTTON_LEFT :
            (message == WM_RBUTTONDOWN || message == WM_RBUTTONUP) ? MGL_BUTTON_RIGHT : MGL_BUTTON_MIDDLE,
            GET_X_LPARAM(lp), GET_Y_LPARAM(lp), modifiers()};
        break;
    case WM_MOUSEWHEEL: {
        POINT point = {GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
        ScreenToClient(hwnd, &point);
        event = (mgl_event){.type=MGL_EVENT_MOUSE_WHEEL_SCROLLED,
            .mouse_wheel_scroll={GET_WHEEL_DELTA_WPARAM(wp) / WHEEL_DELTA, point.x, point.y, modifiers()}};
        break;
    }
    default: return DefWindowProcW(hwnd, message, wp, lp);
    }
    push(window, event);
    return 0;
}

static void deinit(mgl_window *self) {
    win_window *window = self->impl;
    if (context.current_window == self) {
        wglMakeCurrent(NULL, NULL);
        context.current_window = NULL;
    }
    if (window->gl) wglDeleteContext(window->gl);
    if (window->dc) ReleaseDC(window->hwnd, window->dc);
    if (window->hwnd) DestroyWindow(window->hwnd);
    free(window);
    self->impl = NULL;
    self->open = false;
}

static bool poll(mgl_window *self, mgl_event *event) {
    win_window *window = self->impl;
    MSG message;
    while (!window->count && PeekMessageW(&message, window->hwnd, 0, 0, PM_REMOVE)) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    if (!window->count) return false;
    *event = window->events[window->first];
    window->first = (window->first + 1) % EVENT_CAPACITY;
    --window->count;
    if (event->type == MGL_EVENT_RESIZED && context.current_window == self && self->size.x && self->size.y) {
        mgl_window_set_view(self, &(mgl_view){{0, 0}, self->size});
        mgl_window_set_scissor(self, &(mgl_scissor){{0, 0}, self->size});
    }
    return true;
}

static mgl_window_handle handle(const mgl_window *self) { return ((win_window *)self->impl)->hwnd; }
static void visible(mgl_window *self, bool value) { ShowWindow(handle(self), value ? SW_SHOW : SW_HIDE); }
static void close_window(mgl_window *self) { self->open = false; visible(self, false); }
static bool pressed(const mgl_window *self, mgl_key key) {
    const unsigned vk = virtual_key(key);
    return self->focused && vk && GetKeyState(vk) < 0;
}
static bool mouse_pressed(const mgl_window *self, mgl_mouse_button button) {
    static const int buttons[] = {0, VK_LBUTTON, VK_RBUTTON, VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2};
    return self->focused && buttons[button] && GetKeyState(buttons[button]) < 0;
}
static void repeat(mgl_window *self, bool enabled) { self->key_repeat_enabled = enabled; }
static void flush(mgl_window *self) { (void)self; context.gl.glFlush(); }
static void swap(mgl_window *self) { SwapBuffers(((win_window *)self->impl)->dc); }
static void title(mgl_window *self, const char *value) {
    int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value, -1, NULL, 0);
    if (!size) return;
    WCHAR *text = malloc(size * sizeof(*text));
    if (!text) return;
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value, -1, text, size))
        SetWindowTextW(handle(self), text);
    free(text);
}

static void position(mgl_window *self, mgl_vec2i pos) {
    SetWindowPos(handle(self), NULL, pos.x, pos.y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
}
static void size_window(mgl_window *self, mgl_vec2i size) {
    HWND hwnd = handle(self);
    RECT rect = {0, 0, size.x, size.y};
    if (size.x > 0 && size.y > 0 && AdjustWindowRectEx(&rect,
        (DWORD)GetWindowLongPtrW(hwnd, GWL_STYLE), FALSE, (DWORD)GetWindowLongPtrW(hwnd, GWL_EXSTYLE)))
        SetWindowPos(hwnd, NULL, 0, 0, rect.right - rect.left, rect.bottom - rect.top,
                     SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
}
static void set_vsync(mgl_window *self, bool enabled) {
    typedef BOOL (WINAPI *swap_interval_fn)(int);
    swap_interval_fn interval = (swap_interval_fn)(void *)wglGetProcAddress("wglSwapIntervalEXT");
    self->vsync_enabled = enabled && interval && (uintptr_t)interval > 3 &&
                          (intptr_t)interval != -1 && interval(1);
    if (!enabled && interval && (uintptr_t)interval > 3 && (intptr_t)interval != -1) interval(0);
}
static bool vsync(const mgl_window *self) { return self->vsync_enabled; }
static void cursor_visible(mgl_window *self, bool visible) {
    ((win_window *)self->impl)->cursor_hidden = !visible;
    if (self->focused) SetCursor(visible ? LoadCursorW(NULL, MAKEINTRESOURCEW(32512)) : NULL);
}
static void size_limits(mgl_window *self, mgl_vec2i minimum, mgl_vec2i maximum) {
    win_window *window = self->impl;
    window->minimum = minimum;
    window->maximum = maximum;
}
static bool fullscreen(const mgl_window *self) {
    return ((win_window *)self->impl)->fullscreen;
}
static void set_fullscreen(mgl_window *self, bool enabled) {
    win_window *window = self->impl;
    if (window->fullscreen == enabled) return;
    HWND hwnd = window->hwnd;
    if (enabled) {
        MONITORINFO monitor = {.cbSize=sizeof(monitor)};
        if (!GetMonitorInfoW(MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), &monitor)) return;
        window->placement.length = sizeof(window->placement);
        if (!GetWindowPlacement(hwnd, &window->placement)) return;
        window->style = GetWindowLongPtrW(hwnd, GWL_STYLE);
        SetWindowLongPtrW(hwnd, GWL_STYLE, (window->style & ~WS_OVERLAPPEDWINDOW) | WS_POPUP);
        RECT r = monitor.rcMonitor;
        SetWindowPos(hwnd, NULL, r.left, r.top, r.right-r.left, r.bottom-r.top,
                     SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE);
    } else {
        SetWindowLongPtrW(hwnd, GWL_STYLE, window->style);
        SetWindowPlacement(hwnd, &window->placement);
        SetWindowPos(hwnd, NULL, 0, 0, 0, 0,
                     SWP_NOZORDER | SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED | SWP_NOACTIVATE);
    }
    window->fullscreen = enabled;
}
static void *no_egl(mgl_window *self) { (void)self; return NULL; }

static BOOL CALLBACK monitor_output(HMONITOR monitor, HDC dc, LPRECT rect, LPARAM data) {
    (void)dc; (void)rect;
    mgl_window *self = (mgl_window *)data;
    if (self->num_monitors == MGL_MAX_MONITORS) return FALSE;
    MONITORINFOEXW info = {.cbSize=sizeof(info)};
    if (!GetMonitorInfoW(monitor, (MONITORINFO *)&info)) return TRUE;
    win_window *window = self->impl;
    int index = self->num_monitors++;
    char *name = window->monitor_names[index];
    WideCharToMultiByte(CP_UTF8, 0, info.szDevice, -1, name, 128, NULL, NULL);
    DEVMODEW mode = {.dmSize=sizeof(mode)};
    EnumDisplaySettingsW(info.szDevice, ENUM_CURRENT_SETTINGS, &mode);
    RECT r = info.rcMonitor;
    self->monitors[index] = (mgl_monitor){
        .id=index + 1, .crtc_id=index + 1, .name=name,
        .pos={r.left,r.top}, .size={r.right-r.left,r.bottom-r.top},
        .refresh_rate=mode.dmDisplayFrequency > 1 ? (int)mode.dmDisplayFrequency : 0
    };
    return TRUE;
}

static void monitors(mgl_window *self, mgl_active_monitor_callback callback, void *data) {
    self->num_monitors = 0;
    EnumDisplayMonitors(NULL, NULL, monitor_output, (LPARAM)self);
    if (callback)
        for (int i = 0; i < self->num_monitors; ++i) callback(&self->monitors[i], data);
}

static void set_clipboard(mgl_window *self, const char *text, size_t size) {
    if (!size || size > INT_MAX) return;
    int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text, (int)size, NULL, 0);
    if (!count) return;
    HGLOBAL buffer = GlobalAlloc(GMEM_MOVEABLE, ((size_t)count + 1) * sizeof(WCHAR));
    if (!buffer) return;
    WCHAR *wide = GlobalLock(buffer);
    if (!wide) { GlobalFree(buffer); return; }
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text, (int)size, wide, count);
    wide[count] = 0;
    GlobalUnlock(buffer);
    if (OpenClipboard(handle(self))) {
        if (EmptyClipboard() && SetClipboardData(CF_UNICODETEXT, buffer)) buffer = NULL;
        CloseClipboard();
    }
    if (buffer) GlobalFree(buffer);
}

static bool get_clipboard(mgl_window *self, mgl_clipboard_callback callback, void *data, uint32_t types) {
    if (!(types & MGL_CLIPBOARD_TYPE_STRING) || !OpenClipboard(handle(self))) return false;
    HGLOBAL buffer = GetClipboardData(CF_UNICODETEXT);
    const WCHAR *wide = buffer ? GlobalLock(buffer) : NULL;
    char *text = NULL;
    int size = 0;
    if (wide) {
        size_t length = wcsnlen(wide, GlobalSize(buffer) / sizeof(*wide));
        if (length && length <= INT_MAX) {
            size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wide, (int)length, NULL, 0, NULL, NULL);
            text = size ? malloc(size) : NULL;
            if (text) WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wide, (int)length, text, size, NULL, NULL);
        }
        GlobalUnlock(buffer);
    }
    CloseClipboard();
    bool result = text && callback((const unsigned char *)text, size, MGL_CLIPBOARD_TYPE_STRING, data);
    free(text);
    return result;
}

bool layer_mgl_win32_init(mgl_window *self, const char *name, const mgl_window_create_params *params) {
    if (!users || context.current_window || (params && params->parent_window)) return false;
    win_window *window = calloc(1, sizeof(*window));
    if (!window) return false;
    self->impl = window;
    const HINSTANCE instance = GetModuleHandleW(NULL);
    WNDCLASSW wc = {.style=CS_OWNDC, .lpfnWndProc=dispatch, .hInstance=instance,
        .hCursor=LoadCursorW(NULL, MAKEINTRESOURCEW(32512)), .lpszClassName=L"LayerMglWindow"};
    if (!RegisterClassW(&wc) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) goto fail;
    const bool overlay = params && params->window_type == MGL_WINDOW_TYPE_OVERLAY;
    DWORD style = overlay || (params && params->hide_decorations) ? WS_POPUP : WS_OVERLAPPEDWINDOW;
    DWORD ex_style = overlay ? WS_EX_TOPMOST | WS_EX_TOOLWINDOW : 0;
    mgl_vec2i size = params ? params->size : (mgl_vec2i){800, 600};
    mgl_vec2i initial_position = params ? params->position : (mgl_vec2i){CW_USEDEFAULT, CW_USEDEFAULT};
    if (overlay && (size.x <= 0 || size.y <= 0)) {
        POINT point;
        GetCursorPos(&point);
        MONITORINFO monitor = {.cbSize=sizeof(monitor)};
        if (!GetMonitorInfoW(MonitorFromPoint(point, MONITOR_DEFAULTTOPRIMARY), &monitor)) goto fail;
        initial_position = (mgl_vec2i){monitor.rcMonitor.left, monitor.rcMonitor.top};
        size = (mgl_vec2i){monitor.rcMonitor.right - initial_position.x, monitor.rcMonitor.bottom - initial_position.y};
    }
    if (size.x <= 0 || size.y <= 0) goto fail;
    RECT rect = {0, 0, size.x, size.y};
    if (!AdjustWindowRectEx(&rect, style, FALSE, ex_style)) goto fail;
    window->hwnd = CreateWindowExW(ex_style, wc.lpszClassName, L"", style,
        initial_position.x, initial_position.y, rect.right - rect.left, rect.bottom - rect.top, NULL, NULL, instance, self);
    if (!window->hwnd) goto fail;
    window->dc = GetDC(window->hwnd);
    PIXELFORMATDESCRIPTOR pfd = {.nSize=sizeof(pfd), .nVersion=1,
        .dwFlags=PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER,
        .iPixelType=PFD_TYPE_RGBA, .cColorBits=32,
        .cAlphaBits=params && params->support_alpha ? 8 : 0,
        .cDepthBits=params && params->request_depth_buffer ? 24 : 0,
        .cStencilBits=params && params->request_stencil_buffer ? 8 : 0};
    int format = ChoosePixelFormat(window->dc, &pfd);
    if (!format || !SetPixelFormat(window->dc, format, &pfd)) goto fail;
    window->gl = wglCreateContext(window->dc);
    if (!window->gl || !wglMakeCurrent(window->dc, window->gl)) goto fail;
    context.current_window = self;
    if (mgl_gl_load(&context.gl) != 0) goto fail;
    self->get_system_handle = handle;
    self->deinit = deinit;
    self->close = close_window;
    self->poll_event = poll;
    self->swap_buffers = swap;
    self->set_visible = visible;
    self->is_key_pressed = pressed;
    self->is_mouse_button_pressed = mouse_pressed;
    self->set_title = title;
    self->set_position = position;
    self->set_size = size_window;
    self->set_size_limits = size_limits;
    self->set_vsync_enabled = set_vsync;
    self->is_vsync_enabled = vsync;
    self->set_fullscreen = set_fullscreen;
    self->is_fullscreen = fullscreen;
    self->set_cursor_visible = cursor_visible;
    self->set_clipboard = set_clipboard;
    self->get_clipboard = get_clipboard;
    self->get_egl_display = no_egl;
    self->get_egl_context = no_egl;
    self->for_each_active_monitor_output = monitors;
    self->set_key_repeat_enabled = repeat;
    self->flush = flush;
    self->open = true;
    self->size = size;
    if (params) size_limits(self, params->min_size, params->max_size);
    monitors(self, NULL, NULL);
    set_vsync(self, true);
    mgl_window_set_view(self, &(mgl_view){{0, 0}, size});
    mgl_window_set_scissor(self, &(mgl_scissor){{0, 0}, size});
    context.gl.glEnable(GL_SCISSOR_TEST);
    context.gl.glEnable(GL_BLEND);
    context.gl.glBlendFuncSeparate(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
    title(self, name);
    if (!params || !params->hidden) visible(self, true);
    return true;
fail:
    deinit(self);
    return false;
}
