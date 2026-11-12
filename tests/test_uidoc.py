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


class AwarenessOpTests(unittest.TestCase):
    def test_afk_aware_rules_events_and_quest_group_ticks(self):
        d = uidoc.Doc()
        uidoc.apply_ops(d, [
            {"op": "rule", "id": "a", "active_only": True, "defer_max_s": 300, "when": {"type": "pet_missing"}, "then": [{"do": "toast", "text": "pet"}]},
            {"op": "rule", "id": "b", "when": {"type": "returned_active", "min_away_s": 600}, "then": [{"do": "chat", "text": "back after {away_s}"}]},
            {"op": "rule", "id": "c", "when": {"type": "readiness_changed", "ready": False}, "then": [{"do": "toast", "text": "{missing}"}]},
            {"op": "panel", "id": "p", "title": "T", "children": [{"type": "checklist", "id": "l", "items": [
                {"id": "g", "text": "Group", "auto": {"type": "quest_group", "quests": [22, 12, 102], "mode": "all"}}]}]}])
        self.assertTrue(d.rules[0]["active_only"] and d.rules[0]["defer_max_s"] == 300)
        uidoc.apply_ops(d, {"op": "rule", "id": "x", "defer_max_s": 1, "when": {"type": "pet_missing"}, "then": [{"do": "flash"}]})
        self.assertEqual(next(r for r in d.rules if r["id"] == "x")["defer_max_s"], 10)      # numbers clamp to their range
        for bad in ({"op": "rule", "id": "y", "active_only": "yes", "when": {"type": "pet_missing"}, "then": [{"do": "flash"}]},
                    {"op": "panel", "id": "q", "title": "T", "children": [{"type": "checklist", "id": "l", "items": [
                        {"id": "g", "text": "G", "auto": {"type": "quest_group", "quests": []}}]}]}):
            with self.assertRaises(uidoc.OpError):
                uidoc.apply_ops(d, bad)
        text = uidoc.skill_text if hasattr(uidoc, "skill_text") else None
        if text:
            page = text(None) if callable(text) else str(text)
            self.assertIn("active_only", page)

    def test_marks_tomtom_poi_and_skip_to(self):
        d = uidoc.Doc()
        uidoc.apply_ops(d, [{"op": "mark", "id": "g1", "name": "Salma Saldean", "npc": 235, "icon": "quest", "text": "QA 22"},
                            {"op": "mark", "id": "g2", "name": "Goretusk", "icon": "skull"},
                            {"op": "action", "name": "tomtom_poi", "on": False},
                            {"op": "action", "name": "guidelime", "cmd": "skip_to", "step": 20}])
        self.assertEqual([m["id"] for m in d.marks], ["g1", "g2"])
        self.assertEqual(d.to_dict()["marks"][0]["npc"], 235)
        uidoc.apply_ops(d, {"op": "mark", "id": "g1", "remove": True})
        self.assertEqual([m["id"] for m in d.marks], ["g2"])
        for bad in ({"op": "mark", "id": "x"}, {"op": "mark", "id": "x", "name": "A", "icon": "banana"}, {"op": "action", "name": "tomtom_poi"},
                    {"op": "action", "name": "guidelime", "cmd": "skip_to"}):
            with self.assertRaises(uidoc.OpError):
                uidoc.apply_ops(d, bad)
        uidoc.apply_ops(d, {"op": "mark", "clear": True})
        self.assertEqual(d.marks, [])

    def test_verdict_spells_ctx_voice_and_new_rule_types(self):
        d = uidoc.Doc()
        uidoc.apply_ops(d, [{"op": "verdict", "item": 6543, "text": "keep for AH"}, {"op": "watch", "spells": [348, 5782]}, {"op": "builtin", "voice": True},
                            {"op": "panel", "id": "pvp", "title": "Ready", "visibility": {"ctx": ["contested", "wild"]}},
                            {"op": "rule", "id": "s", "when": {"type": "item_count_lte", "item": 6265, "n": 2}, "then": [{"do": "speak", "text": "Low on shards"}]},
                            {"op": "rule", "id": "h", "when": {"type": "hostiles_near_gte", "n": 3, "players": True}, "then": [{"do": "toast", "text": "{count} hostiles"}]},
                            {"op": "rule", "id": "e", "when": {"type": "enemy_player", "class": "Hunter"}, "then": [{"do": "toast", "text": "Hunter {level}"}]},
                            {"op": "rule", "id": "p", "when": {"type": "pet_dead"}, "then": [{"do": "toast", "text": "pet dead"}]},
                            {"op": "action", "name": "speak", "text": "hello"}])
        self.assertEqual((d.verdicts, d.watch_spells, d.builtin), ({"6543": "keep for AH"}, [348, 5782], {"voice": True}))
        uidoc.apply_ops(d, {"op": "verdict", "item": 6543, "remove": True})
        self.assertEqual(d.verdicts, {})
        for bad in ({"op": "panel", "id": "x", "title": "x", "visibility": {"ctx": ["moon"]}}, {"op": "verdict", "item": 1}, {"op": "action", "name": "speak"},
                    {"op": "rule", "id": "z", "when": {"type": "item_count_lte", "n": 1}, "then": [{"do": "flash"}]}):
            with self.assertRaises(uidoc.OpError):
                uidoc.apply_ops(d, bad)
