import json, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from wowmentor.server import uidoc


class UiDocTests(unittest.TestCase):
    def doc(self):
        d = uidoc.Doc()
        uidoc.apply_ops(d, uidoc.catalog()["example"])
        return d

    def test_example_applies_and_wire_is_small_ascii_json(self):
        d = self.doc()
        wire = d.wire()
        self.assertTrue(wire.isascii())
        self.assertLess(len(wire), 5000)
        data = json.loads(wire)
        self.assertEqual(data["v"], 1)
        self.assertEqual(data["rev"], d.rev)
        self.assertEqual([p["id"] for p in data["panels"]], ["plan"])

    def test_validation_messages(self):
        d = self.doc()
        bad = [
            {"op": "panel", "id": "x", "bogus": 1},
            {"op": "child", "panel": "nope", "component": {"type": "text", "text": "a"}},
            {"op": "check", "panel": "plan", "item": "zzz"},
            {"op": "panel", "id": "y", "children": [{"type": "progress"}]},
            {"op": "panel", "id": "bad id!"},
            {"op": "toast"},
            {"op": "theme", "accent": "not-a-colour"},
            {"op": "nope"},
        ]
        for op in bad:
            with self.assertRaises(uidoc.OpError, msg=op):
                uidoc.apply_ops(d, op)

    def test_batches_are_atomic(self):
        d = self.doc()
        before, toasts = d.rev, list(d.toasts)
        with self.assertRaises(uidoc.OpError):
            uidoc.apply_ops(d, [{"op": "toast", "text": "ok"}, {"op": "panel", "id": "x", "w": "wide"}])
        self.assertEqual(d.rev, before)
        self.assertEqual(d.toasts, toasts)

    def test_check_bumps_item_version(self):
        d = self.doc()
        uidoc.apply_ops(d, {"op": "check", "panel": "plan", "item": "wolves", "done": True})
        item = d.panels[0]["children"][2]["items"][0]
        self.assertTrue(item["done"])
        self.assertEqual(item["ver"], d.rev)

    def test_builtin_toggle_and_waypoint_fractions(self):
        d = self.doc()
        uidoc.apply_ops(d, [{"op": "builtin", "me": False}, {"op": "waypoint", "id": "w", "map": 1429, "x": 0.5, "y": 0.25}])
        self.assertIs(d.builtin["me"], False)
        w = next(x for x in d.waypoints if x["id"] == "w")
        self.assertEqual((w["x"], w["y"]), (50, 25))

    def test_skill_page_is_generic_and_mentions_the_essentials(self):
        text = uidoc.skill("http://h/ui/s", "http://h/r?t=<random>", "Goal: be great.")
        for needle in ("wowui brief", "http://h/ui/s", "Goal: be great.", "wowui tug", "/reload"):
            self.assertIn(needle, text)
        self.assertNotIn("{BASE}", text)
        self.assertIn("has not written a profile", uidoc.skill("b", "f", ""))

    def test_skill_covers_rules_inbox_events_webhook_and_the_level_xp_rule(self):
        text = uidoc.skill("http://h/ui/s", "http://h/r?t=<random>", "")
        for needle in ("rule preset bags-full|durability|deaths|buff|lower-rank", "wowui rule add --json", "coach_answer", "wowui questions", "/coach",
                       "wowui events", "http://h/r/events?since=", "X-WoWMentor-Signature", "wowmentor webhook set", "item.auto", "clear_yards", "task",
                       "opId", "partial", "never show the player's level or XP", "SESSION_STATE", "EVENTS_TAIL"):
            self.assertIn(needle, text, needle)
        self.assertNotIn("{feed_root}", text)
        for cond in uidoc.WHEN_TYPES:
            self.assertIn(cond, text, "every rule condition the schema accepts is documented: " + cond)
        for act in uidoc.DO_TYPES:
            self.assertIn(act, text, "every rule action the schema accepts is documented: " + act)
        for kind in uidoc.AUTO_TYPES:
            self.assertIn(kind, text, kind)
        self.assertIn("rule", uidoc.guide())

    def test_rule_texts_and_binds_never_show_level_or_xp(self):
        self.assertEqual(uidoc.BINDS, ("bags", "durability"))
        d = uidoc.Doc()
        with self.assertRaises(uidoc.OpError):
            uidoc.apply_ops(d, {"op": "panel", "id": "x", "children": [{"type": "progress", "bind": "xp"}]})


if __name__ == "__main__":
    unittest.main()
