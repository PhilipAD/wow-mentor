import json, os, shutil, sys, tempfile, time, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RunningServer, jhttp
from wowmentor.bridge import app as bridge, events as evmod, guidesync, presence as presmod, webhook as whmod
from wowmentor.server import agentmem


def kv_summary(**extra):
    kv = dict(level="5", xp="1", xpmax="9", money_copper="1", honorable_kills="0", class_="MAGE", uiboot="4242")
    kv.update(extra)
    return ["KV " + " ".join("%s=%s" % (k.rstrip("_"), v) for k, v in kv.items())]


class FakeServer(object):
    """post(route, obj) stand-in that assigns eids like the real server."""

    def __init__(self):
        self.n, self.calls, self.fail = 0, [], []

    def __call__(self, route, obj):
        self.calls.append((route, obj))
        if self.fail:
            code = self.fail.pop(0)
            return code, None
        stored = []
        for e in obj["events"]:
            self.n += 1
            stored.append({"eid": self.n, "t": e.get("t"), "received_at": e.get("received_at"), "boot": e.get("boot"), "type": e["type"], "data": e.get("data", {})})
        return 200, {"ok": True, "stored": stored, "rejected": [], "next": self.n}


class BridgeStateCase(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(bridge.sections), dict(bridge.partial), dict(bridge.meta), dict(bridge.evj_stats))
        bridge.sections.clear(); bridge.partial.clear()
        bridge.meta["last_line"] = 0
        for k in bridge.evj_stats: bridge.evj_stats[k] = 0
        self.fs = FakeServer()
        self.stored = []
        self.pipe = evmod.EventPipe(post=self.fs, on_stored=self.stored.extend, log=lambda m: None)
        self.pres = presmod.Presence()
        self.p1 = mock.patch.object(bridge, "pipe", self.pipe)
        self.p2 = mock.patch.object(bridge, "presence", self.pres)
        self.p3 = mock.patch.object(bridge, "boots", bridge.BootTracker())
        self.p1.start(); self.p2.start(); self.p3.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop(); self.p3.stop()
        bridge.sections.clear(); bridge.sections.update(self.saved[0])
        bridge.partial.clear(); bridge.partial.update(self.saved[1])
        bridge.meta.update(self.saved[2])

    def summary(self, age=0.0):
        bridge.sections["summary"] = (time.time() - age, kv_summary())


class EvjDecodeTests(BridgeStateCase):
    def test_a_message_becomes_queued_events_with_boot_and_received_at(self):
        self.summary()
        evs = [{"seq": 1, "t": 1794300000, "type": "death", "data": {"zone": "Z"}}, {"seq": 2, "t": 1794300001, "type": "bags_full", "data": {"free": 1}}]
        bridge.handle("evj", "7", 1, 1, json.dumps(evs))
        rows = self.pipe.recent(10)
        self.assertEqual([e["type"] for e in rows], ["death", "bags_full"])
        self.assertEqual(rows[0]["boot"], 4242)
        self.assertGreater(rows[0]["received_at"], 1e9)
        self.assertGreater(bridge.meta["last_line"], 0, "an evj message also proves the addon is alive")
        self.assertEqual(bridge.evj_stats["events"], 2)
        self.assertTrue(self.pipe.step())
        self.assertEqual([e["eid"] for e in self.stored], [1, 2])

    def test_an_event_that_carries_its_own_boot_keeps_it(self):
        self.summary()
        bridge.handle("evj", "1", 1, 1, json.dumps([{"seq": 1, "type": "death", "boot": 99, "data": {}}]))
        self.assertEqual(self.pipe.recent(1)[0]["boot"], 99)

    def test_a_stale_summary_boot_is_not_trusted(self):
        self.summary(age=120)
        bridge.handle("evj", "1", 1, 1, json.dumps([{"seq": 1, "type": "death", "data": {}}]))
        self.assertIsNone(self.pipe.recent(1)[0].get("boot"), "after an addon reload the KV boot can lag: better no boot than a wrong one")

    def test_multipart_messages_are_reassembled(self):
        self.summary()
        text = json.dumps([{"seq": 1, "type": "fight", "data": {"note": "x" * 50}}, {"seq": 2, "type": "death", "data": {}}])
        a, b = text[:40], text[40:]
        bridge.handle("evj", "5", 1, 2, a)
        self.assertEqual(self.pipe.recent(5), [])
        bridge.handle("evj", "5", 2, 2, b)
        self.assertEqual([e["type"] for e in self.pipe.recent(5)], ["fight", "death"])

    def test_a_new_message_id_discards_an_incomplete_older_one(self):
        self.summary()
        bridge.handle("evj", "5", 1, 2, '[{"seq":1,"type":"a')
        bridge.handle("evj", "6", 1, 1, '[{"seq":2,"type":"death","data":{}}]')
        bridge.handle("evj", "5", 2, 2, 'bc","data":{}}]')
        self.assertEqual([e["type"] for e in self.pipe.recent(5)], ["death"])

    def test_malformed_input_is_ignored_safely(self):
        for bad in ("", "not json", "{", '{"type":"death"}', "42", "null", '[1,"x",null,[]]', '[{"type":5}]', '[{"data":{}}]', "[" * 5000):
            bridge.handle("evj", "1", 1, 1, bad)
        self.assertEqual(self.pipe.recent(5), [])
        self.assertGreaterEqual(bridge.evj_stats["malformed"], 8)
        bridge.handle("evj", "1", 1, 1, '[1,{"type":"death","data":{}},"x"]')
        self.assertEqual([e["type"] for e in self.pipe.recent(5)], ["death"], "good items in a mixed array survive")

    def test_at_most_100_events_per_message(self):
        bridge.handle("evj", "1", 1, 1, json.dumps([{"type": "t", "seq": i} for i in range(300)]))
        self.assertEqual(len(self.pipe.recent(500)), 100)

    def test_lifecycle_events_drive_presence(self):
        bridge.handle("evj", "1", 1, 1, json.dumps([{"seq": 1, "t": int(time.time()), "type": "login", "data": {}}]))
        self.assertIsNotNone(self.pres.last_login_at)
        bridge.handle("evj", "2", 1, 1, json.dumps([{"seq": 2, "t": int(time.time()), "type": "logout", "data": {}}]))
        self.assertTrue(self.pres.logout_pending)

    def test_normal_sections_and_events_still_work(self):
        bridge.handle("fast", "1", 1, 2, "line one")
        bridge.handle("fast", "1", 2, 2, "line two")
        self.assertEqual(bridge.sections["fast"][1], ["line one", "line two"])
        bridge.handle("hb", "1", 1, 1, "12:00:00")
        n = len(bridge.events)
        bridge.handle("ev", "1", 1, 1, "something happened")
        self.assertEqual(len(bridge.events), n + 1)


class PipeTests(unittest.TestCase):
    def pipe(self, fs=None, **kw):
        fs = fs or FakeServer()
        stored = []
        return evmod.EventPipe(post=fs, on_stored=stored.extend, log=kw.get("log", lambda m: None), clock=kw.get("clock", time.time)), fs, stored

    def test_dedupe_only_on_a_boot_the_addon_sent_itself(self):
        p, fs, stored = self.pipe()
        p.add([{"type": "death", "seq": 1, "boot": 5}, {"type": "death", "seq": 1, "boot": 5}])
        p.add([{"type": "death", "seq": 1, "boot": 5}])
        p.add([{"type": "death", "seq": 1, "boot": 6}])
        self.assertEqual(len(p.pending), 2)
        p.add([{"type": "death", "seq": 1}, {"type": "death", "seq": 1}], boot=7)       # boot only guessed from the KV line: never dedupes
        p.add([{"type": "death"}, {"type": "death"}])
        self.assertEqual(len(p.pending), 6)

    def test_every_event_gets_a_unique_source_id_so_a_retried_push_is_harmless(self):
        p, fs, stored = self.pipe()
        p.add([{"type": "a"}, {"type": "b"}])
        srcs = [e["src"] for e in p.pending]
        self.assertEqual(len(set(srcs)), 2)
        self.assertTrue(all(s.startswith(p.run_id + ":") for s in srcs))
        self.assertNotEqual(evmod.EventPipe().run_id, p.run_id)

    def test_failure_backoff_and_recovery(self):
        now = [100.0]
        p, fs, stored = self.pipe(clock=lambda: now[0])
        fs.fail = [None, 503]
        p.add([{"type": "death", "seq": 1}], boot=1)
        self.assertFalse(p.step(100.0))
        self.assertFalse(p.step(100.5), "inside the backoff")
        self.assertEqual(len(fs.calls), 1)
        self.assertFalse(p.step(101.0))
        self.assertEqual(len(fs.calls), 2)
        self.assertFalse(p.step(102.5))
        self.assertTrue(p.step(103.1))
        self.assertEqual([e["eid"] for e in stored], [1])
        self.assertEqual(p.pending, [])
        self.assertEqual(p.stats["failures"], 2)
        self.assertEqual(p.recent(5)[0]["eid"], 1)

    def test_client_errors_drop_the_batch_so_it_cannot_block_the_queue(self):
        p, fs, stored = self.pipe()
        fs.fail = [400]
        p.add([{"type": "a"}])
        self.assertTrue(p.step())
        self.assertEqual((p.pending, p.stats["dropped"]), ([], 1))
        fs.fail = [404]
        p.add([{"type": "b"}])
        self.assertFalse(p.step(), "a wrong secret or an older server is fixable: keep the events and retry")
        self.assertEqual(len(p.pending), 1)

    def test_queue_cap_and_recent_shows_unconfirmed_events(self):
        p, fs, stored = self.pipe()
        p.add([{"type": "t", "data": {"i": i}} for i in range(100)])
        p.add([{"type": "t", "data": {"i": i}} for i in range(100, 700)][:100])
        for _ in range(8):
            p.add([{"type": "t"}] * 100)
        self.assertEqual(len(p.pending), evmod.CAP)
        self.assertIsNone(p.recent(1)[0]["eid"])
        self.assertGreater(p.stats["dropped"], 0)

    def test_batches_of_100_and_exception_from_post(self):
        def boom(route, obj):
            raise RuntimeError("secret url http://x/y")
        logs = []
        p = evmod.EventPipe(post=boom, log=logs.append)
        p.add([{"type": "t"}])
        self.assertFalse(p.step())
        self.assertNotIn("http://x/y", " ".join(logs))
        p2, fs, stored = self.pipe()
        p2.add([{"type": "t", "seq": i} for i in range(1, 101)], boot=1)
        p2.add([{"type": "t", "seq": i} for i in range(101, 151)], boot=1)
        self.assertTrue(p2.step())
        self.assertEqual(len(fs.calls[0][1]["events"]), 100)
        self.assertEqual(len(p2.pending), 50)

    def test_consumer_exceptions_do_not_lose_stored_events(self):
        def bad(stored):
            raise ValueError("consumer bug")
        p = evmod.EventPipe(post=FakeServer(), on_stored=bad, log=lambda m: None)
        p.add([{"type": "t"}])
        self.assertTrue(p.step())
        self.assertEqual(p.pending, [])

    def test_add_never_raises(self):
        p, _, _ = self.pipe()
        p.add(None)
        p.add([None, 5, {"type": "ok"}])
        self.assertEqual(len(p.pending), 1)


class FeedAndServerIntegrationTests(BridgeStateCase):
    def test_render_has_session_state_and_events_tail_that_the_brief_can_parse(self):
        self.summary()
        bridge.handle("evj", "1", 1, 1, json.dumps([{"seq": 1, "t": int(time.time()), "type": "death", "data": {"zone": "Z"}}]))
        self.pipe.step()
        bridge.meta["last_line"] = time.time()
        self.pres.update(time.time(), 0.5, True, {}, True)
        text = bridge.render()
        self.assertIn("--- SESSION_STATE (updated 0s ago) ---", text)
        self.assertIn("state=in_world", text)
        self.assertIn("foreground=true", text)
        self.assertIn("--- EVENTS_TAIL (updated 0s ago) ---", text)
        self.assertIn("#1 ", text)
        self.assertIn('"zone":"Z"', text)
        sec, _ = agentmem.feed_sections(text.encode())
        self.assertIn("SESSION_STATE", sec)
        self.assertIn("EVENTS_TAIL", sec)
        self.assertTrue(any("death" in l for l in sec["EVENTS_TAIL"]))

    def test_empty_tail_and_unknown_session(self):
        text = bridge.render()
        self.assertIn("(no events yet)", text)
        self.assertIn("state=unknown", text)

    def test_end_to_end_with_a_real_server(self):
        s = RunningServer()
        try:
            api = bridge.Api(s.ui)
            sent = []
            sender = whmod.WebhookSender("https://hooks.example.test/x", None, session_fn=lambda: {"state": "in_world"}, post=lambda u, b, h, t: sent.append(json.loads(b)) or 200,
                                         clock=lambda: 5000.0, log=lambda m: None)
            pipe = evmod.EventPipe(post=api.post, on_stored=sender.enqueue, log=lambda m: None)
            with mock.patch.object(bridge, "pipe", pipe), mock.patch.object(bridge, "sender", sender):
                self.summary()
                bridge.handle("evj", "1", 1, 1, json.dumps([{"seq": 1, "t": 1794300000, "type": "death", "data": {"zone": "Elwynn Forest"}},
                                                           {"seq": 2, "t": 1794300001, "type": "npc_seen", "data": {"name": "Guard", "id": 68, "map": 1429, "x": 1, "y": 2, "kind": "gossip"}}]))
                self.assertTrue(pipe.step())
                code, r = jhttp(s.ui + "/events?since=0")
                self.assertEqual([e["type"] for e in r["events"]], ["death", "npc_seen"])
                self.assertEqual(r["events"][0]["boot"], 4242)
                self.assertEqual(jhttp(s.ui + "/npcs")[1]["npcs"][0]["name"], "Guard")
                # the webhook got the stored envelopes, eids included
                self.assertTrue(sender.pump(5003.0))
                self.assertEqual([e["eid"] for e in sent[0]["events"]], [1, 2])
                # an addon that repeats unacknowledged events (with its own boot) adds nothing
                bridge.handle("evj", "2", 1, 1, json.dumps([{"seq": 2, "boot": 4242, "type": "npc_seen", "data": {"name": "Guard"}}, {"seq": 3, "boot": 4242, "type": "bags_full", "data": {"free": 0}}]))
                bridge.handle("evj", "3", 1, 1, json.dumps([{"seq": 3, "boot": 4242, "type": "bags_full", "data": {"free": 0}}]))
                pipe.step()
                self.assertEqual([e["type"] for e in jhttp(s.ui + "/events?since=2")[1]["events"]], ["npc_seen", "bags_full"], "the repeated seq 3 is dropped; seq 2 had no boot of its own before")
                # a push whose reply was lost is retried with the same src: the server stores it once
                again = jhttp(s.ui + "/events/push", {"events": [{"type": "bags_full", "src": pipe.confirmed[-1]["src"], "data": {}}]})[1]
                self.assertEqual((again["stored"], again["duplicates"]), ([], 1))
                # presence tick pushes the session object and queues bridge events
                snaps = []
                bridge.meta["last_line"] = time.time()
                snap, evs = bridge.presence_tick(time.time(), api, window_fn=lambda: 1234, foreground_fn=lambda: True)
                self.assertEqual(snap["state"], "in_world")
                code, sess = jhttp(s.ui + "/session")
                self.assertEqual((sess["known"], sess["state"], sess["foreground"], sess["uploader_stale"]), (True, "in_world", True, False))
                self.assertEqual(sess["webhook"]["configured"], True)
                self.assertNotIn("private", json.dumps(sess))
                bridge.presence_tick(time.time() + 1, api, window_fn=lambda: 0, foreground_fn=lambda: False)
                pipe.step()
                types = [e["type"] for e in jhttp(s.ui + "/events?since=0")[1]["events"]]
                self.assertIn("game_closed", types)
                self.assertIn("state_changed", types)
        finally:
            s.close()

    def test_afk_flag_is_read_from_the_kv_or_the_addon_session_section(self):
        self.assertEqual(bridge._afk_from_sections({"afk": "1"}), {"afk": "1"})
        bridge.sections["session_addon"] = (time.time(), ["afk=1 in_combat=0 loading=0"])
        self.assertEqual(bridge._afk_from_sections({"level": "5"})["afk"], "1")
        bridge.sections.pop("session_addon")
        self.assertEqual(bridge._afk_from_sections({"level": "5"}), {"level": "5"})

    @unittest.skipUnless(sys.platform == "darwin", "CoreGraphics")
    def test_frontmost_check_answers_or_says_unknown(self):
        self.assertIn(bridge.frontmost_is_wow(), (True, False, None))


class GuideRestoreSafetyTests(unittest.TestCase):
    def test_restore_ids_cannot_leave_the_backup_folder(self):
        tmp = tempfile.mkdtemp(prefix="wowmentor-gs-")
        try:
            addons = os.path.join(tmp, "AddOns")
            pack = os.path.join(addons, "Guidelime_Pack")
            os.makedirs(pack)
            guide = os.path.join(pack, "g.lua")
            with open(guide, "w") as f:
                f.write("Guidelime.registerGuide([[\n[GA Alliance]\n[NG]\nstep\n]], 'P')")
            outside = os.path.join(tmp, "outside.lua")
            with open(outside, "w") as f:
                f.write("EVIL")
            backups = os.path.join(tmp, "backups")
            os.makedirs(os.path.join(backups, "versions", "g"))
            with open(os.path.join(backups, "versions", "g", "7.lua"), "w") as f:
                f.write("GOOD")
            with mock.patch.multiple(guidesync, ADDONS=addons, LOCAL="http://x", BACKUPS=backups):
                gs = guidesync.GuideSync("secret", lambda: None, lambda: None, log=lambda m: None)
                posts = []
                queue = [{"id": 1, "guide": "g", "restore": {"restore": "../../../outside"}}, {"id": 2, "guide": "g", "restore": {"restore": "7"}}]
                gs._call = lambda route, body=None, timeout=10: {"ok": True, "queue": queue} if route == "pending" else posts.append((route, body))
                gs.apply_pending()
            applied = [b for r, b in posts if r == "applied"]
            self.assertFalse(applied[0]["ok"])
            self.assertIn("not a valid restore point", applied[0]["errors"][0])
            self.assertTrue(applied[1]["ok"])
            with open(guide) as f:
                self.assertEqual(f.read(), "GOOD")
            for bad in ("..", "../x", "a/b", ".hidden", "", "x" * 80, "a b"):
                self.assertFalse(guidesync.VERSION_ID.match(bad), bad)
            for good in ("12", "12-prerestore", "7"):
                self.assertTrue(guidesync.VERSION_ID.match(good), good)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
