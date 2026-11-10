import base64, json, os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import RunningServer, http, jhttp
from wowmentor.server import app as server_app


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = RunningServer()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_health_landing_and_secrets(self):
        s = self.s
        self.assertEqual(http(s.base + "/healthz")[0], 200)
        self.assertIn(b"WoW Mentor server is running", http(s.base + "/")[1])
        self.assertEqual(http(s.base + "/nope")[0], 404)
        self.assertEqual(http("%s/ui/%s/doc" % (s.base, "wrong"))[0], 404)
        self.assertEqual(http("%s/%s" % (s.base, "wrong"))[0], 404)

    def test_feed_push_and_read(self):
        s = self.s
        self.assertEqual(http("%s/push/%s" % (s.base, "bad"), b"x")[0], 404)
        self.assertEqual(http("%s/push/%s" % (s.base, s.cfg["write_secret"]), b"=== WOW LIVE FEED ===\nhello\n")[0], 200)
        code, body = http("%s/%s?t=1" % (s.base, s.cfg["read_token"]))
        self.assertEqual(code, 200)
        self.assertIn(b"hello", body)
        self.assertIn(b"server_age_seconds", body)

    def test_overlay_ops_post_and_get(self):
        s = self.s
        code, r = jhttp(s.ui + "/ops", {"op": "toast", "text": "hi", "level": "good"})
        self.assertEqual((code, r["ok"]), (200, True))
        code, r = jhttp("%s/ops?b=%s" % (s.ui, b64({"op": "panel", "id": "p1", "title": "T", "children": [{"type": "text", "text": "x"}]})))
        self.assertEqual((code, r["ok"]), (200, True))
        code, r = jhttp(s.ui + "/ops", {"op": "panel", "id": "p1", "bogus": 1})
        self.assertEqual(code, 400)
        self.assertIn("unknown field", r["errors"][0])
        code, r = jhttp("%s/state?t=1" % s.ui)
        self.assertIn("p1", [p["id"] for p in r["doc"]["panels"]])
        code, doc = jhttp(s.ui + "/doc")
        self.assertEqual(doc["v"], 1)
        self.assertEqual(jhttp(s.ui + "/schema")[0], 200)

    def test_skill_cli_and_guide_pages(self):
        s = self.s
        code, skill = jhttp(s.ui + "/skill")
        self.assertEqual(code, 200)
        self.assertIn(s.cfg["ui_secret"], skill)
        self.assertIn("wowui brief", skill)
        code, cli = jhttp(s.ui + "/cli")
        self.assertEqual(code, 200)
        self.assertIn("def main", cli)
        self.assertEqual(jhttp(s.ui + "/guide")[0], 200)

    def test_memory_and_brief(self):
        s = self.s
        self.assertTrue(jhttp(s.ui + "/memory/ops", [{"op": "summary", "text": "Plan: reach level 10."}, {"op": "add", "text": "Prefers cloth", "tag": "gear"}])[1]["ok"])
        self.assertEqual(jhttp("%s/memory/ops?b=%s" % (s.ui, b64({"op": "bogus"})))[0], 400)
        code, brief = jhttp(s.ui + "/brief")
        self.assertEqual(code, 200)
        for needle in ("Plan: reach level 10.", "Prefers cloth", "read this first", "Do next"):
            self.assertIn(needle, brief)

    def test_guide_edit_queue_and_reload_notice(self):
        s = self.s
        text = "Guidelime.registerGuide(\r\n[[\r\n[GA Alliance]\r\n[NTest Guide]\r\nAccept: [QA1]\r\nTurnIn: [QT1]\r\n]], \"Pack\")"
        self.assertTrue(jhttp(s.ui + "/tug/file", {"name": "Zone/Test Guide", "text": text})[1]["ok"])
        code, r = jhttp("%s/tug/show?name=test&t=1" % s.ui)
        self.assertIn("Accept: [QA1]", r["text"])
        code, r = jhttp("%s/tug/ops?b=%s" % (s.ui, b64({"guide": "test", "note": "add tip", "ops": [{"op": "insert", "after_match": "Accept:", "text": "A tip"}]})))
        self.assertEqual(code, 200, r)
        eid = r["queued"][0]["id"]
        code, r = jhttp(s.ui + "/tug/ops", {"guide": "test", "ops": [{"op": "replace", "line": 99, "text": "x"}]})
        self.assertEqual(code, 400)
        pend = jhttp(s.ui + "/tug/pending")[1]["queue"]
        self.assertEqual([e["id"] for e in pend], [eid])
        self.assertTrue(jhttp(s.ui + "/tug/applied", {"id": eid, "ok": True, "game_live": True, "boot": "100"})[1]["ok"])
        panels = [p["id"] for p in jhttp("%s/state?t=1" % s.ui)[1]["doc"]["panels"]]
        self.assertIn("tug-reload", panels)
        self.assertTrue(jhttp(s.ui + "/tug/reloaded", {"boot": "101"})[1]["ok"])
        panels = [p["id"] for p in jhttp("%s/state?t=2" % s.ui)[1]["doc"]["panels"]]
        self.assertNotIn("tug-reload", panels)


if __name__ == "__main__":
    unittest.main()
