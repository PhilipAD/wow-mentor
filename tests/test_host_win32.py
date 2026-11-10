"""The Windows host (wowmentor/host/win32.py, win32_supervisor.py) and the server's Windows port hardening.

Most tests run on every OS with fakes for the registry, processes, ctypes and time. The classes at the bottom run only on real Windows
(a windows-latest runner executes them); the ones that need a desktop skip with a reason when there is none.
"""
import contextlib, io, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, types, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wowmentor import config as cfgmod, host
from wowmentor.host import base, win32, win32_supervisor as sup
from wowmentor.server import app as server_app

ON_WINDOWS = sys.platform == "win32"


def make_tree(root, *clients):
    """<root>/<client>/Interface/AddOns for each client; returns the client folders."""
    out = []
    for c in clients:
        d = os.path.join(root, c)
        os.makedirs(os.path.join(d, "Interface", "AddOns"))
        out.append(d)
    return out


class TempHome(unittest.TestCase):
    """Every test runs with its own WOWMENTOR_HOME and WOWMENTOR_LOGS, so nothing touches the real config or Run key."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wm-win32-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        env = mock.patch.dict(os.environ, {"WOWMENTOR_HOME": self.home, "WOWMENTOR_LOGS": os.path.join(self.tmp, "logs")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class FakeRegistry(object):
    def __init__(self, install_paths=(), fail=False):
        self.install_paths, self.run, self.fail = list(install_paths), {}, fail

    def wow_install_paths(self):
        return list(self.install_paths)

    def get_run(self, name):
        return self.run.get(name)

    def set_run(self, name, command):
        if self.fail:
            raise PermissionError("denied")
        self.run[name] = command

    def delete_run(self, name):
        self.run.pop(name, None)


# ---------------------------------------------------------------- server: do not share the port on Windows
class ServerBindTests(unittest.TestCase):
    def test_options_per_platform(self):
        self.assertEqual(server_app.bind_options("linux"), (True, None))
        self.assertEqual(server_app.bind_options("darwin"), (True, None))
        reuse, excl = server_app.bind_options("win32")
        self.assertFalse(reuse)
        self.assertEqual(excl, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5))

    def test_windows_bind_sets_exclusive_use_and_not_reuse(self):
        srv = server_app.BoundedServer(("127.0.0.1", 0), server_app.Handler, bind_and_activate=False)
        self.addCleanup(srv.server_close)
        fake = mock.Mock()
        real, srv.socket = srv.socket, fake
        self.addCleanup(real.close)
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(server_app.ThreadingHTTPServer, "server_bind") as parent:
            srv.server_bind()
        fake.setsockopt.assert_called_once_with(socket.SOL_SOCKET, server_app.bind_options("win32")[1], 1)
        parent.assert_called_once()
        self.assertFalse(srv.allow_reuse_address)

    @unittest.skipIf(ON_WINDOWS, "POSIX behaviour")
    def test_posix_still_reuses_the_address(self):
        srv = server_app.BoundedServer(("127.0.0.1", 0), server_app.Handler)
        self.addCleanup(srv.server_close)
        self.assertTrue(srv.allow_reuse_address)
        self.assertTrue(srv.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR))


# ---------------------------------------------------------------- locations
class LocationsTests(TempHome):
    def setUp(self):
        TempHome.setUp(self)
        self.loc = win32.Win32Locations()

    def test_folders_come_from_appdata_and_localappdata(self):
        with mock.patch.dict(os.environ, {"APPDATA": os.path.join(self.tmp, "Roaming"), "LOCALAPPDATA": os.path.join(self.tmp, "Local")}):
            self.assertEqual(self.loc.config_home(), os.path.join(self.tmp, "Roaming", "WoWMentor"))
            self.assertEqual(self.loc.log_dir(), os.path.join(self.tmp, "Local", "WoWMentor", "logs"))
            self.assertIn(os.path.join(self.tmp, "Local", "Microsoft", "WinGet", "Links"), self.loc.extra_bin_dirs())

    def test_client_base_walks_up_from_whatever_the_registry_says(self):
        base_dir = os.path.join(self.tmp, "World of Warcraft")
        make_tree(base_dir, "_forever_", "_retail_")
        launcher = os.path.join(base_dir, "World of Warcraft Launcher.exe")
        open(launcher, "w").close()
        for value in (base_dir, base_dir + os.sep, '"%s"' % base_dir, os.path.join(base_dir, "_forever_"), os.path.join(base_dir, "_retail_", "Interface"),
                      launcher):
            self.assertEqual(win32.client_base(value), base_dir, value)

    def test_client_base_gives_up_cleanly(self):
        empty = os.path.join(self.tmp, "nothing", "here")
        os.makedirs(empty)
        self.assertIsNone(win32.client_base(empty))
        self.assertIsNone(win32.client_base(""))
        self.assertIsNone(win32.client_base(None))

    def test_default_roots_are_the_documented_places(self):
        roots = win32.default_roots(env={}, drives=["C:", "D:"])
        for want in (r"C:\Program Files (x86)\World of Warcraft", r"C:\Program Files\World of Warcraft", r"D:\Games\World of Warcraft",
                     r"D:\Blizzard\World of Warcraft", r"C:\Games\World of Warcraft", r"C:\Blizzard\World of Warcraft"):
            self.assertIn(want, roots)
        env = {"ProgramFiles(x86)": r"E:\PF86", "ProgramFiles": r"E:\PF"}
        self.assertEqual(win32.default_roots(env=env)[:2], [r"E:\PF86\World of Warcraft", r"E:\PF\World of Warcraft"])

    def test_registry_hits_and_default_folders_are_merged_forever_first_without_duplicates(self):
        reg_base, other = os.path.join(self.tmp, "reg", "World of Warcraft"), os.path.join(self.tmp, "games")
        make_tree(reg_base, "_retail_", "_forever_")
        make_tree(other, "_classic_era_")
        fake = FakeRegistry([os.path.join(reg_base, "_retail_"), reg_base, os.path.join(self.tmp, "nowhere")])
        with mock.patch.object(win32, "registry", fake), mock.patch.object(win32, "fixed_drives", return_value=["C:"]), \
                mock.patch.object(win32, "default_roots", return_value=[other, reg_base]):
            found = self.loc.find_wow_dirs()
        self.assertEqual(found, [os.path.join(reg_base, "_forever_"), os.path.join(other, "_classic_era_"), os.path.join(reg_base, "_retail_")])

    def test_search_roots_are_used_alone(self):
        root = os.path.join(self.tmp, "r")
        (client,) = make_tree(root, "_forever_")
        with mock.patch.object(win32, "registry", FakeRegistry([os.path.join(self.tmp, "x")])) as reg:
            self.assertEqual(self.loc.find_wow_dirs([root]), [client])

    def test_a_broken_registry_never_raises(self):
        class Boom(object):
            def wow_install_paths(self):
                raise ImportError("no winreg here")
        with mock.patch.object(win32, "registry", Boom()), mock.patch.object(win32, "default_roots", return_value=[]):
            self.assertEqual(self.loc.find_wow_dirs(), [])

    def test_unwritable_addons_folder_gets_a_message_that_never_asks_for_admin(self):
        (client,) = make_tree(os.path.join(self.tmp, "w"), "_forever_")
        self.assertIsNone(self.loc.addons_write_problem(client))
        with mock.patch.object(win32.tempfile, "TemporaryFile", side_effect=PermissionError(13, "denied")):
            msg = self.loc.addons_write_problem(client)
        self.assertIn(os.path.join("Interface", "AddOns"), msg)
        self.assertIn("Battle.net", msg)
        self.assertIn("--wow-dir", msg)
        self.assertIn("never asks", msg)
        self.assertNotIn("run as administrator", msg.lower())
        self.assertIsNone(self.loc.addons_write_problem(os.path.join(self.tmp, "missing")))


# ---------------------------------------------------------------- the supervisor's policy
class FakeClock(object):
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeChild(object):
    _next = 100

    def __init__(self, clock, lifetime):
        FakeChild._next += 1
        self.pid, self.clock, self.end, self.returncode = FakeChild._next, clock, clock.t + lifetime, None

    def poll(self):
        if self.returncode is None and self.clock.t >= self.end:
            self.returncode = 1
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class SupervisorPolicyTests(unittest.TestCase):
    def run_sup(self, lifetimes, stop_after=None, stop_at=None):
        clock, launches, kills, beats, said = FakeClock(), [], [], [], []

        def popen(argv, **kw):
            launches.append(clock.t)
            life = lifetimes[min(len(launches) - 1, len(lifetimes) - 1)]
            return FakeChild(clock, life)

        def stop():
            if stop_after is not None and len(launches) >= stop_after:
                return True
            return stop_at is not None and clock.t >= stop_at

        s = sup.Supervisor("bridge", ["x"], popen=popen, clock=clock.now, sleep=clock.sleep, stop_requested=stop,
                           heartbeat=lambda pid: beats.append((clock.t, pid)), kill=kills.append, say=said.append)
        self.assertEqual(s.run(), 0)
        return launches, kills, beats, said

    def test_backoff_steps_1_2_5_15_30_then_stays_at_30(self):
        self.assertEqual([sup.backoff_delay(n) for n in range(1, 8)], [1, 2, 5, 15, 30, 30, 30])
        self.assertEqual(sup.backoff_delay(0), 1)

    def test_a_crashing_child_is_restarted_with_growing_pauses(self):
        launches, kills, beats, said = self.run_sup([2], stop_after=7)
        gaps = [b - a for a, b in zip(launches, launches[1:])]
        self.assertEqual(gaps, [2 + d for d in (1, 2, 5, 15, 30, 30)])

    def test_sixty_seconds_of_uptime_resets_the_backoff(self):
        launches, _, _, _ = self.run_sup([2, 2, 70, 2, 2], stop_after=6)
        gaps = [b - a for a, b in zip(launches, launches[1:])]
        self.assertEqual(gaps, [2 + 1, 2 + 2, 70 + 1, 2 + 2, 2 + 5])

    def test_stop_request_kills_the_child_tree_and_returns(self):
        launches, kills, _, said = self.run_sup([1000], stop_at=10)
        self.assertEqual(len(launches), 1)
        self.assertEqual(len(kills), 1)
        self.assertTrue(any("stopping" in m for m in said))

    def test_stop_during_the_backoff_pause_does_not_start_another_child(self):
        launches, kills, _, _ = self.run_sup([1], stop_at=2)
        self.assertEqual(len(launches), 1)
        self.assertEqual(kills, [])

    def test_heartbeat_is_written_at_start_and_about_every_five_seconds(self):
        _, _, beats, _ = self.run_sup([1000], stop_at=30)
        times = [t for t, _ in beats]
        self.assertEqual(times[0], 0)
        self.assertTrue(all(b - a >= 5 for a, b in zip(times, times[1:])))
        self.assertGreaterEqual(len(times), 5)
        self.assertTrue(all(pid is not None for _, pid in beats))

    def test_a_pid_file_that_cannot_be_written_does_not_stop_the_service(self):
        clock = FakeClock()
        s = sup.Supervisor("bridge", ["x"], popen=lambda a, **k: FakeChild(clock, 1000), clock=clock.now, sleep=clock.sleep,
                           stop_requested=lambda: clock.t >= 12, heartbeat=mock.Mock(side_effect=PermissionError("locked")), kill=lambda p: None)
        self.assertEqual(s.run(), 0)

    def test_a_missing_executable_is_retried_not_fatal(self):
        clock, tries = FakeClock(), []

        def popen(argv, **kw):
            tries.append(clock.t)
            raise FileNotFoundError("python.exe")
        s = sup.Supervisor("tunnel", ["x"], popen=popen, clock=clock.now, sleep=clock.sleep, stop_requested=lambda: len(tries) >= 3, kill=lambda p: None)
        self.assertEqual(s.run(), 0)
        self.assertEqual(len(tries), 3)


class SupervisorHelpersTests(TempHome):
    def test_pythonw_is_preferred_and_children_use_the_console_python(self):
        d = os.path.join(self.tmp, "py")
        os.makedirs(d)
        for name in ("python.exe", "pythonw.exe"):
            open(os.path.join(d, name), "w").close()
        self.assertEqual(sup.gui_python(os.path.join(d, "python.exe")), os.path.join(d, "pythonw.exe"))
        self.assertEqual(sup.gui_python(os.path.join(d, "pythonw.exe")), os.path.join(d, "pythonw.exe"))
        self.assertEqual(sup.console_python(os.path.join(d, "pythonw.exe")), os.path.join(d, "python.exe"))
        lone = os.path.join(self.tmp, "lone", "python.exe")
        self.assertEqual(sup.gui_python(lone), lone)        # no pythonw next to it: python.exe is used (callers add CREATE_NO_WINDOW)

    def test_child_environment_and_argv(self):
        env = sup.child_env({"PYTHONPATH": "old", "PATH": "p"}, home="H")
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep), [sup.PKG_ROOT, "old"])
        self.assertEqual((env["PYTHONUTF8"], env["PYTHONUNBUFFERED"], env["WOWMENTOR_HOME"], env["PATH"]), ("1", "1", "H", "p"))
        self.assertNotIn("WOWMENTOR_HOME", sup.child_env({}))
        self.assertEqual(sup.child_argv("server", "/x/python")[1:], ["-m", "wowmentor", "server"])
        a = sup.supervisor_argv("bridge", "H", "/x/python")
        self.assertEqual(a[1:], [sup.SCRIPT, "bridge", "--home", "H"])
        self.assertEqual(sup.supervisor_argv(None, None, "/x/python")[2:], ["--autostart"])

    def test_pid_file_round_trip_and_garbage(self):
        p = os.path.join(self.tmp, "run", "bridge.pid")
        sup.write_pidfile(p, {"pid": 77, "heartbeat": 1000.0})
        info = sup.read_pidfile(p)
        self.assertEqual((info["pid"], info["heartbeat"]), (77, 1000.0))
        self.assertTrue(sup.is_fresh(info, now=1010.0))
        self.assertFalse(sup.is_fresh(info, now=1000.0 + sup.STALE_AFTER + 1))
        self.assertFalse(sup.is_fresh(None))
        self.assertEqual([f for f in os.listdir(os.path.dirname(p))], ["bridge.pid"])      # no temp file left
        for junk in ("", "not json", "[]", '{"pid": 0}', '{"pid": "x"}', '{"heartbeat": 1}'):
            with open(p, "w", encoding="utf-8") as f:
                f.write(junk)
            self.assertIsNone(sup.read_pidfile(p), junk)
        self.assertIsNone(sup.read_pidfile(os.path.join(self.tmp, "missing.pid")))

    def test_write_pidfile_retries_a_sharing_violation(self):
        p = os.path.join(self.tmp, "run", "x.pid")
        calls = []

        def replace(a, b):
            calls.append(1)
            if len(calls) < 3:
                e = PermissionError(13, "sharing")
                e.winerror = 32
                raise e
            os.replace(a, b)
        sup.write_pidfile(p, {"pid": 5, "heartbeat": 1}, replace=replace, sleep=lambda s: None)
        self.assertEqual((len(calls), sup.read_pidfile(p)["pid"]), (3, 5))

    def test_pid_alive_for_this_process_and_a_dead_one(self):
        self.assertTrue(sup.pid_alive(os.getpid()))
        self.assertFalse(sup.pid_alive(0))
        self.assertFalse(sup.pid_alive(-3))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(sup.pid_alive(p.pid))

    def test_supervisor_running_needs_a_live_pid_and_a_fresh_heartbeat(self):
        path = sup.pidfile_path("bridge")
        sup.write_pidfile(path, {"pid": 4321, "heartbeat": time.time()})
        with mock.patch.object(sup, "pid_alive", return_value=True):
            self.assertTrue(sup.supervisor_running("bridge"))
            self.assertFalse(sup.supervisor_running("server"))
            self.assertFalse(sup.supervisor_running("bridge", now=time.time() + 600))
        with mock.patch.object(sup, "pid_alive", return_value=False):
            self.assertFalse(sup.supervisor_running("bridge"))

    def test_kill_tree_uses_taskkill_without_a_shell(self):
        with mock.patch.object(sup.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(sup.kill_tree(4321))
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["taskkill", "/PID", "4321", "/T", "/F"])
        self.assertFalse(run.call_args[1].get("shell"))
        with mock.patch.object(sup.subprocess, "run", side_effect=FileNotFoundError("taskkill")):
            self.assertFalse(sup.kill_tree(4321))

    def test_launch_detached_starts_pythonw_with_no_inherited_handles_and_records_the_pid(self):
        popen = mock.Mock(return_value=mock.Mock(pid=999))
        os.makedirs(sup.run_dir(), exist_ok=True)
        with open(sup.stopfile_path("bridge"), "w") as f:
            f.write("stale")
        with mock.patch.object(sup, "child_env", return_value={"A": "1"}):
            pid = sup.launch_detached("bridge", home=self.home, exe="/x/python.exe", popen=popen)
        self.assertEqual(pid, 999)
        argv, kw = popen.call_args[0][0], popen.call_args[1]
        self.assertEqual(argv[1:], [sup.SCRIPT, "bridge", "--home", self.home])
        self.assertTrue(kw["close_fds"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kw[stream], subprocess.DEVNULL)
        if sys.platform == "win32":
            self.assertEqual(kw["creationflags"], sup.DETACHED_PROCESS | sup.CREATE_NEW_PROCESS_GROUP | sup.CREATE_NO_WINDOW)
        self.assertEqual(sup.read_pidfile(sup.pidfile_path("bridge"))["pid"], 999)
        self.assertFalse(os.path.exists(sup.stopfile_path("bridge")), "a stale stop request must not kill the new supervisor")

    def test_redirect_output_gives_pythonw_somewhere_to_print(self):
        log = os.path.join(self.tmp, "logs", "bridge.log")
        old = sys.stdout, sys.stderr
        try:
            sys.stdout = sys.stderr = None                 # what pythonw.exe starts with
            raw = sup.redirect_output(log)
            print("hello \u00e9", flush=True)
            sup.say("x")
            sys.stdout.flush()
        finally:
            sys.stdout, sys.stderr = old
        raw.close()
        with open(log, "rb") as f:
            text = f.read().decode("utf-8")
        self.assertIn("hello \u00e9", text)
        self.assertIn("supervisor: x", text)

    def test_redirect_output_rotates_a_huge_log(self):
        log = os.path.join(self.tmp, "logs", "big.log")
        os.makedirs(os.path.dirname(log))
        with open(log, "wb") as f:
            f.write(b"x" * 10)
        with mock.patch.object(sup, "LOG_ROTATE_BYTES", 5):
            old = sys.stdout, sys.stderr
            try:
                raw = sup.redirect_output(log)
            finally:
                sys.stdout, sys.stderr = old
        raw.close()
        self.assertTrue(os.path.exists(log + ".1"))
        self.assertEqual(os.path.getsize(log), 0)

    def test_server_supervisor_does_nothing_when_server_url_is_set(self):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"server_url": "https://wow.example.com"}, f)
        old = sys.stdout, sys.stderr
        try:
            self.assertEqual(sup.supervise("server", self.home), 0)
        finally:
            sys.stdout, sys.stderr = old
        with open(os.path.join(self.tmp, "logs", "server.log"), "r", encoding="utf-8") as f:
            self.assertIn("server_url is set", f.read())
        self.assertFalse(os.path.exists(sup.pidfile_path("server")))

    def test_autostart_launches_only_installed_services_that_are_not_running(self):
        os.makedirs(sup.run_dir())
        for k in ("server", "bridge"):
            open(sup.installed_marker(k), "w").close()
        with mock.patch.object(sup, "supervisor_running", side_effect=lambda k, now=None: k == "server"), \
                mock.patch.object(sup, "launch_detached") as launch:
            self.assertEqual(sup.autostart(self.home), 0)
        launch.assert_called_once_with("bridge", self.home)

    def test_main_rejects_unknown_arguments(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(sup.main(["bogus"]), 2)
            self.assertEqual(sup.main([]), 2)
        self.assertIn("usage", err.getvalue())

    def test_the_script_runs_from_another_folder_without_the_package_on_the_path(self):
        """Exactly how the Run key starts it: a script path, working directory elsewhere, no PYTHONPATH."""
        env = dict(os.environ, WOWMENTOR_HOME=self.home)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, sup.SCRIPT, "--autostart"], cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        r = subprocess.run([sys.executable, sup.SCRIPT, "nope"], cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)


# ---------------------------------------------------------------- services
class ServicesTests(TempHome):
    def setUp(self):
        TempHome.setUp(self)
        self.reg = FakeRegistry()
        p = mock.patch.object(win32, "registry", self.reg)
        p.start()
        self.addCleanup(p.stop)
        self.sv = win32.Win32Services(sleep=lambda s: None)

    def test_install_writes_one_run_value_named_for_this_home(self):
        where = self.sv.install("server", {})
        self.sv.install("bridge", {})
        (name, cmd), = self.reg.run.items()
        self.assertTrue(name.startswith("WoWMentor-"), name)
        self.assertIn(name, where)
        self.assertIn("pythonw.exe" if os.path.exists(os.path.join(os.path.dirname(sys.executable), "pythonw.exe")) else "--autostart", cmd)
        self.assertIn("--autostart", cmd)
        self.assertIn("--home", cmd)
        self.assertTrue(self.sv.is_installed("server") and self.sv.is_installed("bridge"))
        self.assertFalse(self.sv.is_installed("tunnel"))

    def test_without_a_custom_home_the_value_is_plain_wowmentor(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("WOWMENTOR_HOME")
            self.assertEqual(win32.run_value_name(), "WoWMentor")
            self.assertNotIn("--home", win32.run_command())

    def test_two_homes_never_share_a_value(self):
        a = win32.run_value_name()
        with mock.patch.dict(os.environ, {"WOWMENTOR_HOME": os.path.join(self.tmp, "other")}):
            self.assertNotEqual(win32.run_value_name(), a)

    def test_remove_keeps_the_run_value_until_the_last_service_is_gone(self):
        for k in ("server", "bridge"):
            self.sv.install(k, {})
        self.sv.remove("server")
        self.assertEqual(len(self.reg.run), 1)
        self.assertFalse(self.sv.is_installed("server"))
        self.assertTrue(self.sv.is_installed("bridge"))
        self.sv.remove("bridge")
        self.sv.remove("bridge")                            # idempotent
        self.assertEqual(self.reg.run, {})

    def test_a_registry_that_refuses_gives_a_readable_error(self):
        self.reg.fail = True
        with self.assertRaises(base.HostUnsupported) as cm:
            self.sv.install("bridge", {})
        self.assertIn("wowmentor start", str(cm.exception))
        self.assertFalse(self.sv.is_installed("bridge"))

    def test_a_run_command_over_the_registry_limit_is_refused(self):
        with mock.patch.object(win32, "run_command", return_value="x" * 300):
            with self.assertRaises(base.HostUnsupported) as cm:
                self.sv.install("bridge", {})
        self.assertIn("259", str(cm.exception))

    def test_unknown_kinds(self):
        self.assertFalse(self.sv.is_running("nope"))
        self.assertFalse(self.sv.is_installed("nope"))
        with self.assertRaises(ValueError):
            self.sv.install("nope", {})

    def test_build_helpers_has_nothing_to_build(self):
        self.assertEqual(self.sv.build_helpers({}), {"launcher": None, "mask": None, "summary": ""})

    def test_log_path_is_the_kind_log_in_the_log_folder(self):
        self.assertEqual(self.sv.log_path("bridge"), os.path.join(self.tmp, "logs", "bridge.log"))

    # -- start and stop, with a scripted process table
    def scripted(self, ignore_stop=False):
        alive = {}

        def launch(kind, home=None, **kw):
            alive[4242] = True
            sup.write_pidfile(sup.pidfile_path(kind), {"pid": 4242, "heartbeat": time.time()})
            return 4242

        def sleep(s):
            if not ignore_stop and os.path.exists(sup.stopfile_path("bridge")):
                alive[4242] = False                         # the supervisor saw the stop file and exited

        def kill(pid):
            alive[pid] = False
            return True
        self.alive, self.kill = alive, mock.Mock(side_effect=kill)
        self.sv = win32.Win32Services(sleep=sleep)
        for target, repl in (("launch_detached", launch), ("pid_alive", lambda pid: alive.get(pid, False)), ("kill_tree", self.kill)):
            p = mock.patch.object(sup, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def test_start_then_stop_ends_the_supervisor_politely(self):
        self.scripted()
        self.assertFalse(self.sv.is_running("bridge"))
        self.assertTrue(self.sv.start("bridge"))
        self.assertTrue(self.sv.is_running("bridge"))
        self.assertTrue(self.sv.stop("bridge"))
        self.assertFalse(self.sv.is_running("bridge"))
        self.kill.assert_not_called()
        self.assertEqual(os.listdir(sup.run_dir()), [])
        self.assertFalse(self.sv.stop("bridge"))             # nothing left to stop

    def test_a_supervisor_that_ignores_the_stop_file_is_killed_with_its_children(self):
        self.scripted(ignore_stop=True)
        self.sv.start("bridge")
        self.assertTrue(self.sv.stop("bridge"))
        self.kill.assert_called_once_with(4242)

    def test_restart_stops_the_old_supervisor_first(self):
        self.scripted()
        self.sv.start("bridge")
        self.alive[4242] = True
        self.assertTrue(self.sv.start("bridge"))
        self.assertTrue(self.sv.is_running("bridge"))

    def test_a_stale_pid_file_is_never_killed(self):
        self.scripted()
        sup.write_pidfile(sup.pidfile_path("bridge"), {"pid": 4242, "heartbeat": time.time() - 3600})
        self.alive[4242] = True                              # the pid now belongs to some unrelated program
        self.assertFalse(self.sv.is_running("bridge"))
        self.assertFalse(self.sv.stop("bridge"))
        self.kill.assert_not_called()

    def test_start_reports_a_supervisor_that_died_at_once(self):
        self.scripted()
        with mock.patch.object(sup, "pid_alive", return_value=False):
            self.assertFalse(self.sv.start("bridge"))

    def test_start_reports_a_launch_that_fails(self):
        with mock.patch.object(sup, "launch_detached", side_effect=FileNotFoundError("pythonw")):
            self.assertFalse(self.sv.start("bridge"))


# ---------------------------------------------------------------- shell and fs
class ShellTests(unittest.TestCase):
    def test_clipboard_bytes_are_utf16_crlf_nul_terminated(self):
        self.assertEqual(win32.clipboard_bytes("a\nb\r\nc \u00e9\U0001F600"), "a\r\nb\r\nc \u00e9\U0001F600\0".encode("utf-16-le"))
        self.assertEqual(win32.clipboard_bytes(""), b"\0\0")

    def test_copy_prefers_the_api_then_clip_exe_then_gives_up_quietly(self):
        sh = win32.Win32Shell()
        with mock.patch.object(win32, "set_clipboard_text", return_value=True), mock.patch.object(win32, "clip_exe_copy") as clip:
            self.assertTrue(sh.copy_to_clipboard("x"))
            clip.assert_not_called()
        with mock.patch.object(win32, "set_clipboard_text", side_effect=OSError("no user32")), mock.patch.object(win32, "clip_exe_copy", return_value=True):
            self.assertTrue(sh.copy_to_clipboard("x"))
        with mock.patch.object(win32, "set_clipboard_text", return_value=False), mock.patch.object(win32, "clip_exe_copy", return_value=False):
            self.assertFalse(sh.copy_to_clipboard("x"))

    def test_clip_exe_fallback_feeds_utf16_with_a_bom_and_survives_a_missing_clip(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        self.assertTrue(win32.clip_exe_copy("h\u00e9", run=run))
        self.assertEqual(run.call_args[0][0], ["clip"])
        self.assertEqual(run.call_args[1]["input"], b"\xff\xfe" + "h\u00e9".encode("utf-16-le"))
        self.assertFalse(win32.clip_exe_copy("x", run=mock.Mock(side_effect=FileNotFoundError("clip"))))

    def test_open_url_routes_web_addresses_and_files_and_never_raises(self):
        sh = win32.Win32Shell()
        with mock.patch.object(win32.webbrowser, "open") as web, mock.patch.object(os, "startfile", create=True) as sf:
            sh.open_url("https://example.com/a")
            web.assert_called_once_with("https://example.com/a")
            sh.open_url("C:\\x")
            sh.open_text_file("C:\\y.md")
            self.assertEqual([c[0][0] for c in sf.call_args_list], ["C:\\x", "C:\\y.md"])
        with mock.patch.object(os, "startfile", create=True, side_effect=OSError("no association")), mock.patch.object(win32.webbrowser, "open", side_effect=OSError):
            sh.open_url("C:\\x")
            sh.open_text_file("C:\\y")
            sh.open_url("https://e.com")
        sh.open_privacy_settings()


def os_error(winerror, cls=PermissionError):
    e = cls(13, "locked")
    e.winerror = winerror
    return e


class FsTests(TempHome):
    def test_replace_retries_only_the_known_windows_errors(self):
        sleeps, n = [], []

        def flaky(a, b):
            n.append(1)
            if len(n) <= 3:
                raise os_error((5, 32, 1224)[len(n) - 1])
        win32.replace_retrying("a", "b", sleep=sleeps.append, replace=flaky)
        self.assertEqual((len(n), len(sleeps)), (4, 3))
        self.assertTrue(all(0 < s <= 0.4 for s in sleeps))

    def test_replace_raises_other_errors_at_once_and_the_last_one_after_the_retries(self):
        calls = []
        with self.assertRaises(PermissionError):
            win32.replace_retrying("a", "b", sleep=lambda s: None, replace=lambda a, b: calls.append(1) or (_ for _ in ()).throw(os_error(1314)))
        self.assertEqual(len(calls), 1)
        calls.clear()
        with self.assertRaises(PermissionError):
            win32.replace_retrying("a", "b", tries=4, sleep=lambda s: None, replace=lambda a, b: calls.append(1) or (_ for _ in ()).throw(os_error(32)))
        self.assertEqual(len(calls), 4)
        with self.assertRaises(FileNotFoundError):
            win32.replace_retrying("a", "b", sleep=lambda s: None, replace=mock.Mock(side_effect=FileNotFoundError()))

    def test_write_private_restricts_the_temp_file_then_replaces_atomically(self):
        seen = []

        def acl(path):
            seen.append((path, os.path.exists(path)))
            return True
        fs = win32.Win32Fs(acl=acl, sleep=lambda s: None)
        target = os.path.join(self.tmp, "config.json")
        fs.write_private(target, b"one")
        fs.write_private(target, b"two")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"two")
        self.assertEqual(seen, [(target + ".tmp", True)] * 2)
        self.assertNotIn("config.json.tmp", os.listdir(self.tmp))

    def test_write_private_leaves_no_temp_file_and_keeps_the_old_one_on_failure(self):
        target = os.path.join(self.tmp, "keep.json")
        with open(target, "wb") as f:
            f.write(b"old")
        fs = win32.Win32Fs(acl=lambda p: True, sleep=lambda s: None)
        with mock.patch.object(win32, "replace_retrying", side_effect=os_error(32)):
            with self.assertRaises(PermissionError):
                fs.write_private(target, b"new")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"old")
        self.assertFalse(os.path.exists(target + ".tmp"))

    def test_acl_tightening_grants_before_it_drops_inheritance_and_never_raises(self):
        calls = []
        run = lambda argv, **kw: calls.append(argv) or mock.Mock(returncode=0)
        self.assertTrue(win32.tighten_acl("C:\\x\\c.json", run=run, env={"USERNAME": "ann", "USERDOMAIN": "PC"}))
        self.assertEqual(calls, [["icacls", "C:\\x\\c.json", "/grant:r", "PC\\ann:(F)"], ["icacls", "C:\\x\\c.json", "/inheritance:r"]])
        self.assertFalse(win32.tighten_acl("p", run=mock.Mock(side_effect=FileNotFoundError("icacls")), env={"USERNAME": "ann"}))
        self.assertFalse(win32.tighten_acl("p", run=lambda *a, **k: mock.Mock(returncode=5), env={"USERNAME": "ann"}))
        self.assertFalse(win32.tighten_acl("p", run=run, env={}))
        calls.clear()
        win32.tighten_acl("p", run=run, env={"USERNAME": "ann"})
        self.assertEqual(calls[0][3], "ann:(F)")


# ---------------------------------------------------------------- the host object
class HostObjectTests(unittest.TestCase):
    def test_make_imports_and_builds_without_native_bindings(self):
        import ctypes
        with mock.patch.object(ctypes, "CDLL", side_effect=AssertionError("CDLL")), mock.patch.dict(sys.modules, {"winreg": None}), \
                mock.patch.object(sys, "platform", "linux"):
            h = win32.make()
        self.assertEqual(h.name, "win32")
        self.assertIsInstance(h, base.Host)
        self.assertTrue(set(base.TEXT_DEFAULTS) <= set(h.text))
        self.assertEqual(h.text["terminal"], "PowerShell")
        self.assertEqual(h.text["this_machine"], "this PC")
        self.assertEqual(h.text["permission_lines"], ())
        self.assertEqual(h.text["privacy_settings"], "")
        self.assertIn("_forever_", h.text["example_wow_path"])

    def test_capture_and_mask_are_built_lazily_and_a_missing_module_does_not_break_the_host(self):
        h = win32.make()
        with mock.patch.dict(sys.modules, {"wowmentor.host.win32_capture": None, "wowmentor.host.win32_mask": None}):
            self.assertEqual(h.name, "win32")
            self.assertEqual(h.services.build_helpers({})["summary"], "")
            with self.assertRaises(ImportError):
                h.capture
            with self.assertRaises(ImportError):
                h.mask
        fake_cap, fake_mask = type("Win32Capture", (), {}), type("Win32Mask", (), {})
        mods = {"wowmentor.host.win32_capture": types.SimpleNamespace(Win32Capture=fake_cap), "wowmentor.host.win32_mask": types.SimpleNamespace(Win32Mask=fake_mask)}
        with mock.patch.dict(sys.modules, mods):
            self.assertIs(type(h.capture), fake_cap)
            self.assertIs(h.capture, h.capture)
            self.assertIs(type(h.mask), fake_mask)

    def test_host_package_selects_win32_on_windows(self):
        with mock.patch.dict(os.environ), mock.patch.object(sys, "platform", "win32"):
            os.environ.pop("WOWMENTOR_HOST", None)
            self.assertEqual(host.selected_name(), "win32")
            self.assertEqual(host.get().name, "win32")

    def test_queries_never_raise_on_a_machine_that_is_not_windows(self):
        h = win32.make()
        self.assertFalse(h.services.is_installed("bridge") and not ON_WINDOWS)
        self.assertFalse(h.services.is_running("bridge"))
        self.assertIsInstance(h.locations.find_wow_dirs(), list)
        self.assertTrue(h.shell.copy_to_clipboard("x") in (True, False))


# ================================================================ real Windows only
WINDOWS_ONLY = "needs a real Windows machine (a windows-latest runner runs these)"


@unittest.skipUnless(ON_WINDOWS, WINDOWS_ONLY)
class WindowsServerPortTests(unittest.TestCase):
    def test_a_second_process_cannot_bind_the_servers_port(self):
        srv = server_app.BoundedServer(("127.0.0.1", 0), server_app.Handler)
        self.addCleanup(srv.server_close)
        port = srv.server_address[1]
        for reuse in (False, True):
            s = socket.socket()
            self.addCleanup(s.close)
            if reuse:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)     # the hijack the hardening prevents
            with self.assertRaises(OSError):
                s.bind(("127.0.0.1", port))


@unittest.skipUnless(ON_WINDOWS, WINDOWS_ONLY)
class WindowsClipboardTests(unittest.TestCase):
    def test_round_trip(self):
        text = "WoW Mentor \u00e9\u4e2d \U0001F600"
        try:
            ok = win32.set_clipboard_text(text)
        except OSError as e:
            self.skipTest("no usable clipboard in this session: %s" % e)
        if not ok:
            self.skipTest("OpenClipboard failed: no interactive desktop in this session")
        self.assertEqual(win32.get_clipboard_text(), text)
        self.assertTrue(win32.Win32Shell().copy_to_clipboard("line one\nline two"))
        self.assertEqual(win32.get_clipboard_text(), "line one\r\nline two")


@unittest.skipUnless(ON_WINDOWS, WINDOWS_ONLY)
class WindowsRunKeyTests(TempHome):
    def test_install_and_remove_use_a_throwaway_value_name(self):
        sv = win32.Win32Services()
        name = win32.run_value_name()
        self.assertNotEqual(name, "WoWMentor", "the test must never touch the real autostart value")
        self.addCleanup(win32.registry.delete_run, name)
        sv.install("bridge", {})
        cmd = win32.registry.get_run(name)
        self.assertIn("--autostart", cmd)
        self.assertIn(self.home, cmd)
        self.assertTrue(sv.is_installed("bridge"))
        sv.remove("bridge")
        self.assertIsNone(win32.registry.get_run(name))
        self.assertFalse(sv.is_installed("bridge"))


@unittest.skipUnless(ON_WINDOWS, WINDOWS_ONLY)
class WindowsSupervisorLifecycleTests(TempHome):
    """The real thing: a detached supervisor process, a pid file, a stop. The supervised `tunnel` command exits at once (its
    cloudflared is this python, which rejects the arguments), so the supervisor also has to restart it with backoff."""

    def test_start_is_running_and_stop(self):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"cloudflared": sys.executable}, f)
        sv = win32.Win32Services()
        self.addCleanup(sv.stop, "tunnel")
        self.assertTrue(sv.start("tunnel"), "supervisor did not come up")
        deadline = time.time() + 15
        while time.time() < deadline and not (os.path.exists(os.path.join(self.tmp, "logs", "tunnel.log")) and "started tunnel" in open(os.path.join(self.tmp, "logs", "tunnel.log"), encoding="utf-8", errors="replace").read()):
            time.sleep(0.2)
        self.assertTrue(sv.is_running("tunnel"))
        info = sup.read_pidfile(sup.pidfile_path("tunnel"))
        self.assertTrue(sup.pid_alive(info["pid"]))
        self.assertTrue(sv.stop("tunnel"))
        self.assertFalse(sv.is_running("tunnel"))
        self.assertFalse(sup.pid_alive(info["pid"]))
        self.assertFalse(os.path.exists(sup.pidfile_path("tunnel")))
        self.assertFalse(sv.stop("tunnel"))


@unittest.skipUnless(ON_WINDOWS, WINDOWS_ONLY)
class WindowsProcessTests(unittest.TestCase):
    def test_pid_alive_sees_this_process_and_not_a_finished_one(self):
        self.assertTrue(sup.pid_alive(os.getpid()))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(sup.pid_alive(p.pid))
        self.assertFalse(sup.pid_alive(0x7FFFFFF0))


@unittest.skipUnless(ON_WINDOWS, WINDOWS_ONLY)
class WindowsAtomicReplaceTests(TempHome):
    def test_replace_waits_for_a_reader_to_let_go(self):
        dst, src = os.path.join(self.tmp, "dst.txt"), os.path.join(self.tmp, "src.txt")
        for p, data in ((dst, b"old"), (src, b"new")):
            with open(p, "wb") as f:
                f.write(data)
        held = open(dst, "rb")                              # no FILE_SHARE_DELETE: replacing it fails until it is closed
        threading.Timer(0.6, held.close).start()
        self.addCleanup(held.close)
        win32.Win32Fs().atomic_replace(src, dst)
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), b"new")

    def test_replace_gives_up_with_permissionerror_when_the_handle_is_never_released(self):
        dst, src = os.path.join(self.tmp, "dst.txt"), os.path.join(self.tmp, "src.txt")
        for p in (dst, src):
            with open(p, "wb") as f:
                f.write(b"x")
        with open(dst, "rb"):
            with self.assertRaises(PermissionError):
                win32.Win32Fs(sleep=lambda s: None).atomic_replace(src, dst)

    def test_write_private_really_replaces_and_survives_icacls(self):
        target = os.path.join(self.tmp, "secret.json")
        fs = win32.Win32Fs()
        fs.write_private(target, b"1")
        fs.write_private(target, b"2")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"2")
        self.assertFalse(os.path.exists(target + ".tmp"))


@unittest.skipUnless(ON_WINDOWS, WINDOWS_ONLY)
class WindowsFindWowTests(TempHome):
    def test_fake_folder_tree_through_the_real_scan(self):
        root = os.path.join(self.tmp, "Games", "World of Warcraft")
        retail, forever = make_tree(root, "_retail_", "_forever_")
        os.makedirs(os.path.join(root, "_classic_"))              # a client without AddOns is not listed
        loc = win32.Win32Locations()
        self.assertEqual(loc.find_wow_dirs([root]), [forever, retail])
        with mock.patch.object(win32, "registry", FakeRegistry([os.path.join(root, "_retail_")])), \
                mock.patch.object(win32, "default_roots", return_value=[]):
            self.assertEqual(loc.find_wow_dirs(), [forever, retail])

    def test_the_real_registry_and_drive_queries_do_not_raise(self):
        self.assertIsInstance(win32.registry.wow_install_paths(), list)
        self.assertTrue(win32.fixed_drives())
        self.assertIsInstance(win32.Win32Locations().find_wow_dirs(), list)
        self.assertIsNone(win32.registry.get_run("WoWMentorNoSuchValue-test"))


if __name__ == "__main__":
    unittest.main()
