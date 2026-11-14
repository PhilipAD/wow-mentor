"""End to end: a real server, the feed and event endpoints the bridge uses, and the overlay document the game reads."""
import os
os.environ.setdefault("WOWMENTOR_RELOAD_QUIET_S", "0")
import json, shutil, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import RunningServer, http, jhttp
from tests.test_autosquares import world
from wowmentor.server import autosquares

SEQ = [1000]


def feed(quests, map_id=1439):
    lines = ["=== WOW LIVE FEED ===", "--- FAST", "Position: map=%d (Zone) x=40.0 y=50.0" % map_id, "--- QUESTS", "Quests in log: %d" % len(quests)]
    lines += ["[%s] Q%d (L20) #%d" % (st, q, q) for q, st in quests]
    return ("\n".join(lines + ["--- PET", "no pet", ""])).encode()


def ev(typ, **data):
    SEQ[0] += 1
    return {"type": typ, "data": data, "t": int(time.time()), "seq": SEQ[0]}


class AutoSquaresEndToEnd(unittest.TestCase):
    def setUp(self):
        autosquares.STICKY_S = 600
        g = world()                                      # fixture database: Darkshore creatures, quests 986 / 1275 / 4740
        self.data = tempfile.mkdtemp(prefix="wm-e2e-")
        shutil.copy(g.path, os.path.join(self.data, "forever.sqlite"))
        self.s = RunningServer(data_path=self.data)
        self.s_app_delay()
        self.make_panel()

    def s_app_delay(self):
        from wowmentor.server import app
        app.auto.delay = 0.05

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.data, ignore_errors=True)

    def make_panel(self, with_profession=True):
        kids = [{"type": "heading", "id": "hq", "text": "placeholder"}]
        if with_profession:
            kids += [{"type": "heading", "id": "hc", "text": "Cooking"}, {"type": "row", "id": "c1", "children": [{"type": "icon", "item": 2674, "iconSize": 26}]}]
        code, r = jhttp(self.s.ui + "/ops", {"op": "panel", "id": "squares", "title": "What to get here", "children": kids})
        self.assertEqual(code, 200, r)

    def push_feed(self, body):
        self.assertEqual(http("%s/push/%s" % (self.s.base, self.s.cfg["write_secret"]), body)[0], 200)

    def push_events(self, *events):
        code, r = jhttp(self.s.ui + "/events/push", {"boot": 1, "events": list(events)})
        self.assertEqual(code, 200, r)
        return r

    def squares(self):
        doc = jhttp(self.s.ui + "/doc")[1]
        p = [x for x in doc["panels"] if x["id"] == "squares"]
        return p[0]["children"] if p else None

    def targets(self):
        ch = self.squares() or []
        q = [c for c in ch if c.get("id") == "qall"]
        return q[0]["targets"] if q else []

    def wait(self, pred, timeout=4.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.05)
        return pred()

    def status(self):
        return jhttp(self.s.ui + "/autosquares")[1]

    # ------------------------------------------------------------------ scenarios
    def test_feed_then_accept_then_turn_in_then_zone_change(self):
        self.push_feed(feed([(986, "in progress"), (1275, "in progress")]))
        self.assertTrue(self.wait(lambda: set(self.targets()) == {"Moonstalker Sire", "Moonstalker Matriarch"}), self.squares())
        self.push_events(ev("quest_accepted", quest=4740))
        self.assertTrue(self.wait(lambda: "Murkdeep" in self.targets()), self.targets())
        self.push_events(ev("quest_turned_in", quest=986))
        self.assertTrue(self.wait(lambda: self.targets() == ["Murkdeep"]), self.targets())
        self.push_events(ev("zone_changed", map=1440, zone="Ashenvale"))
        self.assertTrue(self.wait(lambda: self.targets() == [] and "nothing" in self.squares()[0]["text"]), self.squares())
        self.assertEqual([c["id"] for c in self.squares()], ["hq", "hc", "c1"])              # the agent's Cooking rows survive every rebuild
        self.assertIsNone(self.status()["last"]["error"])

    def test_stale_feed_does_not_undo_events(self):
        self.push_feed(feed([(986, "in progress")]))
        self.assertTrue(self.wait(lambda: "Moonstalker Sire" in self.targets()))
        self.push_events(ev("quest_removed", quest=986))                                    # abandoned
        self.assertTrue(self.wait(lambda: self.targets() == []))
        self.push_feed(feed([(986, "in progress")]))                                        # the QUESTS section has not caught up yet
        time.sleep(0.4)
        self.assertEqual(self.targets(), [])
        self.push_feed(feed([(986, "COMPLETE")]))                                           # complete quests get no squares either
        time.sleep(0.3)
        self.assertEqual(self.targets(), [])

    def test_restart_rebuilds_from_the_next_feed(self):
        self.push_feed(feed([(986, "in progress")]))
        self.assertTrue(self.wait(lambda: "Moonstalker Sire" in self.targets()))
        cfg = self.s.cfg
        self.s.close()
        self.s = RunningServer(data_path=self.data, config=cfg)
        self.s_app_delay()
        self.assertTrue(self.wait(lambda: "Moonstalker Sire" in self.targets()))            # the saved doc survives the restart
        self.push_feed(feed([(4740, "in progress")]))
        self.assertTrue(self.wait(lambda: self.targets() == ["Murkdeep"]), self.targets())

    def test_events_before_a_restart_are_replayed(self):
        self.push_feed(feed([(986, "in progress"), (4740, "in progress")]))
        self.assertTrue(self.wait(lambda: "Murkdeep" in self.targets()))
        cfg = self.s.cfg
        from wowmentor.server import app
        app.auto.on_events = lambda events: None                                        # this process misses the turn-in...
        self.push_events(ev("quest_turned_in", quest=4740))
        self.s.close()
        self.s = RunningServer(data_path=self.data, config=cfg)                         # ...the next one replays it from the event store
        self.s_app_delay()
        self.assertTrue(self.wait(lambda: "Murkdeep" not in self.targets()), self.targets())

    def test_bad_input_never_breaks_it(self):
        self.push_feed(b"garbage without sections")
        self.push_feed(feed([]))
        self.push_events({"type": "quest_accepted", "data": {"quest": "4740"}},            # string id: ignored
                         {"type": "quest_accepted", "data": {}}, {"type": "zone_changed", "data": {"zone": "x"}},
                         {"type": "quest_accepted", "data": {"quest": 99999999}})            # unknown quest: no rows, no error
        time.sleep(0.3)
        st = self.status()
        self.assertIsNone(st["last"].get("error"), st)
        self.assertEqual(self.targets(), [])
        r = jhttp(self.s.ui + "/events/push", {"events": "nope"})
        self.assertEqual(r[0], 400)

    def test_burst_of_events_settles_on_the_right_state(self):
        self.push_feed(feed([]))
        burst = []
        for i in range(30):
            burst.append(ev("quest_accepted", quest=4740 if i % 2 else 986))
            burst.append(ev("quest_turned_in", quest=986 if i % 2 else 4740))
        self.push_events(*burst)                                                            # last word: 986 accepted then 4740 turned in... in order
        self.assertTrue(self.wait(lambda: set(self.targets()) == {"Murkdeep"} or set(self.targets()) == {"Moonstalker Sire", "Moonstalker Matriarch"}))
        final_986 = burst[-1]["data"]["quest"] == 4740                                     # the last event turned 4740 in, so 986 stays
        want = {"Moonstalker Sire", "Moonstalker Matriarch"} if final_986 else {"Murkdeep"}
        self.assertTrue(self.wait(lambda: set(self.targets()) == want), self.targets())

    def test_no_panel_means_hands_off(self):
        jhttp(self.s.ui + "/ops", {"op": "remove", "panel": "squares"})
        self.push_feed(feed([(986, "in progress")]))
        time.sleep(0.3)
        self.assertIsNone(self.squares())
        self.assertIsNone(self.status()["last"].get("error"))

    def test_agent_edits_alongside(self):
        self.push_feed(feed([(986, "in progress")]))
        self.assertTrue(self.wait(lambda: "Moonstalker Sire" in self.targets()))
        code, r = jhttp(self.s.ui + "/ops", {"op": "panel", "id": "squares", "children": self.squares() + [{"type": "heading", "id": "hf", "text": "First Aid"}]})
        self.assertEqual(code, 200, r)
        self.push_events(ev("quest_accepted", quest=4740))
        self.assertTrue(self.wait(lambda: "Murkdeep" in self.targets()))
        self.assertIn("hf", [c["id"] for c in self.squares()])

    def test_many_quests_are_capped_and_reported(self):
        from wowmentor.server import app
        g = app.gdb
        import sqlite3
        c = sqlite3.connect(g.path)
        for k in range(40):
            nid = 9100 + k
            c.execute("INSERT INTO npc VALUES(?,?,'',17,18,0,148,'{}')", (nid, "Ogre %d" % k))
            c.execute("INSERT INTO spawn VALUES('npc',?,148,1439,50,50)", (nid,))
            c.execute("INSERT INTO quest VALUES(?,?,20,15,NULL,148,0,0,'[]','[]','[]','[]','[]',NULL,NULL,0,?)",
                      (7000 + k, "Kill %d" % k, json.dumps({"objectives": {"1": [[nid, "x"]]}})))
        c.commit(); c.close()
        self.push_feed(feed([(7000 + k, "in progress") for k in range(40)]))
        self.assertTrue(self.wait(lambda: len([x for x in (self.squares() or []) if str(x.get("id", "")).startswith("q") and x["id"] != "qall"]) == autosquares.MAX_ROWS))
        st = self.status()["last"]
        self.assertGreater(st["rows_left_out"], 0)
        self.assertLessEqual(len(self.targets()), 8)


if __name__ == "__main__":
    unittest.main()
