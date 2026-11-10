"""Windows capture: the pure logic and the capture's decisions run everywhere with a fake api; the real GDI path runs only on
Windows (a windows-latest runner), against a real window this test creates and paints."""
import ctypes, os, sys, time, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wowmentor.bridge import strip
from wowmentor.host import base, win32_capture as wc
from wowmentor.host.win32_capture import WindowInfo, Win32Capture
from test_decoder import encode


def win(hwnd=1, w=800, h=600, x=0, y=0, **kw):
    return WindowInfo(hwnd, client_w=w, client_h=h, origin_x=x, origin_y=y, **kw)


class PureHelperTests(unittest.TestCase):
    def test_clamp_rows(self):
        self.assertEqual(wc.clamp_rows(220, 600), 220)
        self.assertEqual(wc.clamp_rows(220, 100), 100)
        self.assertEqual(wc.clamp_rows(0, 100), 0)
        self.assertEqual(wc.clamp_rows(10, 0), 0)
        self.assertEqual(wc.clamp_rows(10, -5), 0)
        self.assertEqual(wc.clamp_rows("x", 10), 0)

    def test_client_rect_to_screen_keeps_negative_origins(self):
        self.assertEqual(wc.client_rect_to_screen((-1920, -40), 1920, 220), (-1920, -40, 1920, 220))
        self.assertEqual(wc.client_rect_to_screen((8, 31), 1280, 8), (8, 31, 1280, 8))

    def test_rect_inside_the_virtual_screen(self):
        desk = (-1920, 0, 3840, 1080)                 # a monitor left of the primary one
        self.assertTrue(wc.rect_inside((-1920, 0, 1920, 220), desk))
        self.assertTrue(wc.rect_inside((0, 0, 1920, 8), desk))
        self.assertFalse(wc.rect_inside((-2000, 0, 100, 8), desk))
        self.assertFalse(wc.rect_inside((1900, 0, 100, 8), desk))
        self.assertFalse(wc.rect_inside((0, 1078, 100, 8), desk))
        self.assertFalse(wc.rect_inside((0, 0, 0, 8), desk))
        self.assertFalse(wc.rect_inside((0, 0, 10, 8), (0, 0, 0, 0)))

    def test_looks_blocked(self):
        self.assertTrue(wc.looks_blocked(b""))
        self.assertTrue(wc.looks_blocked(bytes(4 * 100)))
        self.assertTrue(wc.looks_blocked(b"\x00\x00\x00\xff" * 100))        # black with opaque alpha is still black
        self.assertFalse(wc.looks_blocked(bytes(4 * 99) + b"\x00\x00\x01\x00"))
        self.assertFalse(wc.looks_blocked(bytes(4 * 99) + b"\x00\x01\x00\x00"))
        self.assertFalse(wc.looks_blocked(bytes(4 * 99) + b"\x01\x00\x00\x00"))

    def test_set_opaque_touches_only_alpha(self):
        out = wc.set_opaque(bytes([1, 2, 3, 0, 4, 5, 6, 7]))
        self.assertEqual(out, bytes([1, 2, 3, 255, 4, 5, 6, 255]))

    def test_windows_supported(self):
        self.assertTrue(wc.windows_supported((10, 0, 19045)))
        self.assertTrue(wc.windows_supported((11, 0)))
        self.assertFalse(wc.windows_supported((6, 3, 9600)))
        self.assertFalse(wc.windows_supported(None))

    def test_matches_game_prefers_class_over_title(self):
        self.assertEqual(wc.matches_game("GxWindowClass", ""), 2)
        self.assertEqual(wc.matches_game("Whatever", "World of Warcraft"), 1)
        self.assertEqual(wc.matches_game("Whatever", "my world of warcraft notes"), 1)
        self.assertEqual(wc.matches_game("Chrome_WidgetWin_1", "World of Warcraft - Wowhead"), 0)
        self.assertEqual(wc.matches_game("Other", "Notepad"), 0)
        self.assertEqual(wc.matches_game("Mine", "x", class_names=("Mine",), title_part=""), 2)
        self.assertEqual(wc.matches_game("Other", "World of Warcraft", class_names=("Mine",), title_part=""), 0)

    def test_pick_game(self):
        small, big = win(1, 800, 600), win(2, 1920, 1080)
        self.assertEqual(wc.pick_game([(2, small), (2, big)]), 2)
        self.assertEqual(wc.pick_game([(1, big), (2, small)]), 1)      # a class match beats a bigger title-only match
        self.assertEqual(wc.pick_game([(2, win(3, 1920, 1080, cloaked=True)), (2, small)]), 1)
        self.assertEqual(wc.pick_game([(2, win(3, 1920, 1080, iconic=True))]), None)
        self.assertEqual(wc.pick_game([(2, win(3, 0, 0))]), None)
        self.assertEqual(wc.pick_game([(0, big)]), None)
        self.assertEqual(wc.pick_game([]), None)
        self.assertEqual(wc.pick_game([(2, win(9, 100, 100)), (2, win(5, 100, 100))]), 5)    # tie: the lower handle

    def test_is_game_process(self):
        for n in ("Wow.exe", "WowClassic.exe", "WowT.exe", "WowB.exe", "Wow-64.exe", "wow.exe"):
            self.assertTrue(wc.is_game_process(n), n)
        for n in ("", None, "WowMentor.exe", "wowhead-client.dll", "show.exe", "chrome.exe", "Wow"):
            self.assertFalse(wc.is_game_process(n), n)

    def test_struct_sizes_and_the_dib_header(self):
        self.assertEqual(ctypes.sizeof(wc.BITMAPINFOHEADER), 40)
        self.assertEqual(ctypes.sizeof(wc.RECT), 16)
        self.assertEqual(ctypes.sizeof(wc.POINT), 8)
        if ctypes.sizeof(ctypes.c_wchar) == 2:          # wchar_t is 2 bytes only on Windows; elsewhere the size differs by design
            self.assertEqual(ctypes.sizeof(wc.PROCESSENTRY32W), 568 if ctypes.sizeof(ctypes.c_void_p) == 8 else 556)
        else:
            self.assertEqual(wc.PROCESSENTRY32W.szExeFile.offset, 44 if ctypes.sizeof(ctypes.c_void_p) == 8 else 36)
        h = wc.make_dib_header(1920, 220)
        self.assertEqual((h.biSize, h.biWidth, h.biHeight, h.biPlanes, h.biBitCount, h.biCompression), (40, 1920, -220, 1, 32, 0))


class DpiTests(unittest.TestCase):
    def test_newest_api_wins(self):
        calls = []
        mode = wc.set_dpi_awareness(lambda v: (calls.append(("ctx", v)) or True, None), lambda v: calls.append("shc") or 0, lambda: calls.append("leg") or True)
        self.assertEqual(mode, "per-monitor-v2")
        self.assertEqual(calls, [("ctx", -4)])

    def test_falls_back_in_order(self):
        def no_ctx(v):
            raise AttributeError("SetProcessDpiAwarenessContext")
        self.assertEqual(wc.set_dpi_awareness(no_ctx, lambda v: 0, lambda: True), "per-monitor")
        self.assertEqual(wc.set_dpi_awareness(lambda v: (False, 87), lambda v: 0x80004005, lambda: True), "system")
        self.assertEqual(wc.set_dpi_awareness(None, None, lambda: False), "none")
        self.assertEqual(wc.set_dpi_awareness(None, None, None), "none")

    def test_access_denied_means_already_set(self):
        self.assertEqual(wc.set_dpi_awareness(lambda v: (False, wc.ERROR_ACCESS_DENIED), None, None), "already-set")
        self.assertEqual(wc.set_dpi_awareness(None, lambda v: 0x80070005, None), "already-set")

    def test_ensure_dpi_aware_with_fake_libraries(self):
        class User32(object):
            def __init__(self):
                self.tried = []

            @property
            def SetProcessDpiAwarenessContext(self):
                self.tried.append("ctx")
                return lambda v: 1

        self.assertEqual(wc.ensure_dpi_aware({"user32": User32()}), "per-monitor-v2")

    def test_off_windows_it_is_unsupported(self):
        with mock.patch.object(wc, "_dpi_mode", None), mock.patch.object(sys, "platform", "linux"):
            with self.assertRaises(base.HostUnsupported):
                wc.ensure_dpi_aware()


class FakeApi(object):
    """Stands in for _Win32Api: records the blit and answers from plain data."""

    def __init__(self, windows=(), infos=None, virtual=(0, 0, 1920, 1080), pixels=None, procs=(), fg=0, texts=None):
        self.windows, self.infos, self.virtual, self.procs, self.fg = list(windows), dict(infos or {}), virtual, list(procs), fg
        self.texts, self.pixels, self.blits, self.bound, self.closed = dict(texts or {}), pixels, [], 0, 0

    def bind(self):
        self.bound += 1

    def enum_windows(self):
        return list(self.windows)

    def window_info(self, hwnd):
        return self.infos.get(hwnd)

    def virtual_screen(self):
        return self.virtual

    def blit(self, x, y, w, rows):
        self.blits.append((x, y, w, rows))
        if isinstance(self.pixels, Exception):
            raise self.pixels
        return self.pixels if self.pixels is not None else bytes([10, 20, 30, 0]) * (w * rows)

    def process_names(self):
        return list(self.procs)

    def foreground(self):
        return self.fg

    def window_text(self, hwnd):
        return self.texts.get(hwnd, ("Other", "x"))

    def close(self):
        self.closed += 1


class CaptureDecisionTests(unittest.TestCase):
    def cap(self, **kw):
        self.api = FakeApi(**kw)
        return Win32Capture(api=self.api)

    def test_find_game_picks_the_biggest_class_match(self):
        cap = self.cap(windows=[(1, "GxWindowClass", "World of Warcraft"), (2, "GxWindowClass", "World of Warcraft"), (3, "Notepad", "notes")],
                       infos={1: win(1, 800, 600), 2: win(2, 1920, 1080), 3: win(3, 3000, 2000)})
        self.assertEqual(cap.find_game(), 2)
        self.assertEqual(cap.last_error, "")

    def test_find_game_none_has_a_reason(self):
        cap = self.cap(windows=[(1, "GxWindowClass", "World of Warcraft")], infos={1: win(1, iconic=True)})
        self.assertIsNone(cap.find_game())
        self.assertIn("no visible", cap.last_error)

    def test_find_game_overrides(self):
        windows = [(7, "MyClass", "something")]
        infos = {7: win(7)}
        self.assertIsNone(self.cap(windows=windows, infos=infos).find_game())
        api = FakeApi(windows=windows, infos=infos)
        self.assertEqual(Win32Capture(api=api, class_names=["MyClass"]).find_game(), 7)
        api = FakeApi(windows=[(8, "Z", "My Game Window")], infos={8: win(8)})
        self.assertEqual(Win32Capture(api=api, title_part="game window").find_game(), 8)

    def test_game_running_by_window_or_process(self):
        self.assertTrue(self.cap(windows=[(1, "GxWindowClass", "")]).game_running())
        self.assertTrue(self.cap(procs=["explorer.exe", "WowClassic.exe"]).game_running())
        self.assertFalse(self.cap(procs=["explorer.exe", "chrome.exe"]).game_running())

    def test_game_running_never_raises(self):
        api = FakeApi()
        api.enum_windows = mock.Mock(side_effect=OSError("boom"))
        self.assertFalse(Win32Capture(api=api).game_running())

    def test_foreground_is_game(self):
        cap = self.cap(fg=5, texts={5: ("GxWindowClass", "World of Warcraft")})
        self.assertTrue(cap.foreground_is_game(5))
        self.assertTrue(cap.foreground_is_game())
        self.assertTrue(cap.foreground_is_game(99))          # another WoW window is still WoW
        cap = self.cap(fg=6, texts={6: ("Chrome_WidgetWin_1", "Wowhead")})
        self.assertFalse(cap.foreground_is_game(5))
        self.assertIsNone(self.cap(fg=0).foreground_is_game())

    def test_foreground_never_raises(self):
        api = FakeApi()
        api.foreground = mock.Mock(side_effect=OSError("boom"))
        self.assertIsNone(Win32Capture(api=api).foreground_is_game())

    def test_grab_top_returns_an_opaque_bgra_frame(self):
        cap = self.cap(infos={1: win(1, 640, 480, x=100, y=50)})
        f = cap.grab_top(1, 220)
        self.assertEqual(self.api.blits, [(100, 50, 640, 220)])
        self.assertEqual((f.width, f.height, f.stride, f.size), (640, 220, 2560, (640, 220)))
        self.assertEqual(len(f.raw), 640 * 220 * 4)
        self.assertEqual(f[0, 0], (30, 20, 10, 255))         # (r, g, b, a) from BGRA bytes 10, 20, 30
        self.assertEqual(cap.last_error, "")

    def test_grab_top_clamps_rows_and_handles_negative_origin(self):
        cap = self.cap(infos={1: win(1, 400, 60, x=-1920, y=-30)}, virtual=(-1920, -1080, 3840, 2160))
        f = cap.grab_top(1, 220)
        self.assertEqual(self.api.blits, [(-1920, -30, 400, 60)])
        self.assertEqual(f.height, 60)

    def test_grab_top_failures_return_none_with_a_reason(self):
        cases = [
            ("gone", {}, "gone"),
            ("minimised", {1: win(1, iconic=True)}, "minimised"),
            ("cloaked", {1: win(1, cloaked=True)}, "virtual desktop"),
            ("empty", {1: win(1, 0, 0)}, "empty"),
            ("off-screen", {1: win(1, 640, 480, x=5000, y=0)}, "off-screen"),
        ]
        for name, infos, word in cases:
            cap = self.cap(infos=infos)
            self.assertIsNone(cap.grab_top(1, 220), name)
            self.assertIn(word, cap.last_error, name)
            self.assertEqual(self.api.blits, [], name)       # nothing is read when the window cannot be read

    def test_grab_top_all_black_is_blocked(self):
        cap = self.cap(infos={1: win(1, 64, 48)}, pixels=bytes(64 * 48 * 4))
        self.assertIsNone(cap.grab_top(1, 48))
        self.assertIn("Windowed", cap.last_error)
        self.assertIn("all black", cap.last_error)

    def test_grab_top_swallows_gdi_errors(self):
        cap = self.cap(infos={1: win(1, 64, 48)}, pixels=OSError(5, "access denied"))
        self.assertIsNone(cap.grab_top(1, 48))
        self.assertIn("screen capture failed", cap.last_error)

    def test_last_error_clears_after_success(self):
        cap = self.cap(infos={1: win(1, 64, 48)})
        cap.grab_top(2, 8)
        self.assertNotEqual(cap.last_error, "")
        self.assertIsNotNone(cap.grab_top(1, 8))
        self.assertEqual(cap.last_error, "")

    def test_unsupported_system_raises_for_actions_not_queries(self):
        class Dead(object):
            def window_info(self, hwnd):
                raise base.HostUnsupported("no")
        with self.assertRaises(base.HostUnsupported):
            Win32Capture(api=Dead()).grab_top(1, 8)

    def test_close_releases_the_api(self):
        cap = self.cap()
        cap.close()
        self.assertEqual(self.api.closed, 1)


class AvailableTests(unittest.TestCase):
    def test_not_windows(self):
        with mock.patch.object(sys, "platform", "darwin"):
            ok, why = Win32Capture(api=FakeApi()).available()
        self.assertFalse(ok)
        self.assertIn("Windows 10", why)

    def test_old_windows(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sys, "getwindowsversion", lambda: (6, 1, 7601), create=True):
            ok, why = Win32Capture(api=FakeApi()).available()
        self.assertFalse(ok)
        self.assertIn("Windows 10", why)

    def test_new_windows_binds_the_api(self):
        api = FakeApi()
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sys, "getwindowsversion", lambda: (10, 0, 22631), create=True):
            self.assertEqual(Win32Capture(api=api).available(), (True, ""))
        self.assertEqual(api.bound, 1)

    def test_bind_failure_is_a_reason_not_an_exception(self):
        api = FakeApi()
        api.bind = mock.Mock(side_effect=OSError("user32 missing"))
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(sys, "getwindowsversion", lambda: (10, 0, 22631), create=True):
            ok, why = Win32Capture(api=api).available()
        self.assertFalse(ok)
        self.assertIn("user32 missing", why)

    def test_the_real_api_is_not_bound_at_import_or_construction(self):
        cap = Win32Capture()
        self.assertIsNone(cap._api)
        if sys.platform != "win32":
            self.assertFalse(cap.available()[0])
            self.assertFalse(cap.game_running())
            self.assertIsNone(cap.foreground_is_game())
            with self.assertRaises(base.HostUnsupported):
                cap.find_game()
            with self.assertRaises(base.HostUnsupported):
                cap.grab_top(1, 8)


# ====================================================================== real Windows only
def _make_window_helper():
    """Everything needed to create, paint and destroy a real top-level window with ctypes. Built lazily, only on Windows."""
    u32, g32, k32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    vp, i32 = ctypes.c_void_p, ctypes.c_int
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, vp, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC), ("cbClsExtra", i32), ("cbWndExtra", i32),
                    ("hInstance", vp), ("hIcon", vp), ("hCursor", vp), ("hbrBackground", vp), ("lpszMenuName", ctypes.c_wchar_p),
                    ("lpszClassName", ctypes.c_wchar_p), ("hIconSm", vp)]

    class PAINTSTRUCT(ctypes.Structure):
        _fields_ = [("hdc", vp), ("fErase", i32), ("rcPaint", wc.RECT), ("fRestore", i32), ("fIncUpdate", i32), ("rgbReserved", ctypes.c_byte * 32)]

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", vp), ("message", ctypes.c_uint), ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                    ("time", ctypes.c_uint32), ("pt", wc.POINT), ("lPrivate", ctypes.c_uint32)]

    def proto(lib, name, restype, *argtypes):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, list(argtypes)

    proto(u32, "DefWindowProcW", ctypes.c_ssize_t, vp, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
    proto(u32, "RegisterClassExW", ctypes.c_uint16, ctypes.POINTER(WNDCLASSEXW))
    proto(u32, "UnregisterClassW", i32, ctypes.c_wchar_p, vp)
    proto(u32, "CreateWindowExW", vp, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32, i32, i32, i32, i32, vp, vp, vp, vp)
    proto(u32, "DestroyWindow", i32, vp)
    proto(u32, "ShowWindow", i32, vp, i32)
    proto(u32, "UpdateWindow", i32, vp)
    proto(u32, "InvalidateRect", i32, vp, vp, i32)
    proto(u32, "BeginPaint", vp, vp, ctypes.POINTER(PAINTSTRUCT))
    proto(u32, "EndPaint", i32, vp, ctypes.POINTER(PAINTSTRUCT))
    proto(u32, "FillRect", i32, vp, ctypes.POINTER(wc.RECT), vp)
    proto(u32, "PeekMessageW", i32, ctypes.POINTER(MSG), vp, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint)
    proto(u32, "TranslateMessage", i32, ctypes.POINTER(MSG))
    proto(u32, "DispatchMessageW", ctypes.c_ssize_t, ctypes.POINTER(MSG))
    proto(u32, "SetForegroundWindow", i32, vp)
    proto(u32, "GetForegroundWindow", vp)
    proto(g32, "CreateSolidBrush", vp, ctypes.c_uint32)
    proto(g32, "DeleteObject", i32, vp)
    proto(k32, "GetModuleHandleW", vp, ctypes.c_wchar_p)
    try:
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmFlush.restype = ctypes.c_long
    except OSError:
        dwm = None

    WM_PAINT, WM_ERASEBKGND = 0x000F, 0x0014
    WS_POPUP, WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x80000000, 0x00000008, 0x00000080
    SW_SHOWNOACTIVATE, SW_MINIMIZE, SW_RESTORE = 4, 6, 9

    def colorref(rgb):
        r, g, b = rgb
        return r | (g << 8) | (b << 16)

    class TestWindow(object):
        """A borderless topmost window whose client area is painted from `rects` [(x, y, w, h, (r, g, b))] over a grey background.
        WM_PAINT repaints the same pattern, so a repaint by the system cannot change what the test expects."""

        def __init__(self, cls_name, title, x, y, w, h, rects, background=(40, 40, 40)):
            self.cls_name, self.rects, self.background, self.w, self.h = cls_name, list(rects), background, w, h
            self.hwnd, self.registered = None, False
            self.wndproc = WNDPROC(self._proc)              # a reference must outlive the window
            hinst = k32.GetModuleHandleW(None)
            wcx = WNDCLASSEXW()
            wcx.cbSize, wcx.lpfnWndProc, wcx.hInstance, wcx.lpszClassName = ctypes.sizeof(WNDCLASSEXW), self.wndproc, hinst, cls_name
            if not u32.RegisterClassExW(ctypes.byref(wcx)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.registered, self.hinst = True, hinst
            self.hwnd = u32.CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, cls_name, title, WS_POPUP, x, y, w, h, None, None, hinst, None)
            if not self.hwnd:
                err = ctypes.get_last_error()
                self.close()
                raise ctypes.WinError(err)

        def _fill(self, hdc, x, y, w, h, rgb):
            brush = g32.CreateSolidBrush(colorref(rgb))
            try:
                rc = wc.RECT(x, y, x + w, y + h)
                u32.FillRect(hdc, ctypes.byref(rc), brush)
            finally:
                g32.DeleteObject(brush)

        def _proc(self, hwnd, msg, wparam, lparam):
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_PAINT:
                ps = PAINTSTRUCT()
                hdc = u32.BeginPaint(hwnd, ctypes.byref(ps))
                try:
                    self._fill(hdc, 0, 0, self.w, self.h, self.background)
                    for x, y, w, h, rgb in self.rects:
                        self._fill(hdc, x, y, w, h, rgb)
                finally:
                    u32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def show(self):
            u32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            u32.InvalidateRect(self.hwnd, None, 1)
            u32.UpdateWindow(self.hwnd)
            self.pump(0.4)

        def pump(self, seconds):
            """Dispatch this thread's messages for a while and let the compositor catch up."""
            end, msg = time.time() + seconds, MSG()
            while time.time() < end:
                while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    u32.TranslateMessage(ctypes.byref(msg))
                    u32.DispatchMessageW(ctypes.byref(msg))
                if dwm is not None:
                    dwm.DwmFlush()
                time.sleep(0.02)

        def minimise(self):
            u32.ShowWindow(self.hwnd, SW_MINIMIZE)
            self.pump(0.4)

        def restore(self):
            u32.ShowWindow(self.hwnd, SW_RESTORE)
            self.pump(0.4)

        def foreground(self):
            u32.SetForegroundWindow(self.hwnd)
            self.pump(0.2)
            return u32.GetForegroundWindow() or 0

        def close(self):
            if self.hwnd:
                u32.DestroyWindow(self.hwnd)
                self.hwnd = None
            if self.registered:
                u32.UnregisterClassW(self.cls_name, self.hinst)
                self.registered = False

    return TestWindow


def _grab_when_ready(test, cap, window, hwnd, rows, tries=15):
    """grab_top a few times (the compositor can lag a frame behind); skip the test if the desktop gives nothing but black."""
    frame = None
    for _ in range(tries):
        frame = cap.grab_top(hwnd, rows)
        if frame is not None:
            return frame
        window.pump(0.1)
    if "all black" in cap.last_error or "off-screen" in cap.last_error:
        test.skipTest("no usable interactive desktop on this machine (%s)" % cap.last_error)
    test.fail("grab_top returned None: %s" % cap.last_error)


def _close(a, b, tol=8):
    return all(abs(x - y) <= tol for x, y in zip(a[:3], b[:3]))


PALETTE = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255), (200, 100, 50), (10, 20, 30)]


@unittest.skipUnless(sys.platform == "win32", "needs a real Windows desktop (GDI, DWM)")
class RealWindowsCaptureTests(unittest.TestCase):
    CLASS = "WoWMentorCaptureTest"

    def setUp(self):
        self.cap = Win32Capture()
        ok, why = self.cap.available()
        if not ok:
            self.skipTest("capture unavailable: %s" % why)
        wc.ensure_dpi_aware()
        self.Window = _make_window_helper()
        self.windows = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for w in self.windows:
            w.close()
        self.cap.close()

    def make(self, rects, w=480, h=160, cls=None, title="WoW Mentor capture test"):
        try:
            win_ = self.Window(cls or self.CLASS, title, 40, 40, w, h, rects)
        except OSError as e:
            self.skipTest("cannot create a window here: %s" % e)
        self.windows.append(win_)
        win_.show()
        return win_

    def test_top_rows_read_back_exactly(self):
        cell, rows_h = 8, 4
        rects = [(i * cell, 0, cell, rows_h, rgb) for i, rgb in enumerate(PALETTE)]
        window = self.make(rects)
        self.assertGreater(len(wc.GAME_CLASSES), 0)
        found = Win32Capture(class_names=[self.CLASS]).find_game()
        self.assertEqual(found, window.hwnd)
        frame = _grab_when_ready(self, self.cap, window, window.hwnd, 12)
        self.assertEqual(frame.width, 480)
        self.assertEqual(frame.height, 12)
        self.assertEqual(frame.stride, 480 * 4)
        self.assertEqual(len(frame.raw), 480 * 12 * 4)
        for i, rgb in enumerate(PALETTE):
            for y in range(rows_h):
                for x in (i * cell, i * cell + cell // 2, i * cell + cell - 1):
                    px = frame[x, y]
                    self.assertTrue(_close(px, rgb), "cell %d at (%d,%d): got %r want %r" % (i, x, y, px, rgb))
                    self.assertEqual(px[3], 255)
        self.assertTrue(_close(frame[3, rows_h + 2], (40, 40, 40)), "background below the cells")
        self.assertEqual(self.cap.last_error, "")

    def test_default_class_is_found_and_foreground_is_answered(self):
        window = self.make([(0, 0, 20, 4, (255, 0, 0))], cls="GxWindowClass", title="World of Warcraft (WoW Mentor capture test)")
        found = self.cap.find_game()
        if found != window.hwnd:
            self.skipTest("a real WoW window is open on this machine")
        self.assertTrue(self.cap.game_running())
        fg = window.foreground()
        got = self.cap.foreground_is_game(window.hwnd)
        self.assertIn(got, (True, False, None))
        if fg == window.hwnd:
            self.assertTrue(got)
        elif fg:
            self.assertFalse(got)

    def test_minimised_window_is_refused_with_a_reason(self):
        window = self.make([(0, 0, 20, 4, (255, 0, 0))])
        window.minimise()
        self.assertIsNone(self.cap.grab_top(window.hwnd, 8))
        self.assertIn("minimised", self.cap.last_error)
        window.restore()
        _grab_when_ready(self, self.cap, window, window.hwnd, 8)

    def test_a_destroyed_window_is_refused(self):
        window = self.make([(0, 0, 20, 4, (255, 0, 0))])
        hwnd = window.hwnd
        window.close()
        self.assertIsNone(self.cap.grab_top(hwnd, 8))
        self.assertIn("gone", self.cap.last_error)

    def test_process_list_works_without_external_tools(self):
        names = self.cap.api.process_names()
        self.assertGreater(len(names), 5)
        self.assertTrue(all(isinstance(n, str) for n in names))

    def test_repeated_grabs_reuse_the_surface_and_resize_cleanly(self):
        window = self.make([(0, 0, 40, 4, (255, 0, 0))])
        _grab_when_ready(self, self.cap, window, window.hwnd, 6)
        surface = self.cap.api._surface
        self.assertIsNotNone(surface)
        for _ in range(20):
            self.assertIsNotNone(self.cap.grab_top(window.hwnd, 6))
        self.assertIs(self.cap.api._surface, surface)
        self.assertIsNotNone(self.cap.grab_top(window.hwnd, 10))
        self.assertIsNot(self.cap.api._surface, surface)
        self.assertIsNone(surface.dc)                # the old one was freed
        self.cap.close()
        self.assertIsNone(self.cap.api._surface)


@unittest.skipUnless(sys.platform == "win32", "needs a real Windows desktop (GDI, DWM)")
class RealWindowsStripPathTests(unittest.TestCase):
    """Capture to text, end to end: draw a strip the way the addon does, grab it from the screen, then find_hint and decode."""
    CLASS = "WoWMentorStripTest"

    def strip_rects(self, cells, u, ox, oy):
        rects = [(ox, oy, strip.CW * u, strip.CH * u, (255, 255, 255))]                # white anchor
        for i, v in enumerate(cells):
            rgb = (255 if v & 4 else 0, 255 if v & 2 else 0, 255 if v & 1 else 0)
            rects.append((ox + (i + 1) * strip.CW * u, oy, strip.CW * u, strip.CH * u, rgb))
        return rects

    def test_grab_then_find_hint_then_decode(self):
        cap = Win32Capture()
        ok, why = cap.available()
        if not ok:
            self.skipTest("capture unavailable: %s" % why)
        wc.ensure_dpi_aware()
        Window = _make_window_helper()
        text = "summary\x1f7\x1f1\x1f1\x1fKV level=6 xp=40\nLevel 6 Human Warlock | caf\u00e9"
        cells = encode(4242, text.encode("utf-8"))
        for u, ox, oy in ((1, 2, 1), (2, 0, 3)):
            width = ox + (len(cells) + 2) * strip.CW * u + 8
            if width > 1000:
                self.skipTest("strip too wide for this test window")
            try:
                window = Window(self.CLASS, "WoW Mentor strip test", 20, 20, width, 140, self.strip_rects(cells, u, ox, oy))
            except OSError as e:
                self.skipTest("cannot create a window here: %s" % e)
            try:
                window.show()
                frame = _grab_when_ready(self, cap, window, window.hwnd, strip.CROP)
                self.assertEqual(frame.width, width)
                self.assertEqual(frame.height, min(strip.CROP, 140))
                hint = strip.find_hint(frame, *frame.size)
                self.assertEqual(hint, (u, ox, oy), "scale u=%d" % u)
                self.assertEqual(strip.decode(frame, frame.width, frame.height, hint), (4242, text))
            finally:
                window.close()
                cap.close()


if __name__ == "__main__":
    unittest.main()
