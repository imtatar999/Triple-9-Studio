import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_tracker as st


def row(era, number, title, producers, location, record):
    cells = ["9", era, number, title, "Juice WRLD", producers, "Eng", "-", "file", "inst", location, record,
             "-", "Surfaced June 1, 2020.", "3:00", "Original Demo Track", "files"]
    return "<tr>" + "".join("<td>" + c.replace("\n", "<br>") + "</td>" for c in cells) + "</tr>"


PAGE = "<table>" + "".join([
    row("JW3", "1", "Hell Girl\nForever | Forever Girl\nSong is titled after the instrumental.", "Nick Mira",
        "Paramount Recording Studio,\nSanta Monica, CA.", "Recorded\nMarch 20-21, 2018.\nFile Exported\nMarch 22, 2018."),
    row("WOD", "2", "Roses (v1)", "Nick Mira", "-", "Recorded\nSeptember 30, 2019."),
    row("GB&GR", "3", "Roses (v2)", "Zaytoven", "-", "Juice's Vocals\nOctober [?], 2018."),
    row("DRFL", "4", "Unknown Date", "-", "-", "Recorded\n[?] [?], 2019."),
    row("DRFL", "5", "No Date", "-", "-", "-"),
]) + "</table>"


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        pages = {name: PAGE if name == "unreleased" else "<table></table>" for name in st.TABS}
        pages["released"] = "<table>" + row("TPNE", "1", "Condone It", "Dre", "Home", "Recorded\nJuly 15, 2019.") + "</table>"
        self.counts = st.update(self.folder.name, pages=pages)
        self.tracker = st.Tracker(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_two_files_are_written_released_separately(self):
        names = sorted(p.name for p in Path(self.folder.name).iterdir())
        self.assertEqual(names, ["juicewrld_tracker_released.txt", "juicewrld_tracker_unreleased.txt"])
        self.assertEqual(self.counts["juicewrld_tracker_released.txt"], 1)

    def test_dates_notes_location_and_producers(self):
        hell = next(e for e in self.tracker.entries if e["number"] == "1" and e["tab"] == "Unreleased")
        self.assertEqual(hell["names"], ["Hell Girl", "Forever", "Forever Girl"])  # the note line is dropped
        self.assertEqual(hell["date"], "2018-03")  # a day range: the day is not exact
        self.assertEqual(hell["location"], "Paramount Recording Studio, Santa Monica, CA.")
        self.assertEqual(hell["producers"], "Nick Mira")
        dates = {e["names"][0]: e["date"] for e in self.tracker.entries}
        self.assertEqual(dates["Roses (v1)"], "2019-09-30")
        self.assertEqual(dates["Roses (v2)"], "2018-10")  # Juice's vocals, "[?]" day
        self.assertEqual(dates["Unknown Date"], "2019")
        self.assertEqual(dates["No Date"], "")
        self.assertEqual(dates["Condone It"], "2019-07-15")

    def test_matching_sure_and_confirm(self):
        status, found = self.tracker.match("Hell Girl (JW3).mp3")
        self.assertEqual((status, found[0]["date"]), ("sure", "2018-03"))
        status, found = self.tracker.match("Forever (Sessions).mp3")  # only an alternative name
        self.assertEqual(status, "sure")
        status, found = self.tracker.match("Roses.mp3")  # two versions, two dates
        self.assertEqual(status, "confirm")
        self.assertEqual({e["date"] for e in found}, {"2019-09-30", "2018-10"})
        status, found = self.tracker.match("Roses (V2) (GB&GR).mp3")
        self.assertEqual((status, [e["date"] for e in found]), ("sure", ["2018-10"]))
        self.assertEqual(self.tracker.match("Not In Sheet.mp3")[0], None)
        self.assertEqual(self.tracker.match("No Date.mp3")[0], None)

    def test_only_more_detail_replaces_a_date(self):
        self.assertTrue(st.better_date("", "2019"))
        self.assertTrue(st.better_date("2019", "2019-09-30"))
        self.assertTrue(st.better_date("2019-09", "2019-09-30"))
        self.assertFalse(st.better_date("2019-09-30", "2019"))
        self.assertFalse(st.better_date("2019-09-30", "2019-09-30"))
        self.assertFalse(st.better_date("2018", "2019-09-30"))
        self.assertTrue(st.conflicts("2018", "2019-09-30"))
        self.assertFalse(st.conflicts("2019", "2019-09-30"))

    def test_comment_gets_studio_and_producer_once(self):
        text = st.comment_with_extras("ERA - JW3 (Recorded July 15, 2019)", "Home Studio, LA", "Nick Mira")
        self.assertEqual(text, "ERA - JW3 (Recorded July 15, 2019) (Studio: Home Studio, LA) (Producer: Nick Mira)")
        self.assertEqual(st.comment_with_extras(text, "Home Studio, LA", "Nick Mira"), text)
        self.assertEqual(st.comment_with_extras("x", "", ""), "x")


if __name__ == "__main__":
    unittest.main()
