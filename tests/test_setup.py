import contextlib, io, json, os, shutil, sys, tempfile, time, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wowmentor import cli, config as cfgmod, doctor, host, install
from wowmentor.host.fake import FakeCapture, FakeHost
from wowmentor.bridge import webhook as whmod
from helpers import RunningServer, jhttp
from test_webhook import Receiver


class SetupTests(unittest.TestCase):
    """Runs the installer against a fake WoW folder and a fake home. Never touches launchd, the real home or the real game."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowmentor-setup-")
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp, "WOWMENTOR_HOME": os.path.join(self.tmp, "cfg"), "WOWMENTOR_LOGS": os.path.join(self.tmp, "logs")})
        self.env.start()
        self.wow = os.path.join(self.tmp, "Applications", "World of Warcraft", "_forever_")
        os.makedirs(os.path.join(self.wow, "Interface", "AddOns", "SomeOtherAddon"))
        with open(os.path.join(self.wow, "Interface", "AddOns", "SomeOtherAddon", "x.lua"), "w") as f:
            f.write("keep me")
        self.fake = FakeHost(name="darwin", capture=FakeCapture(game=None))      # nothing here touches launchd, the screen or the real game
        self.host = host.using(self.fake)
        self.host.__enter__()

    def tearDown(self):
        self.host.__exit__(None, None, None)
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_setup_is_complete_and_idempotent(self):
        code, out = self.run_cli("setup", "--wow-dir", self.wow, "--no-build", "--no-launchd")
        self.assertEqual(code, 0, out)
        cfg = cfgmod.load()
        for k in ("read_token", "write_secret", "ui_secret"):
            self.assertGreaterEqual(len(cfg[k]), 32)
        if os.name == "posix":
            self.assertEqual(oct(os.stat(cfgmod.path("config.json")).st_mode & 0o777), "0o600", "secrets file is private")
        addons = os.path.join(self.wow, "Interface", "AddOns")
        for f in ("WoWFeed.toc", "WoWFeed.lua", "UI.lua", "JSON.lua", "Codec.lua"):
            self.assertTrue(os.path.exists(os.path.join(addons, "WoWFeed", f)), f)
        self.assertEqual(install.slots_present(self.wow), install.POOL + 1)
        self.assertTrue(os.path.exists(os.path.join(addons, "WoWFeed", "sig", "ok.wav")))
        with open(os.path.join(addons, "SomeOtherAddon", "x.lua")) as f:
            self.assertEqual(f.read(), "keep me", "other addons untouched")
        self.assertTrue(os.path.exists(cfgmod.path("profile.md")))
        first = dict(cfg)
        code, _ = self.run_cli("setup", "--wow-dir", self.wow, "--no-build", "--no-launchd")
        self.assertEqual(code, 0)
        self.assertEqual({k: cfgmod.load()[k] for k in ("read_token", "write_secret", "ui_secret")}, {k: first[k] for k in ("read_token", "write_secret", "ui_secret")}, "secrets are stable")

    def test_find_wow_dirs_prefers_forever(self):
        root = os.path.join(self.tmp, "Applications", "World of Warcraft")
        os.makedirs(os.path.join(root, "_retail_", "Interface", "AddOns"))
        found = install.find_wow_dirs([root])
        self.assertEqual(found[0], self.wow)
        self.assertEqual(len(found), 2)

    def test_links_and_prompt(self):
        self.run_cli("setup", "--wow-dir", self.wow, "--no-build", "--no-launchd")
        cfg = cfgmod.load()
        code, out = self.run_cli("links")
        self.assertEqual(code, 0)
        self.assertIn(cfg["ui_secret"], out)
        self.assertIn(cfg["read_token"], out)
        self.assertIn("wowmentor share on", out)
        self.assertIn("/skill", out)
        cfg["public_url"] = "https://example.trycloudflare.com"
        cfgmod.save(cfg)
        self.assertIn("https://example.trycloudflare.com/ui/" + cfg["ui_secret"], self.run_cli("links")[1])

    def test_no_launchd_is_still_accepted_as_the_old_name_of_no_services(self):
        for flag in ("--no-launchd", "--no-services"):
            code, out = self.run_cli("setup", "--wow-dir", self.wow, "--no-build", flag)
            self.assertEqual(code, 0, out)
            self.assertIn("Skipped starting services", out)
        self.assertEqual([c for c in self.fake.services.calls if c[0] in ("install", "start")], [], "no service is touched")

    def test_setup_starts_both_services_and_opens_the_permission_page_when_the_host_has_one(self):
        self.fake.text["permission_lines"] = ("5/5  grant it", "     choose %(app)s")
        code, out = self.run_cli("setup", "--wow-dir", self.wow, "--no-build")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.services.calls, [("install", "server"), ("install", "bridge"), ("start", "server"), ("start", "bridge")])
        self.assertIn("choose WoW Mentor Bridge", out)
        self.assertEqual(self.fake.shell.privacy_opened, 1)

    def test_setup_says_so_and_stops_on_a_host_without_capture(self):
        with host.using(FakeHost(capture=FakeCapture(ok=False, reason="no capture here"))):
            code, out = self.run_cli("setup", "--wow-dir", self.wow, "--no-build")
        self.assertEqual(code, 1)
        self.assertIn("no capture here", out)

    def test_doctor_reports_without_crashing_when_nothing_runs(self):
        cfg = cfgmod.load()
        self.assertIn((doctor.BAD, "Not set up yet"), [(st, t) for st, t, _ in doctor.checks(cfg)], "not set up yet is a clear red item")
        self.run_cli("setup", "--wow-dir", self.wow, "--no-build", "--no-launchd")
        cfg = cfgmod.load()
        cfg["port"] = 1                      # nothing listens there
        titles = [t for _, t, _ in doctor.checks(cfg)]
        self.assertTrue(any("Server is not running" in t for t in titles), titles)

    def test_uninstall_removes_only_ours(self):
        self.run_cli("setup", "--wow-dir", self.wow, "--no-build", "--no-launchd")
        code, out = self.run_cli("uninstall", "--yes")
        self.assertEqual(code, 0, out)
        addons = os.path.join(self.wow, "Interface", "AddOns")
        self.assertEqual(sorted(os.listdir(addons)), ["SomeOtherAddon"])
        self.assertTrue(os.path.exists(cfgmod.path("config.json")), "config kept without --purge")

    def test_profile_text_strips_template_comments(self):
        self.assertEqual(cfgmod.profile_text(), "")
        os.makedirs(cfgmod.home(), exist_ok=True)
        with open(cfgmod.path("profile.md"), "w") as f:
            f.write("# comment\nGoal: win.\n")
        self.assertEqual(cfgmod.profile_text(), "Goal: win.")


class RemoteServerAndWebhookTests(unittest.TestCase):
    """server_url, `wowmentor webhook ...` and the doctor rows for session and webhook. Fake home; never touches launchd or the real game."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowmentor-cli-")
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp, "WOWMENTOR_HOME": os.path.join(self.tmp, "cfg"), "WOWMENTOR_LOGS": os.path.join(self.tmp, "logs")})
        self.env.start()
        os.environ.pop("WOWMENTOR_WEBHOOK_SECRET", None)
        self.wow = os.path.join(self.tmp, "World of Warcraft", "_forever_")
        os.makedirs(os.path.join(self.wow, "Interface", "AddOns"))
        self.fake = FakeHost(name="darwin", capture=FakeCapture(game=None))
        self.host = host.using(self.fake)
        self.host.__enter__()

    def tearDown(self):
        self.host.__exit__(None, None, None)
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO(stdin or "")):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def cfg_with_secrets(self, **extra):
        cfg = cfgmod.load()
        cfgmod.ensure_secrets(cfg)
        cfg.update(extra)
        cfgmod.save(cfg)
        return cfg

    # ---- config
    def test_server_url_changes_every_base_url(self):
        cfg = cfgmod.ensure_secrets(cfgmod.load())
        self.assertEqual(cfgmod.server_root(cfg), "http://127.0.0.1:8095")
        self.assertIsNone(cfgmod.server_url(cfg))
        cfg["server_url"] = " https://wow.example.test:8443/ "
        self.assertEqual(cfgmod.server_url(cfg), "https://wow.example.test:8443")
        self.assertEqual(cfgmod.ui_base(cfg), "https://wow.example.test:8443/ui/" + cfg["ui_secret"])
        self.assertEqual(cfgmod.feed_url(cfg), "https://wow.example.test:8443/" + cfg["read_token"])
        cfg["public_url"] = "https://tunnel.example.test"
        self.assertTrue(cfgmod.ui_base(cfg, public=True).startswith("https://tunnel.example.test/ui/"), "a share tunnel still wins for public links")
        for ok in ("http://10.0.0.5:8095", "https://a.example.test", "https://a.example.test/prefix"):
            self.assertTrue(cfgmod.valid_server_url(ok), ok)
        for bad in ("", None, "ftp://x", "https://u:p@host", "https://host?x=1", "https://host#f", "https://ho st", "host:80"):
            self.assertFalse(cfgmod.valid_server_url(bad), bad)

    def test_server_url_command(self):
        self.assertEqual(self.run_cli("server-url", "status")[0], 0)
        self.assertEqual(self.run_cli("server-url", "set", "nonsense")[0], 1)
        code, out = self.run_cli("server-url", "set", "https://wow.example.test/")
        self.assertEqual(code, 0)
        self.assertEqual(cfgmod.load()["server_url"], "https://wow.example.test")
        if os.name == "posix":
            self.assertEqual(oct(os.stat(cfgmod.path("config.json")).st_mode & 0o777), "0o600")
        self.assertIn("https://wow.example.test/...", self.run_cli("server-url", "status")[1])
        self.assertEqual(self.run_cli("server-url", "off")[0], 0)
        self.assertIsNone(cfgmod.load()["server_url"])

    def test_setup_installs_no_local_server_when_server_url_is_set(self):
        cfg = self.cfg_with_secrets(server_url="https://wow.example.test")
        code, out = self.run_cli("setup", "--wow-dir", self.wow, "--no-build")
        self.assertEqual(code, 0, out)
        self.assertEqual([c[1] for c in self.fake.services.calls if c[0] == "install"], ["bridge"])
        self.assertEqual([c[1] for c in self.fake.services.calls if c[0] == "start"], ["bridge"])
        self.assertIn("No local server installed", out)
        self.assertNotIn("wow.example.test/ui", out)
        self.fake.services.calls.clear()
        c = cfgmod.load(); c["server_url"] = None; cfgmod.save(c)
        self.run_cli("setup", "--wow-dir", self.wow, "--no-build")
        self.assertEqual([c[1] for c in self.fake.services.calls if c[0] == "install"], ["server", "bridge"], "without server_url both services are installed as before")

    def test_start_skips_the_server_for_a_remote_one_and_share_refuses(self):
        self.cfg_with_secrets(server_url="https://wow.example.test")
        self.run_cli("start")
        self.assertEqual([c[1] for c in self.fake.services.calls if c[0] == "start"], ["bridge"])
        code, out = self.run_cli("share", "on")
        self.assertEqual(code, 1)
        self.assertIn("server_url is set", out)

    def test_links_show_the_remote_server_and_the_events_link(self):
        cfg = self.cfg_with_secrets(server_url="https://wow.example.test")
        code, out = self.run_cli("links")
        self.assertEqual(code, 0)
        self.assertIn("server_url", out)
        self.assertIn("https://wow.example.test/ui/" + cfg["ui_secret"], out)
        self.assertIn("https://wow.example.test/%s/events" % cfg["read_token"], out)
        self.assertNotIn("share on", out.split("Paste this")[0], "sharing makes no sense for a remote server")

    # ---- webhook command
    def test_webhook_set_validates_saves_and_hides_secrets(self):
        self.assertEqual(self.run_cli("webhook", "status")[0], 0)
        self.assertIn("off", self.run_cli("webhook", "status")[1])
        for bad in ("nope", "ftp://x.test/a", "file:///etc/passwd", "https://"):
            self.assertEqual(self.run_cli("webhook", "set", bad)[0], 1, bad)
        self.assertIsNone(cfgmod.load()["webhook_url"])
        url = "https://hooks.example.test/in/very-private-token?k=1"
        code, out = self.run_cli("webhook", "set", url, "--secret", "topsecret-value")
        self.assertEqual(code, 0, out)
        cfg = cfgmod.load()
        self.assertEqual((cfg["webhook_url"], cfg["webhook_secret"]), (url, "topsecret-value"))
        if os.name == "posix":
            self.assertEqual(oct(os.stat(cfgmod.path("config.json")).st_mode & 0o777), "0o600")
        for text in (out, self.run_cli("webhook", "status")[1]):
            self.assertNotIn("very-private-token", text)
            self.assertNotIn("topsecret-value", text)
            self.assertIn("hooks.example.test", text)
        # no --secret keeps the old one; --no-secret removes it; '-' reads stdin
        self.run_cli("webhook", "set", "https://hooks.example.test/other")
        self.assertEqual(cfgmod.load()["webhook_secret"], "topsecret-value")
        self.run_cli("webhook", "set", "https://hooks.example.test/other", "--no-secret")
        self.assertIsNone(cfgmod.load()["webhook_secret"])
        self.run_cli("webhook", "set", "https://hooks.example.test/other", "--secret", "-", stdin="from-stdin\n")
        self.assertEqual(cfgmod.load()["webhook_secret"], "from-stdin")
        with mock.patch.dict(os.environ, {"WOWMENTOR_WEBHOOK_SECRET": "from-env"}):
            self.run_cli("webhook", "set", "https://hooks.example.test/other")
        self.assertEqual(cfgmod.load()["webhook_secret"], "from-env")
        self.assertIn("not encrypted", self.run_cli("webhook", "set", "http://hooks.example.test/x")[1])
        self.assertNotIn("not encrypted", self.run_cli("webhook", "set", "http://127.0.0.1:9/x")[1])
        self.assertEqual(self.run_cli("webhook", "off")[0], 0)
        cfg = cfgmod.load()
        self.assertEqual((cfg["webhook_url"], cfg["webhook_secret"]), (None, None))

    def test_webhook_test_reports_the_response_code(self):
        self.assertEqual(self.run_cli("webhook", "test")[0], 1, "nothing configured")
        r = Receiver([200, 500, 404])
        try:
            self.cfg_with_secrets(webhook_url=r.url, webhook_secret="sig-secret")
            code, out = self.run_cli("webhook", "test")
            self.assertEqual(code, 0, out)
            self.assertIn("HTTP 200", out)
            self.assertIn("(signed)", out)
            req = r.requests[0]
            self.assertTrue(whmod.verify("sig-secret", req["body"], req["headers"]["x-wowmentor-signature"]))
            body = json.loads(req["body"])
            self.assertEqual((body["v"], body["events"][0]["type"]), (1, "webhook_test"))
            for text in (out,):
                self.assertNotIn("abc-private-path", text)
                self.assertNotIn("sig-secret", text)
            code, out = self.run_cli("webhook", "test")
            self.assertEqual(code, 1)
            self.assertIn("HTTP 500", out)
            self.assertIn("2xx", out)
            self.assertIn("HTTP 404", self.run_cli("webhook", "test")[1])
        finally:
            r.close()
        self.cfg_with_secrets(webhook_url="http://127.0.0.1:1/never")
        code, out = self.run_cli("webhook", "test")
        self.assertEqual(code, 1)
        self.assertIn("Could not reach", out)
        self.assertNotIn("/never", out)

    def test_webhook_status_reads_the_bridge_report(self):
        s = RunningServer()
        try:
            url = "https://hooks.example.test/in/private"
            self.cfg_with_secrets(server_url=s.base, ui_secret=s.cfg["ui_secret"], read_token=s.cfg["read_token"], write_secret=s.cfg["write_secret"], webhook_url=url)
            self.assertIn("no report yet", self.run_cli("webhook", "status")[1])
            jhttp(s.ui + "/session", {"state": "in_world", "uploader_alive_at": time.time(),
                                     "webhook": {"configured": True, "sent_events": 9, "posts": 4, "queued": 2, "failures": 1, "last_status": 500, "failing": True, "last_error": "HTTP 500"}})
            out = self.run_cli("webhook", "status")[1]
            for needle in ("9 event(s) in 4 post(s)", "queued: 2", "failures: 1", "HTTP 500", "Currently failing"):
                self.assertIn(needle, out)
            self.assertNotIn("private", out)
        finally:
            s.close()

    # ---- doctor rows (need only a server, so they run on any OS)
    def test_doctor_session_and_webhook_rows(self):
        s = RunningServer()
        try:
            cfg = self.cfg_with_secrets(server_url=s.base, ui_secret=s.cfg["ui_secret"], read_token=s.cfg["read_token"], write_secret=s.cfg["write_secret"])
            now = time.time()
            rows = doctor.session_checks(cfg, now)
            self.assertEqual(rows[0][0], doctor.WARN)
            self.assertIn("has not reported", rows[0][1])
            jhttp(s.ui + "/session", {"state": "in_world", "foreground": True, "uploader_alive_at": now})
            rows = doctor.session_checks(cfg, now)
            self.assertEqual(rows[0][:2], (doctor.OK, "Game session: in_world (WoW is in front)"))
            self.assertEqual(len(rows), 1, "no webhook row when none is configured")
            jhttp(s.ui + "/session", {"state": "game_closed", "foreground": False, "uploader_alive_at": now})
            self.assertEqual(doctor.session_checks(cfg, now)[0][0], doctor.WARN)
            # the bridge went quiet
            s2 = RunningServer()
            try:
                cfg2 = dict(cfg, server_url=s2.base, ui_secret=s2.cfg["ui_secret"])
                jhttp(s2.ui + "/session", {"state": "in_world", "uploader_alive_at": now - 600})
                self.assertIn("stopped reporting", doctor.session_checks(cfg2, now)[0][1])
            finally:
                s2.close()
            # webhook rows
            cfg["webhook_url"] = "https://hooks.example.test/in/private-path"
            cfg["webhook_secret"] = None
            wb = {"configured": True, "queued": 0}
            jhttp(s.ui + "/session", {"state": "in_world", "uploader_alive_at": now, "webhook": wb})
            row = doctor.session_checks(cfg, now)[1]
            self.assertEqual(row[0], doctor.OK)
            self.assertIn("nothing delivered yet", row[1])
            self.assertIn("not signed", row[1])
            jhttp(s.ui + "/session", {"state": "in_world", "uploader_alive_at": now, "webhook": dict(wb, last_ok_at=now - 5, sent_events=3)})
            self.assertIn("delivers", doctor.session_checks(cfg, now)[1][1])
            jhttp(s.ui + "/session", {"state": "in_world", "uploader_alive_at": now, "webhook": dict(wb, failing=True, last_error="HTTP 500", queued=4)})
            row = doctor.session_checks(cfg, now)[1]
            self.assertEqual(row[0], doctor.WARN)
            self.assertIn("HTTP 500", row[1])
            self.assertEqual(row[2], "Run: wowmentor webhook test")
            jhttp(s.ui + "/session", {"state": "in_world", "uploader_alive_at": now, "webhook": {"configured": False}})
            self.assertIn("not active", doctor.session_checks(cfg, now)[1][1])
            for _, title, fix in doctor.session_checks(cfg, now):
                self.assertNotIn("private-path", title + fix)
            cfg["webhook_url"] = "gopher://x"
            self.assertEqual(doctor.session_checks(cfg, now)[1][0], doctor.BAD)
        finally:
            s.close()

    def test_doctor_session_check_when_the_server_is_down(self):
        cfg = self.cfg_with_secrets(server_url="http://127.0.0.1:1")
        rows = doctor.session_checks(cfg)
        self.assertEqual(rows[0][0], doctor.WARN)
        self.assertIn("Could not read the session", rows[0][1])

    def test_doctor_names_a_remote_server(self):
        cfg = self.cfg_with_secrets(server_url="http://127.0.0.1:1", wow_dir=self.wow)
        titles = [t for _, t, _ in doctor.checks(cfg)]
        self.assertTrue(any(t.startswith("Remote server http://127.0.0.1:1/... is not reachable") for t in titles), titles)


if __name__ == "__main__":
    unittest.main()
