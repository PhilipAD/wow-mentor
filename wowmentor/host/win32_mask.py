"""The Windows screen mask: a click-through overlay over the data strip that the bridge's own screen reads never see.

Why it works: the overlay window calls SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE). The player sees it on the monitor, but
BitBlt of the screen, Desktop Duplication and Windows.Graphics.Capture all see what is *under* it (the strip). It needs Windows 10
2004 (build 19041); on older builds the same call degrades to WDA_MONITOR, which paints the overlay BLACK in the capture and would
blind the bridge, so below that build the mask reports `available() == False` and the bridge runs without it.
  https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity

Two halves live here:
  Win32Mask    the `base.Mask` the bridge talks to. It starts the helper subprocess and feeds it lines on stdin.
  main()       the helper (`python -m wowmentor.host.win32_mask --log FILE`): a pure ctypes window, no tkinter, written so it also
               runs under pythonw (it never prints; everything goes to the log file).

Helper protocol (stdin, one line each, UTF-8/ASCII, same shape as the macOS helper in bridge/mask/wowmask.swift):
    "x y w h capW"     the strip as ABSOLUTE SCREEN PIXELS (virtual-desktop coordinates, may be negative on a left monitor).
                       The parent has already converted from game-client pixels (see compute_frame_rect), so the helper needs no
                       window lookup. `capW` is the width the numbers refer to; the parent always sends capW == w (scale 1).
    "R <base64>"       fallback only: one row of RGB pixels (every 4th pixel) taken just below the strip, painted when the helper
                       cannot read the screen itself.
The helper exits when stdin closes (the bridge died) and hides the overlay when no strip line arrived for STALE_SECONDS.
Exit codes: 0 normal, EXIT_UNSUPPORTED (3) = cannot be made invisible to capture here (old Windows, affinity refused); the parent then
stops trying.

Window recipe (each choice has a reason): WS_POPUP + TOPMOST|TOOLWINDOW|NOACTIVATE|LAYERED|TRANSPARENT so it floats above a borderless
game, stays out of Alt-Tab, never takes focus and passes every click through. SetLayeredWindowAttributes(alpha 255) once keeps the
window layered (a hide/show of an unlayered excluded window can turn black in captures: electron/electron#29085) and UpdateLayeredWindow
is never called (it makes SetWindowDisplayAffinity fail, microsoft/Windows.UI.Composition-Win32-Samples#56). The affinity is verified
with GetWindowDisplayAffinity after every show and the window is hidden if it is wrong.

Importing this module touches no native binding: ctypes/windll/WINFUNCTYPE are only used inside functions.
"""
import argparse, base64, binascii, os, subprocess, sys, threading, time

from . import base

MIN_BUILD = 19041                     # Windows 10 2004, first build with WDA_EXCLUDEFROMCAPTURE
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
EXIT_UNSUPPORTED = 3
STALE_SECONDS = 5.0                   # hide the overlay when the bridge stops talking (game closed, bridge stalled)
KEEPALIVE_SECONDS = 1.0               # parent repeats the rect at least this often
ROW_INTERVAL = 0.1                    # parent asks for a fallback row at most this often
TOPMOST_SECONDS = 1.0                 # re-assert HWND_TOPMOST this often
PAINT_MS = 16                         # ~60 Hz
HOUSEKEEPING_MS = 1000
MAX_STRIP_W = 16384
MAX_STRIP_H = 256
RUN_OK_SECONDS = 10.0                 # a helper that lived this long counts as healthy (resets the restart backoff)
LOG_LIMIT = 256 * 1024

# Window constants (winuser.h)
WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
EX_STYLES = WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED | WS_EX_TRANSPARENT
LWA_ALPHA = 0x2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW, SWP_HIDEWINDOW = 0x1, 0x2, 0x10, 0x40, 0x80
SW_HIDE = 0
SRCCOPY, BLACKNESS, COLORONCOLOR = 0x00CC0020, 0x00000042, 3
DIB_RGB_COLORS = 0
WM_DESTROY, WM_CLOSE, WM_PAINT, WM_ERASEBKGND, WM_TIMER = 0x2, 0x10, 0xF, 0x14, 0x113
WM_NCHITTEST, WM_MOUSEACTIVATE = 0x84, 0x21
HTTRANSPARENT, MA_NOACTIVATE = -1, 3
TIMER_PAINT, TIMER_HOUSE = 1, 2
CLASS_NAME = "WowMentorMask"
DPI_PER_MONITOR_V2 = -4


# ---------------------------------------------------------------- pure logic (unit tested on every OS)
def should_enable(build):
    """True if this Windows build can hide a window from capture without blinding the bridge. `build` may be None (unknown)."""
    try:
        return int(build) >= MIN_BUILD
    except (TypeError, ValueError):
        return False


def parse_line(line):
    """One stdin line -> ("rect", (x, y, w, h, capW)) | ("row", rgb_bytes) | None for anything malformed. Never raises."""
    try:
        if isinstance(line, (bytes, bytearray)):
            line = bytes(line).decode("ascii", "replace")
        line = line.strip()
        if line.startswith("R "):
            rgb = base64.b64decode(line[2:].strip(), validate=True)
            if len(rgb) < 6 or len(rgb) % 3:
                return None
            return ("row", rgb)
        parts = line.split()
        if len(parts) != 5:
            return None
        x, y, w, h, cap = [int(round(float(p))) for p in parts]
        if not (0 < w <= MAX_STRIP_W and 0 < h <= MAX_STRIP_H and cap > 0):
            return None
        return ("rect", (x, y, w, h, cap))
    except (ValueError, TypeError, AttributeError, binascii.Error, OverflowError):
        return None


def compute_frame_rect(strip_rect, capture_width, client_origin, client_width):
    """Strip in captured-client pixels -> absolute screen pixels (x, y, w, h).

    `client_origin` is the screen position of the game client area's top-left (ClientToScreen), `client_width` its width in pixels.
    The capture is lossless so capture_width normally equals client_width (scale 1); when it does not (a scaled capture) the strip
    is scaled by client_width / capture_width. Height is at least 1 pixel."""
    x, y, w, h = strip_rect
    ox, oy = client_origin
    scale = 1.0
    if capture_width and client_width and capture_width > 0 and client_width > 0:
        scale = float(capture_width) / float(client_width)
    return (int(round(ox + x / scale)), int(round(oy + y / scale)), max(1, int(round(w / scale))), max(1, int(round(h / scale))))


def format_rect_line(rect):
    """The exact line sent to the helper for a screen rect (capW == w: the numbers are already screen pixels)."""
    x, y, w, h = rect
    return "%d %d %d %d %d\n" % (x, y, w, h, w)


def source_rect(rect):
    """Where the overlay copies its picture from: the single screen row directly below the strip, as (x, y, w, h)."""
    x, y, w, h = rect
    return (x, y + h, w, 1)


def rgb_row_to_dib(rgb):
    """RGB bytes (one pixel row) -> (pixel_count, bottom-up 24-bit BGR bytes padded to 4 bytes), the layout StretchDIBits wants."""
    n = len(rgb) // 3
    bgr = bytearray(3 * n)
    bgr[0::3], bgr[1::3], bgr[2::3] = rgb[2:3 * n:3], rgb[1:3 * n:3], rgb[0:3 * n:3]
    bgr += b"\0" * ((4 - (3 * n) % 4) % 4)
    return n, bytes(bgr)


def is_stale(now, last_update, limit=STALE_SECONDS):
    """True if the bridge has been silent for `limit` seconds (or never spoke)."""
    return last_update is None or now - last_update > limit


def restart_delay(failures):
    """Seconds to wait before starting the helper again after `failures` quick deaths in a row: 0, 2, 4, 8, ... capped at 30."""
    return 0.0 if failures <= 0 else float(min(30, 2 ** failures))


def build_command(python=None, log_path=None):
    """The helper command line. `-m` needs wowmentor importable, so the caller sets PYTHONPATH (see Win32Mask._spawn)."""
    cmd = [python or sys.executable, "-m", "wowmentor.host.win32_mask"]
    if log_path:
        cmd += ["--log", log_path]
    return cmd


def hwnd_of(handle):
    """The integer HWND inside whatever opaque handle `capture.find_game()` returned (int, object with .hwnd, or dict), else None."""
    if isinstance(handle, bool):
        return None
    if isinstance(handle, int):
        return handle or None
    for get in (lambda h: getattr(h, "hwnd", None), lambda h: h.get("hwnd") if isinstance(h, dict) else None):
        try:
            v = get(handle)
        except Exception:
            v = None
        if isinstance(v, int) and v:
            return v
    return None


# ---------------------------------------------------------------- lazy ctypes binding (Windows only)
_api_cache = []


def _build_api():
    """All Windows bindings in one namespace, created on first use so import stays free of ctypes.windll/WINFUNCTYPE."""
    if _api_cache:
        return _api_cache[0]
    import ctypes as C
    from ctypes import wintypes as W
    from types import SimpleNamespace as NS
    u32, g32, k32 = C.WinDLL("user32", use_last_error=True), C.WinDLL("gdi32", use_last_error=True), C.WinDLL("kernel32", use_last_error=True)
    LRESULT = C.c_ssize_t
    WNDPROC = C.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

    class WNDCLASSEXW(C.Structure):
        _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", C.c_int), ("cbWndExtra", C.c_int),
                    ("hInstance", W.HINSTANCE), ("hIcon", W.HANDLE), ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                    ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR), ("hIconSm", W.HANDLE)]

    class PAINTSTRUCT(C.Structure):
        _fields_ = [("hdc", W.HDC), ("fErase", W.BOOL), ("rcPaint", W.RECT), ("fRestore", W.BOOL), ("fIncUpdate", W.BOOL),
                    ("rgbReserved", C.c_byte * 32)]

    class BITMAPINFOHEADER(C.Structure):
        _fields_ = [("biSize", W.DWORD), ("biWidth", C.c_long), ("biHeight", C.c_long), ("biPlanes", W.WORD), ("biBitCount", W.WORD),
                    ("biCompression", W.DWORD), ("biSizeImage", W.DWORD), ("biXPelsPerMeter", C.c_long), ("biYPelsPerMeter", C.c_long),
                    ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]

    def sig(fn, res, *args):
        fn.restype, fn.argtypes = res, list(args)
    sig(u32.DefWindowProcW, LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    sig(u32.RegisterClassExW, W.ATOM, C.POINTER(WNDCLASSEXW))
    sig(u32.CreateWindowExW, W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
    sig(u32.DestroyWindow, W.BOOL, W.HWND)
    sig(u32.ShowWindow, W.BOOL, W.HWND, C.c_int)
    sig(u32.IsWindowVisible, W.BOOL, W.HWND)
    sig(u32.SetWindowPos, W.BOOL, W.HWND, C.c_void_p, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
    sig(u32.SetLayeredWindowAttributes, W.BOOL, W.HWND, W.COLORREF, C.c_ubyte, W.DWORD)
    sig(u32.SetWindowDisplayAffinity, W.BOOL, W.HWND, W.DWORD)
    sig(u32.GetWindowDisplayAffinity, W.BOOL, W.HWND, C.POINTER(W.DWORD))
    sig(u32.GetWindowLongW, C.c_long, W.HWND, C.c_int)
    sig(u32.GetDC, W.HDC, W.HWND)
    sig(u32.ReleaseDC, C.c_int, W.HWND, W.HDC)
    sig(u32.BeginPaint, W.HDC, W.HWND, C.POINTER(PAINTSTRUCT))
    sig(u32.EndPaint, W.BOOL, W.HWND, C.POINTER(PAINTSTRUCT))
    sig(u32.SetTimer, C.c_size_t, W.HWND, C.c_size_t, W.UINT, C.c_void_p)
    sig(u32.KillTimer, W.BOOL, W.HWND, C.c_size_t)
    sig(u32.PostMessageW, W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    sig(u32.PostQuitMessage, None, C.c_int)
    sig(u32.GetMessageW, C.c_int, C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
    sig(u32.PeekMessageW, W.BOOL, C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT)
    sig(u32.TranslateMessage, W.BOOL, C.POINTER(W.MSG))
    sig(u32.DispatchMessageW, LRESULT, C.POINTER(W.MSG))
    sig(u32.WindowFromPoint, W.HWND, W.POINT)
    sig(u32.GetSystemMetrics, C.c_int, C.c_int)
    sig(g32.StretchBlt, W.BOOL, W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.DWORD)
    sig(g32.BitBlt, W.BOOL, W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.HDC, C.c_int, C.c_int, W.DWORD)
    sig(g32.PatBlt, W.BOOL, W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.DWORD)
    sig(g32.SetStretchBltMode, C.c_int, W.HDC, C.c_int)
    sig(g32.StretchDIBits, C.c_int, W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int, C.c_int, C.c_void_p,
        C.c_void_p, W.UINT, W.DWORD)
    sig(g32.CreateCompatibleDC, W.HDC, W.HDC)
    sig(g32.CreateCompatibleBitmap, W.HBITMAP, W.HDC, C.c_int, C.c_int)
    sig(g32.SelectObject, W.HGDIOBJ, W.HDC, W.HGDIOBJ)
    sig(g32.DeleteObject, W.BOOL, W.HGDIOBJ)
    sig(g32.DeleteDC, W.BOOL, W.HDC)
    sig(g32.GetPixel, W.COLORREF, W.HDC, C.c_int, C.c_int)
    sig(k32.GetModuleHandleW, W.HMODULE, W.LPCWSTR)
    api = NS(C=C, W=W, u32=u32, g32=g32, k32=k32, WNDPROC=WNDPROC, WNDCLASSEXW=WNDCLASSEXW, PAINTSTRUCT=PAINTSTRUCT,
             BITMAPINFOHEADER=BITMAPINFOHEADER, LRESULT=LRESULT)
    _api_cache.append(api)
    return api


def set_dpi_aware():
    """Per-Monitor-V2 so every coordinate is a physical pixel (the strip's cells are 3x2 device pixels). Best effort, never raises.
    https://learn.microsoft.com/en-us/windows/win32/hidpi/setting-the-default-dpi-awareness-for-a-process"""
    try:
        api = _build_api()
        fn = api.u32.SetProcessDpiAwarenessContext
        fn.restype, fn.argtypes = api.W.BOOL, [api.C.c_void_p]
        if fn(api.C.c_void_p(DPI_PER_MONITOR_V2)):
            return True
    except Exception:
        pass
    try:
        api = _build_api()
        return bool(api.u32.SetProcessDPIAware())
    except Exception:
        return False


def client_geometry(hwnd):
    """(origin_x, origin_y, width, height) of a window's client area in physical screen pixels, or None. Used by the parent: it
    switches THIS THREAD to Per-Monitor-V2 for the call, so the answer is physical pixels even if the bridge process is not aware."""
    try:
        api = _build_api()
        C, W, u32 = api.C, api.W, api.u32
        set_thread = getattr(u32, "SetThreadDpiAwarenessContext", None)
        old = None
        if set_thread is not None:
            set_thread.restype, set_thread.argtypes = C.c_void_p, [C.c_void_p]
            old = set_thread(C.c_void_p(DPI_PER_MONITOR_V2))
        try:
            rc, pt = W.RECT(), W.POINT(0, 0)
            u32.GetClientRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
            u32.ClientToScreen.argtypes = [W.HWND, C.POINTER(W.POINT)]
            if not u32.GetClientRect(hwnd, C.byref(rc)) or not u32.ClientToScreen(hwnd, C.byref(pt)):
                return None
            return (pt.x, pt.y, rc.right - rc.left, rc.bottom - rc.top)
        finally:
            if set_thread is not None and old:
                set_thread(C.c_void_p(old))
    except Exception:
        return None


# ---------------------------------------------------------------- the overlay window (helper process; tests also use it in-process)
_windows = {}                          # hwnd -> MaskWindow, for the shared window procedure
_wndproc_ref = []


def _window_proc(hwnd, msg, wp, lp):
    api = _build_api()
    win = _windows.get(hwnd)
    try:
        if win is not None:
            if msg == WM_TIMER:
                win.on_timer(int(wp)); return 0
            if msg == WM_PAINT:
                ps = api.PAINTSTRUCT()
                hdc = api.u32.BeginPaint(hwnd, api.C.byref(ps))
                try: win.paint_to(hdc)
                finally: api.u32.EndPaint(hwnd, api.C.byref(ps))
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
        if msg == WM_CLOSE:
            api.u32.DestroyWindow(hwnd); return 0
        if msg == WM_DESTROY:
            _windows.pop(hwnd, None)
            if win is not None and win.quit_on_destroy:
                api.u32.PostQuitMessage(0)
            return 0
    except Exception as e:                       # a Python error must never unwind into the Windows message pump
        if win is not None:
            win.log("window procedure error: %r" % (e,))
    return api.u32.DefWindowProcW(hwnd, msg, wp, lp)


class MaskWindow(object):
    """The overlay. Owns one top-level window; every method must run on the thread that created it."""

    def __init__(self, log=None, quit_on_destroy=True):
        self.log = log or (lambda msg: None)
        self.quit_on_destroy = quit_on_destroy
        self.hwnd = None
        self.rect = None                   # screen rect (x, y, w, h) the window is placed at
        self.row = None                    # (pixel_count, dib_bytes) fallback row from the pipe
        self.blocked = False               # affinity could not be verified: never show
        self.pending = None                # rect requested by the reader thread
        self.last_update = None
        self.lock = threading.Lock()
        self.last_topmost = 0.0
        self.screen_ok = True

    # -- creation
    def create(self):
        """Create the hidden window and exclude it from capture. Returns True when the affinity is verified."""
        api = _build_api()
        C, u32 = api.C, api.u32
        hinst = api.k32.GetModuleHandleW(None)
        if not _wndproc_ref:
            _wndproc_ref.append(api.WNDPROC(_window_proc))        # one shared callback, kept alive for the process lifetime
        wc = api.WNDCLASSEXW(C.sizeof(api.WNDCLASSEXW), 0, _wndproc_ref[0], 0, 0, hinst, None, None, None, None, CLASS_NAME, None)
        if not u32.RegisterClassExW(C.byref(wc)) and C.get_last_error() != 1410:   # 1410 = class already registered (second window)
            raise OSError(C.get_last_error(), "RegisterClassExW failed")
        self.hwnd = u32.CreateWindowExW(EX_STYLES, CLASS_NAME, "WoW Mentor mask", WS_POPUP, 0, 0, 1, 1, None, None, hinst, None)
        if not self.hwnd:
            raise OSError(C.get_last_error(), "CreateWindowExW failed")
        _windows[self.hwnd] = self
        # Alpha 255 once keeps the window layered without UpdateLayeredWindow (see the module docstring).
        u32.SetLayeredWindowAttributes(self.hwnd, 0, 255, LWA_ALPHA)
        return self.apply_affinity()

    def affinity(self):
        api = _build_api()
        v = api.W.DWORD(0)
        if not api.u32.GetWindowDisplayAffinity(self.hwnd, api.C.byref(v)):
            return None
        return v.value

    def apply_affinity(self):
        """Set WDA_EXCLUDEFROMCAPTURE and read it back. False means the overlay must not be shown."""
        api = _build_api()
        if self.affinity() != WDA_EXCLUDEFROMCAPTURE:
            if not api.u32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE):
                self.log("SetWindowDisplayAffinity failed, error %d" % api.C.get_last_error())
        ok = self.affinity() == WDA_EXCLUDEFROMCAPTURE
        self.blocked = not ok
        return ok

    def timers(self):
        api = _build_api()
        api.u32.SetTimer(self.hwnd, TIMER_PAINT, PAINT_MS, None)
        api.u32.SetTimer(self.hwnd, TIMER_HOUSE, HOUSEKEEPING_MS, None)

    # -- placement
    def place(self, rect):
        """Move/size/show the overlay at a screen rect, topmost, without activating. Re-verifies the affinity after a show."""
        api = _build_api()
        was_visible = bool(api.u32.IsWindowVisible(self.hwnd))
        if self.blocked and not self.apply_affinity():
            return False
        x, y, w, h = rect
        api.u32.SetWindowPos(self.hwnd, api.C.c_void_p(-1), x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)   # HWND_TOPMOST = -1
        self.rect = tuple(rect)
        self.last_topmost = time.monotonic()
        if not was_visible and not self.apply_affinity():
            self.log("affinity lost after show; hiding the overlay so the bridge is not blinded")
            self.hide()
            return False
        return True

    def hide(self):
        if self.hwnd:
            _build_api().u32.ShowWindow(self.hwnd, SW_HIDE)
        self.rect = None

    def reassert_topmost(self):
        api = _build_api()
        api.u32.SetWindowPos(self.hwnd, api.C.c_void_p(-1), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.last_topmost = time.monotonic()

    # -- painting
    def set_row(self, rgb):
        self.row = rgb_row_to_dib(rgb)

    def paint_to(self, hdc):
        """Fill the window: the screen row under the strip (the overlay is invisible to the screen DC, so this is real game
        pixels), else the last pipe row, else black. Returns "screen", "row" or "black"."""
        api = _build_api()
        if not self.rect:
            return "black"
        _x, _y, w, h = self.rect
        api.g32.SetStretchBltMode(hdc, COLORONCOLOR)
        if self.screen_ok:
            sx, sy, sw, sh = source_rect(self.rect)
            scr = api.u32.GetDC(None)
            try:
                ok = scr and api.g32.StretchBlt(hdc, 0, 0, w, h, scr, sx, sy, sw, sh, SRCCOPY)
            finally:
                if scr: api.u32.ReleaseDC(None, scr)
            if ok:
                return "screen"
            self.log("screen read failed, using the bridge's row")
            self.screen_ok = False                  # retried at the next housekeeping tick
        if self.row:
            n, dib = self.row
            bih = api.BITMAPINFOHEADER(40, n, 1, 1, 24, 0, len(dib), 0, 0, 0, 0)      # positive height = bottom-up, one row anyway
            buf = api.C.create_string_buffer(dib, len(dib))
            if api.g32.StretchDIBits(hdc, 0, 0, w, h, 0, 0, n, 1, buf, api.C.byref(bih), DIB_RGB_COLORS, SRCCOPY):
                return "row"
        api.g32.PatBlt(hdc, 0, 0, w, h, BLACKNESS)
        return "black"

    def paint(self):
        """Paint now through the window DC (the timer path). Returns what paint_to returned, or None when hidden."""
        api = _build_api()
        if not self.hwnd or not self.rect or not api.u32.IsWindowVisible(self.hwnd):
            return None
        hdc = api.u32.GetDC(self.hwnd)
        if not hdc:
            return None
        try:
            return self.paint_to(hdc)
        finally:
            api.u32.ReleaseDC(self.hwnd, hdc)

    # -- timers (window thread)
    def on_timer(self, timer_id):
        now = time.monotonic()
        if timer_id == TIMER_PAINT:
            with self.lock:
                want, self.pending = self.pending, None
                last = self.last_update
            if is_stale(time.monotonic(), last):
                if self.rect: self.hide()
                return
            if want is not None and want != self.rect:
                self.place(want)
            self.paint()
        elif timer_id == TIMER_HOUSE:
            self.screen_ok = True
            if self.rect:
                if self.affinity() != WDA_EXCLUDEFROMCAPTURE and not self.apply_affinity():
                    self.log("affinity changed under us; hiding")
                    self.hide()
                elif now - self.last_topmost >= TOPMOST_SECONDS:
                    self.reassert_topmost()

    def request(self, rect):
        """Thread-safe: ask the window thread to show the overlay at this screen rect."""
        with self.lock:
            self.pending = tuple(rect)
            self.last_update = time.monotonic()

    def post_close(self):
        """Thread-safe: ask the window to close (PostMessage is allowed from any thread)."""
        if self.hwnd:
            _build_api().u32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def pump(self):
        """Dispatch queued messages without blocking (in-process tests). False once WM_QUIT was seen."""
        api = _build_api()
        msg = api.W.MSG()
        while api.u32.PeekMessageW(api.C.byref(msg), None, 0, 0, 1):
            if msg.message == 0x12:
                return False
            api.u32.TranslateMessage(api.C.byref(msg)); api.u32.DispatchMessageW(api.C.byref(msg))
        return True

    def destroy(self):
        if self.hwnd:
            h, self.hwnd = self.hwnd, None
            _build_api().u32.DestroyWindow(h)
            _windows.pop(h, None)


# ---------------------------------------------------------------- the helper process
class FileLog(object):
    """Append-only log for a process that may have no console (pythonw): never raises, never prints."""

    def __init__(self, path):
        self.path = path
        if path:
            try:
                if os.path.getsize(path) > LOG_LIMIT:
                    os.remove(path)
            except OSError:
                pass

    def __call__(self, msg):
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write("%s win32_mask[%d] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), msg))
        except OSError:
            pass


def _read_stdin(win, log):
    """Reader thread: feed lines to the window; close it when the pipe ends."""
    try:
        stream = sys.stdin.buffer if getattr(sys, "stdin", None) is not None else os.fdopen(0, "rb")
        for raw in iter(stream.readline, b""):
            got = parse_line(raw)
            if got is None:
                continue
            if got[0] == "rect":
                x, y, w, h, _cap = got[1]
                win.request((x, y, w, h))
            else:
                win.set_row(got[1])
    except Exception as e:
        log("stdin reader stopped: %r" % (e,))
    win.post_close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wowmentor.host.win32_mask")
    ap.add_argument("--log", default=None, help="log file (the helper never writes to stdout)")
    args = ap.parse_args(argv)
    log = FileLog(args.log)
    if sys.platform != "win32":
        log("not Windows"); return EXIT_UNSUPPORTED
    build = getattr(sys.getwindowsversion(), "build", 0)
    if not should_enable(build):
        log("Windows build %s is older than %d: WDA_EXCLUDEFROMCAPTURE is unavailable, so the mask would blind the bridge; not showing it"
            % (build, MIN_BUILD))
        return EXIT_UNSUPPORTED
    set_dpi_aware()
    win = MaskWindow(log=log)
    try:
        if not win.create():
            log("the overlay could not be excluded from capture; not showing it")
            return EXIT_UNSUPPORTED
    except Exception as e:
        log("could not create the window: %r" % (e,))
        return 1
    win.timers()
    log("ready, build %s" % build)
    threading.Thread(target=_read_stdin, args=(win, log), daemon=True).start()
    api = _build_api()
    msg = api.W.MSG()
    while api.u32.GetMessageW(api.C.byref(msg), None, 0, 0) > 0:
        api.u32.TranslateMessage(api.C.byref(msg)); api.u32.DispatchMessageW(api.C.byref(msg))
    log("exiting")
    return 0


# ---------------------------------------------------------------- the base.Mask the bridge uses
def _default_build():
    try:
        return sys.getwindowsversion().build
    except Exception:
        return None


class Win32Mask(base.Mask):
    """Drives the helper process. Mirrors DarwinMask: lazy spawn from update(), restart if it dies, never raises. Extra: a restart
    backoff so a crashing helper cannot respawn 10 times a second, and a permanent 'unavailable' when it reports EXIT_UNSUPPORTED.

    Injectable for tests: platform, build, spawn(cmd, **kw) -> Popen-like, geometry(hwnd) -> (ox, oy, w, h), find_game() -> handle."""

    def __init__(self, platform=None, build=None, spawn=None, geometry=None, find_game=None, clock=None):
        self.platform = platform or sys.platform
        self.build = _default_build() if build is None else build
        self._spawn_fn = spawn or subprocess.Popen
        self._geometry = geometry or client_geometry
        self._find_game = find_game
        self.clock = clock or time.time
        self.cfg = {}
        self.proc = None
        self.sent = None
        self.sent_at = 0.0
        self.row_at = 0.0
        self.started_at = 0.0
        self.failures = 0
        self.next_spawn = 0.0
        self.unsupported = False
        self.handle = None
        self.handle_at = 0.0

    def available(self):
        return self.platform == "win32" and should_enable(self.build) and not self.unsupported

    def start(self, cfg):
        self.cfg = dict(cfg or {})

    # -- game geometry (parent side, so the helper needs no window lookup)
    def _game_hwnd(self, now):
        if self.handle is None or now - self.handle_at > 5.0:
            self.handle_at = now
            try:
                find = self._find_game
                if find is None:
                    from .. import host as hostmod
                    find = hostmod.get().capture.find_game
                self.handle = hwnd_of(find())
            except Exception:
                self.handle = None
        return self.handle

    def _client(self, now):
        hwnd = self._game_hwnd(now)
        if not hwnd:
            return None
        geo = self._geometry(hwnd)
        if geo is None:
            self.handle = None                      # window gone: look again next time
        return geo

    def _log_path(self):
        try:
            from .. import config as cfgmod
            d = cfgmod.log_dir()
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, "mask.log")
        except Exception:
            return None

    def _spawn(self, now):
        """Start the helper. PYTHONPATH makes `-m wowmentor...` work from any cwd; CREATE_NO_WINDOW avoids a console flash."""
        from ..install import PKG_ROOT
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([PKG_ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env["PYTHONIOENCODING"] = "utf-8"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.proc = self._spawn_fn(build_command(log_path=self._log_path()), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=env, creationflags=flags)
        self.started_at, self.sent = now, None

    def _check_dead(self, now):
        """If the helper exited, learn why and set the restart delay. Returns True when it is alive."""
        p = self.proc
        if p is None:
            return False
        code = p.poll()
        if code is None:
            if now - self.started_at > RUN_OK_SECONDS:
                self.failures = 0
            return True
        self.proc = None
        if code == EXIT_UNSUPPORTED:
            self.unsupported = True
            print("mask: this Windows cannot hide the overlay from capture; running without it", flush=True)
            return False
        if now - self.started_at < RUN_OK_SECONDS:
            self.failures += 1
        else:
            self.failures = 0
        self.next_spawn = now + restart_delay(self.failures)
        return False

    def update(self, strip_rect, capture_width, row_provider=None):
        try:
            if not self.available():
                return
            now = self.clock()
            alive = self._check_dead(now)
            if not alive and (self.unsupported or now < self.next_spawn):
                return
            geo = self._client(now)
            if geo is None:
                return
            rect = compute_frame_rect(tuple(strip_rect), capture_width, (geo[0], geo[1]), geo[2])
            msg = format_rect_line(rect)
            row = None
            if row_provider is not None and now - self.row_at >= ROW_INTERVAL:
                try: row = row_provider()
                except Exception: row = None
            fresh = self.sent == msg and alive and now - self.sent_at < KEEPALIVE_SECONDS
            if fresh and row is None:
                return
            if not alive:
                self._spawn(now)
            if self.sent != msg or now - self.sent_at >= KEEPALIVE_SECONDS or not alive:
                self.proc.stdin.write(msg.encode("ascii")); self.sent, self.sent_at = msg, now
            if row:
                self.proc.stdin.write(b"R " + bytes(row) + b"\n"); self.row_at = now
            self.proc.stdin.flush()
        except Exception as e:
            print("mask error:", e, flush=True)
            self.proc = None
            self.failures += 1
            self.next_spawn = self.clock() + restart_delay(self.failures)

    def stop(self):
        p, self.proc, self.sent = self.proc, None, None
        if p is not None:
            try: p.stdin.close()                    # the helper exits when stdin closes
            except Exception: pass
            try: p.wait(timeout=2)
            except Exception:
                try: p.kill()
                except Exception: pass


def make():
    return Win32Mask()


if __name__ == "__main__":
    sys.exit(main())
