import ast
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_resync

SOURCE = Path(__file__).resolve().parents[1] / "triplenine_lyrics_downloader.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))


def load(names, **extra):
    nodes = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in names]
    namespace = {"Path": Path, "os": os, "re": re, "studio_resync": studio_resync}
    namespace.update(extra)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<cleanup>", "exec"), namespace)
    return namespace


class CleanupFixTests(unittest.TestCase):
    def test_plain_txt_wins_over_configured_lrc(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            song = folder / "Song.mp3"
            song.write_bytes(b"")
            (folder / "LYRICS").mkdir()
            (folder / "LYRICS" / "Song.lrc").write_text("[00:01.00] timed", encoding="utf-8")
            config = {"lyrics_subfolder": True}
            ns = load({"_find_plain_lyrics_for_audio", "_find_plain_txt_for_audio", "get_lyrics_path"},
                      config=config)
            find = ns["_find_plain_lyrics_for_audio"]
            self.assertEqual(Path(find(str(song))).name, "Song.lrc")  # LRC only as a fallback
            (folder / "Song.txt").write_text("plain", encoding="utf-8")
            self.assertEqual(Path(find(str(song))).name, "Song.txt")

    def test_source_footer_is_not_sent_to_sync(self):
        ns = load({"_prepare_syncalong_lyrics_text"})
        with tempfile.TemporaryDirectory() as d:
            txt = Path(d) / "a.txt"
            txt.write_text("Line one\nLine two\n\nSource selected manually by user\n"
                           "Source confirmed by user\nMatch accuracy: 84%\n"
                           "https://genius.com/A-song-lyrics\n", encoding="utf-8")
            self.assertEqual(ns["_prepare_syncalong_lyrics_text"](str(txt)), "Line one\nLine two\n")

    def test_lrc_locations_prefer_synced_lyrics_folder(self):
        names = [p.parent.name for p in studio_resync.lrc_locations(Path("C:/music/Song.mp3"))]
        self.assertEqual(names, ["SYNCED LYRICS", "LYRICS", "music"])


if __name__ == "__main__":
    unittest.main()
