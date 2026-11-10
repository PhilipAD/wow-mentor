import base64, http.client as httpclient, json, os, socket, sys, tempfile, threading, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RunningServer, http, jhttp
from wowmentor.server import events as ev


def ev_(typ, n=1, **data):
    return {"type": typ, "t": 1794300000 + n, "data": data}


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


class StoreTests(unittest.TestCase):
    """The store without HTTP: ordering, ring buffer, persistence, derived caches."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wowmentor-ev-")
        self.path = os.path.join(self.tmp, "events.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_eids_are_monotonic_and_query_filters(self):
        st = ev.EventStore(self.path)
        stored, rejected, dups = st.push([ev_("death", 1, zone="A"), ev_("fight", 2), ev_("death", 3, zone="B")], boot=7)
        self.assertEqual([e["eid"] for e in stored], [1, 2, 3])
        self.assertEqual((rejected, dups), ([], 0))
        self.assertEqual(stored[0]["boot"], 7)
        r = st.query(since=0)
        self.assertEqual([e["eid"] for e in r["events"]], [1, 2, 3])
        self.assertEqual(r["next"], 3)
        self.assertEqual([e["eid"] for e in st.query(since=1, types=["death"])["events"]], [3])
        r = st.query(since=0, limit=2)
        self.assertEqual((len(r["events"]), r["next"]), (2, 2), "next points at the last returned event when truncated")
        r = st.query(since=0, types=["fight"], limit=5)
        self.assertEqual(r["next"], 3, "a filter that matches nothing later still advances the cursor to the end")
        self.assertEqual([e["eid"] for e in st.query(None, None, 2)["events"]], [2, 3], "no since: the newest events")
        self.assertTrue(st.query(since=99)["reset"])
        self.assertEqual(st.query(since=99)["next"], 3)

    def test_ring_buffer_keeps_500_and_flags_gaps(self):
        st = ev.EventStore(self.path)
        for i in range(0, 600, 100):
            st.push([ev_("zone_changed", i + k) for k in range(100)])
        r = st.query(since=0, limit=500)
        self.assertEqual(len(st.events), 500)
        self.assertEqual(r["oldest_eid"], 101)
        self.assertTrue(r["gap"])
        self.assertEqual(r["events"][0]["eid"], 101)
        self.assertEqual(r["last_eid"], 600)
        self.assertNotIn("gap", st.query(since=100))

    def test_persistence_survives_restart_and_eids_continue(self):
        st = ev.EventStore(self.path)
        st.push([ev_("npc_seen", 1, name="Innkeeper", id=1, map=1429, x=10, y=20, kind="vendor"), ev_("coach_question", 2, id=3, text="Where now?")], boot=1)
        again = ev.EventStore(self.path)
        self.assertEqual(again.last_eid(), 2)
        stored, _, _ = again.push([ev_("death")], boot=1)
        self.assertEqual(stored[0]["eid"], 3)
        self.assertEqual(len(again.npc_list()), 1)
        self.assertEqual(again.open_questions()[0]["text"], "Where now?")
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(ev.EventStore(self.path).last_eid(), 0, "a corrupt file starts clean instead of crashing")

    def test_boot_seq_dedupe_makes_retries_idempotent(self):
        st = ev.EventStore(self.path)
        e = {"type": "death", "t": 5, "seq": 4, "data": {}}
        self.assertEqual(len(st.push([e], boot=9)[0]), 1)
        stored, _, dups = st.push([e], boot=9)
        self.assertEqual((stored, dups), ([], 1))
        self.assertEqual(len(st.push([e], boot=10)[0]), 1, "another boot is a different addon load")
        self.assertEqual(len(st.push([dict(e)])[0]), 1, "no boot: cannot dedupe, store it")
        self.assertEqual(len(st.push([dict(e)])[0]), 1)

    def test_src_dedupe_beats_a_guessed_boot(self):
        st = ev.EventStore(self.path)
        a = {"type": "death", "seq": 1, "src": "run1:1", "boot": 5}
        self.assertEqual(len(st.push([a])[0]), 1)
        self.assertEqual(st.push([dict(a)])[2], 1, "the same src again is a retry")
        b = {"type": "death", "seq": 1, "src": "run1:2", "boot": 5}
        self.assertEqual(len(st.push([b])[0]), 1, "same (boot, seq) but a different src: the boot was a guess, keep the event")
        self.assertEqual(st.events[0]["src"], "run1:1")
        again = ev.EventStore(self.path)
        self.assertEqual(again.push([dict(a)])[2], 1, "dedupe state survives a restart")

    def test_validation_rejects_without_losing_the_rest(self):
        st = ev.EventStore(self.path)
        good = ev_("death")
        stored, rejected, _ = st.push([good, "x", {"type": "Bad Type"}, {"type": "ok", "data": []}, {"type": "big", "data": {"x": "y" * 20000}}, ev_("fight")])
        self.assertEqual([e["type"] for e in stored], ["death", "fight"])
        self.assertEqual([r["index"] for r in rejected], [1, 2, 3, 4])
        with self.assertRaises(ev.StoreError):
            st.push([ev_("x")] * (ev.MAX_PUSH + 1))
        with self.assertRaises(ev.StoreError):
            st.push("nope")
        stored, _, _ = st.push([{"type": "death", "t": "garbage", "data": None}], boot=True)
        self.assertEqual(stored[0]["data"], {})
        self.assertIsNone(stored[0]["boot"])

    def test_npc_cache_aggregates(self):
        st = ev.EventStore(self.path)
        st.push([ev_("npc_seen", 1, name="Vendor Vee", id=55, map=1429, x=40.123, y=41, kind="merchant"),
                 ev_("npc_seen", 5, name="Vendor Vee", id="55", map=1429, x=41, y=42, kind="gossip"),
                 ev_("npc_seen", 3, name="Nameless", kind="trainer"),
                 ev_("npc_seen", 4, id=9),            # no name: ignored
                 ev_("npc_seen", 6, name="Vendor Vee", id=56, kind="merchant")])
        rows = {(n["name"], n["id"]): n for n in st.npc_list()}
        vee = rows[("Vendor Vee", 55)]
        self.assertEqual((vee["count"], vee["x"], vee["y"], vee["kind"], vee["last_seen"]), (2, 41.0, 42.0, "gossip", 1794300005))
        self.assertEqual(vee["kinds"], ["merchant", "gossip"])
        self.assertEqual(len(rows), 3, "same name with another id is another NPC")
        self.assertEqual([n["name"] for n in st.npc_list("nameless")], ["Nameless"])
        self.assertNotIn("key", vee)

    def test_questions_and_boot_collision(self):
        st = ev.EventStore(self.path)
        st.push([ev_("coach_question", 1, id=1, text="First?")], boot=1)
        qid = st.find_open_question(1)
        self.assertEqual(qid, "1")
        self.assertTrue(st.close_question(qid, "Do this"))
        self.assertFalse(st.close_question(qid, "again"))
        st.push([ev_("coach_question", 2, id=1, text="After reload?")], boot=2)
        opens = st.open_questions()
        self.assertEqual([q["text"] for q in opens], ["After reload?"])
        self.assertEqual(opens[0]["id"], "2-1")
        self.assertEqual(st.find_open_question("1"), "2-1", "the addon's own id still finds the only open question with it")
        self.assertEqual(len(st.open_questions(include_all=True)), 2)

    def test_session_validation_and_staleness(self):
        now = [1000.0]
        st = ev.EventStore(self.path, clock=lambda: now[0])
        self.assertFalse(st.get_session()["known"])
        st.set_session({"state": "in_world", "foreground": True, "uploader_alive_at": 1000, "last_login_at": 900, "junk": 1})
        s = st.get_session()
        self.assertEqual((s["state"], s["known"], s["uploader_stale"]), ("in_world", True, False))
        self.assertNotIn("junk", s)
        now[0] = 1100.0
        self.assertTrue(st.get_session()["uploader_stale"])
        for bad in ({"state": "dancing"}, {"foreground": "maybe", "state": "afk"}, {"last_login_at": "x", "state": "afk"}, {}, "x"):
            with self.assertRaises(ev.StoreError):
                st.set_session(bad)

    def test_thread_safety_unique_eids(self):
        st = ev.EventStore(self.path)

        def work(k):
            for i in range(20):
                st.push([ev_("zone_changed", i, k=k)])
        ts = [threading.Thread(target=work, args=(k,)) for k in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        eids = [e["eid"] for e in st.events]
        self.assertEqual(sorted(eids), list(range(1, 161)))

    def test_redact_and_event_line(self):
        out = ev.redact({"a": ["tok-abcdef123456 here", {"k": "ok"}], "n": 3}, ["tok-abcdef123456", "short"])
        self.assertEqual(out["a"][0], "[redacted] here")
        self.assertEqual(ev.redact("short", ["short"]), "short", "secrets under 8 chars are not treated as secrets")
        line = ev.event_line({"eid": 4, "t": 1794300000, "type": "death", "data": {"zone": "Z"}})
        self.assertTrue(line.startswith("#4 ") and "death" in line and '"zone":"Z"' in line, line)
        self.assertTrue(ev.event_line({"eid": None, "t": 1, "type": "x", "data": {}}).startswith("#- "))


class EventsHttpTests(unittest.TestCase):
    def setUp(self):
        self.s = RunningServer()          # a fresh server per test: eids and open questions do not leak between tests

    def tearDown(self):
        self.s.close()

    def declared_length(self, path, length):
        """Status of a POST that only *declares* a body of `length` bytes (the server must refuse before reading it)."""
        c = httpclient.HTTPConnection("127.0.0.1", self.s.port, timeout=5)
        c.putrequest("POST", path)
        c.putheader("Content-Length", str(length))
        c.endheaders()
        try:
            return c.getresponse().status
        finally:
            c.close()

    def push(self, events, boot=None):
        body = {"events": events}
        if boot is not None:
            body["boot"] = boot
        return jhttp(self.s.ui + "/events/push", body)

    def test_push_query_and_read_token(self):
        s = self.s
        code, r = self.push([ev_("death", 1, zone="Elwynn Forest", note="leaked " + s.cfg["ui_secret"] + " here"), ev_("fight", 2)], boot=11)
        self.assertEqual(code, 200, r)
        self.assertEqual([e["eid"] for e in r["stored"]], [1, 2])
        base_eid = r["stored"][0]["eid"]
        code, r = jhttp("%s/events?since=%d&types=death&limit=5" % (s.ui, base_eid - 1))
        self.assertEqual((code, r["ok"], [e["type"] for e in r["events"]]), (200, True, ["death"]))
        self.assertEqual(r["events"][0]["boot"], 11)
        self.assertIn("received_at", r["events"][0])
        # the read-only twin: same shape, no secrets
        code, rr = jhttp("%s/%s/events?since=%d" % (s.base, s.cfg["read_token"], base_eid - 1))
        self.assertEqual(code, 200)
        self.assertEqual([e["type"] for e in rr["events"]], ["death", "fight"])
        raw = json.dumps(rr)
        self.assertNotIn(s.cfg["ui_secret"], raw)
        self.assertIn("[redacted]", raw)
        self.assertEqual(jhttp("%s/%s/events" % (s.base, "wrong"))[0], 404)
        self.assertEqual(http("%s/%s/events" % (s.base, s.cfg["read_token"]), b"{}", "POST")[0], 404, "the read token cannot write")
        self.assertEqual(jhttp("%s/%s/events/push" % (s.base, s.cfg["read_token"]), {"events": []})[0], 404)
        self.assertEqual(jhttp("%s/events?since=abc" % s.ui)[0], 400)
        self.assertEqual(jhttp("%s/events?types=Bad%%20Type" % s.ui)[0], 400)

    def test_push_requires_the_secret_and_a_sane_body(self):
        s = self.s
        self.assertEqual(jhttp("%s/ui/%s/events/push" % (s.base, "wrong"), {"events": []})[0], 404)
        self.assertEqual(jhttp(s.ui + "/events/push", ["not", "an", "object"])[0], 400)
        self.assertEqual(jhttp(s.ui + "/events/push", {"events": "x"})[0], 400)
        code, r = jhttp(s.ui + "/events/push", {"events": [{"type": "Bad"}, ev_("level_up", 1, level=2)]})
        self.assertEqual(code, 200)
        self.assertEqual((len(r["stored"]), len(r["rejected"])), (1, 1))
        self.assertEqual(http(s.ui + "/events/push", b"{not json")[0], 400)

    def test_body_limits_and_bad_content_length(self):
        s = self.s
        self.assertEqual(self.declared_length("/ui/%s/ops" % s.cfg["ui_secret"], 400_000), 413)
        self.assertEqual(self.declared_length("/ui/%s/events/push" % s.cfg["ui_secret"], 2_700_000), 413)
        self.assertEqual(self.declared_length("/push/%s" % s.cfg["write_secret"], 4_100_000), 413)
        self.assertEqual(http(s.ui + "/events/push", json.dumps({"events": [ev_("zone_changed", i, pad="x" * 1000) for i in range(200)]}).encode())[0], 200, "a full push fits")
        for bad in ("abc", "-5"):
            c = httpclient.HTTPConnection("127.0.0.1", s.port, timeout=5)
            c.putrequest("POST", "/ui/%s/ops" % s.cfg["ui_secret"])
            c.putheader("Content-Length", bad)
            c.endheaders()
            self.assertEqual(c.getresponse().status, 400)
            c.close()

    def test_non_ascii_path_does_not_crash_the_secret_compare(self):
        s = self.s
        sock = socket.create_connection(("127.0.0.1", s.port), timeout=5)
        sock.sendall("GET /ui/café/doc HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode("latin-1"))
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        self.assertIn(b" 404 ", data.split(b"\r\n")[0])

    def test_npcs_and_session_routes(self):
        s = self.s
        self.push([ev_("npc_seen", 1, name="Test Trainer", id=77, map=1429, x=30, y=31, kind="trainer")])
        code, r = jhttp(s.ui + "/npcs?name=trainer")
        self.assertEqual(code, 200)
        self.assertEqual(r["npcs"][0]["name"], "Test Trainer")
        self.assertEqual(jhttp(s.ui + "/npcs?name=zzzz")[1]["count"], 0)
        code, r = jhttp(s.ui + "/session")
        self.assertEqual((code, r["known"]), (200, False))
        self.assertEqual(jhttp(s.ui + "/session", {"state": "afk", "foreground": False, "uploader_alive_at": 5, "webhook": {"queued": 2}})[0], 200)
        code, r = jhttp(s.ui + "/session")
        self.assertEqual((r["state"], r["foreground"], r["known"], r["webhook"]["queued"]), ("afk", False, True, 2))
        self.assertEqual(jhttp(s.ui + "/session", {"state": "bogus"})[0], 400)
        self.assertEqual(jhttp(s.ui + "/session", {"state": "in_world"})[0], 200)

    def test_coach_questions_and_answer_sugar(self):
        s = self.s
        self.push([ev_("coach_question", 1, id=101, text="Which quest first?"), ev_("coach_question", 2, id=102, text="Gear check?")], boot=3)
        code, r = jhttp(s.ui + "/coach/questions")
        ids = [q["id"] for q in r["questions"]]
        self.assertTrue({"101", "102"} <= set(ids), ids)
        code, r = jhttp(s.ui + "/ops", {"op": "coach_answer", "id": "101", "text": "Do the wolves first", "toast": True})
        self.assertEqual((code, r["ok"], r["answered"]), (200, True, ["101"]), r)
        self.assertTrue(r["rev"] >= 1)
        inbox = jhttp(s.ui + "/doc")[1]["inbox"]
        self.assertEqual(inbox[-1]["text"], "Do the wolves first")
        self.assertTrue(inbox[-1]["toast"])
        self.assertNotIn("101", [q["id"] for q in jhttp(s.ui + "/coach/questions")[1]["questions"]])
        self.assertIn("101", [q["id"] for q in jhttp(s.ui + "/coach/questions?all=1")[1]["questions"]])
        # unknown or closed ids are errors, in atomic mode nothing is applied
        rev = jhttp(s.ui + "/doc")[1]["rev"]
        code, r = jhttp(s.ui + "/ops", [{"op": "toast", "text": "x"}, {"op": "coach_answer", "id": "101", "text": "again"}])
        self.assertEqual(code, 400)
        self.assertIn("no open question", r["errors"][0])
        self.assertEqual(jhttp(s.ui + "/doc")[1]["rev"], rev)
        # partial: the valid ops apply, the answer is rejected with its index
        code, r = jhttp(s.ui + "/ops", {"partial": True, "ops": [{"op": "toast", "text": "kept"}, {"op": "coach_answer", "id": "nope", "text": "x"},
                                                              {"op": "coach_answer", "id": "102", "text": "Looks fine"}]})
        self.assertEqual(code, 200, r)
        self.assertEqual([x["index"] for x in r["rejected"]], [1])
        self.assertIn("no open question", r["rejected"][0]["errors"][0])
        self.assertEqual(r["answered"], ["102"])
        # the addon's own id works when it is unambiguous; text is required
        self.push([ev_("coach_question", 3, id=555, text="third")], boot=3)
        self.assertEqual(jhttp(s.ui + "/ops", {"op": "coach_answer", "id": 555})[0], 400)
        self.assertIn("555", [q["id"] for q in jhttp(s.ui + "/coach/questions")[1]["questions"]], "a failed answer keeps the question open")
        # dismiss via POST
        code, r = jhttp(s.ui + "/coach/questions", {"dismiss": ["555"]})
        self.assertEqual(r["dismissed"], ["555"])
        # GET twin of ops also translates
        self.push([ev_("coach_question", 4, id=556, text="fourth")], boot=3)
        code, r = jhttp("%s/ops?b=%s" % (s.ui, b64({"op": "coach_answer", "id": 556, "text": "ok", "chat": False})))
        self.assertEqual((code, r.get("answered")), (200, ["556"]))

    def test_opid_idempotency_and_mismatch(self):
        s = self.s
        body = {"opId": "op-1", "ops": [{"op": "toast", "text": "once"}]}
        code, r1 = jhttp(s.ui + "/ops", body)
        self.assertEqual(code, 200)
        toasts = len(jhttp(s.ui + "/doc")[1]["toasts"])
        code, r2 = jhttp(s.ui + "/ops", body)
        self.assertEqual((code, r2["replayed"], r2["rev"]), (200, True, r1["rev"]))
        self.assertEqual(len(jhttp(s.ui + "/doc")[1]["toasts"]), toasts, "a replay never applies twice")
        code, r3 = jhttp(s.ui + "/ops", {"opId": "op-1", "ops": [{"op": "toast", "text": "different"}]})
        self.assertEqual(code, 409)
        self.assertEqual(jhttp(s.ui + "/ops", {"opId": "x" * 200, "ops": [{"op": "toast", "text": "a"}]})[0], 400)
        # answered question is not closed twice on replay
        self.push([ev_("coach_question", 9, id=900, text="q")], boot=5)
        a = {"opId": "op-ans", "ops": [{"op": "coach_answer", "id": "900", "text": "a"}]}
        self.assertEqual(jhttp(s.ui + "/ops", a)[1]["answered"], ["900"])
        r = jhttp(s.ui + "/ops", a)[1]
        self.assertTrue(r["replayed"])

    def test_existing_bare_list_and_ops_get_unchanged(self):
        s = self.s
        code, r = jhttp(s.ui + "/ops", [{"op": "toast", "text": "bare"}])
        self.assertEqual((code, r["ok"]), (200, True))
        for key in ("rev", "applied_rev", "bytes", "warnings", "rejected"):
            self.assertIn(key, r)
        self.assertNotIn("answered", r)
        code, r = jhttp("%s/ops?b=%s" % (s.ui, b64([{"op": "toast", "text": "get"}])))
        self.assertEqual((code, r["ok"]), (200, True))

    def test_brief_includes_session_events_questions_and_rules(self):
        s = self.s
        jhttp(s.ui + "/session", {"state": "in_world", "foreground": True, "uploader_alive_at": __import__("time").time()})
        self.push([ev_("death", 1, zone="Briefland"), ev_("coach_question", 2, id=700, text="Brief me?")], boot=8)
        jhttp(s.ui + "/ops", {"op": "rule", "id": "r-brief", "when": {"type": "bags_free_lte", "n": 3}, "then": [{"do": "toast", "text": "Bags!"}]})
        code, brief = jhttp(s.ui + "/brief")
        self.assertEqual(code, 200)
        for needle in ("## Session", "state=in_world", "Last 10 events", "Briefland", "Open coach questions", "Brief me?", "Active rules", "r-brief", "bags_free_lte(n=3)"):
            self.assertIn(needle, brief)

    def test_schema_lists_the_server_additions(self):
        c = jhttp(self.s.ui + "/schema")[1]
        self.assertIn("coach_answer", c["ops"])
        self.assertIn("events_api", c)
        self.assertIn("opId", c["request"]["opId"])


if __name__ == "__main__":
    unittest.main()
