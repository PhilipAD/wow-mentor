"""macOS host pieces that are pure logic (they run on every OS): launchd plists, the compiled launcher's source, launchctl's guard."""
import os, plistlib, shutil, subprocess, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from wowmentor import host, install_mac
from wowmentor.host import base, darwin


class LauncherTests(unittest.TestCase):
    def test_launcher_source_escapes_hostile_paths(self):
        src = darwin.launcher_source('/Users/a b/"; system("rm -rf ~"); "/py\\thon?', "/pkg/\u00e9")
        define = [l for l in src.splitlines() if l.startswith("#define PYTHON")][0]
        self.assertEqual(define, '#define PYTHON "/Users/a b/\\"; system(\\"rm -rf ~\\"); \\"/py\\\\thon\\077"')
        self.assertIn('#define PYPATH "/pkg/\\303\\251"', src)

    def test_launchctl_off_mac_says_so(self):
        with mock.patch.object(darwin.sys, "platform", "linux"):
            with self.assertRaises(RuntimeError) as cm:
                darwin.launchctl("print", "x")
        self.assertIsInstance(cm.exception, base.HostUnsupported)
        self.assertIn("macOS only", str(cm.exception))


class PlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowmentor-plist-")
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp, "WOWMENTOR_HOME": os.path.join(self.tmp, 'we"ird home'), "WOWMENTOR_LOGS": os.path.join(self.tmp, "my logs")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plists_are_valid(self):
        for kind in ("server", "bridge", "tunnel"):
            p = darwin.write_plist(kind)
            with open(p, "rb") as f:
                data = plistlib.load(f)
            self.assertEqual(data["Label"], darwin.label(kind))
            self.assertNotEqual(data["Label"], darwin.LABELS[kind], "a custom WOWMENTOR_HOME must not share the real install's launchd label")
            self.assertTrue(data["KeepAlive"])
            self.assertIn("wowmentor", " ".join(data["ProgramArguments"]) + data["EnvironmentVariables"]["PYTHONPATH"])
            if sys.platform == "darwin" and shutil.which("plutil"):
                self.assertEqual(subprocess.run(["plutil", "-lint", p], capture_output=True).returncode, 0)

    def test_plist_with_spaces_and_quotes_in_paths_round_trips(self):
        p = darwin.make_plist("bridge", python="/opt/py thon/bin/python3 -c 'x'")
        back = plistlib.loads(plistlib.dumps(p))
        self.assertEqual(back["ProgramArguments"][0], "/opt/py thon/bin/python3 -c 'x'")
        self.assertEqual(back["ProgramArguments"][1:], ["-m", "wowmentor", "bridge"], "arguments stay separate: no shell string is built")
        self.assertEqual(back["EnvironmentVariables"]["WOWMENTOR_HOME"], os.path.join(self.tmp, 'we"ird home'))

    def test_the_bridge_plist_uses_the_launcher_app_when_given(self):
        p = darwin.make_plist("bridge", bridge_app="/Users/x/Applications/WoW Mentor Bridge.app")
        self.assertEqual(p["ProgramArguments"], ["/Users/x/Applications/WoW Mentor Bridge.app/Contents/MacOS/wowmentor-bridge"])

    def test_the_services_object_installs_with_the_launcher_from_the_helpers(self):
        svc = darwin.make().services
        path = svc.install("bridge", {}, {"launcher": "/L/App.app"})
        with open(path, "rb") as f:
            self.assertEqual(plistlib.load(f)["ProgramArguments"], ["/L/App.app/Contents/MacOS/wowmentor-bridge"])
        self.assertTrue(svc.is_installed("bridge"))
        with mock.patch.object(darwin, "stop_agent", return_value=True):
            svc.remove("bridge")
        self.assertFalse(svc.is_installed("bridge"))
        self.assertFalse(svc.is_installed("server"))

    def test_the_old_install_mac_names_still_resolve(self):
        for name in ("POOL", "ADDON", "PKG", "PKG_ROOT", "find_wow_dirs", "install_addon", "create_slots", "remove_addon", "slots_present",
                     "addon_installed_version", "addons_dir", "valid_wav", "label", "make_plist", "write_plist", "plist_path", "launchctl",
                     "start_agent", "stop_agent", "agent_running", "remove_plist", "build_launcher", "build_mask", "launcher_source", "APP_NAME", "app_path"):
            self.assertTrue(hasattr(install_mac, name), name)


class LocationsTests(unittest.TestCase):
    def test_default_places_on_a_mac(self):
        loc = darwin.make().locations
        self.assertTrue(loc.config_home().endswith(os.path.join(".config", "wowmentor")))
        self.assertTrue(loc.log_dir().endswith(os.path.join("Library", "Logs", "WoWMentor")))
        self.assertIn("/opt/homebrew/bin", loc.extra_bin_dirs())

    def test_text_names_the_mac(self):
        t = darwin.make().text
        self.assertEqual(t["this_machine"], "this Mac")
        self.assertIn("Screen & System Audio Recording", t["privacy_settings"])


if __name__ == "__main__":
    unittest.main()
