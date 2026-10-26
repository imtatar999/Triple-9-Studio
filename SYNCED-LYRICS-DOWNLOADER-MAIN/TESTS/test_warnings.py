import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import studio_resync  # noqa: E402
import studio_warnings  # noqa: E402


def load_detector():
    tree = ast.parse((APP_DIR / "triplenine_lyrics_downloader.py").read_text(encoding="utf-8"))
    ns = {"re": re}
    wanted = {"_lrc_timestamp_milliseconds", "_find_lrc_duplicate_timestamp_runs"}
    for node in tree.body:
        if (isinstance(node, ast.FunctionDef) and node.name in wanted) or \
                (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_LRC_TIMED_TEXT_RE" for t in node.targets)):
            exec(compile(ast.Module([node], []), "x", "exec"), ns)
    return ns["_find_lrc_duplicate_timestamp_runs"]


BAD_LRC = "[00:10.00] first line here\n[00:10.00] second line here\n[00:10.00] third line here\n[00:30.00] fine\n"
GOOD_LRC = "[00:01.00] one\n[00:04.50] two\n[00:09.20] three\n"


class WarningIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lyrics = self.root / "SYNCED LYRICS"
        self.lyrics.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.index = studio_warnings.WarningIndex(self.state / "scan_cache.json", self.state / "ignored.json",
                                                  load_detector(), studio_resync)

    def tearDown(self):
        self.tmp.cleanup()

    def song(self, name, lrc_text):
        audio = self.root / (name + ".mp3")
        audio.write_bytes(b"x")
        (self.lyrics / (name + ".lrc")).write_text(lrc_text, encoding="utf-8")
        return audio

    def test_clean_song_and_song_without_lrc_have_no_warning(self):
        clean = self.song("clean", GOOD_LRC)
        lonely = self.root / "lonely.mp3"
        lonely.write_bytes(b"x")
        self.index.begin()
        self.assertIsNone(self.index.for_audio(clean))
        self.assertIsNone(self.index.for_audio(lonely))

    def test_same_time_lines_are_an_error_with_a_readable_tooltip(self):
        audio = self.song("bad", BAD_LRC)
        self.index.begin()
        info = self.index.for_audio(audio)
        self.assertEqual(info["level"], "error")
        self.assertEqual((info["first_line"], info["last_line"]), (1, 4))  # 3 same-time lines + the far line after
        tip = studio_warnings.sync_tooltip(info)
        self.assertIn("possible sync errors", tip)
        self.assertIn("at the same time", tip)
        self.assertIn("Fix this song", tip)

    def test_cached_scan_result_is_used_and_a_changed_file_is_checked_again(self):
        audio = self.song("cached", BAD_LRC)
        lrc = self.lyrics / "cached.lrc"
        runs = load_detector()(BAD_LRC)
        stat = lrc.stat()
        (self.state / "scan_cache.json").write_text(json.dumps({str(lrc.resolve()): {
            "signature": [stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns],
            "version": studio_resync.SCAN_VERSION, "runs": runs}}), encoding="utf-8")
        self.index.detect = lambda text: self.fail("the cache entry matches: nothing should be detected again")
        self.index.begin()
        self.assertEqual(self.index.for_audio(audio)["level"], "error")
        lrc.write_text(GOOD_LRC, encoding="utf-8")  # fixed after the scan
        self.index.detect = load_detector()
        self.index.begin()
        self.assertIsNone(self.index.for_audio(audio))

    def test_ignored_warnings_do_not_count(self):
        audio = self.song("ignored", BAD_LRC)
        lrc = self.lyrics / "ignored.lrc"
        runs = [dict(run, path=str(lrc.resolve())) for run in load_detector()(BAD_LRC)]
        studio_resync.ignore(runs, self.state / "ignored.json")
        self.index.begin()
        self.assertIsNone(self.index.for_audio(audio))

    def test_minor_only_song_is_marked_minor(self):
        text = ("[00:10.00] Bulletproof doors on the back today\n[00:10.27] Got the crest to the clown, T-Rex\n"
                "[00:18.00] Next line\n")
        audio = self.song("minor", text)
        self.index.begin()
        info = self.index.for_audio(audio)
        self.assertEqual(info["level"], "minor")
        self.assertIn("small possible timing issues", studio_warnings.sync_tooltip(info))


class LyricsMatchTests(unittest.TestCase):
    def test_low_match_is_suspect_but_few_lines_or_a_good_match_are_not(self):
        with tempfile.TemporaryDirectory() as folder:
            store = studio_warnings.LyricsMatchStore(Path(folder) / "match.json")
            audio, txt = Path(folder) / "a.mp3", Path(folder) / "a.txt"
            audio.write_bytes(b"x")
            txt.write_text("lyrics", encoding="utf-8")
            self.assertAlmostEqual(store.record(audio, 4, 20, txt), 0.2)
            entry = store.suspect_entry(audio, txt)
            self.assertIsNotNone(entry)
            self.assertIn("20%", studio_warnings.lyrics_tooltip(entry))
            store.record(audio, 12, 20, txt)
            self.assertIsNone(store.suspect_entry(audio, txt))
            store.record(audio, 1, 5, txt)  # only 5 sung lines: says nothing
            self.assertIsNone(store.suspect_entry(audio, txt))

    def test_editing_the_txt_after_the_sync_clears_the_suspicion(self):
        with tempfile.TemporaryDirectory() as folder:
            store = studio_warnings.LyricsMatchStore(Path(folder) / "match.json")
            audio, txt = Path(folder) / "b.mp3", Path(folder) / "b.txt"
            audio.write_bytes(b"x")
            txt.write_text("old wrong lyrics", encoding="utf-8")
            store.record(audio, 2, 30, txt)
            self.assertIsNotNone(store.suspect_entry(audio, txt))
            import os
            later = txt.stat().st_mtime + 50
            txt.write_text("new right lyrics", encoding="utf-8")
            os.utime(txt, (later, later))
            self.assertIsNone(store.suspect_entry(audio, txt))
            store.forget(audio)
            self.assertEqual(store.load(), {})


if __name__ == "__main__":
    unittest.main()
