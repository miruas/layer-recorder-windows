param([string]$Compiler = 'gcc', [switch]$GenerateOnly)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$checkout = Join-Path $root 'build/native/gsr-ui/source'
$mgl = Join-Path $checkout 'depends/mglpp/depends/mgl'
$port = Join-Path $root 'native/gsr-windows'
$build = Join-Path $root 'build/native/gsr-windows'
if (!(Test-Path "$mgl/src/window/window.c")) {
    throw 'Prepare the pinned source with tools/build-gsr-windows-config.ps1 first.'
}
$revision = git -C $mgl rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $revision -ne '90d7c9190cf704901dd4bf204eb27a9e56a4e7ac') {
    throw 'MGL checkout has the wrong pinned revision.'
}
New-Item -ItemType Directory -Force -Path $build | Out-Null

# Keep the MGL platform-independent window functions and its GL function table.
# Replace the Linux-only initialization and symbol loader with Win32 equivalents.
$source = Get-Content "$mgl/src/window/window.c" -Raw
$start = $source.IndexOf('int mgl_window_create(')
if ($start -lt 0) { throw 'Could not find the MGL window API.' }
$prefix = @(
    '#include <mgl/window/window.h>'
    '#include <mgl/mgl.h>'
    '#include <mgl/graphics/text.h>'
    '#include <stdlib.h>'
    '#include <unistd.h>'
    '#include <assert.h>'
    '#include <string.h>'
    'extern bool layer_mgl_win32_init(mgl_window *, const char *, const mgl_window_create_params *);'
    'static int mgl_window_init(mgl_window *self, const char *name, const mgl_window_create_params *params, mgl_window_handle existing) {'
    '    memset(self, 0, sizeof(*self));'
    '    self->key_repeat_enabled = true;'
    '    mgl_clock_init(&self->frame_timer);'
    '    return !existing && layer_mgl_win32_init(self, name, params) ? 0 : -1;'
    '}'
)
$window = Join-Path $build 'window_api.c'
Set-Content $window -Value ($prefix + $source.Substring($start)) -Encoding utf8

$source = Get-Content "$mgl/src/gl.c" -Raw
$start = $source.IndexOf('typedef struct')
$end = $source.IndexOf('static int mgl_gl_load_glx')
if ($start -lt 0 -or $end -le $start) { throw 'Could not find the MGL OpenGL function table.' }
$loader = $source.Substring($start, $end - $start)
$a = $loader.IndexOf('    dlerror();')
$b = $loader.IndexOf('    return sym;', $a)
if ($a -lt 0 -or $b -le $a) { throw 'Could not find the MGL symbol loader.' }
$resolve = @(
    '    (void)required;'
    '    void *sym = (void *)GetProcAddress((HMODULE)handle, name);'
    '    if (!sym) {'
    '        sym = (void *)wglGetProcAddress(name);'
    '        if ((intptr_t)sym == -1 || (uintptr_t)sym <= 3) sym = NULL;'
    '    }'
) -join [Environment]::NewLine
$loader = $loader.Substring(0, $a) + $resolve + [Environment]::NewLine + $loader.Substring($b)
$gl = Join-Path $build 'gl_loader.c'
Set-Content $gl -Value (@('#include <windows.h>', '#include <mgl/gl.h>', '#include <string.h>') + $loader + @(
    'int mgl_gl_load(mgl_gl *self) {'
    '    memset(self, 0, sizeof(*self));'
    '    self->gl_library = GetModuleHandleW(L"opengl32.dll");'
    '    return self->gl_library ? mgl_gl_load_gl(self) : -1;'
    '}'
    'void mgl_gl_unload(mgl_gl *self) { memset(self, 0, sizeof(*self)); }'
)) -Encoding utf8

if ($GenerateOnly) { return }

$exe = Join-Path $build 'mgl-win32-smoke.exe'
& $Compiler -std=gnu11 -O2 -Wall -Wextra -static "-I$mgl/include" "$port/mgl_win32.c" "$port/mgl_smoke.c" "$port/mgl_smoke_text.c" $window $gl "$mgl/src/graphics/rectangle.c" "$mgl/src/system/clock.c" -o $exe -lopengl32 -lgdi32 -luser32
if ($LASTEXITCODE -ne 0) { throw 'MGL Win32 smoke build failed.' }
& $exe
if ($LASTEXITCODE -ne 0) { throw "MGL Win32 smoke failed with code $LASTEXITCODE." }
