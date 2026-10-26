import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_swap as sw

try:
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC, TCON, COMM, USLT, APIC, TPE2
except ImportError:  # pragma: no cover
    ID3 = None

SILENT_MP3 = bytes.fromhex("fffb9064") + bytes(413)  # one MPEG frame is enough for mutagen


def make_mp3(path, title, cover=b"", lyrics=""):
    path.write_bytes(SILENT_MP3 * 20)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=[title])); tags.add(TPE1(encoding=3, text=[title + " artist"]))
    tags.add(TALB(encoding=3, text=[title + " album"])); tags.add(TRCK(encoding=3, text=["3"]))
    tags.add(TDRC(encoding=3, text=["2019"])); tags.add(TCON(encoding=3, text=["Hip-Hop"]))
    tags.add(TPE2(encoding=3, text=["Juice WRLD"]))
    tags.add(COMM(encoding=3, lang="eng", desc="", text=[title + " comment"]))
    if lyrics:
        tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
    if cover:
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=cover))
    tags.save(str(path))


@unittest.skipIf(ID3 is None, "mutagen missing")
class SwapTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "album").mkdir(); (self.dir / "new").mkdir()
        self.old = self.dir / "album" / "Lucid Dreams.mp3"
        self.new = self.dir / "new" / "lucid hq.mp3"
        make_mp3(self.old, "Lucid Dreams", cover=b"COVER", lyrics="[00:01.00] line")
        make_mp3(self.new, "lucid hq")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_all_metadata_and_swap(self):
        keys = [k for k, _ in sw.FIELDS + sw.EXTRAS]
        new_final, old_final, backup = sw.swap(self.old, self.new, keys, self.dir / "backups")
        self.assertEqual(new_final, self.dir / "album" / "Lucid Dreams.mp3")  # new file takes old's place and name
        self.assertEqual(old_final, self.dir / "new" / "Old Metadata swapped songs" / "Lucid Dreams.mp3")
        got = sw.read(new_final)
        self.assertEqual((got["title"], got["artist"], got["album"], got["tracknumber"], got["date"], got["genre"]),
                         ("Lucid Dreams", "Lucid Dreams artist", "Lucid Dreams album", "3", "2019", "Hip-Hop"))
        self.assertEqual(got["comment"], "Lucid Dreams comment")
        self.assertEqual(got["albumartist"], "Juice WRLD")
        self.assertTrue(got["lyrics"] and got["cover"])
        self.assertEqual(len(list(backup.rglob("*.mp3"))), 2)

    def test_only_chosen_fields(self):
        new_final, _old_final, _ = sw.swap(self.old, self.new, ["title", "cover"], self.dir / "backups")
        self.assertEqual(new_final.name, "lucid hq.mp3")  # file name not chosen: kept
        got = sw.read(new_final)
        self.assertEqual(got["title"], "Lucid Dreams")
        self.assertEqual(got["artist"], "lucid hq artist")  # not chosen: unchanged
        self.assertTrue(got["cover"]) and self.assertFalse(got["lyrics"])

    def test_write_fields_edits_and_clears(self):
        sw.write_fields(self.old, {"title": "New Title", "artist": "", "date": "2021", "comment": "hi", "tracknumber": "7"},
                        self.dir / "backups")
        got = sw.read(self.old)
        self.assertEqual((got["title"], got["artist"], got["date"], got["comment"], got["tracknumber"]),
                         ("New Title", "", "2021", "hi", "7"))
        self.assertEqual(got["album"], "Lucid Dreams album")  # untouched
        self.assertTrue(got["cover"] and got["lyrics"])  # lyrics and cover survive a tag edit
        self.assertEqual(len(list((self.dir / "backups" / "METADATA-EDITS").rglob("*.mp3"))), 1)

    def test_strip_brackets_with_and_without_spaces(self):
        cases = {"Swerve(JW3)": "Swerve", "swerve (jw3)": "swerve", "Song (feat. X) (JW3)": "Song (feat. X)", "A (ft B)(WOD)": "A (ft B)", "A (with Me) (x)": "A (with Me)", "A (Without Me)": "A", "Song (OG) (WOD)": "Song (OG)", "Song (Alt) (WOD)": "Song (Alt)",
                 "Song [Sessions](JW3)": "Song [Sessions]", "Song (Session Edit) (WOD)": "Song (Session Edit)", "Two  Words  (WOD) ": "Two Words", "No brackets": "No brackets",
                 "(Intro)": "(Intro)", "A (B (C)) D": "A D", "Rock-n-Roll (Live)": "Rock-n-Roll"}
        for given, expected in cases.items():
            self.assertEqual(sw.strip_brackets(given), expected, given)

    def test_filename_from_title(self):
        self.assertEqual(sw.filename_from_title('What? A: "Song" / Mix'), "What A Song Mix")

    def test_same_song_is_refused(self):
        with self.assertRaises(ValueError):
            sw.swap(self.old, self.old, ["title"], self.dir / "backups")


if __name__ == "__main__":
    unittest.main()
