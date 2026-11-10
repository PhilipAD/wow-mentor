"""Windows screen capture: reads the top rows of the WoW window's client area with GDI, standard library only (ctypes).

Why this way: WoW draws with Direct3D, so a window DC (or PrintWindow) comes back black. The screen DC holds what the desktop
compositor (DWM) shows, so a BitBlt of the client rectangle from it returns the real pixels in Windowed and Windowed (Fullscreen)
mode, needs no permission prompt and no admin rights. It cannot see true exclusive fullscreen (black) and it sees whatever
window covers the strip; both are reported through `last_error` rather than guessed around. Nothing here sends input to the game.

Layout of this file:
  * pure helpers (geometry, window choice, blocked-frame test, struct builders) that run and are tested on every OS;
  * `_Win32Api`, the only code that touches ctypes handles, bound lazily on first use (never at import);
  * `Win32Capture`, the `base.Capture` implementation, which only talks to an api object (so tests inject a fake one).

Docs behind the non-obvious choices:
  BitBlt / SRCCOPY (CAPTUREBLT is left out on purpose: it would also copy layered windows such as our own overlay)
    https://learn.microsoft.com/en-us/windows/win32/api/wingdi/nf-wingdi-bitblt
  top-down 32 bit DIB section (negative biHeight), GdiFlush before reading the bits
    https://learn.microsoft.com/en-us/windows/win32/api/wingdi/nf-wingdi-createdibsection
  DPI awareness, per-monitor v2 (-4), ERROR_ACCESS_DENIED when already set
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setprocessdpiawarenesscontext
  DWMWA_CLOAKED (14): a window on another virtual desktop is visible to IsWindowVisible but cloaked
    https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwmwindowattribute
  process list without external tools
    https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-createtoolhelp32snapshot
"""
import ctypes, re, sys, threading

from . import base

# ---------------------------------------------------------------- constants
GAME_CLASSES = ("GxWindowClass",)               # the class WoW's client registers (all retail/classic builds)
GAME_TITLE = "World of Warcraft"                # fallback: a window whose title contains this (case-insensitive)
# A browser tab titled "World of Warcraft - Wowhead" must never be mistaken for the game by title alone.
TITLE_ONLY_DENY_CLASSES = ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0", "MozillaWindowClass", "ApplicationFrameWindow",
                           "CabinetWClass", "Notepad", "ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS")
SRCCOPY = 0x00CC0020
DWMWA_CLOAKED = 14
DIB_RGB_COLORS = 0
TH32CS_SNAPPROCESS = 0x2
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
DPI_CONTEXT_PER_MONITOR_AWARE_V2 = -4
ERROR_ACCESS_DENIED = 5
E_ACCESSDENIED = 0x80070005
MIN_WINDOWS_MAJOR = 10                          # Windows 10 or newer (Windows 11 reports major 10 too)
WIN_REQUIRED = "The WoW Mentor bridge reads the game window with Windows APIs and needs Windows 10 or newer."

# Window as the capture sees it: everything the choice needs, nothing that needs a native handle.
WindowState = ("cloaked", "iconic", "client_w", "client_h", "origin_x", "origin_y")


class WindowInfo(object):
    """Plain record for one window: filled by the api, consumed by the pure selection functions below."""
    __slots__ = ("hwnd",) + WindowState

    def __init__(self, hwnd, cloaked=False, iconic=False, client_w=0, client_h=0, origin_x=0, origin_y=0):
        self.hwnd, self.cloaked, self.iconic = hwnd, bool(cloaked), bool(iconic)
        self.client_w, self.client_h, self.origin_x, self.origin_y = client_w, client_h, origin_x, origin_y

    @property
    def area(self):
        return max(self.client_w, 0) * max(self.client_h, 0)

    def __repr__(self):
        return "<WindowInfo %r %dx%d at %d,%d%s%s>" % (self.hwnd, self.client_w, self.client_h, self.origin_x, self.origin_y,
                                                     " cloaked" if self.cloaked else "", " iconic" if self.iconic else "")


# ---------------------------------------------------------------- pure helpers (tested on every OS)
def windows_supported(version):
    """True for Windows 10 or newer. `version` is sys.getwindowsversion() or any tuple whose first item is the major number."""
    try:
        return int(version[0]) >= MIN_WINDOWS_MAJOR
    except (TypeError, ValueError, IndexError):
        return False


def clamp_rows(rows, client_h):
    """Rows to read: at least 1 and never more than the window's client height; 0 when the client area is empty."""
    try:
        rows, client_h = int(rows), int(client_h)
    except (TypeError, ValueError):
        return 0
    if client_h <= 0 or rows <= 0:
        return 0
    return min(rows, client_h)


def client_rect_to_screen(origin, client_w, rows):
    """(x, y, w, h) in virtual-screen pixels of the top `rows` rows of a client area whose top-left corner is `origin` (the result
    of ClientToScreen(0, 0)). On multi-monitor setups x or y can be negative (a monitor left of or above the primary one)."""
    return int(origin[0]), int(origin[1]), int(client_w), int(rows)


def rect_inside(rect, virtual):
    """True if the whole (x, y, w, h) rect lies inside the virtual screen (x, y, w, h). BitBlt silently fills pixels outside the
    desktop with black, which would look like a working capture of an empty strip, so we refuse such a window instead."""
    x, y, w, h = rect
    vx, vy, vw, vh = virtual
    return w > 0 and h > 0 and vw > 0 and vh > 0 and x >= vx and y >= vy and x + w <= vx + vw and y + h <= vy + vh


def looks_blocked(raw):
    """True if captured BGRA bytes carry no colour at all (every B, G and R byte is zero; alpha is ignored because GDI leaves it 0 or
    255). A real game frame always has some non-black pixel in its top rows, so all-black means exclusive fullscreen, a protected
    surface or a dead window, not a valid strip."""
    if not raw:
        return True
    return not (raw[0::4].strip(b"\x00") or raw[1::4].strip(b"\x00") or raw[2::4].strip(b"\x00"))


def set_opaque(raw):
    """The same BGRA bytes with the alpha byte of every pixel set to 255, as a new bytes object. GDI does not define alpha, and
    downstream code that reads it (Frame.__getitem__) should see a normal opaque pixel."""
    buf = bytearray(raw)
    buf[3::4] = b"\xff" * (len(buf) // 4)
    return bytes(buf)


def matches_game(cls, title, class_names=GAME_CLASSES, title_part=GAME_TITLE):
    """0 = not the game, 2 = the game's window class, 1 = only the title matches (weaker, see TITLE_ONLY_DENY_CLASSES)."""
    if cls in class_names:
        return 2
    if title_part and title and title_part.lower() in title.lower() and cls not in TITLE_ONLY_DENY_CLASSES:
        return 1
    return 0


def pick_game(candidates):
    """The handle of the best window among `candidates`, a list of (rank, WindowInfo) with rank from `matches_game`.
    Visible is the caller's job. Cloaked (other virtual desktop), minimised and empty windows are dropped; a class match beats a
    title-only match; then the largest client area wins (ties: the lower handle, so the answer is stable). None if nothing is left."""
    usable = [(rank, w) for rank, w in candidates if rank and not w.cloaked and not w.iconic and w.area > 0]
    if not usable:
        return None
    best = max(usable, key=lambda rw: (rw[0], rw[1].area, -(rw[1].hwnd or 0)))
    return best[1].hwnd


def is_game_process(name):
    """True for WoW client executables: Wow.exe, WowClassic.exe, WowT.exe, WowB.exe, Wow-64.exe ... (never WoW Mentor's own tools)."""
    n = (name or "").strip().lower()
    return bool(re.match(r"^wow[a-z0-9_\-]*\.exe$", n)) and not n.startswith("wowmentor")


def make_dib_header(width, rows):
    """BITMAPINFOHEADER for a top-down (negative height), 32 bit, uncompressed DIB of width x rows. Top-down means the first byte
    of the bits is the top-left pixel, so no flipping is needed; 32 bit rows are DWORD aligned, so stride is exactly width * 4."""
    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth, hdr.biHeight = int(width), -int(rows)
    hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, 0          # BI_RGB
    return hdr


def set_dpi_awareness(set_context, set_shcore, set_legacy):
    """Make the process DPI aware, trying the newest API first, so every coordinate and BitBlt is in physical pixels (the strip's
    3x2 pixel cells must not be resampled). Each argument is a callable or None:
      set_context(-4) -> (ok, err)   SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2), Windows 10 1703+
      set_shcore(2)   -> hresult     SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE), Windows 8.1+
      set_legacy()    -> bool        SetProcessDPIAware(), system aware
    Returns a short mode name. "already-set" means a manifest or an earlier call fixed it (ERROR_ACCESS_DENIED), which is fine:
    the process is as aware as it will get. "none" means every call failed. Never raises."""
    if set_context is not None:
        try:
            ok, err = set_context(DPI_CONTEXT_PER_MONITOR_AWARE_V2)
            if ok:
                return "per-monitor-v2"
            if err == ERROR_ACCESS_DENIED:
                return "already-set"
        except Exception:
            pass
    if set_shcore is not None:
        try:
            hr = set_shcore(2) & 0xFFFFFFFF
            if hr == 0:
                return "per-monitor"
            if hr == E_ACCESSDENIED:
                return "already-set"
        except Exception:
            pass
    if set_legacy is not None:
        try:
            if set_legacy():
                return "system"
        except Exception:
            pass
    return "none"


# ---------------------------------------------------------------- native structures (plain ctypes, no windll / wintypes needed)
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32), ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_int32), ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * 260)]


# ---------------------------------------------------------------- DPI: once per process
_dpi_lock = threading.Lock()
_dpi_mode = None


def ensure_dpi_aware(libs=None):
    """Set DPI awareness once for the whole process, before the first window or DC call, and return the mode name. `libs` is a dict
    with "user32" and optionally "shcore" (tests pass fakes); by default the real DLLs are loaded here, lazily."""
    global _dpi_mode
    injected = libs is not None
    with _dpi_lock:
        if _dpi_mode is not None and not injected:
            return _dpi_mode
        if not injected:
            if sys.platform != "win32":
                raise base.HostUnsupported(WIN_REQUIRED)
            libs = {"user32": ctypes.WinDLL("user32", use_last_error=True)}
            try:
                libs["shcore"] = ctypes.WinDLL("shcore", use_last_error=True)
            except OSError:
                pass
        user32, shcore = libs.get("user32"), libs.get("shcore")

        def ctx(value):
            fn = user32.SetProcessDpiAwarenessContext      # AttributeError before Windows 10 1703 -> next fallback
            fn.argtypes, fn.restype = [ctypes.c_void_p], ctypes.c_int
            ok = fn(ctypes.c_void_p(value))
            return bool(ok), (None if ok else _last_error())

        def shc(value):
            fn = shcore.SetProcessDpiAwareness
            fn.argtypes, fn.restype = [ctypes.c_int], ctypes.c_long
            return fn(value)

        def legacy():
            fn = user32.SetProcessDPIAware
            fn.argtypes, fn.restype = [], ctypes.c_int
            return bool(fn())

        mode = set_dpi_awareness(ctx if user32 is not None else None, shc if shcore is not None else None,
                                 legacy if user32 is not None else None)
        if not injected:
            _dpi_mode = mode
        return mode


def _last_error():
    try:
        return ctypes.get_last_error()
    except Exception:
        return None


# ---------------------------------------------------------------- the only code that touches native handles
class _Surface(object):
    """A memory DC with one top-down 32 bit DIB section selected into it, reused until the size changes."""

    def __init__(self, api, screen_dc, width, rows):
        g = api.gdi32
        self.api, self.width, self.rows = api, width, rows
        self.dc = self.bitmap = self.old = None
        self.bits = ctypes.c_void_p()
        self.dc = g.CreateCompatibleDC(screen_dc)
        if not self.dc:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            hdr = make_dib_header(width, rows)
            self.bitmap = g.CreateDIBSection(screen_dc, ctypes.byref(hdr), DIB_RGB_COLORS, ctypes.byref(self.bits), None, 0)
            if not self.bitmap or not self.bits.value:
                raise ctypes.WinError(ctypes.get_last_error())
            self.old = g.SelectObject(self.dc, self.bitmap)
        except BaseException:
            self.close()
            raise

    def close(self):
        """Deselect and delete everything, in the order GDI requires; safe to call twice."""
        g = self.api.gdi32
        if self.dc and self.old:
            g.SelectObject(self.dc, self.old)
        self.old = None
        if self.bitmap:
            g.DeleteObject(self.bitmap)
            self.bitmap = None
        if self.dc:
            g.DeleteDC(self.dc)
            self.dc = None


class _Win32Api(object):
    """Thin, lazily bound wrapper over user32, gdi32, dwmapi and kernel32. Every public method returns plain Python values.
    One thread at a time (Win32Capture holds a lock around its use)."""

    def __init__(self):
        self.user32 = self.gdi32 = self.dwm = self.kernel32 = None
        self._surface = None
        self.dpi_mode = None

    # -- binding
    def bind(self):
        """Load the DLLs, declare 64-bit safe prototypes and set DPI awareness (once per process). Idempotent. Raises HostUnsupported
        off Windows, OSError when a library is missing."""
        if self.user32 is not None:
            return
        if sys.platform != "win32":
            raise base.HostUnsupported(WIN_REQUIRED)
        c = ctypes
        vp, i32, u32 = c.c_void_p, c.c_int, c.c_uint32
        self.dpi_mode = ensure_dpi_aware()          # before any window or DC call
        user32 = c.WinDLL("user32", use_last_error=True)
        gdi32 = c.WinDLL("gdi32", use_last_error=True)
        kernel32 = c.WinDLL("kernel32", use_last_error=True)
        try:
            dwm = c.WinDLL("dwmapi", use_last_error=True)
        except OSError:
            dwm = None

        def proto(lib, name, restype, *argtypes):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, list(argtypes)

        self.enum_proc = c.WINFUNCTYPE(i32, vp, c.c_ssize_t)
        proto(user32, "EnumWindows", i32, self.enum_proc, c.c_ssize_t)
        proto(user32, "IsWindow", i32, vp)
        proto(user32, "IsWindowVisible", i32, vp)
        proto(user32, "IsIconic", i32, vp)
        proto(user32, "GetClassNameW", i32, vp, c.c_wchar_p, i32)
        proto(user32, "GetWindowTextW", i32, vp, c.c_wchar_p, i32)
        proto(user32, "GetClientRect", i32, vp, c.POINTER(RECT))
        proto(user32, "ClientToScreen", i32, vp, c.POINTER(POINT))
        proto(user32, "GetForegroundWindow", vp)
        proto(user32, "GetSystemMetrics", i32, i32)
        proto(user32, "GetDC", vp, vp)
        proto(user32, "ReleaseDC", i32, vp, vp)
        proto(gdi32, "CreateCompatibleDC", vp, vp)
        proto(gdi32, "CreateDIBSection", vp, vp, vp, u32, c.POINTER(vp), vp, u32)
        proto(gdi32, "SelectObject", vp, vp, vp)
        proto(gdi32, "BitBlt", i32, vp, i32, i32, i32, i32, vp, i32, i32, u32)
        proto(gdi32, "DeleteObject", i32, vp)
        proto(gdi32, "DeleteDC", i32, vp)
        proto(gdi32, "GdiFlush", i32)
        proto(kernel32, "CreateToolhelp32Snapshot", vp, u32, u32)
        proto(kernel32, "Process32FirstW", i32, vp, c.POINTER(PROCESSENTRY32W))
        proto(kernel32, "Process32NextW", i32, vp, c.POINTER(PROCESSENTRY32W))
        proto(kernel32, "CloseHandle", i32, vp)
        if dwm is not None:
            proto(dwm, "DwmGetWindowAttribute", c.c_long, vp, u32, vp, u32)
        self.user32, self.gdi32, self.dwm, self.kernel32 = user32, gdi32, dwm, kernel32

    # -- windows
    def enum_windows(self):
        """[(hwnd, class name, title)] for every visible top-level window."""
        self.bind()
        out = []

        def cb(hwnd, _lparam):
            try:
                if self.user32.IsWindowVisible(hwnd):
                    cls, title = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
                    self.user32.GetClassNameW(hwnd, cls, 256)
                    self.user32.GetWindowTextW(hwnd, title, 256)
                    out.append((hwnd, cls.value, title.value))
            except Exception:
                pass
            return 1                                    # keep enumerating whatever happens

        proc = self.enum_proc(cb)                       # keep a reference until EnumWindows returns
        self.user32.EnumWindows(proc, 0)
        return out

    def window_info(self, hwnd):
        """WindowInfo for `hwnd`, or None if it no longer exists."""
        self.bind()
        u = self.user32
        if not hwnd or not u.IsWindow(hwnd):
            return None
        info = WindowInfo(hwnd)
        info.iconic = bool(u.IsIconic(hwnd))
        if self.dwm is not None:
            cloaked = ctypes.c_uint32(0)
            if self.dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4) == 0:
                info.cloaked = bool(cloaked.value)
        rc, pt = RECT(), POINT(0, 0)
        if not u.GetClientRect(hwnd, ctypes.byref(rc)) or not u.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        info.client_w, info.client_h = rc.right - rc.left, rc.bottom - rc.top
        info.origin_x, info.origin_y = pt.x, pt.y         # signed: monitors left of or above the primary one are negative
        return info

    def foreground(self):
        self.bind()
        return self.user32.GetForegroundWindow() or 0

    def window_text(self, hwnd):
        """(class name, title) of one window."""
        self.bind()
        cls, title = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, cls, 256)
        self.user32.GetWindowTextW(hwnd, title, 256)
        return cls.value, title.value

    def virtual_screen(self):
        """(x, y, w, h) of the whole desktop in physical pixels (x and y are negative when a monitor sits left of or above the primary)."""
        self.bind()
        m = self.user32.GetSystemMetrics
        return m(SM_XVIRTUALSCREEN), m(SM_YVIRTUALSCREEN), m(SM_CXVIRTUALSCREEN), m(SM_CYVIRTUALSCREEN)

    # -- pixels
    def blit(self, x, y, width, rows):
        """BGRA bytes (width * rows * 4, top row first) of the screen rectangle. Raises OSError when GDI fails."""
        self.bind()
        u, g = self.user32, self.gdi32
        screen = u.GetDC(None)
        if not screen:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            s = self._surface
            if s is None or s.width != width or s.rows != rows:
                if s is not None:
                    s.close()
                    self._surface = None
                s = self._surface = _Surface(self, screen, width, rows)
            if not g.BitBlt(s.dc, 0, 0, width, rows, screen, x, y, SRCCOPY):
                raise ctypes.WinError(ctypes.get_last_error())
            g.GdiFlush()                                 # GDI batches; flush before reading the DIB memory ourselves
            return ctypes.string_at(s.bits.value, width * rows * 4)
        finally:
            u.ReleaseDC(None, screen)

    # -- processes
    def process_names(self):
        """Executable names of every running process (a toolhelp snapshot, no external tools)."""
        self.bind()
        k = self.kernel32
        snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap is None or snap == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        names = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            more = k.Process32FirstW(snap, ctypes.byref(entry))
            while more:
                names.append(entry.szExeFile)
                more = k.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k.CloseHandle(snap)
        return names

    def close(self):
        """Free the reused DIB section and memory DC. Idempotent."""
        if self._surface is not None:
            try:
                self._surface.close()
            finally:
                self._surface = None


# ---------------------------------------------------------------- the capture
class Win32Capture(base.Capture):
    """base.Capture for Windows. `api` is injectable for tests; `class_names` and `title_part` override how the game window is
    recognised (module defaults: class GxWindowClass, or a title containing "World of Warcraft"). After a `grab_top` or `find_game`
    that returns None, `last_error` holds one line saying why; it is "" after a success. One thread at a time (a lock enforces it)."""

    def __init__(self, api=None, class_names=None, title_part=None):
        self._api = api
        self.class_names = tuple(class_names) if class_names else GAME_CLASSES
        self.title_part = GAME_TITLE if title_part is None else title_part
        self.last_error = ""
        self._lock = threading.RLock()

    @property
    def api(self):
        if self._api is None:
            self._api = _Win32Api()
        return self._api

    # -- queries
    def available(self):
        """(ok, reason). Never raises."""
        try:
            if sys.platform != "win32":
                return False, WIN_REQUIRED
            getver = getattr(sys, "getwindowsversion", None)
            if getver is not None and not windows_supported(getver()):
                return False, WIN_REQUIRED
            if sys.version_info < (3, 9):
                return False, "WoW Mentor needs Python 3.9 or newer."
            self.api.bind()
            return True, ""
        except Exception as e:
            return False, "The Windows screen capture could not start (%s)." % (e,)

    def find_game(self):
        """hwnd (int) of the biggest visible, uncloaked, non-minimised WoW window, or None. Raises HostUnsupported off Windows."""
        with self._lock:
            api = self.api
            cands = []
            for hwnd, cls, title in api.enum_windows():
                rank = matches_game(cls, title, self.class_names, self.title_part)
                if not rank:
                    continue
                info = api.window_info(hwnd)
                if info is not None:
                    cands.append((rank, info))
            found = pick_game(cands)
            self.last_error = "" if found else "no visible World of Warcraft window (minimised, on another desktop or not running)"
            return found

    def game_running(self):
        """True if a visible WoW window exists, or a Wow*.exe process does (the window may not exist yet). Never raises."""
        try:
            with self._lock:
                api = self.api
                for hwnd, cls, title in api.enum_windows():
                    if matches_game(cls, title, self.class_names, self.title_part):
                        return True
                return any(is_game_process(n) for n in api.process_names())
        except Exception:
            return False

    def foreground_is_game(self, handle=None):
        """True if the foreground window is the game (`handle`, or any window that matches), False if another window is, None if
        unknown. Never raises, needs no permission."""
        try:
            with self._lock:
                api = self.api
                fg = api.foreground()
                if not fg:
                    return None
                if handle and fg == handle:
                    return True
                cls, title = api.window_text(fg)
                return bool(matches_game(cls, title, self.class_names, self.title_part))
        except Exception:
            return None

    # -- pixels
    def grab_top(self, handle, rows):
        """Frame (BGRA, top row first, stride width * 4, alpha 255) of the top `rows` rows of the client area, or None with
        `last_error` set: window gone, minimised, cloaked, off-screen, GDI failure, or an all-black strip (exclusive fullscreen)."""
        from ..bridge.strip import Frame
        with self._lock:
            self.last_error = ""
            api = self.api
            try:
                info = api.window_info(handle)
                if info is None:
                    return self._fail("the game window is gone")
                if info.iconic:
                    return self._fail("the game window is minimised")
                if info.cloaked:
                    return self._fail("the game window is on another virtual desktop")
                n = clamp_rows(rows, info.client_h)
                if info.client_w <= 0 or n <= 0:
                    return self._fail("the game window has an empty client area")
                rect = client_rect_to_screen((info.origin_x, info.origin_y), info.client_w, n)
                if not rect_inside(rect, api.virtual_screen()):
                    return self._fail("the game window is off-screen (move it onto a monitor)")
                raw = api.blit(*rect)
            except base.HostUnsupported:
                raise
            except Exception as e:
                return self._fail("screen capture failed (%s)" % (e,))
            if looks_blocked(raw):
                return self._fail("the top of the game window is all black: use Windowed or Windowed (Fullscreen) in WoW's graphics "
                                  "settings, and keep other windows off its top edge")
            return Frame(set_opaque(raw), rect[2], rect[3], rect[2] * 4)

    def _fail(self, why):
        self.last_error = why
        return None

    def close(self):
        """Release the reused GDI objects. The capture can be used again afterwards (it rebuilds them)."""
        with self._lock:
            if self._api is not None:
                self._api.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
