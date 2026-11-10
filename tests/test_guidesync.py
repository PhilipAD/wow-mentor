import os, shutil, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RunningServer, jhttp
from wowmentor.bridge import guidesync as G
from wowmentor.server import tugdoc

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "guides")


class GuideSyncTests(unittest.TestCase):
    def setUp(self):
        self.s = RunningServer()
        self.tmp = tempfile.mkdtemp(prefix="wowmentor-gs-")
        self.addons = os.path.join(self.tmp, "AddOns")
        pack = os.path.join(self.addons, "Guidelime_SamplePack", "Guides", "Zones")
        os.makedirs(pack)
        for f in os.listdir(FIX):
            shutil.copyfile(os.path.join(FIX, f), os.path.join(pack, f))
        os.makedirs(os.path.join(self.addons, "Guidelime"))                # the Guidelime addon itself is not a pack
        with open(os.path.join(self.addons, "Guidelime", "x.lua"), "w") as fh:
            fh.write("Guidelime.registerGuide(")
        G.configure(self.addons, self.s.base, os.path.join(self.tmp, "backups"))
        self.state = {"kv": {"uiboot": "100"}, "age": 1.0}
        self.gs = G.GuideSync(self.s.cfg["ui_secret"], lambda: self.state["kv"], lambda: self.state["age"], log=lambda m: None)

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discovery_names(self):
        self.assertEqual(sorted(self.gs.files), ["Sample 1-3 Start Zone", "Sample 3-5 Next Zone"])

    def test_full_edit_cycle(self):
        gs, ui = self.gs, self.s.ui
        gs.scan()
        self.assertEqual(jhttp(ui + "/tug/status")[1]["guides"], 2)
        path = gs.path("Sample 1-3 Start Zone")
        before = G.read(path)
        code, r = jhttp(ui + "/tug/ops", {"guide": "start zone", "note": "tip", "ops": [{"op": "insert", "after_match": "Learn your Level 2", "text": "Mentor tip [A Warlock]"}]})
        self.assertEqual(code, 200, r)
        gs.apply_pending()
        after = G.read(path)
        self.assertIn("Mentor tip [A Warlock]", after)
        self.assertEqual(after.count("\r\n"), after.count("\n"), "line endings kept")
        self.assertEqual(jhttp(ui + "/tug/status")[1]["reload"]["needed"], True)
        self.assertEqual(G.read(os.path.join(self.tmp, "backups", "orig", "Sample 1-3 Start Zone.lua")), before)
        # a second edit made against a stale server cache is re-checked against the real file and refused
        G.write_atomic(path, after.replace("Mentor tip", "Hand edit"))
        jhttp(ui + "/tug/ops", {"guide": "start zone", "ops": [{"op": "replace", "match": "Mentor tip", "text": "x"}]})
        gs.apply_pending()
        self.assertFalse(jhttp(ui + "/tug/status")[1]["recent"][-1]["ok"])
        self.assertIn("Hand edit", G.read(path))
        # restore to orig
        gs.scan()
        jhttp(ui + "/tug/ops", {"guide": "start zone", "restore": "orig"})
        gs.apply_pending()
        self.assertEqual(G.read(path), before)
        # reload detection clears the notice
        self.state["kv"] = {"uiboot": "100"}
        gs.watch_reload()
        self.assertTrue(gs.pending_reload)
        self.state["kv"] = {"uiboot": "101"}
        gs.watch_reload()
        self.assertFalse(gs.pending_reload)
        panels = [p["id"] for p in jhttp(ui + "/state?t=1")[1]["doc"]["panels"]]
        self.assertNotIn("tug-reload", panels)


if __name__ == "__main__":
    unittest.main()
