import json, os, sqlite3, sys, tempfile, threading, unittest

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
    for nid, area, n in ((8001, 148, 3), (8002, 148, 2), (8005, 719, 4), (8006, 148, 1)):
        for i in range(n):
            c.execute("INSERT INTO spawn VALUES('npc',?,?,1439,?,80)", (nid, area, 40 + i))
    c.execute("INSERT INTO item VALUES(9001,'Fine Moonstalker Pelt',15,0,12,0,NULL,?)", (json.dumps({"npcDrops": [8001, 8002]}),))
    c.execute("INSERT INTO item VALUES(9003,'Brain Stem',15,0,12,0,NULL,?)", (json.dumps({"npcDrops": [8005]}),))
    c.execute("INSERT INTO drop_rate VALUES(8002,9001,55,30,'forever',0)")
    c.execute("INSERT INTO drop_rate VALUES(8001,9001,40,NULL,'classic',0)")
    c.execute("INSERT INTO boss_loot VALUES('BFD','Blackfathom Deeps','DUNGEON','20-30','Gelihast',NULL,1,NULL)")
    for qid, name, obj in ((986, "A Lost Master", {"3": [[9001, "Pelt"]]}), (1275, "Researching the Corruption", {"3": [[9003, "Stem"]]}),
                           (4740, "WANTED: Murkdeep!", {"1": [[8006, "Murkdeep slain"]]})):
        c.execute("INSERT INTO quest VALUES(?,?,20,15,NULL,148,0,0,'[]','[]','[]','[]','[]',NULL,NULL,0,?)", (qid, name, json.dumps({"name": name, "objectives": obj})))
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
        self.a = autosquares.AutoSquares(world(), threading.Lock(), lambda: self.doc, self.save, uidoc.apply_ops, delay=60)

    def save(self):
        self.saved += 1

    def children(self):
        return [p for p in self.doc.panels if p["id"] == "squares"][0]["children"]

    def test_feed_builds_quest_rows_and_keeps_profession_rows(self):
        self.a.on_feed(FEED); self.a.timer.cancel(); self.a.run()
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
        self.a.on_feed(FEED); self.a.timer.cancel(); self.a.run()
        self.a.on_events([{"type": "quest_accepted", "data": {"quest": 4740}}, {"type": "quest_turned_in", "data": {"quest": 986}}])
        self.a.timer.cancel(); self.a.run()
        names = [c for c in self.children() if c["id"] == "qall"][0]["targets"]
        self.assertEqual(names, ["Murkdeep"])
        self.a.on_feed(FEED); self.a.timer.cancel(); self.a.run()                    # a stale QUESTS section does not bring 986 back
        self.assertEqual([c for c in self.children() if c["id"] == "qall"][0]["targets"], ["Murkdeep"])
        self.a.on_events([{"type": "quest_turned_in", "data": {"quest": 4740}}]); self.a.timer.cancel(); self.a.run()
        ch = self.children()
        self.assertEqual([c["id"] for c in ch], ["hq", "hc", "c1"])
        self.assertIn("nothing", ch[0]["text"])

    def test_no_panel_no_change_and_same_state_no_rewrite(self):
        self.a.on_feed(FEED); self.a.timer.cancel(); self.a.run(); self.a.run()
        self.assertEqual(self.saved, 1)
        self.doc.panels = []
        self.a.on_events([{"type": "quest_accepted", "data": {"quest": 4740}}]); self.a.timer.cancel(); self.a.run()
        self.assertEqual(self.saved, 1)
        self.assertIsNone(self.a.last.get("error"))


if __name__ == "__main__":
    unittest.main()
