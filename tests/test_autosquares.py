import json, os, sqlite3, sys, tempfile, threading, time, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wowmentor.server import autosquares, uidoc
from wowmentor.server.gamedb import GameDB
from tests.test_gamedb import make_db


def world():
    d = tempfile.mkdtemp()
    make_db(os.path.join(d, "forever.sqlite"))
    c = sqlite3.connect(os.path.join(d, "forever.sqlite"))
    c.execute("INSERT INTO zone VALUES(148,'Darkshore',1439,NULL)")
    c.execute("INSERT INTO zone VALUES(719,'Blackfathom Deeps',1439,NULL)")          # a dungeon area that maps onto the zone
    for nid, name in ((8001, "Moonstalker Sire"), (8002, "Moonstalker Matriarch"), (8005, "Blackfathom Oracle"), (8006, "Murkdeep")):
        c.execute("INSERT INTO npc VALUES(?,?,'',17,18,0,148,'{}')", (nid, name))
    for nid, area, n in ((8001, 148, 3), (8002, 148, 2), (8005, 148, 4), (8006, 148, 1)):
        for i in range(n):
            c.execute("INSERT INTO spawn VALUES('npc',?,?,1439,?,80)", (nid, area, 40 + i))
    c.execute("INSERT INTO item VALUES(9001,'Fine Moonstalker Pelt',15,0,12,0,NULL,?)", (json.dumps({"npcDrops": [8001, 8002]}),))
    c.execute("INSERT INTO item VALUES(9003,'Brain Stem',15,0,12,0,NULL,?)", (json.dumps({"npcDrops": [8005]}),))
    c.execute("INSERT INTO drop_rate VALUES(8002,9001,55,30,'forever',0)")
    c.execute("INSERT INTO drop_rate VALUES(8001,9001,40,NULL,'classic',0)")
    c.execute("INSERT INTO boss_loot VALUES('BFD','Blackfathom Deeps','DUNGEON','20-30','Gelihast',NULL,1,NULL)")
    for qid, name, obj in ((986, "A Lost Master", {"3": [[9001, "Pelt"]]}), (1275, "Researching the Corruption", {"3": [[9003, "Stem"]]}),
                           (4740, "WANTED: Murkdeep!", {"1": [[8006, "Murkdeep slain"]]})):
        zone = 719 if qid == 1275 else 148                                          # 1275 is a Blackfathom Deeps quest
        c.execute("INSERT INTO quest VALUES(?,?,20,15,NULL,?,0,0,'[]','[]','[]','[]','[]',NULL,NULL,0,?)", (qid, name, zone, json.dumps({"name": name, "objectives": obj})))
    c.commit(); c.close()
    return GameDB(d)


FEED = b"--- FAST\nPosition: map=1439 (Darkshore) x=40 y=50\n--- QUESTS\nQuests in log: 3\n[in progress] A Lost Master (L20) #986\n" \
       b"[in progress] Researching the Corruption (L24) #1275\n[COMPLETE] Fruit of the Sea (L17) #1138\n--- PET\nno pet\n"


class AutoSquaresTests(unittest.TestCase):
    def setUp(self):
        self.doc = uidoc.Doc()
        uidoc.apply_ops(self.doc, [{"op": "panel", "id": "squares", "title": "What to get here", "children": [
            {"type": "heading", "id": "hq", "text": "old"}, {"type": "row", "id": "q1", "children": [{"type": "spacer"}]},
            {"type": "heading", "id": "hc", "text": "Cooking"}, {"type": "row", "id": "c1", "children": [{"type": "icon", "item": 2674, "iconSize": 26}]}]}])
        self.saved = 0
        self.a = autosquares.AutoSquares(world(), threading.Lock(), lambda: self.doc, self.save, uidoc.apply_ops, normalize=uidoc.normalize_component, worker=False)

    def save(self):
        self.saved += 1

    def children(self):
        return [p for p in self.doc.panels if p["id"] == "squares"][0]["children"]

    def test_feed_builds_quest_rows_and_keeps_profession_rows(self):
        self.a.on_feed(FEED); self.a.run()
        ch = self.children()
        self.assertEqual([c["id"] for c in ch], ["hq", "qall", "q1", "hc", "c1"])
        row = [c for c in ch if c["id"] == "q1"][0]["children"]
        self.assertEqual(row[0]["tip"], "Moonstalker Matriarch")                     # measured 55% beats the 40% estimate: first
        self.assertEqual(row[1]["tip"], "Fine Moonstalker Pelt (55% from Moonstalker Matriarch)")
        self.assertEqual({row[0]["quest"], row[1]["quest"]}, {986})
        names = [c for c in ch if c["id"] == "qall"][0]["targets"]
        self.assertNotIn("Blackfathom Oracle", names)                               # dungeon spawn left out
        self.assertEqual(set(names), {"Moonstalker Sire", "Moonstalker Matriarch"})
        self.assertEqual(self.saved, 1)

    def test_events_add_and_remove_before_the_feed_catches_up(self):
        self.a.on_feed(FEED); self.a.run()
        self.a.on_events([{"type": "quest_accepted", "data": {"quest": 4740}}, {"type": "quest_turned_in", "data": {"quest": 986}}])
        self.a.run()
        names = [c for c in self.children() if c["id"] == "qall"][0]["targets"]
        self.assertEqual(names, ["Murkdeep"])
        self.a.on_feed(FEED); self.a.run()                    # a stale QUESTS section does not bring 986 back
        self.assertEqual([c for c in self.children() if c["id"] == "qall"][0]["targets"], ["Murkdeep"])
        self.a.on_events([{"type": "quest_turned_in", "data": {"quest": 4740}}]); self.a.run()
        ch = self.children()
        self.assertEqual([c["id"] for c in ch], ["hq", "hc", "c1"])
        self.assertIn("nothing", ch[0]["text"])

    def test_no_panel_no_change_and_same_state_no_rewrite(self):
        self.a.on_feed(FEED); self.a.run(); self.a.run()
        self.assertEqual(self.saved, 1)
        self.doc.panels = []
        self.a.on_events([{"type": "quest_accepted", "data": {"quest": 4740}}]); self.a.run()
        self.assertEqual(self.saved, 1)
        self.assertIsNone(self.a.last.get("error"))

    def ids(self):
        return [c["id"] for c in self.children()]

    def test_agent_rewrite_is_repaired_on_the_next_feed(self):
        self.a.on_feed(FEED); self.a.run()
        uidoc.apply_ops(self.doc, [{"op": "panel", "id": "squares", "children": [{"type": "heading", "id": "hc", "text": "Cooking"}]}])
        self.a.on_feed(FEED); self.a.run()                                           # same quests and map, but our rows were wiped
        self.assertEqual(self.ids()[:3], ["hq", "qall", "q1"])
        self.assertIn("hc", self.ids())

    def test_invalid_agent_child_is_dropped_not_blocking(self):
        self.a.on_feed(FEED); self.a.run()
        kids = self.children() + [{"type": "icon", "id": "bad", "npc": 1, "tip": "Murkdeep (rare!)", "iconSize": 26}]
        self.doc.panels[0]["children"] = kids                                        # saved before the stricter name rule existed
        self.a.on_events([{"type": "quest_accepted", "data": {"quest": 4740}}]); self.a.run()
        self.assertIn("Murkdeep", [c for c in self.children() if c["id"] == "qall"][0]["targets"])
        self.assertNotIn("bad", self.ids())
        self.assertEqual(self.a.last["dropped_invalid_children"], ["bad"])

    def test_malformed_quests_and_ids_are_skipped(self):
        import sqlite3
        c = sqlite3.connect(self.a.gdb.path)
        c.execute("INSERT INTO quest VALUES(5001,'Broken',20,15,NULL,148,0,0,'[]','[]','[]','[]','[]',NULL,NULL,0,?)",
                  (json.dumps({"objectives": {"1": [None, [], [8006]], "3": [None, ["x"]]}}),))
        c.execute("INSERT INTO item VALUES(9004,'Odd',15,0,12,0,NULL,?)", (json.dumps({"npcDrops": {"8001": 1}}),))
        c.execute("INSERT INTO quest VALUES(5002,'Odd drops',20,15,NULL,148,0,0,'[]','[]','[]','[]','[]',NULL,NULL,0,?)",
                  (json.dumps({"objectives": {"3": [[9004, "x"]]}}),))
        c.commit(); c.close()
        self.a.on_feed(FEED)
        self.a.on_events([{"type": "quest_accepted", "data": {"quest": q}} for q in (5001, 5002, 10 ** 30, True, "986", -1)])
        self.a.run()
        self.assertIsNone(self.a.last.get("error"), self.a.last)
        names = [c for c in self.children() if c["id"] == "qall"][0]["targets"]
        self.assertIn("Murkdeep", names)                                             # the broken quest's good objective still counts
        self.assertEqual(sorted(self.a.quests()[1]), [986, 1275, 5001, 5002])

    def test_dungeon_quests_skipped_without_needroll(self):
        import sqlite3
        c = sqlite3.connect(self.a.gdb.path)
        c.execute("DELETE FROM boss_loot"); c.commit(); c.close()
        self.a.on_feed(FEED); self.a.run()
        self.assertNotIn("Blackfathom Oracle", [c for c in self.children() if c["id"] == "qall"][0]["targets"])

    def test_old_replayed_event_loses_to_a_newer_section(self):
        old = time.time() - 3600
        self.a.on_feed(FEED)
        self.a.on_events([{"type": "quest_turned_in", "data": {"quest": 986}, "t": old}])   # an hour old, the section is from now
        self.assertIn(986, self.a.quests()[1])
        self.a.on_events([{"type": "quest_complete", "data": {"quest": 986}}])             # objectives done now: no squares needed
        self.assertNotIn(986, self.a.quests()[1])

    def test_kill_credit_and_duplicate_creatures(self):
        import sqlite3
        c = sqlite3.connect(self.a.gdb.path)
        c.execute("INSERT INTO quest VALUES(5003,'Credit',20,15,NULL,148,0,0,'[]','[]','[]','[]','[]',NULL,NULL,0,?)",
                  (json.dumps({"objectives": {"5": [[[8006], 8006, "Murkdeep slain"]]}}),))
        c.commit(); c.close()
        self.a.on_feed(FEED)
        self.a.on_events([{"type": "quest_accepted", "data": {"quest": 5003}}, {"type": "quest_accepted", "data": {"quest": 4740}}])
        self.a.run()
        cells = [k for c in self.children() if str(c["id"]).startswith("q") and c["id"] != "qall" for k in c["children"] if k.get("npc") == 8006]
        self.assertEqual(len(cells), 1)                                              # one Murkdeep square for both quests

    def test_texture_matches_whole_words(self):
        self.assertEqual(autosquares.texture("Bloodfeather Bearer"), "Ability_Hunter_SniperShot")
        self.assertEqual(autosquares.texture("Rabid Thistle Bear"), "Ability_Hunter_Pet_Bear")

    def test_section_ages(self):
        now = 1000.0
        m, mt, q, qt = autosquares.feed_state(b"--- FAST (updated 2s ago) ---\nPosition: map=1439 (D) x=1 y=2\n--- QUESTS (updated 90s ago) ---\n[in progress] A (L1) #986\n", now)
        self.assertEqual((m, mt, q, qt), (1439, 998.0, {986: "in progress"}, 910.0))


if __name__ == "__main__":
    unittest.main()
