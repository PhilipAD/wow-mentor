"""The Windows screen mask: pure logic and the Win32Mask process driver run everywhere (mocks); the window tests run on real Windows only.

On a windows-latest runner read the result of the capture-exclusion test with:  python -m pytest -s -rA tests/test_win32_mask.py
(it prints a line starting with WIN32_MASK_EXCLUSION, or the reason it was skipped).
"""
import base64, os, sys, time, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from wowmentor.host import base, win32_mask as wm

ON_WINDOWS = sys.platform == "win32"


class PureTests(unittest.TestCase):
    def test_should_enable_gates_on_build_19041(self):
        self.assertFalse(wm.should_enable(19040))
        self.assertTrue(wm.should_enable(19041))
        self.assertTrue(wm.should_enable(22631))
        self.assertFalse(wm.should_enable(None))
        self.assertFalse(wm.should_enable("x"))

    def test_parse_rect_line(self):
        self.assertEqual(wm.parse_line("10 20 300 8 300"), ("rect", (10, 20, 300, 8, 300)))
        self.assertEqual(wm.parse_line(b"-1920 0 1920 6 1920\n"), ("rect", (-1920, 0, 1920, 6, 1920)))
        self.assertEqual(wm.parse_line("1.0 2.0 3.0 4.0 5.0"), ("rect", (1, 2, 3, 4, 5)))

    def test_parse_rejects_garbage(self):
        for bad in ("", "1 2 3", "a b c d e", "0 0 0 5 5", "0 0 5 0 5", "0 0 5 5 0", "0 0 99999 5 5", "0 0 5 9999 5", "R !!!", "R " + base64.b64encode(b"ab").decode(),
                    "R " + base64.b64encode(b"abcdefg").decode(), None, 12):
            self.assertIsNone(wm.parse_line(bad), bad)

    def test_parse_row_line(self):
        rgb = bytes(range(12))
        self.assertEqual(wm.parse_line(b"R " + base64.b64encode(rgb) + b"\n"), ("row", rgb))

    def test_compute_frame_rect_scale_one(self):
        self.assertEqual(wm.compute_frame_rect((0, 5, 1920, 8), 1920, (100, 50), 1920), (100, 55, 1920, 8))

    def test_compute_frame_rect_negative_monitor_origin(self):
        self.assertEqual(wm.compute_frame_rect((0, 0, 1000, 6), 1000, (-1920, 10), 1000), (-1920, 10, 1000, 6))

    def test_compute_frame_rect_scaled_capture(self):
        # captured at half size: 1000 px wide frame of a 2000 px client -> everything doubles
        self.assertEqual(wm.compute_frame_rect((10, 4, 1000, 3), 1000, (0, 0), 2000), (20, 8, 2000, 6))

    def test_compute_frame_rect_minimum_height_and_bad_widths(self):
        self.assertEqual(wm.compute_frame_rect((0, 0, 1000, 4), 1000, (0, 0), 100)[3], 1)
        self.assertEqual(wm.compute_frame_rect((3, 4, 10, 2), 0, (1, 1), 0), (4, 5, 10, 2))

    def test_format_rect_line_roundtrips_through_parse(self):
        line = wm.format_rect_line((-5, 7, 640, 6))
        self.assertEqual(line, "-5 7 640 6 640\n")
        self.assertEqual(wm.parse_line(line), ("rect", (-5, 7, 640, 6, 640)))

    def test_source_rect_is_the_row_below_the_strip(self):
        self.assertEqual(wm.source_rect((10, 20, 300, 6)), (10, 26, 300, 1))

    def test_rgb_row_to_dib_swaps_to_bgr_and_pads(self):
        n, dib = wm.rgb_row_to_dib(bytes([1, 2, 3, 4, 5, 6]))
        self.assertEqual(n, 2)
        self.assertEqual(dib, bytes([3, 2, 1, 6, 5, 4, 0, 0]))            # 6 bytes padded to 8
        n, dib = wm.rgb_row_to_dib(bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]))
        self.assertEqual((n, len(dib)), (4, 12))

    def test_is_stale(self):
        self.assertTrue(wm.is_stale(10.0, None))
        self.assertFalse(wm.is_stale(10.0, 8.0))
        self.assertTrue(wm.is_stale(10.0, 4.0))

    def test_restart_delay_backs_off_and_caps(self):
        self.assertEqual([wm.restart_delay(n) for n in (0, 1, 2, 3)], [0.0, 2.0, 4.0, 8.0])
        self.assertEqual(wm.restart_delay(20), 30.0)

    def test_build_command(self):
        self.assertEqual(wm.build_command("py.exe", "C:\\l\\mask.log"), ["py.exe", "-m", "wowmentor.host.win32_mask", "--log", "C:\\l\\mask.log"])
        self.assertEqual(wm.build_command("py.exe"), ["py.exe", "-m", "wowmentor.host.win32_mask"])

    def test_hwnd_of(self):
        class H: hwnd = 77
        self.assertEqual(wm.hwnd_of(5), 5)
        self.assertEqual(wm.hwnd_of(H()), 77)
        self.assertEqual(wm.hwnd_of({"hwnd": 9}), 9)
        for bad in (None, 0, True, "x", {}):
            self.assertIsNone(wm.hwnd_of(bad))

    def test_is_a_mask_and_import_is_native_free(self):
        if not ON_WINDOWS:
            self.assertEqual(wm._api_cache, [])
        self.assertTrue(issubclass(wm.Win32Mask, base.Mask))

    def test_filelog_never_raises_and_writes_utf8(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.log")
            wm.FileLog(p)("hello")
            with open(p, encoding="utf-8") as f:
                self.assertIn("hello", f.read())
        wm.FileLog(os.path.join("no", "such", "dir", "x.log"))("x")      # swallowed
        wm.FileLog(None)("x")


class FakeProc(object):
    def __init__(self):
        self.written = []
        self.code = None
        self.stdin = mock.Mock()
        self.stdin.write.side_effect = lambda b: self.written.append(bytes(b))
        self.killed = False

    def poll(self): return self.code
    def wait(self, timeout=None): return self.code
    def kill(self): self.killed = True


class Clock(object):
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class DriverTests(unittest.TestCase):
    def make(self, build=19045, platform="win32", geo=(100, 50, 1920, 1080)):
        self.clock = Clock()
        self.procs = []
        def spawn(cmd, **kw):
            p = FakeProc(); p.cmd, p.kw = cmd, kw; self.procs.append(p); return p
        m = wm.Win32Mask(platform=platform, build=build, spawn=spawn, geometry=lambda h: geo, find_game=lambda: 4242, clock=self.clock)
        m.start({})
        return m

    def lines(self, p):
        return b"".join(p.written).decode().splitlines()

    def test_unavailable_off_windows_and_on_old_builds(self):
        self.assertFalse(self.make(platform="darwin").available())
        self.assertFalse(self.make(build=19000).available())
        self.assertTrue(self.make().available())

    def test_update_is_a_noop_when_unavailable(self):
        m = self.make(build=18000)
        m.update((0, 0, 100, 6), 100)
        self.assertEqual(self.procs, [])

    def test_first_update_spawns_and_sends_screen_pixels(self):
        m = self.make()
        m.update((0, 4, 1920, 8), 1920)
        self.assertEqual(len(self.procs), 1)
        p = self.procs[0]
        self.assertEqual(p.cmd[1:3], ["-m", "wowmentor.host.win32_mask"])
        self.assertIn("--log", p.cmd)
        self.assertEqual(self.lines(p), ["100 54 1920 8 1920"])
        self.assertIn("PYTHONPATH", p.kw["env"])
        self.assertTrue(p.kw["creationflags"] & 0x08000000)

    def test_unchanged_rect_is_not_resent_until_the_keepalive(self):
        m = self.make()
        m.update((0, 4, 1920, 8), 1920)
        m.update((0, 4, 1920, 8), 1920)
        p = self.procs[0]
        self.assertEqual(len(self.lines(p)), 1)
        self.clock.t += wm.KEEPALIVE_SECONDS + 0.1
        m.update((0, 4, 1920, 8), 1920)
        self.assertEqual(len(self.lines(p)), 2)

    def test_changed_rect_is_sent(self):
        m = self.make()
        m.update((0, 4, 1920, 8), 1920)
        m.update((0, 4, 1920, 10), 1920)
        self.assertEqual(self.lines(self.procs[0])[-1], "100 54 1920 10 1920")

    def test_row_is_sent_at_most_every_row_interval(self):
        m = self.make()
        row = base64.b64encode(bytes(12))
        m.update((0, 4, 1920, 8), 1920, lambda: row)
        m.update((0, 4, 1920, 8), 1920, lambda: row)
        self.assertEqual([l for l in self.lines(self.procs[0]) if l.startswith("R ")], ["R " + row.decode()])
        self.clock.t += wm.ROW_INTERVAL + 0.01
        m.update((0, 4, 1920, 8), 1920, lambda: row)
        self.assertEqual(len([l for l in self.lines(self.procs[0]) if l.startswith("R ")]), 2)

    def test_a_failing_row_provider_is_swallowed(self):
        m = self.make()
        m.update((0, 4, 1920, 8), 1920, mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(len(self.lines(self.procs[0])), 1)

    def test_no_game_window_means_no_helper(self):
        m = self.make()
        m._find_game = lambda: None
        m.update((0, 4, 1920, 8), 1920)
        self.assertEqual(self.procs, [])

    def test_restarts_after_a_crash_with_backoff(self):
        m = self.make()
        m.update((0, 4, 1920, 8), 1920)
        self.procs[0].code = 1                                   # died right away
        self.clock.t += 0.1
        m.update((0, 4, 1920, 8), 1920)
        self.assertEqual(len(self.procs), 1)                     # still inside the backoff
        self.clock.t += 2.5
        m.update((0, 4, 1920, 8), 1920)
        self.assertEqual(len(self.procs), 2)
        self.assertEqual(self.lines(self.procs[1])[0], "100 54 1920 8 1920")   # a fresh helper gets the rect again

    def test_a_healthy_helper_that_exits_restarts_at_once(self):
        m = self.make()
        m.update((0, 4, 1920, 8), 1920)
        self.clock.t += wm.RUN_OK_SECONDS + 5
        self.procs[0].code = 0
        m.update((0, 4, 1920, 8), 1920)
        self.assertEqual(len(self.procs), 2)

    def test_unsupported_exit_code_stops_for_good(self):
        m = self.make()
        m.update((0, 4, 1920, 8), 1920)
        self.procs[0].code = wm.EXIT_UNSUPPORTED
        self.clock.t += 1
        with mock.patch("builtins.print"):
            m.update((0, 4, 1920, 8), 1920)
        self.assertFalse(m.available())
        self.clock.t += 100
        m.update((0, 4, 1920, 8), 1920)
        self.assertEqual(len(self.procs), 1)

    def test_broken_pipe_is_swallowed_and_retried_later(self):
        m = self.make()
        m.update((0, 4, 1920, 8), 1920)
        self.procs[0].stdin.write.side_effect = BrokenPipeError()
        self.clock.t += 2
        with mock.patch("builtins.print"):
            m.update((0, 4, 1920, 9), 1920)
        self.assertIsNone(m.proc)

    def test_stop_closes_stdin_and_is_idempotent(self):
        m = self.make()
        m.update((0, 4, 1920, 8), 1920)
        p = self.procs[0]
        m.stop(); m.stop()
        p.stdin.close.assert_called_once()
        self.assertIsNone(m.proc)

    def test_stop_kills_a_helper_that_will_not_exit(self):
        m = self.make()
        m.update((0, 4, 1920, 8), 1920)
        p = self.procs[0]
        p.wait = mock.Mock(side_effect=TimeoutError())
        m.stop()
        self.assertTrue(p.killed)

    def test_main_off_windows_exits_unsupported_and_never_prints(self):
        if ON_WINDOWS:
            self.skipTest("only meaningful off Windows")
        with mock.patch("builtins.print") as pr:
            self.assertEqual(wm.main([]), wm.EXIT_UNSUPPORTED)
        pr.assert_not_called()


# ---------------------------------------------------------------- real Windows only
def _windows_ready():
    """(ok, reason): Windows 10 2004+ with an interactive desktop (the screen DC shows real pixels)."""
    if not ON_WINDOWS:
        return False, "not Windows"
    if not wm.should_enable(sys.getwindowsversion().build):
        return False, "Windows build %d is older than %d" % (sys.getwindowsversion().build, wm.MIN_BUILD)
    return True, ""


@unittest.skipUnless(ON_WINDOWS, "needs real Windows")
class WindowsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok, why = _windows_ready()
        if not ok:
            raise unittest.SkipTest(why)
        wm.set_dpi_aware()
        cls.api = wm._build_api()
        if cls.api.u32.GetSystemMetrics(0) <= 0 or cls.api.u32.GetSystemMetrics(1) <= 0:
            raise unittest.SkipTest("no interactive desktop: screen size is 0")

    def setUp(self):
        self.win = wm.MaskWindow(log=lambda m: print("mask log:", m), quit_on_destroy=False)
        self.created = []
        self.addCleanup(self.win.destroy)
        self.addCleanup(self.destroy_extras)

    def destroy_extras(self):
        for h in self.created:
            self.api.u32.DestroyWindow(h)

    def pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.win.pump(); time.sleep(0.02)

    def static_window(self, kind, rect):
        """A built-in STATIC control as a coloured rectangle: SS_WHITERECT (6) / SS_BLACKRECT (4), topmost, no activation."""
        api = self.api
        x, y, w, h = rect
        hw = api.u32.CreateWindowExW(wm.WS_EX_TOPMOST | wm.WS_EX_TOOLWINDOW | wm.WS_EX_NOACTIVATE, "STATIC", "", wm.WS_POPUP | kind,
                                     x, y, w, h, None, None, None, None)
        self.assertTrue(hw, "could not create the underlay window")
        self.created.append(hw)
        api.u32.SetWindowPos(hw, api.C.c_void_p(-1), x, y, w, h, wm.SWP_NOACTIVATE | wm.SWP_SHOWWINDOW)
        return hw

    def screen_pixel(self, x, y):
        api = self.api
        scr = api.u32.GetDC(None)
        try:
            return api.g32.GetPixel(scr, x, y)
        finally:
            api.u32.ReleaseDC(None, scr)

    def test_affinity_styles_and_stretchblt(self):
        self.assertTrue(self.win.create())
        self.assertEqual(self.win.affinity(), wm.WDA_EXCLUDEFROMCAPTURE)
        ex = self.api.u32.GetWindowLongW(self.win.hwnd, -20) & 0xFFFFFFFF
        for flag in (wm.WS_EX_TOPMOST, wm.WS_EX_TOOLWINDOW, wm.WS_EX_NOACTIVATE, wm.WS_EX_LAYERED, wm.WS_EX_TRANSPARENT):
            self.assertTrue(ex & flag, "missing extended style 0x%x" % flag)
        self.assertTrue(self.win.place((50, 50, 200, 6)))
        self.pump(0.1)
        self.assertEqual(self.win.affinity(), wm.WDA_EXCLUDEFROMCAPTURE, "affinity must survive a show")
        hdc = self.api.u32.GetDC(self.win.hwnd)
        try:
            scr = self.api.u32.GetDC(None)
            try:
                self.assertTrue(self.api.g32.StretchBlt(hdc, 0, 0, 200, 6, scr, 50, 56, 200, 1, wm.SRCCOPY), "StretchBlt from the screen DC failed")
            finally:
                self.api.u32.ReleaseDC(None, scr)
        finally:
            self.api.u32.ReleaseDC(self.win.hwnd, hdc)
        self.assertIn(self.win.paint(), ("screen", "row", "black"))
        self.win.hide()
        self.assertTrue(self.win.place((50, 50, 200, 6)))
        self.assertEqual(self.win.affinity(), wm.WDA_EXCLUDEFROMCAPTURE, "affinity must be intact after hide/show")

    def test_row_fallback_and_black_fallback_paint(self):
        self.assertTrue(self.win.create())
        self.win.place((60, 60, 100, 4))
        self.win.screen_ok = False
        self.assertEqual(self.win.paint(), "black")
        self.win.set_row(bytes([10, 20, 30] * 8))
        self.assertEqual(self.win.paint(), "row")

    def test_click_through(self):
        self.assertTrue(self.win.create())
        under = self.static_window(4, (300, 300, 120, 60))        # SS_BLACKRECT
        self.assertTrue(self.win.place((300, 300, 120, 20)))
        self.pump(0.3)
        pt = self.api.W.POINT(310, 305)
        hit = self.api.u32.WindowFromPoint(pt)
        self.assertNotEqual(hit, self.win.hwnd, "the mask must not receive mouse hit tests")
        self.assertEqual(self.win.rect, (300, 300, 120, 20))
        self.assertTrue(under)

    def test_screen_capture_does_not_see_the_mask(self):
        """Underlay: white strip area with a black row under it. The mask copies the black row, so if it were visible to a screen-DC
        read the strip area would read black. It must read white."""
        self.assertTrue(self.win.create())
        x, y, w, h = 400, 400, 160, 6
        self.static_window(6, (x, y, w, h))                        # SS_WHITERECT where the strip is
        self.static_window(4, (x, y + h, w, 40))                   # SS_BLACKRECT below it
        self.pump(0.4)
        before = self.screen_pixel(x + 20, y + 2)
        below = self.screen_pixel(x + 20, y + h + 3)
        if before != 0xFFFFFF or below != 0x000000:
            msg = "no interactive desktop (screen DC read white=%06x black=%06x)" % (before, below)
            print("WIN32_MASK_EXCLUSION: SKIPPED, " + msg)
            self.skipTest(msg)
        self.assertTrue(self.win.place((x, y, w, h)))
        self.win.screen_ok = True
        for _ in range(10):
            self.win.paint(); self.pump(0.05)
        after = self.screen_pixel(x + 20, y + 2)
        verdict = "EXCLUDED (strip still reads %06x)" % after if after == 0xFFFFFF else "VISIBLE TO CAPTURE (strip reads %06x)" % after
        print("WIN32_MASK_EXCLUSION: " + verdict + ", build %d" % sys.getwindowsversion().build)
        self.assertEqual(after, 0xFFFFFF, "the mask is visible to a screen-DC BitBlt, WDA_EXCLUDEFROMCAPTURE is not working here")

    def test_driver_end_to_end_with_the_real_helper(self):
        """Win32Mask spawns the real helper; it must stay alive, accept a rect, and exit when stdin closes."""
        geo = (500, 500, 800, 600)
        m = wm.Win32Mask(geometry=lambda h: geo, find_game=lambda: 1234)
        m.start({})
        self.assertTrue(m.available())
        m.update((0, 2, 800, 6), 800)
        self.assertIsNotNone(m.proc, "helper did not start")
        time.sleep(1.0)
        self.assertIsNone(m.proc.poll(), "helper exited early (see mask.log in the log folder)")
        p = m.proc
        m.stop()
        self.assertIsNotNone(p.poll(), "helper must exit when stdin closes")


if __name__ == "__main__":
    unittest.main()
