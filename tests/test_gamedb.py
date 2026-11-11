import base64, gzip, hashlib, json, os, sqlite3, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wowmentor.data import build
from wowmentor.server.gamedb import GameDB


def make_db(path):
    c = sqlite3.connect(path)
    c.executescript(build.SCHEMA)
    c.execute("INSERT INTO zone VALUES(12,'Elwynn Forest',1429,NULL)")
    c.execute("INSERT INTO sort VALUES(-61,'WARLOCK')")
    q = {"name": "Surena Caledon", "questLevel": 10, "requiredLevel": 10, "requiredClasses": 256, "requiredRaces": 1, "startedBy": [[6122]], "finishedBy": [[6122]],
         "nextQuestInChain": 1689, "zoneOrSort": -61}
    c.execute("INSERT INTO quest VALUES(1688,'Surena Caledon',10,10,NULL,-61,1,256,'[6122]','[6122]','[]','[]','[]',1689,NULL,0,?)", (json.dumps(q),))
    c.execute("INSERT INTO quest VALUES(1689,'The Binding',10,10,NULL,-61,1,256,'[6122]','[6122]','[1688]','[]','[]',NULL,NULL,0,?)", (json.dumps({"name": "The Binding"}),))
    c.execute("INSERT INTO npc VALUES(6122,'Gakin the Darkbinder','Warlock Trainer',20,20,18,12,?)", (json.dumps({"name": "Gakin", "questStarts": [1688], "questEnds": [1688]}),))
    c.execute("INSERT INTO spawn VALUES('npc',6122,12,1429,39.2,85.2)")
    c.execute("INSERT INTO meta VALUES('fingerprint','1:1')")
    c.commit(); c.close()


class GameDbTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        make_db(os.path.join(self.d, "forever.sqlite"))
        self.g = GameDB(self.d, lambda n: ("g", "[QA1688] x [G39.5,85 Elwynn Forest]\n[QA424242] y\n[QT1688] z [G5,5 Elwynn Forest]"))

    def get(self, route, **q):
        return self.g.handle("GET", "db/" + route, {k: [str(v)] for k, v in q.items()}, None)

    def test_quest_npc_chain_waypoint(self):
        code, r = self.get("quest", id=1688)
        self.assertEqual(code, 200)
        self.assertEqual(r["quests"][0]["finishedBy"]["npcs"][0]["name"], "Gakin the Darkbinder")
        self.assertEqual(r["quests"][0]["classes"], ["Warlock"])
        self.assertEqual(self.get("npc", name="Gakin")[1]["npcs"][0]["startsQuests"][0]["id"], 1688)
        self.assertEqual([x["id"] for x in self.get("chain", id=1689)[1]["mustCompleteBefore"]], [1688])
        w = self.get("waypoint", quest=1688, which="finish")[1]
        self.assertEqual((w["first"]["uiMapId"], w["first"]["x"]), (1429, 39.2))
        self.assertEqual(self.get("trainers", zone="Elwynn Forest", like="warlock")[1]["count"], 1)
        self.assertEqual(self.get("find_quests", zone="-61")[0], 400)           # a sort id is not a zone name

    def test_validate_flags_unknown_wrong_class_and_far(self):
        r = self.get("validate", guide="g", **{"class": "Mage"})[1]
        kinds = sorted(i["kind"] for i in r["issues"])
        self.assertEqual(kinds, ["FAR_FROM_NPC", "UNKNOWN_QUEST", "WRONG_CLASS", "WRONG_CLASS"])

    def test_missing_db_and_bad_requests(self):
        g = GameDB(tempfile.mkdtemp())
        self.assertEqual(g.handle("GET", "db/quest", {"id": ["1"]}, None)[0], 503)
        self.assertEqual(g.handle("GET", "db/nope", {}, None)[0], 404)
        self.assertEqual(self.get("quest")[0], 400)

    def test_chunked_upload_swaps_the_file(self):
        src = os.path.join(self.d, "new.sqlite")
        make_db(src)
        blob = gzip.compress(open(src, "rb").read())
        sha = hashlib.sha256(blob).hexdigest()
        parts = [blob[i:i + 700] for i in range(0, len(blob), 700)]
        g = GameDB(tempfile.mkdtemp())
        for i, p in enumerate(parts):
            code, r = g.handle("POST", "db/upload", {}, {"sha": sha, "part": i, "of": len(parts), "data": base64.b64encode(p).decode()})
            self.assertEqual(code, 200, r)
        self.assertTrue(r["complete"])
        self.assertTrue(g.status()["present"])
        code, r = g.handle("POST", "db/upload", {}, {"sha": "bad", "part": 0, "of": 1, "data": base64.b64encode(b"x").decode()})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()


class WatchOpTests(unittest.TestCase):
    def test_watch_op_replace_add_remove_and_limits(self):
        from wowmentor.server import uidoc
        d = uidoc.Doc()
        self.assertTrue(uidoc.apply_ops(d, {"op": "watch", "items": [2589, 2592, 2589]})["ok"] if isinstance(uidoc.apply_ops(d, {"op": "watch", "items": [2589, 2592, 2589]}), dict) else True)
        self.assertEqual(d.watch, [2589, 2592])
        uidoc.apply_ops(d, {"op": "watch", "add": [4306], "remove": [2589]})
        self.assertEqual(d.watch, [2592, 4306])
        with self.assertRaises(Exception):
            uidoc.apply_ops(d, {"op": "watch", "items": ["x"]})
        with self.assertRaises(Exception):
            uidoc.apply_ops(d, {"op": "watch", "items": list(range(1, 40))})
        self.assertIn("watch", d.to_dict())
