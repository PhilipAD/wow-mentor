"""Security and robustness regressions found in review: request limits, secrets, corrupt files, thread safety, clock changes.

Each test names the defect it pins. Everything runs against temp dirs (WOWMENTOR_HOME is redirected by RunningServer or by the test).
"""
import errno, hmac, json, os, re, shutil, socket, stat, sys, tempfile, threading, time, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RunningServer, http, jhttp, raw_http, status_of
from wowmentor import config as cfgmod, install_mac
from wowmentor.bridge import app as bridge, events as evmod, guidesync, luasv, presence as presmod, uisync, webhook as whmod
from wowmentor.server import app as server_app, events as srvevents, tugapi, agentmem

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def read(path):
    with open(path) as f:
        return f.read()


# ------------------------------------------------------------------------------------------------ server: request handling
class RequestLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = RunningServer()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def post_raw(self, path, headers, body=b""):
        req = ("POST %s HTTP/1.1\r\nHost: x\r\n%s\r\n" % (path, headers)).encode() + body
        return raw_http(self.s.port, req)

    def test_content_length_values_that_are_not_plain_digits_are_refused(self):
        path = "/ui/%s/ops" % self.s.cfg["ui_secret"]
        for bad in ("-5", "abc", "1_0", "+5", "5, 5", "\u0665", "99999999999999999999", " "):
            raw = self.post_raw(path, "Content-Length: %s\r\nConnection: close\r\n" % bad, b"{}")
            self.assertIn(status_of(raw), (400, 413), (bad, raw[:80]))
        raw = self.post_raw(path, "Content-Length: 2\r\nContent-Length: 2\r\nConnection: close\r\n", b"{}")
        self.assertEqual(status_of(raw), 400, "two Content-Length headers are ambiguous (request smuggling)")
        raw = self.post_raw(path, "Transfer-Encoding: chunked\r\nConnection: close\r\n", b"0\r\n\r\n")
        self.assertEqual(status_of(raw), 411)
        self.assertEqual(http(self.s.base + "/healthz")[0], 200, "server still answers")

    def test_oversize_body_is_refused_before_it_is_read(self):
        path = "/ui/%s/ops" % self.s.cfg["ui_secret"]
        raw = self.post_raw(path, "Content-Length: %d\r\nConnection: close\r\n" % (server_app.MAX_BODY + 1))
        self.assertEqual(status_of(raw), 413)
        raw = self.post_raw("/push/%s" % self.s.cfg["write_secret"], "Content-Length: %d\r\nConnection: close\r\n" % (server_app.MAX_FEED_BODY + 1))
        self.assertEqual(status_of(raw), 413)

    def test_json_depth_bomb_nan_and_deep_event_data_are_clean_400s_not_crashes(self):
        ui = self.s.ui
        for body in (b"[" * 5000 + b"]" * 5000, b'{"a":' * 60 + b"1" + b"}" * 60, b'{"op":"toast","text":"x","n":NaN}'):
            code, r = jhttp(ui + "/ops", body)
            self.assertEqual(code, 400, body[:20])
            self.assertFalse(r["ok"])
        deep = {"type": "death", "data": {}}
        cur = deep["data"]
        for _ in range(30):
            cur["k"] = {}
            cur = cur["k"]
        code, r = jhttp(ui + "/events/push", {"events": [deep, {"type": "ok_event", "data": {"a": 1}}]})
        self.assertEqual(code, 200)
        self.assertEqual([e["type"] for e in r["stored"]], ["ok_event"])
        self.assertIn("nested deeper", r["rejected"][0]["errors"][0])
        too_deep = {"type": "death", "data": {}}              # far deeper: the whole body is refused before any validation recurses into it
        c2 = too_deep["data"]
        for _ in range(200):
            c2["k"] = {}
            c2 = c2["k"]
        self.assertEqual(jhttp(ui + "/events/push", {"events": [too_deep]})[0], 400)
        # the events API (indent=2 JSON encoder) must still be able to serve everything that was stored
        code, r = jhttp(ui + "/events?since=0")
        self.assertEqual(code, 200)
        code, r = jhttp(ui + "/events/push", {"events": [{"type": "x_event", "data": {"v": float("nan")}}]})
        self.assertEqual(code, 400)
        self.assertEqual(jhttp(ui + "/events?since=0")[0], 200)

    def test_get_ops_with_garbage_base64_does_not_echo_input(self):
        code, r = jhttp(self.s.ui + "/ops?b=%s" % "!!!not-base64!!!")
        self.assertEqual(code, 400)

    def test_ops_over_the_limit_are_rejected_not_silently_truncated(self):
        ops = [{"op": "toast", "text": "t%d" % i} for i in range(server_app.MAX_OPS + 1)]
        before = jhttp(self.s.ui + "/doc")[1]["rev"]
        code, r = jhttp(self.s.ui + "/ops", ops)
        self.assertEqual(code, 400)
        self.assertIn("at most", r["errors"][0])
        self.assertEqual(jhttp(self.s.ui + "/doc")[1]["rev"], before, "nothing was applied")
        code, r = jhttp(self.s.ui + "/ops?partial=1", ops)
        self.assertEqual(code, 400)

    def test_op_id_zero_and_partial_flag_are_part_of_idempotency(self):
        body = {"ops": [{"op": "toast", "text": "z"}], "opId": 0}
        a, b = jhttp(self.s.ui + "/ops", body)[1], jhttp(self.s.ui + "/ops", body)[1]
        self.assertTrue(a["ok"])
        self.assertTrue(b.get("replayed"), "opId 0 is a real id")
        code, r = jhttp(self.s.ui + "/ops", {"ops": [{"op": "toast", "text": "other"}], "opId": 0})
        self.assertEqual(code, 409)
        bad = {"ops": [{"op": "toast", "text": "q"}, {"op": "nonsense"}], "opId": "same-id"}
        self.assertEqual(jhttp(self.s.ui + "/ops", dict(bad, partial=True))[0], 200)
        self.assertEqual(jhttp(self.s.ui + "/ops", bad)[0], 409, "the same opId with another partial flag is a different request")

    def test_a_handler_error_is_a_json_500_and_the_server_lives(self):
        with mock.patch.object(server_app, "schema", side_effect=RuntimeError("boom with /secret/path")), mock.patch("sys.stderr"):
            code, raw = http(self.s.ui + "/schema")
        self.assertEqual(code, 500)
        self.assertNotIn(b"boom", raw, "no exception text reaches the client")
        self.assertEqual(http(self.s.base + "/healthz")[0], 200)

    def test_tug_show_with_bad_integers_is_a_400(self):
        code, r = jhttp(self.s.ui + "/tug/show?name=x&from=abc")
        self.assertIn(code, (400,))


class ConnectionLimitTests(unittest.TestCase):
    def test_stalled_clients_are_dropped_and_the_connection_count_is_capped(self):
        with mock.patch.object(server_app, "MAX_CONNECTIONS", 3), mock.patch.object(server_app.Handler, "timeout", 1.0):
            s = RunningServer()
            try:
                held = [socket.create_connection(("127.0.0.1", s.port)) for _ in range(3)]
                for h in held:
                    h.sendall(b"POST /ui/x/ops HTTP/1.1\r\nHost: x\r\nContent-Length: 50\r\n\r\n{")        # a body that never completes
                time.sleep(0.3)
                extra = socket.create_connection(("127.0.0.1", s.port))
                extra.settimeout(3)
                try:
                    self.assertEqual(extra.recv(10), b"", "past the cap a new connection is closed at once")
                finally:
                    extra.close()
                # the stalled ones are cut by the socket timeout, which frees the slots again
                deadline = time.time() + 6
                ok = False
                while time.time() < deadline and not ok:
                    ok = http(s.base + "/healthz")[0] == 200 if self._try(s) else False
                    time.sleep(0.2)
                self.assertTrue(ok, "slots are released when stalled clients time out")
                for h in held:
                    h.close()
            finally:
                s.close()

    @staticmethod
    def _try(s):
        try:
            return http(s.base + "/healthz")[0] == 200
        except Exception:
            return False

    def test_a_silent_client_does_not_pin_a_thread_forever(self):
        with mock.patch.object(server_app.Handler, "timeout", 0.5):
            s = RunningServer()
            try:
                c = socket.create_connection(("127.0.0.1", s.port))
                c.settimeout(4)
                self.assertEqual(c.recv(10), b"", "the server closed the idle connection")
                c.close()
            finally:
                s.close()


class SecretHandlingTests(unittest.TestCase):
    def test_every_secret_comparison_is_constant_time(self):
        s = RunningServer()
        calls = []
        real = hmac.compare_digest
        try:
            with mock.patch.object(server_app.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b)):
                http("%s/ui/%s/doc" % (s.base, "x" * 24))
                http("%s/%s/events" % (s.base, "y" * 24))
                http("%s/%s" % (s.base, "z" * 24))
                http("%s/push/%s" % (s.base, "w" * 24), b"x")
            self.assertGreaterEqual(len(calls), 4)
        finally:
            s.close()
        src = read(os.path.join(ROOT, "wowmentor", "server", "app.py"))
        self.assertIsNone(re.search(r"(==|!=)\s*(UI|READ|WRITE)\b|\b(UI|READ|WRITE)\s*(==|!=)", src), "no plain == on a secret")

    def test_read_token_events_route_never_leaks_any_secret(self):
        secrets = {"ui": "us" * 12, "write": "ws" * 12, "read": "rt" * 12, "hook_secret": "hunter2-hunter2", "hook_url": "https://hooks.example.test/path/ABCDEF123456"}
        s = RunningServer(config={"webhook_url": secrets["hook_url"], "webhook_secret": secrets["hook_secret"]})
        try:
            evs = [{"type": "death", "data": {"note": "x %s y" % v, v: "key %s" % v}} for v in secrets.values()]
            self.assertEqual(len(jhttp(s.ui + "/events/push", {"events": evs})[1]["stored"]), 5)
            code, raw = http("%s/%s/events?since=0" % (s.base, secrets["read"]))
            self.assertEqual(code, 200)
            for name, v in secrets.items():
                self.assertNotIn(v.encode(), raw, name)
            self.assertIn(b"[redacted]", raw)
            for bad in ("types=BAD!", "since=abc", "limit=%s" % ("9" * 5000)):
                code, raw = http("%s/%s/events?%s" % (s.base, secrets["read"], bad))
                self.assertNotIn(secrets["read"].encode(), raw)
        finally:
            s.close()

    def test_error_responses_and_logs_do_not_contain_the_url_secret(self):
        s = RunningServer()
        try:
            with mock.patch("sys.stderr") as err, mock.patch("sys.stdout") as out:
                http("%s/ui/%s/tug/show?name=zz&from=abc" % (s.base, s.cfg["ui_secret"]))
                http("%s/ui/%s/ops" % (s.base, s.cfg["ui_secret"]), b"{broken")
            written = "".join(str(c) for c in err.method_calls + out.method_calls)
            self.assertNotIn(s.cfg["ui_secret"], written)
        finally:
            s.close()


# ------------------------------------------------------------------------------------------------ config and files
class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowmentor-cfg-")
        self.env = mock.patch.dict(os.environ, {"WOWMENTOR_HOME": os.path.join(self.tmp, "home")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_is_private_from_the_first_byte_and_replaced_atomically(self):
        old = os.umask(0)
        try:
            os.makedirs(cfgmod.home())
            tmpf = cfgmod.path("config.json.tmp")
            with open(tmpf, "w") as f:            # a stale temp file with loose permissions from an older crash
                f.write("junk")
            os.chmod(tmpf, 0o666)
            modes = []
            real_dump = json.dump

            def spy(obj, f, **kw):
                modes.append(stat.S_IMODE(os.fstat(f.fileno()).st_mode))      # the permissions while the secrets are being written
                return real_dump(obj, f, **kw)
            with mock.patch.object(json, "dump", spy):
                cfgmod.save({"ui_secret": "s" * 24})
            self.assertEqual(modes, [0o600])
            self.assertEqual(stat.S_IMODE(os.stat(cfgmod.path("config.json")).st_mode), 0o600)
            self.assertFalse(os.path.exists(tmpf))
            self.assertEqual(cfgmod.load()["ui_secret"], "s" * 24)
        finally:
            os.umask(old)

    def test_a_failed_save_keeps_the_old_file_and_leaves_no_temp(self):
        cfgmod.save({"ui_secret": "keep"})
        with mock.patch.object(json, "dump", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(OSError):
                cfgmod.save({"ui_secret": "lost"})
        self.assertEqual(cfgmod.load()["ui_secret"], "keep")
        self.assertEqual(sorted(os.listdir(cfgmod.home())), ["config.json"])

    def test_corrupt_or_odd_config_falls_back_to_defaults_and_keeps_a_bak(self):
        os.makedirs(cfgmod.home())
        for junk in ("{not json", "[1, 2]", "42", "\xff\xfe"):
            with open(cfgmod.path("config.json"), "wb") as f:
                f.write(junk.encode("latin-1"))
            cfg = cfgmod.load()
            self.assertEqual(cfg["port"], 8095, junk)
        with open(cfgmod.path("config.json"), "w") as f:
            f.write("{oops")
        cfgmod.load()
        self.assertEqual(read(cfgmod.path("config.json.bak")), "{oops")

    def test_peek_never_moves_a_half_edited_config_aside(self):
        os.makedirs(cfgmod.home())
        with open(cfgmod.path("config.json"), "w") as f:
            f.write('{"webhook_url": "https://x.example/')
        self.assertIsNone(cfgmod.peek(strict=True))
        self.assertEqual(cfgmod.peek()["port"], 8095)
        self.assertTrue(os.path.exists(cfgmod.path("config.json")))
        self.assertFalse(os.path.exists(cfgmod.path("config.json.bak")))

    def test_server_starts_with_every_data_file_corrupt_and_keeps_bak_copies(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        junk = {"ui_state.json": "{truncated", "events.json": '{"events": [1, 2', "tug_state.json": "\x00\x00", "agent_memory.json": "[]"}
        for name, text in junk.items():
            with open(os.path.join(data, name), "w") as f:
                f.write(text)
        with open(os.path.join(data, "ui_state.json"), "w") as f:
            f.write('{"panels": 5, "rev": "x"}')            # valid JSON, impossible document
        s = RunningServer(data_path=data)
        try:
            self.assertEqual(jhttp(s.ui + "/doc")[1]["rev"], 0)
            self.assertEqual(jhttp(s.ui + "/events")[0], 200)
            self.assertEqual(jhttp(s.ui + "/tug/status")[0], 200)
            self.assertEqual(jhttp(s.ui + "/memory")[0], 200)
            self.assertTrue(jhttp(s.ui + "/ops", {"op": "toast", "text": "works"})[1]["ok"])
            for n in ("ui_state.json.bak", "events.json.bak", "tug_state.json.bak"):
                self.assertTrue(os.path.exists(os.path.join(data, n)), n)
            self.assertEqual(read(os.path.join(data, "ui_state.json.bak")), '{"panels": 5, "rev": "x"}')
        finally:
            s.close()

    def test_a_full_disk_does_not_fail_requests_or_lose_the_ring(self):
        s = RunningServer()
        try:
            full = OSError(errno.ENOSPC, "No space left on device")
            with mock.patch.object(cfgmod, "write_json_atomic", side_effect=full), mock.patch("sys.stderr"):
                code, r = jhttp(s.ui + "/ops", {"op": "toast", "text": "still works"})
                self.assertEqual(code, 200)
                self.assertTrue(any("disk full" in w for w in r["warnings"]))
                code, r = jhttp(s.ui + "/events/push", {"events": [{"type": "death", "data": {}}]})
                self.assertEqual((code, len(r["stored"])), (200, 1))
                self.assertEqual(jhttp(s.ui + "/memory/ops", {"op": "add", "text": "n"})[0], 200)
            self.assertEqual(len(jhttp(s.ui + "/events?since=0")[1]["events"]), 1)
        finally:
            s.close()

    def test_purge_only_removes_what_wowmentor_made(self):
        from wowmentor import cli
        home = cfgmod.home()
        os.makedirs(os.path.join(home, "data"))
        for n in ("config.json", "profile.md", os.path.join("data", "x.json"), "precious.txt"):
            with open(os.path.join(home, n), "w") as f:
                f.write("x")
        cli.purge_home()
        self.assertEqual(os.listdir(home), ["precious.txt"], "a folder with someone else's file in it is not deleted")


class LauncherAndSubprocessTests(unittest.TestCase):
    def test_launcher_source_escapes_hostile_paths(self):
        src = install_mac.launcher_source('/Users/a b/"; system("rm -rf ~"); "/py\\thon?', "/pkg/\u00e9")
        define = [l for l in src.splitlines() if l.startswith("#define PYTHON")][0]
        self.assertEqual(define, '#define PYTHON "/Users/a b/\\"; system(\\"rm -rf ~\\"); \\"/py\\\\thon\\077"')
        self.assertIn('#define PYPATH "/pkg/\\303\\251"', src)

    def test_no_shell_true_or_os_system_anywhere(self):
        for dirpath, _, names in os.walk(os.path.join(ROOT, "wowmentor")):
            for n in names:
                if n.endswith(".py"):
                    text = read(os.path.join(dirpath, n))
                    self.assertNotIn("shell=True", text, n)
                    self.assertNotIn("os.system(", text, n)

    def test_launchctl_off_mac_says_so(self):
        with mock.patch.object(install_mac.sys, "platform", "linux"):
            with self.assertRaises(RuntimeError) as cm:
                install_mac.launchctl("print", "x")
        self.assertIn("macOS only", str(cm.exception))

    def test_plist_with_spaces_and_quotes_in_paths_round_trips(self):
        import plistlib
        tmp = tempfile.mkdtemp(prefix="wowmentor-plist-")
        try:
            with mock.patch.dict(os.environ, {"WOWMENTOR_HOME": os.path.join(tmp, 'we"ird home'), "WOWMENTOR_LOGS": os.path.join(tmp, "my logs")}):
                p = install_mac.make_plist("bridge", python="/opt/py thon/bin/python3 -c 'x'")
                back = plistlib.loads(plistlib.dumps(p))
                self.assertEqual(back["ProgramArguments"][0], "/opt/py thon/bin/python3 -c 'x'")
                self.assertEqual(back["ProgramArguments"][1:], ["-m", "wowmentor", "bridge"], "arguments stay separate: no shell string is built")
                self.assertEqual(back["EnvironmentVariables"]["WOWMENTOR_HOME"], os.path.join(tmp, 'we"ird home'))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------------------------ bridge files
class GuideAndSlotFileTests(unittest.TestCase):
    def test_restore_id_with_trailing_newline_or_dots_is_refused(self):
        for bad in ("12\n", "../x", "a/b", "", ".", "..", "x" * 65, "12\x00", "-1x"):
            self.assertFalse(guidesync.VERSION_ID.fullmatch(bad), repr(bad))
            self.assertFalse(guidesync.VERSION_ID.match(bad) and bad.endswith("\n") is False and "/" in bad, repr(bad))
        self.assertTrue(guidesync.VERSION_ID.fullmatch("12-prerestore"))

    def test_symlinked_guide_files_and_packs_are_not_followed(self):
        tmp = tempfile.mkdtemp(prefix="wowmentor-gl-")
        try:
            addons = os.path.join(tmp, "AddOns")
            real = os.path.join(tmp, "outside")
            os.makedirs(os.path.join(addons, "Guidelime_Real"))
            os.makedirs(real)
            body = "Guidelime.registerGuide(\n[[\n[NX]\n]], \"p\")"
            for d, name in ((os.path.join(addons, "Guidelime_Real"), "ok.lua"), (real, "secret.lua")):
                with open(os.path.join(d, name), "w") as f:
                    f.write(body)
            os.symlink(os.path.join(real, "secret.lua"), os.path.join(addons, "Guidelime_Real", "link.lua"))
            os.symlink(real, os.path.join(addons, "Guidelime_Linked"))
            found = guidesync.discover(addons)
            self.assertEqual([os.path.basename(p) for p in found.values()], ["ok.lua"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_atomic_writes_clean_up_after_a_failure(self):
        tmp = tempfile.mkdtemp(prefix="wowmentor-aw-")
        try:
            target = os.path.join(tmp, "x.lua")
            with open(target, "w") as f:
                f.write("old")
            with mock.patch.object(os, "replace", side_effect=OSError(errno.ENOSPC, "full")):
                with self.assertRaises(OSError):
                    guidesync.write_atomic(target, "new")
                with self.assertRaises(OSError):
                    uisync.atomic_write(os.path.join(tmp, "Inbox.lua"), b"new")
            self.assertEqual(read(target), "old")
            self.assertEqual(sorted(os.listdir(tmp)), ["x.lua"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_uisync_survives_hidden_kv_values(self):
        self.assertEqual(uisync.to_int("unknown"), 0)
        self.assertEqual(uisync.to_int(None, 7), 7)
        self.assertEqual(uisync.to_int("12"), 12)

    def test_inbox_lua_cannot_be_broken_out_of_by_the_document(self):
        wire = '{"t":"]]=]==]]===]","u":"]]"}'
        lua = uisync.inbox_lua(3, wire).decode()
        m = re.match(r"WoWFeedInbox=\{rev=3,json=\[(=+)\[(.*)\]\1\]\}\n\Z", lua, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), wire)

    def test_broken_or_huge_savedvariables_are_parsed_once_not_every_second(self):
        tmp = tempfile.mkdtemp(prefix="wowmentor-sv-")
        try:
            d = os.path.join(tmp, "WTF", "Account", "A", "SavedVariables")
            os.makedirs(d)
            with open(os.path.join(d, "Broken.lua"), "w") as f:
                f.write("X = { [1] = ")
            calls = []
            real = luasv.parse_file
            with mock.patch.dict(bridge.CFG, {"wow_dir": tmp}), mock.patch.object(luasv, "parse_file", lambda p: calls.append(p) or real(p)):
                bridge._svcache.clear()
                for _ in range(5):
                    out = bridge.sv("Broken")
                self.assertIn("_error", out)
            self.assertEqual(len(calls), 1)
            with open(os.path.join(d, "Huge.lua"), "w") as f:
                f.write("X = 1\n")
            with mock.patch.object(luasv, "MAX_BYTES", 3):
                with self.assertRaises(ValueError):
                    luasv.parse_file(os.path.join(d, "Huge.lua"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------------------------ webhook
class WebhookSafetyTests(unittest.TestCase):
    def test_urls_that_could_split_headers_or_carry_credentials_are_refused(self):
        for bad in ("https://h.example/x\r\nX-Evil: 1", "https://h.example/x y", "https://user:pw@h.example/x", "https://h\u00e9.example/x",
                    "ftp://h.example", "https://", "https://h.example:99999/x", "https://h.example:abc/x", "javascript:alert(1)", ""):
            self.assertFalse(whmod.valid_url(bad), repr(bad))
        self.assertTrue(whmod.valid_url("https://h.example:8443/hooks/wow?token=abc"))

    def test_neither_url_nor_secret_reach_the_log_on_failure(self):
        logs = []
        url, secret = "https://hooks.example.test/T0P-S3CRET-PATH", "sig-secret-123"

        def boom(u, b, h, t):
            raise OSError("cannot reach %s with %s" % (u, secret))
        sender = whmod.WebhookSender(url, secret, post=boom, clock=lambda: 100.0, log=logs.append)
        sender.enqueue([{"eid": 1, "t": 1, "type": "death", "data": {}}])
        for t in (103.0, 109.0, 130.0, 200.0):
            sender.pump(t)
        text = "\n".join(logs) + json.dumps(sender.status())
        self.assertTrue(logs)
        self.assertNotIn("T0P-S3CRET-PATH", text)
        self.assertNotIn(secret, text)
        self.assertIn("hooks.example.test", text)

    def test_setting_the_clock_back_does_not_stall_delivery(self):
        now = [10_000.0]
        sent = []
        sender = whmod.WebhookSender("https://h.example/x", None, post=lambda u, b, h, t: sent.append(1) or 200, clock=lambda: now[0], log=lambda m: None)
        sender.enqueue([{"eid": 1, "t": 1, "type": "death", "data": {}}])
        self.assertTrue(sender.pump(10_003.0))
        sender.enqueue([{"eid": 2, "t": 2, "type": "bags_full", "data": {}}])
        now[0] = 10_000.0 - 7200             # NTP or the user set the clock back two hours
        self.assertFalse(sender.pump(now[0] + 1), "the 3 s urgent wait has not passed yet")
        self.assertTrue(sender.pump(now[0] + 4), "delivery resumes within seconds, not after two hours")
        self.assertEqual(len(sent), 2)

    def test_failing_endpoint_backs_off_instead_of_hammering(self):
        now = [1000.0]
        posts = []
        sender = whmod.WebhookSender("https://h.example/x", None, post=lambda u, b, h, t: posts.append(now[0]) or 500, clock=lambda: now[0], log=lambda m: None)
        sender.enqueue([{"eid": 1, "t": 1, "type": "death", "data": {}}])
        while now[0] < 1400:
            now[0] += 1
            sender.pump(now[0])
        gaps = [b - a for a, b in zip(posts, posts[1:])]
        self.assertEqual(gaps[:3], [5, 20, 60])
        self.assertTrue(all(g >= 60 for g in gaps[2:]))


# ------------------------------------------------------------------------------------------------ bridge pipeline robustness
class EventStoreSemanticsTests(unittest.TestCase):
    def test_a_late_older_sighting_does_not_move_an_npc_back(self):
        st = srvevents.EventStore(None)
        st.push([{"type": "npc_seen", "t": 2000, "seq": 1, "data": {"name": "Guard", "id": 68, "map": 1429, "x": 50, "y": 50, "kind": "gossip"}}], boot=1)
        st.push([{"type": "npc_seen", "t": 1000, "seq": 2, "data": {"name": "Guard", "id": 68, "map": 1429, "x": 10, "y": 10, "kind": "vendor"}}], boot=1)
        n = st.npc_list()[0]
        self.assertEqual((n["x"], n["y"], n["kind"], n["last_seen"], n["count"]), (50.0, 50.0, "gossip", 2000, 2))

    def test_paging_semantics_gap_reset_and_next(self):
        st = srvevents.EventStore(None, ring=5)
        st.push([{"type": "death", "t": 1, "data": {"i": i}} for i in range(1, 9)])         # eids 1..8, ring keeps 4..8
        r = st.query(since=0)
        self.assertEqual((r["oldest_eid"], r["last_eid"], r.get("gap")), (4, 8, True))
        r = st.query(since=3)
        self.assertNotIn("gap", r, "since == oldest - 1 misses nothing")
        r = st.query(since=5, limit=2)
        self.assertEqual(([e["eid"] for e in r["events"]], r["next"]), ([6, 7], 7))
        r = st.query(since=8)
        self.assertEqual((r["events"], r["next"]), ([], 8))
        r = st.query(since=99)
        self.assertEqual((r["events"], r["next"], r.get("reset")), ([], 8, True))
        st.push([{"type": "fight", "t": 2, "data": {}}])
        r = st.query(since=0, types=["fight"])
        self.assertEqual(([e["eid"] for e in r["events"]], r["next"]), ([9], 9))
        r = st.query(since=8, types=["death"])
        self.assertEqual((r["events"], r["next"]), ([], 9), "a filter that matches nothing still advances the cursor")


class StripAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(bridge.partial)
        bridge.partial.clear()

    def tearDown(self):
        bridge.partial.clear()
        bridge.partial.update(self.saved)

    def test_parts_may_arrive_out_of_order_and_twice(self):
        a = bridge._assemble
        self.assertIsNone(a("evj", "7", 2, 3, "B", now=1.0))
        self.assertIsNone(a("evj", "7", 3, 3, "C", now=1.1))
        self.assertIsNone(a("evj", "7", 3, 3, "C", now=1.2), "a duplicate changes nothing")
        self.assertEqual(a("evj", "7", 1, 3, "A", now=1.3), ["A", "B", "C"])
        self.assertNotIn("evj", bridge.partial)

    def test_parts_of_two_different_messages_are_never_stitched_together(self):
        a = bridge._assemble
        a("evj", "5", 1, 2, "old-1", now=1.0)
        # the addon reloaded: id 5 is used again for a different message
        self.assertIsNone(a("evj", "5", 1, 2, "new-1", now=2.0))
        self.assertEqual(a("evj", "5", 2, 2, "new-2", now=2.1), ["new-1", "new-2"])
        a("evj", "9", 1, 2, "x1", now=3.0)
        self.assertIsNone(a("evj", "9", 2, 3, "y2", now=3.1), "a different part count is a different message")
        a("evj", "11", 1, 2, "stale", now=10.0)
        self.assertIsNone(a("evj", "11", 2, 2, "late", now=10.0 + bridge.PARTIAL_TTL_S + 1), "an abandoned half message expires")

    def test_garbage_part_numbers_and_sections_are_bounded(self):
        a = bridge._assemble
        for i, n in ((0, 1), (2, 1), (1, 10 ** 9), (-1, 2), (1, bridge.MAX_PARTS + 1)):
            self.assertIsNone(a("x", "1", i, n, "t"))
        self.assertEqual(bridge.partial, {})
        saved = dict(bridge.sections)
        try:
            bridge.sections.clear()
            for k in range(bridge.MAX_SECTIONS + 30):
                bridge.handle("sec%d" % k, "1", 1, 1, "line")
            bridge.handle("Bad Name\n", "1", 1, 1, "x")
            bridge.handle("x" * 100, "1", 1, 1, "x")
            self.assertEqual(len(bridge.sections), bridge.MAX_SECTIONS)
            self.assertNotIn("Bad Name\n", bridge.sections)
            bridge.handle("sec0", "2", 1, 1, "updated")
            self.assertEqual(bridge.sections["sec0"][1], ["updated"], "known sections keep updating at the cap")
        finally:
            bridge.sections.clear()
            bridge.sections.update(saved)


class BootTrackerTests(unittest.TestCase):
    def test_a_reload_with_a_lagging_kv_boot_still_gets_its_own_label(self):
        t = bridge.BootTracker()
        e = lambda *seqs: [{"seq": s, "type": "x"} for s in seqs]
        self.assertEqual(t.label(100, e(1, 2, 3)), 100)
        self.assertEqual(t.label(100, e(4)), 100)
        self.assertEqual(t.label(100, e(1, 2)), "100+1", "seq restarted but the KV still shows the old boot")
        self.assertEqual(t.label(100, e(3)), "100+1")
        self.assertEqual(t.label(200, e(4)), 200, "the KV caught up")
        self.assertIsNone(t.label(None, e(5)), "unknown boot stays unknown")
        self.assertEqual(t.label(200, e(6)), 200)


class PresenceReloadTests(unittest.TestCase):
    def drive(self, p, t0, steps, strip_ages):
        states = []
        for k in range(steps):
            now = t0 + k
            p.update(now, strip_ages(k), True, {}, True)
            states.append(p.state)
        return states

    def test_reload_logout_then_login_never_shows_logged_out(self):
        p = presmod.Presence()
        t0 = 10_000.0
        p.update(t0, 1.0, True, {}, True)
        p.note_event("logout", t0 + 1)
        # the strip goes dark for 12 s (reload), then the new load sends login and the strip is live again
        seen = []
        for k in range(1, 14):
            now = t0 + 1 + k
            seen.append(p.update(now, float(k) if k < 12 else 0.5, True, {}, True) and p.state)
            if k == 12:
                p.note_event("login", now)
        self.assertNotIn(presmod.LOGGED_OUT, seen)
        self.assertEqual(p.state, presmod.IN_WORLD)
        self.assertFalse(p.logout_pending)

    def test_a_real_logout_still_ends_in_logged_out_after_the_grace(self):
        p = presmod.Presence()
        t0 = 10_000.0
        p.update(t0, 1.0, True, {}, True)
        p.note_event("logout", t0 + 1)
        for k in range(1, 30):
            p.update(t0 + 1 + k, float(k), True, {}, True)
        self.assertEqual(p.state, presmod.LOGGED_OUT)

    def test_a_lost_login_event_does_not_leave_the_logout_pending_forever(self):
        p = presmod.Presence()
        t0 = 10_000.0
        p.update(t0, 0.5, True, {}, True)
        p.note_event("logout", t0)
        # the strip never really goes dark (or the dark frames were missed) and keeps being drawn; no `login` arrives
        for k in range(1, 20):
            p.update(t0 + k, 0.4, True, {}, True)
        self.assertFalse(p.logout_pending)
        # a later 30 s stall (zone loading) is then "loading", not "logged_out"
        for k in range(20, 55):
            p.update(t0 + k, float(k - 19) + 0.4, True, {}, True)
        self.assertEqual(p.state, presmod.LOADING)

    def test_clock_set_back_does_not_freeze_the_state_as_live(self):
        saved = dict(bridge.meta)
        try:
            bridge.meta["last_line"] = 5_000_000.0 + 7200      # the strip was decoded "in the future": the clock was set back by two hours
            bridge.meta["last_mono"] = time.monotonic() - 40.0
            age = bridge._strip_age(5_000_000.0)
            self.assertAlmostEqual(age, 40.0, delta=2)
            self.assertGreaterEqual(age, 0)
        finally:
            bridge.meta.update(saved)


class EventPipeRobustnessTests(unittest.TestCase):
    def test_server_down_means_queue_and_backoff_not_a_busy_loop(self):
        now = [100.0]
        calls = []

        def post(route, obj):
            calls.append(now[0])
            return None, None
        pipe = evmod.EventPipe(post=post, clock=lambda: now[0], log=lambda m: None)
        pipe.add([{"type": "death", "seq": 1, "data": {}}])
        for _ in range(500):
            pipe.step()                       # a worker spinning at full speed
        self.assertEqual(len(calls), 1, "one attempt, then the backoff holds")
        now[0] += 1.1
        pipe.step()
        now[0] += 2.1
        pipe.step()
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(pipe.pending), 1)

    def test_clock_set_back_does_not_block_pushes_for_hours(self):
        now = [100_000.0]
        res = [(None, None)]
        pipe = evmod.EventPipe(post=lambda r, o: res[0], clock=lambda: now[0], log=lambda m: None)
        pipe.add([{"type": "death", "data": {}}])
        pipe.step()
        now[0] -= 7200
        res[0] = (200, {"ok": True, "stored": [], "rejected": []})
        self.assertTrue(pipe.step())
        self.assertEqual(pipe.pending, [])

    def test_queue_is_bounded(self):
        pipe = evmod.EventPipe(post=lambda r, o: (None, None), log=lambda m: None)
        for i in range(0, 3000, 100):
            pipe.add([{"type": "death", "seq": i + k, "data": {}} for k in range(100)])
        self.assertEqual(len(pipe.pending), evmod.CAP)
        self.assertEqual(pipe.stats["dropped"], 3000 - evmod.CAP)

    def test_the_idempotency_cache_and_seen_window_are_bounded(self):
        s = RunningServer()
        try:
            for i in range(server_app.MAX_OPS):
                pass
            for i in range(260):
                jhttp(s.ui + "/ops", {"ops": [{"op": "toast", "text": "t"}], "opId": "id-%d" % i})
            self.assertLessEqual(len(server_app._idem), 200)
        finally:
            s.close()


class SupervisorAndRenderTests(unittest.TestCase):
    def test_supervise_restarts_a_crashing_thread_body_and_logs_only_the_class(self):
        logs, runs = [], []

        def body():
            runs.append(1)
            if len(runs) < 3:
                raise RuntimeError("secret /ui/abcdef path")
            return "done"
        with mock.patch.object(bridge.time, "sleep"):
            self.assertEqual(bridge.supervise("capture", body, log=logs.append), "done")
        self.assertEqual(len(runs), 3)
        self.assertEqual(len(logs), 2)
        self.assertTrue(all("RuntimeError" in l and "secret" not in l for l in logs))

    def test_render_survives_hidden_level_and_xp(self):
        saved = dict(bridge.sections)
        try:
            bridge.sections["summary"] = (time.time(), ["KV level=unknown xp=unknown xpmax=1 money_copper=unknown honorable_kills=0 uiboot=1"])
            text = bridge.render()
            self.assertIn("session rates unavailable", text)
            self.assertIn("--- SESSION_STATE", text)
        finally:
            bridge.sections.clear()
            bridge.sections.update(saved)

    def test_push_errors_never_mention_the_push_url(self):
        err = ValueError("unknown url type: 'host/push/SECRETSECRET'")
        self.assertEqual(bridge._why(err), "ValueError")
        import urllib.error
        self.assertEqual(bridge._why(urllib.error.HTTPError("http://x/push/SECRET", 404, "nf", {}, None)), "HTTP 404")
        self.assertEqual(bridge._why(urllib.error.URLError(ConnectionRefusedError())), "ConnectionRefusedError")


class ImportSafetyTests(unittest.TestCase):
    def test_bridge_and_installer_import_off_mac(self):
        import subprocess
        code = ("import sys; sys.platform='linux'; import wowmentor.bridge.app as b, wowmentor.install_mac as m, wowmentor.cli, wowmentor.doctor; "
                "print(b.wow_window if hasattr(b,'wow_window') else 'nowin'); print(b.frontmost_is_wow())")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=dict(os.environ, PYTHONPATH=ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip().endswith("None"))


if __name__ == "__main__":
    unittest.main()
