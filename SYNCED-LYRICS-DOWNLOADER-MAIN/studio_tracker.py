"""Juice WRLD tracker data (the public Google Sheet "Discography Tracker") for dating songs.

* update(): downloads the sheet tabs and writes two plain-text (tab separated) files into ``tracker/``:
  ``juicewrld_tracker_unreleased.txt`` (Unreleased + Unsurfaced + Misc.) and
  ``juicewrld_tracker_released.txt`` (Released Discography). One line per song: tab, era/project, number,
  names, recorded date (YYYY-MM-DD / YYYY-MM / YYYY), recording location, producers, the raw "Record Date" cell.
* Tracker: loads those files and matches MP3 file names to songs ("sure" or "confirm?").
* comment_with_extras(): adds "(Studio: …) (Producer: …)" to an MP3 comment.
"""
import html
import re
import urllib.request
from pathlib import Path

import studio_metadata as sm

SHEET = "1I6g2jrFdxIoYraZqnJ6NfivVHQXLmHR29rFvTAymE_E"
TABS = {"unreleased": ("Unreleased", 1705371403), "unsurfaced": ("Unsurfaced", 1472282293),
        "misc": ("Misc", 161411054), "released": ("Released", 0)}
FILES = {"juicewrld_tracker_unreleased.txt": ("unreleased", "unsurfaced", "misc"),
         "juicewrld_tracker_released.txt": ("released",)}
TRACKER_DIR = Path(__file__).resolve().parent / "tracker"
HEADER = "# tab\tera/project\t#\tnames (separated by ' | ')\trecorded\tlocation\tproducers\traw record cell"


# ------------------------------------------------------------------ reading the sheet
def table_rows(page):
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = []
        for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S):
            cell = re.sub(r"<br\s*/?>", "\n", cell)
            cells.append(html.unescape(re.sub(r"<[^>]+>", "", cell)).replace("\xa0", " ").strip())
        rows.append(cells)
    return rows


def parse_names(cell):
    names = []
    for line in cell.split("\n"):
        for part in line.split(" | "):
            part = part.strip()
            if not part:
                continue
            words = len(part.split())
            if (words >= 4 and part.endswith((".", "!"))) or words > 9:
                continue  # a note such as "Song is titled after the instrumental."
            names.append(part)
    return names


def record_date(cell):
    """"2019-09-30" / "2019-09" / "2019" from a "Record Date(s)" cell, or ""."""
    found = []  # (label, date text)
    label = ""
    for line in (l.strip() for l in cell.split("\n") if l.strip()):
        if re.search(r"\b(19|20)\d{2}\b", line):
            found.append((label, line))
        else:
            label = line

    def rank(item):
        low = item[0].casefold()
        if "export" in low:
            return 9
        return 0 if low.startswith("recorded") else 1 if "juice" in low else 2

    for item in sorted(found, key=rank):
        if rank(item) == 9:
            continue
        date = sm.recorded_date("(Recorded " + item[1].replace("[?]", "?") + ")")
        if date:
            return date
    return ""


def _tidy(text):
    return re.sub(r",?\s*\n\s*", ", ", text).strip(" ,") if text.strip("-– ") else ""


def parse_tab(page, tab):
    entries = []
    for row in table_rows(page):
        if len(row) < 12 or not re.fullmatch(r"\d+", row[2] or "") or not row[3]:
            continue
        names = parse_names(row[3])
        if not names:
            continue
        entries.append({"tab": tab, "era": row[1], "number": row[2], "names": names, "date": record_date(row[11]),
                        "location": _tidy(row[10]), "producers": _tidy(row[5]),
                        "raw": re.sub(r"\s*\n\s*", " / ", row[11])})
    return entries


def fetch(gid):
    url = f"https://docs.google.com/spreadsheets/d/{SHEET}/htmlview/sheet?headers=true&gid={gid}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(request, timeout=90).read().decode("utf-8")


def _line(entry):
    fields = [entry["tab"], entry["era"], entry["number"], " | ".join(entry["names"]), entry["date"],
              entry["location"], entry["producers"], entry["raw"]]
    return "\t".join(str(f).replace("\t", " ").replace("\n", " ") for f in fields)


def update(folder=TRACKER_DIR, pages=None):
    """Download the sheet tabs and (re)write the two text files; returns {file name: song count}."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    counts = {}
    for file_name, tabs in FILES.items():
        lines = [HEADER]
        for tab in tabs:
            page = pages[tab] if pages else fetch(TABS[tab][1])
            lines.extend(_line(e) for e in parse_tab(page, TABS[tab][0]))
        (folder / file_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        counts[file_name] = len(lines) - 1
    return counts


# ------------------------------------------------------------------ matching
def _numbers(text):
    return set(re.findall(r"\bv\s?(\d+)", str(text).casefold()))


def _eras(name):
    stem = Path(name).stem
    return {g.strip().upper() for g in re.findall(r"[(\[]([^()\[\]]*)[)\]]", stem) if sm.ALBUM_MARK.match(g.strip())}


class Tracker:
    def __init__(self, folder=TRACKER_DIR):
        self.entries = []
        self.folder = Path(folder)
        for file_name in FILES:
            path = self.folder / file_name
            if path.is_file():
                self.load(path)
        self.by_title = {}
        for entry in self.entries:
            for title in entry["titles"]:
                self.by_title.setdefault(title, []).append(entry)

    def load(self, path):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            f = (line.split("\t") + [""] * 8)[:8]
            names = [n for n in f[3].split(" | ") if n]
            keys = [sm.song_key(n) for n in names]
            self.entries.append({"tab": f[0], "era": f[1], "number": f[2], "names": names, "date": f[4],
                                 "location": f[5], "producers": f[6], "raw": f[7],
                                 "titles": {k[0] for k in keys if k[0]},
                                 "aliases": {a for k in keys for a in k[2]}, "versions": _numbers(f[3])})

    def __bool__(self):
        return bool(self.entries)

    def match(self, filename):
        """("sure" | "confirm", [entries]) or (None, []). "sure": one clear song (or all matches give one date)."""
        title, _marks, aliases = sm.song_key(filename)
        if not title:
            return None, []
        strong = list(self.by_title.get(title, []))
        weak = []
        if not strong:
            wanted = set(aliases)
            for entry in self.entries:
                if title in entry["aliases"] or wanted & entry["titles"] or wanted & entry["aliases"]:
                    weak.append(entry)
        found = strong or weak
        if not found:
            return None, []
        eras = _eras(filename)
        if eras:
            found = [e for e in found if e["era"].upper() in eras] or found
        versions = _numbers(Path(filename).stem)
        if versions:
            found = [e for e in found if e["versions"] & versions] or found
        dated = [e for e in found if e["date"]]
        if not dated:
            return None, found
        status = "sure" if strong and len({e["date"] for e in dated}) == 1 else "confirm"
        return status, dated


# ------------------------------------------------------------------ applying to a song
def precision(date):
    return {4: 1, 7: 2, 10: 3}.get(len(date or ""), 0)


def better_date(current, proposed):
    """True when ``proposed`` adds detail to ``current`` (or current is empty); False for equal/less detail."""
    current = (current or "")[:10]
    return bool(proposed) and (not current or (precision(proposed) > precision(current)
                                              and proposed.startswith(current)))


def conflicts(current, proposed):
    current = (current or "")[:10]
    return bool(current) and bool(proposed) and not (proposed.startswith(current) or current.startswith(proposed))


def comment_with_extras(comment, location="", producers=""):
    """The comment with "(Studio: …)" and "(Producer: …)" appended (only what is missing)."""
    text = str(comment or "").rstrip()
    add = []
    if location and "(studio:" not in text.casefold():
        add.append(f"(Studio: {location})")
    if producers and "(producer:" not in text.casefold():
        add.append(f"(Producer: {producers})")
    return (text + " " + " ".join(add)).strip() if add else text


def write_comment(path, new_text):
    """Replace the text of the MP3's first comment (adds one when there is none); returns the old text."""
    from mutagen.id3 import ID3, COMM
    tags = ID3(str(path))
    version = 3 if tags.version[:2] == (2, 3) else 4
    frames = tags.getall("COMM")
    old = str(frames[0].text[0]) if frames and frames[0].text else ""
    if frames:
        frames[0].text = [new_text]
    else:
        tags.add(COMM(encoding=3, lang="eng", desc="", text=[new_text]))
    tags.save(str(path), v2_version=version)
    return old
