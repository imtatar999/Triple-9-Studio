import ast
from pathlib import Path
import re
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "triplenine_lyrics_downloader.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))


def load_functions():
    names = {
        "_lrc_timestamp_milliseconds",
        "_find_lrc_duplicate_timestamp_runs",
        "_two_pass_sync_score",
        "_pick_two_pass_result",
    }
    nodes = []
    for node in TREE.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "_LRC_TIMED_TEXT_RE"
                   for target in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
    namespace = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<two pass>", "exec"), namespace)
    return namespace


CLEAN = """[00:10.00] first line
[00:14.00] second line
[00:18.00] third line
[00:22.00] fourth line
"""
COLLAPSED = """[00:10.00] first line
[00:10.00] second line
[00:10.00] third line
[00:22.00] fourth line
"""


class TwoPassSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_functions()

    def score(self, text, direct):
        return self.ns["_two_pass_sync_score"](text, direct)

    def test_score_counts_suspicious_lines(self):
        self.assertEqual(self.score(CLEAN, 4)["suspicious_lines"], 0)
        collapsed = self.score(COLLAPSED, 4)
        self.assertEqual(collapsed["suspicious_lines"], 4)  # 3 on one time + the line 12 s later
        self.assertEqual(collapsed["warnings"], 1)

    def test_fewer_warnings_beat_more_direct_matches(self):
        pick = self.ns["_pick_two_pass_result"]
        # Prompt ON "hears" every line but collapses timestamps: OFF must win.
        self.assertFalse(pick(self.score(COLLAPSED, 4), self.score(CLEAN, 2)))
        self.assertTrue(pick(self.score(CLEAN, 2), self.score(COLLAPSED, 4)))

    def test_direct_matches_break_ties_and_full_tie_keeps_prompt_on(self):
        pick = self.ns["_pick_two_pass_result"]
        self.assertFalse(pick(self.score(CLEAN, 2), self.score(CLEAN, 3)))
        self.assertTrue(pick(self.score(CLEAN, 3), self.score(CLEAN, 2)))
        self.assertTrue(pick(self.score(CLEAN, 3), self.score(CLEAN, 3)))


if __name__ == "__main__":
    unittest.main()
