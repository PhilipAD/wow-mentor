import os, tempfile, unittest
from wowmentor.server import agentmem as am


class MemoryLimits(unittest.TestCase):
    def setUp(self):
        self.mem = am.AgentMemory(os.path.join(tempfile.mkdtemp(), "m.json"))

    def test_limits_are_roomy(self):
        self.assertGreaterEqual(am.MAX_NOTES, 2000)
        self.assertGreaterEqual(am.MAX_SUMMARY, 12000)

    def test_overflow_archives_instead_of_dropping(self):
        old, am.MAX_NOTES = am.MAX_NOTES, 5
        try:
            for i in range(8):
                self.mem.handle("POST", "memory/ops", {}, {"op": "add", "text": "n%d" % i})
            self.assertEqual([n["text"] for n in self.mem.d["notes"]], ["n3", "n4", "n5", "n6", "n7"])
            self.assertEqual([n["text"] for n in self.mem.d["archive"]], ["n0", "n1", "n2"])
            code, shown = self.mem.handle("GET", "memory", {}, None)
            self.assertNotIn("archive", shown)
            self.assertEqual(shown["archived"], 3)
            code, shown = self.mem.handle("GET", "memory", {"archive": ["1"]}, None)
            self.assertEqual(len(shown["archive"]), 3)
            code, shown = self.mem.handle("GET", "memory", {"archive": ["0"]}, None)
            self.assertNotIn("archive", shown)
        finally:
            am.MAX_NOTES = old

    def test_long_summary_survives_reload(self):
        self.mem.handle("POST", "memory/ops", {}, {"op": "summary", "text": "x" * 9000})
        again = am.AgentMemory(self.mem.path)
        self.assertEqual(len(again.d["summary"]), 9000)


if __name__ == "__main__":
    unittest.main()
