import ast
import difflib
from pathlib import Path
import re
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "triplenine_lyrics_downloader.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))


def load_functions():
    names = {"_sung_lyric_lines", "_apply_text_edit_to_lrc", "_lrc_timestamp_milliseconds"}
    nodes = []
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_LRC_TIMED_TEXT_RE" for t in node.targets):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
    namespace = {"re": re, "difflib": difflib}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<text edit>", "exec"), namespace)
    return namespace


LRC = """
[00:03.18] Uh-huh
[00:13.90] Yeah, yeah, yeah


[00:17.14] Diamonds, they heavy, I don't got no balance
[00:19.08] I fuck on a bitch, I be up in her panties
[00:21.34] I do the Percocets, dont fuck with Xannies
"""

TXT = """[Intro]
Uh-huh
Yeah, yeah, yeah

[Chorus]
Diamonds, they heavy, I don't got no balance
I fuck on a bitch, I be up in her panties
I do the Percocets, don't fuck with Xannies

Match accuracy: 84%
https://genius.com/Juice-wrld-chrome-lyrics
"""


class TextEditLrcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_functions()

    def apply(self, lrc, txt):
        return self.ns["_apply_text_edit_to_lrc"](lrc, txt)

    def test_changed_line_keeps_timestamp_and_rest_is_untouched(self):
        new, changed, removed, estimated = self.apply(LRC, TXT)
        self.assertEqual((changed, removed, estimated), (1, 0, 0))
        self.assertIn("[00:21.34] I do the Percocets, don't fuck with Xannies\n", new)
        self.assertEqual(new.replace("don't fuck", "dont fuck"), LRC)

    def test_deleted_line_is_removed_from_lrc(self):
        new, _changed, removed, _estimated = self.apply(LRC, TXT.replace("Uh-huh\n", ""))
        self.assertEqual(removed, 1)
        self.assertNotIn("Uh-huh", new)
        self.assertIn("[00:13.90] Yeah, yeah, yeah", new)

    def test_added_line_gets_an_estimated_time_between_neighbours(self):
        edited = TXT.replace("Uh-huh\n", "Uh-huh\nBrand new line\n")
        new, changed, removed, estimated = self.apply(LRC, edited)
        self.assertEqual((changed, removed, estimated), (1, 0, 1))
        lines = new.splitlines()
        added = lines.index("[00:08.54] Brand new line")  # halfway between 00:03.18 and 00:13.90
        self.assertEqual(lines[added - 1], "[00:03.18] Uh-huh")
        self.assertEqual(lines[added + 1], "[00:13.90] Yeah, yeah, yeah")

    def test_five_lines_replaced_by_one_keeps_first_time_and_removes_rest(self):
        # The real "No Evidence" edit: five lines replaced by a single new line.
        lrc = ("[01:00.00] keep before\n[01:02.00] a\n[01:04.00] b\n[01:06.00] c\n"
               "[01:08.00] d\n[01:10.00] e\n[01:12.00] keep after\n")
        txt = "keep before\nYeah, yeah porridge, yeah, yea\nkeep after\n"
        new, changed, removed, estimated = self.apply(lrc, txt)
        self.assertEqual((changed, removed, estimated), (1, 4, 0))
        self.assertEqual(new, "[01:00.00] keep before\n[01:02.00] Yeah, yeah porridge, yeah, yea\n"
                              "[01:12.00] keep after\n")

    def test_one_line_split_into_two_adds_an_estimated_line(self):
        lrc = "[00:10.00] first\n[00:12.00] one long line\n[00:16.00] last\n"
        new, changed, removed, estimated = self.apply(lrc, "first\none long\nline\nlast\n")
        self.assertEqual((changed, removed, estimated), (1, 0, 1))
        self.assertEqual(new, "[00:10.00] first\n[00:12.00] one long\n[00:14.00] line\n[00:16.00] last\n")

    def test_lines_added_at_the_end_follow_the_last_line(self):
        lrc = "[00:10.00] first\n[00:12.00] second"
        new, _changed, _removed, estimated = self.apply(lrc, "first\nsecond\nthird\n")
        self.assertEqual(estimated, 1)
        self.assertEqual(new, "[00:10.00] first\n[00:12.00] second\n[00:14.00] third\n")

    def test_headers_blanks_and_footer_are_not_sung_lines(self):
        self.assertEqual(self.ns["_sung_lyric_lines"](TXT)[:2], ["Uh-huh", "Yeah, yeah, yeah"])
        self.assertEqual(len(self.ns["_sung_lyric_lines"](TXT)), 5)


if __name__ == "__main__":
    unittest.main()
