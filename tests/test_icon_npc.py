import unittest
from wowmentor.server import uidoc


class IconHover(unittest.TestCase):
    def norm(self, comp):
        errs = []
        out = uidoc.normalize_component(comp, errs, "c")
        return out, errs

    def test_icon_accepts_npc_and_tip(self):
        out, errs = self.norm({"type": "icon", "npc": 2237, "tip": "Moonstalker Sire", "texture": "Ability_Hunter_Pet_Cat", "iconSize": 32})
        self.assertEqual(errs, [])
        self.assertEqual(out["npc"], 2237)
        self.assertEqual(out["tip"], "Moonstalker Sire")

    def test_icon_with_item_and_tip(self):
        out, errs = self.norm({"type": "icon", "item": 2589, "tip": "Linen Cloth", "iconSize": 32})
        self.assertEqual(errs, [])
        self.assertEqual(out["item"], 2589)

    def test_npc_alone_is_enough(self):
        out, errs = self.norm({"type": "icon", "npc": 2164})
        self.assertEqual(errs, [])

    def test_icon_needs_a_source(self):
        out, errs = self.norm({"type": "icon", "tip": "x"})
        self.assertTrue(any("icon needs" in e for e in errs))

    def test_panel_with_icon_row_applies(self):
        doc = uidoc.Doc()
        ops = [{"op": "panel", "id": "p", "title": "t", "children": [
            {"type": "row", "children": [
                {"type": "icon", "npc": 2237, "tip": "Moonstalker Sire", "texture": "Ability_Hunter_Pet_Cat", "iconSize": 32},
                {"type": "icon", "item": 2589, "tip": "Linen Cloth", "iconSize": 32}]}]}]
        warns = uidoc.apply_ops(doc, ops)           # raises OpError when invalid
        self.assertEqual(warns, [])
        row = doc.panels[0]["children"][0]
        self.assertEqual(row["children"][0]["npc"], 2237)
        self.assertEqual(row["children"][0]["tip"], "Moonstalker Sire")


if __name__ == "__main__":
    unittest.main()
