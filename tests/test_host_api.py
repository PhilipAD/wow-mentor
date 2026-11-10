"""The host layer: selection, the fake host, the unsupported host's messages, shared helpers, and the bridge/CLI talking to a fake host."""
import contextlib, io, os, shutil, stat, sys, tempfile, time, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wowmentor import cli, config as cfgmod, host, install
from wowmentor.bridge import app as bridge, strip
from wowmentor.host import base, common, darwin, fake, unsupported
from wowmentor.host.fake import FakeCapture, FakeHost, FakeMask
from test_decoder import encode, make_frame


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        os.environ.pop("WOWMENTOR_HOST", None)

    def tearDown(self):
        self.env.stop()

    def name_for(self, platform):
        with mock.patch.object(sys, "platform", platform):
            return host.selected_name()

    def test_platform_picks_the_host(self):
        self.assertEqual(self.name_for("darwin"), "darwin")
        self.assertEqual(self.name_for("linux"), "unsupported")
        self.assertEqual(self.name_for("freebsd13"), "unsupported")

    def test_windows_falls_back_to_unsupported_until_win32_exists(self):
        import importlib.util
        has = importlib.util.find_spec("wowmentor.host.win32") is not None
        self.assertEqual(self.name_for("win32"), "win32" if has else "unsupported")

    def test_env_overrides_the_platform(self):
        for name in ("fake", "darwin", "unsupported"):
            with mock.patch.dict(os.environ, {"WOWMENTOR_HOST": name}), mock.patch.object(sys, "platform", "linux"):
                self.assertEqual(host.selected_name(), name)
                self.assertEqual(host.get().name, name)

    def test_unknown_env_value_is_an_error_not_a_silent_fallback(self):
        with mock.patch.dict(os.environ, {"WOWMENTOR_HOST": "amiga"}):
            with self.assertRaises(base.HostUnsupported) as cm:
                host.get()
        self.assertIn("amiga", str(cm.exception))

    def test_using_swaps_and_restores_and_nests(self):
        outer = host.get()
        a, b = FakeHost(name="a"), FakeHost(name="b")
        with host.using(a):
            self.assertIs(host.get(), a)
            with host.using(b):
                self.assertIs(host.get(), b)
            self.assertIs(host.get(), a)
        self.assertIs(host.get(), outer)

    def test_using_restores_after_an_exception(self):
        outer = host.get()
        with self.assertRaises(KeyError):
            with host.using(FakeHost()):
                raise KeyError("x")
        self.assertIs(host.get(), outer)

    def test_get_follows_a_patched_platform(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(host.get().name, "unsupported")
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(host.get().name, "darwin")


class EveryHostModuleTests(unittest.TestCase):
    """make() builds a complete Host under any sys.platform, without loading a native library (no CDLL, no windll, no winreg)."""

    def test_make_never_touches_native_bindings(self):
        import ctypes
        for platform in ("win32", "linux", "darwin"):
            for mod in (darwin, unsupported, fake):
                with mock.patch.object(sys, "platform", platform), mock.patch.object(ctypes, "CDLL", side_effect=AssertionError("CDLL at make()")), \
                        mock.patch.dict(sys.modules, {"winreg": None}):
                    h = mod.make()
                for part in ("locations", "capture", "mask", "services", "shell", "fs"):
                    self.assertIsNotNone(getattr(h, part), "%s.%s on %s" % (mod.__name__, part, platform))
                self.assertTrue(set(base.TEXT_DEFAULTS) <= set(h.text), "text keys of %s" % mod.__name__)

    def test_the_abstract_interfaces_are_fully_implemented(self):
        for mod in (darwin, unsupported, fake):
            h = mod.make()      # instantiating an abstract class with a missing method would raise TypeError
            self.assertIsInstance(h, base.Host)

    def test_queries_on_darwin_code_are_safe_off_mac(self):
        with mock.patch.object(sys, "platform", "linux"):
            h = darwin.make()
            ok, why = h.capture.available()
            if sys.platform != "darwin" and not os.path.exists("/System/Library/Frameworks/CoreGraphics.framework"):
                self.assertFalse(ok)
                self.assertIn("Mac", why)
                self.assertIsNone(h.capture.foreground_is_game())
            self.assertFalse(h.mask.available())


class UnsupportedHostTests(unittest.TestCase):
    def setUp(self):
        self.h = unsupported.make()

    def test_actions_raise_with_a_message_that_says_what_is_supported(self):
        calls = [lambda: self.h.capture.find_game(), lambda: self.h.capture.grab_top(1, 5), lambda: self.h.capture.game_running(),
                 lambda: self.h.mask.start({}), lambda: self.h.mask.update((0, 0, 1, 1), 1), lambda: self.h.mask.stop(),
                 lambda: self.h.services.install("bridge", {}), lambda: self.h.services.start("bridge"), lambda: self.h.services.stop("bridge"),
                 lambda: self.h.services.is_running("bridge"), lambda: self.h.services.remove("bridge"), lambda: self.h.services.build_helpers({})]
        for fn in calls:
            with self.assertRaises(base.HostUnsupported) as cm:
                fn()
            self.assertIn("not available on this system", str(cm.exception))
            self.assertIn("macOS", str(cm.exception))
            self.assertIn("server", str(cm.exception))

    def test_queries_never_raise(self):
        ok, why = self.h.capture.available()
        self.assertFalse(ok)
        self.assertIn("macOS", why)
        self.assertIsNone(self.h.capture.foreground_is_game())
        self.assertFalse(self.h.mask.available())
        self.assertFalse(self.h.services.is_installed("bridge"))
        self.assertFalse(self.h.shell.copy_to_clipboard("x"))
        self.assertEqual(self.h.locations.find_wow_dirs([]), [])

    def test_the_cli_prints_the_message_instead_of_a_traceback(self):
        tmp = tempfile.mkdtemp()
        try:
            with mock.patch.dict(os.environ, {"WOWMENTOR_HOME": tmp, "WOWMENTOR_LOGS": tmp}), host.using(self.h):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = cli.main(["status"])
                self.assertEqual(code, 1)
                self.assertIn("not available on this system", out.getvalue())
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = cli.main(["setup", "--no-build"])
                self.assertEqual(code, 1)
                self.assertIn("cannot run on this system yet", out.getvalue())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_bridge_refuses_to_start(self):
        out = io.StringIO()
        with host.using(self.h), contextlib.redirect_stdout(out):
            bridge.run({})
        self.assertIn("macOS", out.getvalue())


class FakeHostTests(unittest.TestCase):
    def test_services_record_calls_and_track_state(self):
        f = FakeHost()
        s = f.services
        self.assertFalse(s.is_running("bridge"))
        s.install("bridge", {})
        self.assertTrue(s.start("bridge"))
        self.assertTrue(s.is_running("bridge") and s.is_installed("bridge"))
        self.assertTrue(s.stop("bridge"))
        self.assertFalse(s.stop("bridge"), "stopping what is not running says so")
        s.remove("bridge")
        self.assertFalse(s.is_installed("bridge"))
        self.assertEqual(s.calls, [("install", "bridge"), ("start", "bridge"), ("stop", "bridge"), ("stop", "bridge"), ("remove", "bridge")])

    def test_capture_scripts_frames_and_the_last_one_repeats(self):
        f1, f2 = make_frame(encode(1, b"a")), make_frame(encode(2, b"b"))
        c = FakeCapture(game="W", frames=[f1, f2])
        self.assertEqual(c.find_game(), "W")
        self.assertIs(c.grab_top("W", 120), f1)
        self.assertIs(c.grab_top("W", 120), f2)
        self.assertIs(c.grab_top("W", 120), f2)
        self.assertIsNone(FakeCapture(game=None).find_game())
        self.assertIsNone(FakeCapture().grab_top("W", 5))
        self.assertEqual(FakeCapture(ok=False).available()[0], False)

    def test_shell_clipboard(self):
        f = FakeHost()
        self.assertTrue(f.shell.copy_to_clipboard("hello"))
        self.assertEqual(f.shell.clipboard, "hello")

    def test_text_overrides_merge_over_the_defaults(self):
        f = FakeHost(text={"this_machine": "this PC"})
        self.assertEqual(f.text["this_machine"], "this PC")
        self.assertEqual(f.text["terminal"], base.TEXT_DEFAULTS["terminal"])

    def test_wording_follows_the_host(self):
        tmp = tempfile.mkdtemp()
        try:
            with mock.patch.dict(os.environ, {"WOWMENTOR_HOME": tmp, "WOWMENTOR_LOGS": tmp}), host.using(FakeHost(text={"this_machine": "this PC"})):
                text = cli.paste_prompt({"ui_secret": "s" * 8, "port": 8095, "public_url": None})
            self.assertIn("only work on this PC", text)
            self.assertNotIn("Mac", text)
            with host.using(darwin.make()):
                self.assertIn("only work on this Mac", cli.paste_prompt({"ui_secret": "s" * 8, "port": 8095, "public_url": None}))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_clipboard_and_links_go_through_the_host(self):
        tmp = tempfile.mkdtemp()
        f = FakeHost()
        try:
            with mock.patch.dict(os.environ, {"WOWMENTOR_HOME": tmp, "WOWMENTOR_LOGS": tmp}), host.using(f):
                cfg = cfgmod.load(); cfgmod.ensure_secrets(cfg); cfgmod.save(cfg)
                out = io.StringIO()
                with contextlib.redirect_stdout(out), mock.patch.object(cli.doctor, "_get", return_value=(200, "")):
                    code = cli.main(["prompt"])
            self.assertEqual(code, 0)
            self.assertIn("(copied to your clipboard)", out.getvalue())
            self.assertIn(cfg["ui_secret"], f.shell.clipboard)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BridgeThroughTheHostTests(unittest.TestCase):
    def test_frames_come_from_the_host_capture_and_decode(self):
        text = "summary\x1f1\x1f1\x1f1\x1fKV level=6"
        frame = make_frame(encode(77, text.encode()))
        cap = FakeCapture(game="W", frames=[frame])
        with host.using(FakeHost(capture=cap)):
            im = next(bridge.frames())
            hint = strip.find_hint(im, *im.size)
            self.assertEqual(strip.decode(im, im.width, im.height, hint), (77, text))
        self.assertEqual(cap.calls[0], ("find_game",))
        self.assertEqual(cap.calls[1], ("grab_top", "W", bridge.CROP))

    def test_the_old_names_still_work(self):
        for name in ("Frame", "find_hint", "magic", "decode", "measure_bars", "cellv", "CW", "CH", "CROP", "bars"):
            self.assertIs(getattr(bridge, name), getattr(strip, name), name)

    def test_wow_window_and_foreground_delegate(self):
        with host.using(FakeHost(capture=FakeCapture(game="W", foreground=True))):
            self.assertEqual(bridge.wow_window(), "W")
            self.assertTrue(bridge.frontmost_is_wow())
        with host.using(FakeHost(capture=FakeCapture(game=None))):
            self.assertIsNone(bridge.wow_window())
            self.assertIsNone(bridge.frontmost_is_wow())

    def test_update_mask_passes_the_strip_rect_and_a_row_provider(self):
        mask = FakeMask()
        frame = make_frame(encode(5, b"x" * 40), oy=63, height=140)
        hint = strip.find_hint(frame, *frame.size)
        with host.using(FakeHost(mask=mask)):
            bridge.update_mask(hint, frame.width, frame, frame.height)
        (rect, cap_w, row), = mask.updates
        u, oy = hint[0], hint[2]
        self.assertEqual(rect, (0, max(0, oy - 1), frame.width, 2 * strip.CH * u + 2))
        self.assertEqual(cap_w, frame.width)
        self.assertTrue(row and len(row) > 8, "the provider returns the base64 row below the strip")
        with host.using(FakeHost(mask=FakeMask(ok=False))) as h:
            bridge.update_mask(hint, frame.width, frame, frame.height)
            self.assertEqual(h.mask.updates, [], "an unavailable mask is not driven")

    def test_run_checks_capture_available_before_starting(self):
        out = io.StringIO()
        with host.using(FakeHost(capture=FakeCapture(ok=False, reason="nope"))), contextlib.redirect_stdout(out):
            bridge.run({})
        self.assertEqual(out.getvalue().strip(), "nope")


class DarwinMaskProtocolTests(unittest.TestCase):
    """The helper's stdin protocol is unchanged: 'x y w h capW' lines and 'R <base64>' lines."""

    def test_messages_written_to_the_helper(self):
        tmp = tempfile.mkdtemp()
        try:
            helper = os.path.join(tmp, "wowmask")
            open(helper, "w").close()
            written = []

            class Proc(object):
                stdin = mock.Mock(write=lambda b: written.append(b), flush=lambda: None)
                def poll(self): return None
            m = darwin.DarwinMask()
            m.start({"mask_path": helper})
            with mock.patch.object(darwin.subprocess, "Popen", return_value=Proc()) as popen:
                m.update((0, 62, 1800, 6), 1800, lambda: b"QUJD")
                m.update((0, 62, 1800, 6), 1800, None)           # same rect, no row: nothing new is sent
            popen.assert_called_once()
            self.assertEqual(written, [b"0 62 1800 6 1800\n", b"R QUJD\n"])
            self.assertFalse(darwin.DarwinMask().available(), "no path configured = unavailable")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FsAndHelpersTests(unittest.TestCase):
    def test_replace_retries_a_sharing_violation_then_succeeds(self):
        err = PermissionError(13, "in use"); err.winerror = 32
        sleeps = []
        with mock.patch.object(common.os, "replace", side_effect=[err, err, None]) as rep:
            common.replace_with_retry("a", "b", sleep=sleeps.append)
        self.assertEqual(rep.call_count, 3)
        self.assertEqual(len(sleeps), 2)

    def test_replace_gives_up_after_the_retries(self):
        err = PermissionError(13, "in use"); err.winerror = 5
        with mock.patch.object(common.os, "replace", side_effect=err) as rep:
            with self.assertRaises(PermissionError):
                common.replace_with_retry("a", "b", tries=3, sleep=lambda s: None)
        self.assertEqual(rep.call_count, 3)

    def test_a_real_permission_error_is_not_retried(self):
        with mock.patch.object(common.os, "replace", side_effect=PermissionError(13, "denied")) as rep:
            with self.assertRaises(PermissionError):
                common.replace_with_retry("a", "b", sleep=lambda s: self.fail("slept"))
        self.assertEqual(rep.call_count, 1)

    def test_write_private_is_atomic_and_private(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "secret.json")
            fs = common.CommonFs()
            fs.write_private(p, b"one")
            fs.write_private(p, b"two")
            with open(p, "rb") as f:
                self.assertEqual(f.read(), b"two")
            self.assertFalse(os.path.exists(p + ".tmp"))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            with mock.patch.object(common.os, "replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    fs.write_private(p, b"three")
            self.assertFalse(os.path.exists(p + ".tmp"), "no temp file left behind")
            with open(p, "rb") as f:
                self.assertEqual(f.read(), b"two", "the old file is intact")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stdio_never_raises_on_an_unencodable_character(self):
        raw = io.BytesIO()
        wrapper = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with mock.patch.object(sys, "stdout", wrapper), mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(UnicodeEncodeError):
                wrapper.write("\u2705"); wrapper.flush()
            common.ensure_utf8_stdio()
            wrapper.write("\u2705 ok"); wrapper.flush()
        self.assertTrue(raw.getvalue().endswith(b"? ok"))

    def test_scan_wow_roots_puts_forever_first(self):
        tmp = tempfile.mkdtemp()
        try:
            for n in ("_retail_", "_forever_", "Data"):
                os.makedirs(os.path.join(tmp, n, "Interface", "AddOns") if n != "Data" else os.path.join(tmp, n))
            found = common.scan_wow_roots([os.path.join(tmp, "missing"), tmp])
            self.assertEqual([os.path.basename(p) for p in found], ["_forever_", "_retail_"])
            self.assertEqual(install.find_wow_dirs([tmp]), found)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_config_home_and_logs_come_from_the_host_unless_overridden(self):
        f = FakeHost()
        with host.using(f), mock.patch.dict(os.environ):
            os.environ.pop("WOWMENTOR_HOME", None)
            os.environ.pop("WOWMENTOR_LOGS", None)
            self.assertEqual(cfgmod.home(), os.path.abspath("/fake/config"))
            os.environ["WOWMENTOR_HOME"] = "/elsewhere"
            self.assertEqual(cfgmod.home(), os.path.abspath("/elsewhere"))

    def test_no_services_is_the_new_name_of_no_launchd(self):
        with mock.patch.object(cli, "cmd_setup", side_effect=lambda a: a):          # main() returns whatever the command returns
            for flag in ("--no-launchd", "--no-services"):
                self.assertTrue(cli.main(["setup", flag]).no_services, flag)
            self.assertFalse(cli.main(["setup"]).no_services)


if __name__ == "__main__":
    unittest.main()
