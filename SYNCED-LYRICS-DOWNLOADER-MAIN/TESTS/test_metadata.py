import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import studio_metadata as m  # noqa: E402

SAMPLE_MP3 = Path(r"C:\Users\Admin\Downloads\Testjuice\Juice WRLD\Juice WRLD\UNRELEASED") / "All Out (WOD).mp3"


class SongKeyTests(unittest.TestCase):
    def test_album_marks_and_featured_artists_are_left_out(self):
        self.assertEqual(m.song_key("Condone It (DRFL) .mp3"), m.song_key("Condone It.mp3"))
        self.assertEqual(m.song_key("Oxy (feat. Lil Wayne).mp3"), m.song_key("Oxy (feat. Future,Lil Wayne).mp3"))
        self.assertEqual(m.song_key("ON MY MIND.mp3"), m.song_key("On My Mind (WOD).mp3"))
        self.assertEqual(m.song_key("Song ft. Somebody.mp3")[0], "song")

    def test_version_marks_make_a_different_song(self):
        base = m.song_key("Make It Back (WOD).mp3")
        for other in ("Make It Back (Sessions) (WOD).mp3", "Make It Back (Session).mp3", "Make It Back (V2).mp3",
                      "Make It Back (Session Edit).mp3", "Make It Back (OG).mp3", "Make It Back (Stem).mp3"):
            self.assertNotEqual(m.song_key(other), base, other)

    def test_alternative_names_are_kept_apart(self):
        title, marks, aliases = m.song_key("Tropical Storm (I Wonder) (JW3) .mp3")
        self.assertEqual((title, marks, aliases), ("tropical storm", (), ("i wonder",)))


class FindDuplicatesTests(unittest.TestCase):
    def test_same_and_possible_groups_with_both_locations(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for rel in ("RELEASED/Condone It.mp3", "UNRELEASED/Condone It (DRFL) .mp3",
                        "UNRELEASED/Floor It.mp3", "UNRELEASED/Floor It (Different Colored Pills) (JW3).mp3",
                        "UNRELEASED/Make It Back (WOD).mp3", "UNRELEASED/Make It Back (Sessions) (WOD).mp3",
                        "UNRELEASED/Lonely.mp3", "BACKUPS/x/Lonely.mp3", "UNRELEASED/notes.txt"):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
            found = m.find_duplicates([root])
            summary = [(kind, [str(p.relative_to(root)).replace("\\", "/") for p in paths]) for kind, paths in found]
            self.assertEqual(summary, [
                ("same", ["RELEASED/Condone It.mp3", "UNRELEASED/Condone It (DRFL) .mp3"]),
                ("possible", ["UNRELEASED/Floor It (Different Colored Pills) (JW3).mp3", "UNRELEASED/Floor It.mp3"]),
            ])  # Sessions ≠ normal version; backups are not searched


class RecordedYearTests(unittest.TestCase):
    def test_the_recorded_year_is_taken(self):
        self.assertEqual(m.recorded_year("Era - JW3 (Outsiders) (Recorded July 15, 2019) (Leaked Jan 24, 2026)"), 2019)
        self.assertEqual(m.recorded_year("ERA - WOD (Recorded June 19-20, 2018?) (Leaked Sept 6, 2019)"), 2018)
        self.assertEqual(m.recorded_year("recorded: 2017"), 2017)

    def test_exact_dates_keep_month_and_day(self):
        cases = {
            "(Recorded Sept 30, 2019) (Leaked Jan 1, 2026)": "2019-09-30",
            "(Recorded September 3 2019)": "2019-09-03",
            "(Recorded Oct 4th, 2018)": "2018-10-04",
            "(Recorded 15 March 2017)": "2017-03-15",
            "(Recorded Dec. 25, 2016)": "2016-12-25",
            "(Recorded 2019-09-30)": "2019-09-30",
            "(Recorded March 2020)": "2020-03",
            "(Recorded June 19-20, 2018?)": "2018-06",
            "(Recorded July 15, 2019?)": "2019-07",
            "(Recorded 2019)": "2019",
            "(Recorded ?, 2019)": "2019",
        }
        for comment, expected in cases.items():
            self.assertEqual(m.recorded_date(comment), expected, comment)

    def test_leaked_unknown_or_missing_dates_are_not_used(self):
        for comment in ("ERA - WOD (Leaked Oct 4, 2022)", "ERA - DRFL (Recorded ?) (Leaked March 23, 2023)",
                        "(Recorded ?, Leaked 2021)", "(Recorded Unknown) (Leaked Jan 1, 2020)", "", None):
            self.assertIsNone(m.recorded_year(comment), comment)


@unittest.skipUnless(SAMPLE_MP3.is_file(), "sample MP3 not available")
class WriteYearTests(unittest.TestCase):
    def test_year_is_written_and_read_back_on_a_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / SAMPLE_MP3.name
            shutil.copy2(SAMPLE_MP3, copy)
            m.write_year(copy, 2011)
            self.assertEqual(m.read_year(copy), "2011")
            self.assertEqual(m.read_comments(copy), m.read_comments(SAMPLE_MP3))  # the comment is untouched
            plan = m.plan_years([copy])
            self.assertEqual(len(plan), 1)  # comment says 2018, the file now says 2011
            self.assertEqual(plan[0][2], "2011")
            self.assertEqual(plan[0][3][:4], "2018")


if __name__ == "__main__":
    unittest.main()
