import glob, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from wowmentor.server import tugdoc as T

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "guides")


def read(name):
    with open(os.path.join(FIX, name), newline="", encoding="utf-8") as f:
        return f.read()


class TugDocTests(unittest.TestCase):
    def test_roundtrip_is_lossless_and_keeps_line_endings(self):
        for f in glob.glob(FIX + "/*.lua"):
            with open(f, newline="", encoding="utf-8") as fh:
                text = fh.read()
            pre, lines, suf, eol = T.split_file(text)
            self.assertEqual(T.join_file(pre, lines, suf, eol), text)
            errs, _ = T.lint(lines, T.header_counts(lines))
            self.assertEqual(errs, [])

    def test_edit_ops(self):
        src = read("Sample 1-3 Start Zone.lua")
        out, summary, warns, new = T.apply_to_text(src, [
            {"op": "insert", "after_match": "Learn your Level 2", "text": "Note one\nNote two"},
            {"op": "replace", "match": "Note two", "expect": "two", "text": "Note 2 [A Warlock]"},
            {"op": "sub", "find": "Test Arrow", "replace": "Test Arrows", "match": "Buy: [CI555"},
            {"op": "delete", "match": "Note one"},
            {"op": "insert", "at": "end", "text": "Last line"},
        ])
        self.assertIn("\r\n", out)
        self.assertNotIn("\n", out.replace("\r\n", ""), "CRLF preserved")
        self.assertIn("Note 2 [A Warlock]", out)
        self.assertNotIn("Note one", out)
        self.assertIn("Test Arrows", out)
        self.assertEqual(len(summary), 5)

    def test_rejections_explain_themselves(self):
        src = read("Sample 1-3 Start Zone.lua")
        cases = {
            "end the guide": [{"op": "replace", "line": 3, "text": "oops ]] end"}],
            "unbalanced": [{"op": "replace", "line": 3, "text": "unbalanced [QA1"}],
            "matches": [{"op": "delete", "match": "TurnIn"}, {"op": "delete", "match": "["}],
            "expect": [{"op": "replace", "line": 5, "expect": "NOPE", "text": "x"}],
            "header": [{"op": "delete", "match": "[NSample 1-3"}],
            "needs one of": [{"op": "insert", "text": "x"}],
            "unknown op": [{"op": "bogus"}],
            "between 1 and": [{"op": "replace", "line": 9999, "text": "x"}],
        }
        for needle, ops in cases.items():
            with self.assertRaises(T.TugError) as cm:
                T.apply_to_text(src, ops)
            self.assertTrue(any(needle in e for e in cm.exception.errors), (needle, cm.exception.errors))

    def test_unknown_tags_warn_but_apply(self):
        _, _, warns, _ = T.apply_to_text(read("Sample 1-3 Start Zone.lua"), [{"op": "insert", "at": "end", "text": "Do: [QZ123]"}])
        self.assertTrue(any("QZ" in w for w in warns))

    def test_atomic(self):
        src = read("Sample 1-3 Start Zone.lua")
        with self.assertRaises(T.TugError):
            T.apply_to_text(src, [{"op": "insert", "at": "end", "text": "fine"}, {"op": "replace", "line": 999, "text": "x"}])


if __name__ == "__main__":
    unittest.main()
