import json, os, subprocess, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RunningServer, jhttp
from wowmentor.agent import wowui

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


class AgentCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = RunningServer()
        text = "Guidelime.registerGuide(\n[[\n[GA Alliance]\n[NDemo Guide]\nAccept: [QA1]\nLearn your Level 2 skills.\n]], \"P\")"
        jhttp(cls.s.ui + "/tug/file", {"name": "Zone/Demo Guide", "text": text})

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def wowui(self, *args, ok=True, url=None, stdin=None):
        env = dict(os.environ, WOWUI_URL=url or self.s.ui, PYTHONPATH=ROOT)
        r = subprocess.run([sys.executable, "-m", "wowmentor.agent"] + list(args), capture_output=True, text=True, env=env, cwd=ROOT, input=stdin, timeout=60)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        else:
            self.assertEqual(r.returncode, 1)
        return r

    def test_overlay_commands(self):
        r = self.wowui("plan", "next", "--title", "Next steps", "--task", "a::!First thing::why", "--task", "Second", "--progress", "bags", "--callout", "Tip", "--idle-fade", "30")
        self.assertTrue(json.loads(r.stdout)["ok"])
        self.wowui("add-task", "next", "Third", "--priority", "low")
        self.wowui("check", "next", "a")
        self.wowui("toast", "Hello", "--level", "good")
        self.wowui("waypoint", "w", "--map", "1429", "--x", "48.9", "--y", "41", "--title", "Here")
        self.wowui("builtin", "me", "off")
        state = json.loads(self.wowui("state", "--compact").stdout)
        self.assertIn("next", state["panels"])
        self.assertEqual(state["waypoints"], ["w"])
        doc = json.loads(self.wowui("doc").stdout)
        self.assertEqual(doc["builtin"], {"me": False})

    def test_errors_are_json_on_stderr(self):
        r = self.wowui("plan", "bad", "--task", "x", "--anchor", "MIDDLE", ok=False)
        self.assertFalse(json.loads(r.stderr)["ok"])
        self.assertIn("anchor", r.stderr)

    def test_guide_memory_and_brief_commands(self):
        out = self.wowui("tug", "list").stdout
        self.assertIn("Demo Guide", out)
        self.assertIn("Learn your Level 2", self.wowui("tug", "show", "demo").stdout)
        r = self.wowui("tug", "insert", "demo", "--after-match", "Accept:", "--note", "tip", "Added by test")
        self.assertTrue(json.loads(r.stdout)["ok"])
        self.assertIn("Added by test", self.wowui("tug", "show", "demo").stdout)
        self.wowui("tug", "delete", "demo", "--match", "no such text", ok=False)
        self.assertIn("pending", self.wowui("tug", "status").stdout)
        self.wowui("memory", "summary", "Reach level 10 safely.")
        self.wowui("memory", "add", "Likes bandages", "--tag", "note")
        brief = self.wowui("brief").stdout
        self.assertIn("Reach level 10 safely.", brief)
        self.assertIn("Likes bandages", brief)

    def test_guide_and_example_text(self):
        self.assertIn("overlay", self.wowui("guide").stdout.lower())
        self.assertIn("panel", self.wowui("example").stdout)

    def test_missing_endpoint_has_friendly_error(self):
        env = dict(os.environ, PYTHONPATH=ROOT, WOWMENTOR_HOME="/nonexistent-wowmentor-home")
        env.pop("WOWUI_URL", None)
        r = subprocess.run([sys.executable, "-m", "wowmentor.agent", "state"], capture_output=True, text=True, env=env, cwd=ROOT)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no endpoint", r.stderr)

    # ---------------------------------------------------------------- rules, coach, events
    def rules(self):
        return {r["id"]: r for r in jhttp(self.s.ui + "/doc")[1]["rules"]}

    def push_events(self, events, boot=1):
        return jhttp(self.s.ui + "/events/push", {"boot": boot, "events": events})[1]

    def test_rule_presets_make_the_five_example_rules(self):
        self.wowui("rule", "clear")
        for name in ("bags-full", "durability", "deaths", "lower-rank"):
            self.assertTrue(json.loads(self.wowui("rule", "preset", name).stdout)["ok"])
        self.wowui("rule", "preset", "buff", "--set", "name=Power Word: Fortitude")
        rules = self.rules()
        self.assertEqual(sorted(rules), ["preset-bags-full", "preset-buff-power-word-fortitude", "preset-deaths", "preset-durability", "preset-lower-rank"])
        self.assertEqual(rules["preset-bags-full"]["when"], {"type": "bags_free_lte", "n": 2})
        self.assertEqual(rules["preset-durability"]["when"], {"type": "durability_lte", "pct": 20})
        self.assertEqual(rules["preset-deaths"]["when"], {"type": "deaths_in_subzone", "count": 2, "minutes": 10})
        self.assertEqual(rules["preset-lower-rank"]["when"], {"type": "lower_rank_on_bar"})
        self.assertEqual(rules["preset-buff-power-word-fortitude"]["when"], {"type": "buff_missing", "ooc": True, "name": "Power Word: Fortitude"})
        for r in rules.values():
            self.assertEqual([a["do"] for a in r["then"]], ["toast", "chat"])
            self.assertTrue(r["enabled"])
            for a in r["then"]:
                self.assertNotRegex(a["text"].lower(), r"\blevel\b|\bxp\b")
        self.assertIn("{free}", rules["preset-bags-full"]["then"][0]["text"])
        self.assertIn("{pct}", rules["preset-durability"]["then"][0]["text"])
        self.assertIn("{count}", rules["preset-deaths"]["then"][0]["text"])

    def test_rule_preset_settings_and_errors(self):
        self.wowui("rule", "clear")
        self.wowui("rule", "preset", "bags-full", "--set", "n=5", "--set", "cooldown_s=30", "--set", "chat=false", "--set", "text=Bags: {free} left", "--set", "id=mybags", "--set", "once=true")
        r = self.rules()["mybags"]
        self.assertEqual((r["when"]["n"], r["cooldown_s"], r["once"], [a["do"] for a in r["then"]], r["then"][0]["text"]), (5, 30, True, ["toast"], "Bags: {free} left"))
        self.assertIn("name", self.wowui("rule", "preset", "buff", ok=False).stderr)
        self.assertIn("unknown --set key", self.wowui("rule", "preset", "durability", "--set", "bogus=1", ok=False).stderr)
        self.assertIn("key=value", self.wowui("rule", "preset", "durability", "--set", "nonsense", ok=False).stderr)
        self.assertIn("nothing to do", self.wowui("rule", "preset", "durability", "--set", "toast=false", "--set", "chat=false", ok=False).stderr)
        self.wowui("rule", "preset", "durability", "--set", "pct=999", ok=True)         # the server clamps numbers; it is not an error
        self.wowui("rule", "preset", "buff", "--set", "spell=1243", "--set", "pet=true", "--set", "ooc=false")
        self.assertEqual(self.rules()["preset-buff-1243"]["when"], {"type": "buff_missing", "ooc": False, "spell": 1243, "pet": True})

    def test_rule_add_list_remove_clear(self):
        self.wowui("rule", "clear")
        rule = {"id": "ding", "when": {"type": "quest_complete"}, "then": [{"do": "toast", "text": "Turn in {name}", "level": "good"}], "cooldown_s": 5}
        self.assertTrue(json.loads(self.wowui("rule", "add", "--json", json.dumps(rule)).stdout)["ok"])
        self.wowui("rule", "add", stdin=json.dumps([dict(rule, id="a2"), dict(rule, id="a3", once=True)]))
        listed = json.loads(self.wowui("rule", "list").stdout)
        self.assertEqual((listed["count"], sorted(r["id"] for r in listed["rules"])), (3, ["a2", "a3", "ding"]))
        self.assertEqual(json.loads(self.wowui("rule", "list", "--compact").stdout)["count"], 3)
        r = self.wowui("rule", "add", "--json", json.dumps({"id": "bad", "when": {"type": "nope"}, "then": []}), ok=False)
        self.assertIn("when", r.stderr)
        self.wowui("rule", "add", "--json", "{not json", ok=False)
        self.wowui("rule", "add", "--json", "[1,2]", ok=False)
        self.wowui("rule", "remove", "a2")
        self.assertIn("no rule", self.wowui("rule", "remove", "a2", ok=False).stderr)
        self.assertEqual(sorted(self.rules()), ["a3", "ding"])
        self.wowui("rule", "clear")
        self.assertEqual(self.rules(), {})

    def test_coach_messages(self):
        self.assertTrue(json.loads(self.wowui("coach", "Nice fight").stdout)["ok"])
        self.wowui("coach", "Careful here", "--toast", "--level", "warn", "--sound", "warning")
        self.wowui("coach", "Quiet note", "--no-chat")
        inbox = jhttp(self.s.ui + "/doc")[1]["inbox"]
        self.assertEqual([(m["text"], m["chat"], m["toast"]) for m in inbox[-3:]], [("Nice fight", True, False), ("Careful here", True, True), ("Quiet note", False, False)])
        self.assertEqual((inbox[-2]["level"], inbox[-2]["sound"]), ("warn", "warning"))

    def test_questions_and_answers(self):
        self.push_events([{"seq": 1, "type": "coach_question", "t": 1794300000, "data": {"id": 31, "text": "Should I buy the bag?"}}], boot=77)
        qs = json.loads(self.wowui("questions").stdout)
        self.assertIn("Should I buy the bag?", [q["text"] for q in qs["questions"]])
        r = json.loads(self.wowui("coach", "Yes, bags first", "--answer", "31").stdout)
        self.assertEqual(r["answered"], ["31"])
        self.assertNotIn("31", [q["id"] for q in json.loads(self.wowui("questions").stdout)["questions"]])
        self.assertIn("31", [q["id"] for q in json.loads(self.wowui("questions", "--all").stdout)["questions"]])
        self.assertIn("no open question", self.wowui("coach", "late", "--answer", "31", ok=False).stderr)

    def test_events_npcs_session(self):
        base = jhttp(self.s.ui + "/events?limit=1")[1]["last_eid"]
        self.push_events([{"seq": i, "type": t, "t": 1794300000 + i, "data": {"n": i}} for i, t in ((1, "death"), (2, "fight"), (3, "death"))], boot=88)
        self.push_events([{"seq": 4, "type": "npc_seen", "data": {"name": "Cli Vendor", "id": 4242, "map": 1429, "x": 3, "y": 4, "kind": "merchant"}}], boot=88)
        r = json.loads(self.wowui("events", "--since", str(base), "--types", "death").stdout)
        self.assertEqual([e["data"]["n"] for e in r["events"]], [1, 3])
        r = json.loads(self.wowui("events", "--since", str(base), "--limit", "2", "--compact").stdout)
        self.assertEqual((len(r["events"]), r["next"]), (2, base + 2))
        self.assertGreaterEqual(len(json.loads(self.wowui("events").stdout)["events"]), 4)
        self.assertEqual(json.loads(self.wowui("npcs", "--name", "cli vendor").stdout)["npcs"][0]["id"], 4242)
        self.assertEqual(json.loads(self.wowui("session").stdout)["state"], "unknown")
        jhttp(self.s.ui + "/session", {"state": "loading", "foreground": "unknown", "uploader_alive_at": time.time()})
        self.assertEqual(json.loads(self.wowui("session").stdout)["state"], "loading")

    def test_events_follow_prints_new_events_as_json_lines(self):
        last = jhttp(self.s.ui + "/events?limit=1")[1]["last_eid"]

        def later():
            time.sleep(1.0)
            self.push_events([{"seq": 901, "type": "bags_full", "data": {"free": 0}}, {"seq": 902, "type": "death", "data": {}}], boot=91)
        t = threading.Thread(target=later)
        t.start()
        out = self.wowui("events", "--follow", "--interval", "0.3", "--duration", "3", "--types", "bags_full,death").stdout
        t.join()
        lines = [json.loads(l) for l in out.splitlines()]
        self.assertEqual([e["type"] for e in lines], ["bags_full", "death"])
        self.assertTrue(all(e["eid"] > last for e in lines), "follow starts at 'now', not at history")
        out = self.wowui("events", "--follow", "--since", str(last), "--interval", "0.3", "--duration", "0.5").stdout
        self.assertTrue(len(out.splitlines()) >= 2, "--since replays from that cursor first")

    def test_partial_flag_applies_valid_ops_and_reports_the_rest(self):
        ops = [{"op": "toast", "text": "partial ok"}, {"op": "panel", "id": "bad id!"}, {"op": "toast", "text": "second"}]
        r = self.wowui("ops", "--partial", stdin=json.dumps(ops))
        res = json.loads(r.stdout)
        self.assertEqual((res["applied"], [x["index"] for x in res["rejected"]]), (2, [1]))
        r = self.wowui("--partial", "ops", stdin=json.dumps(ops))
        self.assertEqual(json.loads(r.stdout)["applied"], 2, "the flag works before the command too")
        self.wowui("ops", stdin=json.dumps(ops), ok=False)
        self.wowui("ops", "--partial", stdin=json.dumps([{"op": "nope"}]), ok=False)

    def test_op_id_flag_makes_an_identical_call_a_replay(self):
        r1 = json.loads(self.wowui("toast", "only once", "--op-id", "cli-fixed-1").stdout)
        n = len(jhttp(self.s.ui + "/doc")[1]["toasts"])
        r2 = json.loads(self.wowui("toast", "only once", "--op-id", "cli-fixed-1").stdout)
        self.assertTrue(r2["replayed"])
        self.assertEqual(r2["rev"], r1["rev"])
        self.assertEqual(len(jhttp(self.s.ui + "/doc")[1]["toasts"]), n)

    def test_brief_includes_session_events_questions_and_rules(self):
        self.wowui("rule", "clear")
        self.wowui("rule", "preset", "durability")
        self.push_events([{"seq": 1, "type": "death", "data": {"zone": "Brieftown"}}, {"seq": 2, "type": "coach_question", "data": {"id": 8, "text": "Anything else?"}}], boot=55)
        brief = self.wowui("brief").stdout
        for needle in ("## Session", "Active rules", "preset-durability", "durability_lte(pct=20)", "Brieftown", "Anything else?", "Last 10 events"):
            self.assertIn(needle, brief)

    def test_skill_page_documents_the_new_features(self):
        skill = jhttp(self.s.ui + "/skill")[1]
        for needle in ("wowui rule preset", "wowui coach", "wowui events", "wowui questions", "wowui session", "wowui npcs", "opId", "partial", "clear_yards", "auto",
                       "X-WoWMentor-Signature", "SESSION_STATE", "never show the player's level or XP", "/events?since="):
            self.assertIn(needle, skill)
        self.assertNotIn("Level 10: visit", skill)
        # every command the skill promises exists in the CLI
        for sub in (["rule", "list"], ["coach", "x", "--help"], ["events", "--help"], ["npcs", "--help"], ["session", "--help"], ["questions", "--help"]):
            self.wowui(*sub)


class OpsRetryTests(unittest.TestCase):
    """wowui sends every ops request with an opId and retries with the *same* one."""

    def setUp(self):
        outer = self
        self.seen = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append(body)
                code = 503 if len(outer.seen) == 1 else 200
                data = json.dumps({"ok": code == 200, "rev": 3} if code == 200 else {"ok": False, "errors": ["busy"]}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d/ui/x" % self.srv.server_address[1]

    def tearDown(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()

    def test_retry_reuses_the_op_id(self):
        with mock.patch.object(wowui.time, "sleep"):
            code, res = wowui.send(self.base, {"op": "toast", "text": "x"})
        self.assertEqual((code, res["ok"]), (200, True))
        self.assertEqual(len(self.seen), 2)
        self.assertEqual(self.seen[0]["opId"], self.seen[1]["opId"])
        self.assertEqual(len(self.seen[0]["opId"]), 32)
        self.assertEqual(self.seen[0]["ops"], [{"op": "toast", "text": "x"}])
        self.assertNotIn("partial", self.seen[0])

    def test_each_invocation_gets_a_fresh_op_id_and_partial_is_forwarded(self):
        self.seen[:] = [None]                       # skip the scripted 503
        wowui.send(self.base, {"op": "toast", "text": "a"}, partial=True)
        wowui.send(self.base, {"op": "toast", "text": "a"})
        self.assertNotEqual(self.seen[1]["opId"], self.seen[2]["opId"])
        self.assertTrue(self.seen[1]["partial"])

    def test_connection_failure_gives_up_with_a_json_error(self):
        self.srv.shutdown()
        self.srv.server_close()
        with mock.patch.object(wowui.time, "sleep") as sl, self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                wowui.send(self.base, {"op": "toast", "text": "x"})
        self.assertEqual(sl.call_count, wowui.RETRIES - 1)
        self.srv = None


class PublicPathTests(unittest.TestCase):
    """The GET ?b= path used through tunnels: opId and partial travel inside the base64 body; big batches are chunked with derived ids."""

    @classmethod
    def setUpClass(cls):
        cls.s = RunningServer()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_get_path_is_idempotent_and_chunked(self):
        with mock.patch.object(wowui, "is_local", return_value=False):
            code, r1 = wowui.send(self.s.ui, [{"op": "toast", "text": "via get"}], opid="pub-1")
            code, r2 = wowui.send(self.s.ui, [{"op": "toast", "text": "via get"}], opid="pub-1")
            self.assertTrue(r2["replayed"])
            self.assertEqual(r1["rev"], r2["rev"])
            big = [{"op": "panel", "id": "p%d" % i, "title": "t" * 700, "children": [{"type": "text", "text": "x" * 700}]} for i in range(8)]
            code, r = wowui.send(self.s.ui, big, opid="pub-big", partial=True)
            self.assertEqual(code, 200)
            self.assertGreater(r["requests"], 1)
            code, r = wowui.send(self.s.ui, [{"op": "toast", "text": "ok"}, {"op": "panel", "id": "bad id"}], opid="pub-part", partial=True)
            self.assertEqual([x["index"] for x in r["rejected"]], [1])


if __name__ == "__main__":
    unittest.main()
