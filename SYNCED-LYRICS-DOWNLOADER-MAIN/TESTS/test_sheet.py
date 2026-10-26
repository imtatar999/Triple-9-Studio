import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_sheet as ss

HEADER = ["ERA:", "#:", "ERA | Track Title(s):", "Credited Artist(s):", "Producer(s):", "Recording Location(s):",
          "Record Date(s):", "Date(s):"]
ROWS = [["intro text"], HEADER,
        ["GB&GR", "1", "Take No Chance", "Juice WRLD", "Nick Mira", "Glass Tree, Chicago", "Recorded\nApril 29, 2018.", "Surfaced\nJuly 28, 2021."],
        ["JW3", "2", "Take No Chance (v2)", "Juice WRLD", "Unknown", "-", "-", "-"],
        ["JW3", "3", "Hell\nPotions", "Juice WRLD", "Dorien Theus", "Studio X", "3/20/2017", "Aug 2018"]]


class SheetTests(unittest.TestCase):
    def setUp(self):
        self.sheet = ss.Sheet([{"name": "Unreleased", "rows": ROWS}, {"name": "Notes", "rows": [["a", "b"]]}])

    def test_only_song_tables_are_read(self):
        self.assertEqual([t["name"] for t in self.sheet.tables], ["Unreleased"])
        self.assertEqual(self.sheet.song_count, 3)

    def test_find_all_places_and_alias(self):
        found = self.sheet.find("Take No Chance (GB&GR).mp3")
        self.assertEqual([song["row"] for _s, _t, song in found][0], 3)
        self.assertEqual(len(found), 2)  # the v2 row is offered too, the user decides
        self.assertTrue(self.sheet.find("Potions.mp3"))
        self.assertEqual(self.sheet.find("Unknown Thing.mp3"), [])

    def test_dates_and_targets(self):
        self.assertEqual(ss.parse_date("Recorded\nApril 29, 2018."), "2018-04-29")
        self.assertEqual(ss.parse_date("3/20/2017"), "2017-03-20")
        self.assertEqual(ss.parse_date("Aug 2018"), "2018-08")
        self.assertEqual(ss.parse_date("2019-09-30"), "2019-09-30")
        self.assertEqual(ss.parse_date("Recorded\nJuly 23-24, 2019."), "2019-07-24")
        self.assertEqual(ss.parse_date("July 23 - August 2, 2019"), "2019-08-02")
        self.assertEqual(ss.default_target("Record Date(s):"), "date")
        self.assertEqual(ss.default_target("Producer(s):"), "comment_producer")
        self.assertEqual(ss.default_target("Credited Artist(s):"), "artist")
        self.assertEqual(ss.default_target("Duration:"), "skip")

    def test_apply_values_keeps_comment(self):
        new = ss.apply_values({"comment": "good (Studio: A)", "date": "2018"},
                              [("date", "2018-04-29"), ("comment_studio", "A"), ("comment_producer", "Nick Mira"),
                               ("artist", "Juice WRLD")])
        self.assertEqual(new, {"date": "2018-04-29", "comment": "good (Studio: A) (Producer: Nick Mira)",
                               "artist": "Juice WRLD"})


if __name__ == "__main__":
    unittest.main()
