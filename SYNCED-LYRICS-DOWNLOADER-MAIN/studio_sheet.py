"""Metadata → Import from Spreadsheet: read any public Google Sheet (or Doc with tables) and find songs in it.

* ``parse_link`` / ``fetch`` download every tab of the sheet behind a link (the "anyone with the link" view) and
  ``Sheet`` keeps them (cached as JSON in ``tracker/``).
* A tab is a song table when one of its first rows is a header row with a title column ("Track Title(s)", "Song",
  "Name" …). Every other header (Date, Record Date, Producer, Credited Artist, Recording Location …) becomes a
  column whose value can be imported.
* ``Sheet.find(file name)`` returns every row that could be the song (best first); the window asks which one it is.
* ``default_target`` guesses where a column belongs in the tags, ``parse_date`` reads dates written as
  "Recorded / July 13, 2016", "2019-09-30", "9/30/2019", "Sep 2019" …
"""
import difflib
import html
import json
import re
import time
import urllib.request
from pathlib import Path

import studio_metadata as sm
import studio_tracker as tracker

CACHE_DIR = Path(__file__).resolve().parent / "tracker"
DEFAULT_LINK = "https://docs.google.com/spreadsheets/d/" + tracker.SHEET + "/edit"

# (key, label) — where a column can go in the song's tags
TARGETS = (
    ("skip", "Don't import"), ("date", "Year / date"), ("artist", "Artist"), ("albumartist", "Album artist"),
    ("album", "Album"), ("title", "Title"), ("genre", "Genre"),
    ("comment_studio", "Comment: (Studio: …)"), ("comment_producer", "Comment: (Producer: …)"),
    ("comment", "Comment: add at the end"),
)
TARGET_LABELS = dict(TARGETS)


# ------------------------------------------------------------------ downloading
def parse_link(link):
    """("sheet" | "doc", id, gid or "") for a Google Sheets / Docs link; ValueError otherwise."""
    link = str(link or "").strip()
    match = re.search(r"/spreadsheets/d/([\w-]{20,})", link)
    if match:
        gid = re.search(r"[#&?]gid=(\d+)", link)
        return "sheet", match.group(1), gid.group(1) if gid else ""
    match = re.search(r"/document/d/([\w-]{20,})", link)
    if match:
        return "doc", match.group(1), ""
    raise ValueError("This is not a Google Sheets or Google Docs link "
                     "(it should contain docs.google.com/spreadsheets/d/… or /document/d/…).")


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(request, timeout=90).read().decode("utf-8", "replace")


def sheet_tabs(page):
    """[(name, gid)] from the htmlview page of a spreadsheet."""
    tabs = []
    for name, gid in re.findall(r'items\.push\(\{name:\s*"((?:[^"\\]|\\.)*)",.*?gid:\s*"(\d+)"', page):
        name = html.unescape(re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), name)).replace("\\/", "/")
        tabs.append((name, gid))
    return tabs


def fetch(link, progress=None):
    """Download every tab / table behind ``link``: [{"name", "rows": [[cell, …], …]}]."""
    kind, doc_id, _gid = parse_link(link)
    tables = []
    if kind == "sheet":
        base = f"https://docs.google.com/spreadsheets/d/{doc_id}"
        tabs = sheet_tabs(_get(base + "/htmlview")) or [("Sheet", _gid or "0")]
        for index, (name, gid) in enumerate(tabs, 1):
            if progress:
                progress(f"Downloading tab {index} of {len(tabs)}: {name}…")
            rows = tracker.table_rows(_get(f"{base}/htmlview/sheet?headers=true&gid={gid}"))
            tables.append({"name": name, "rows": [row[1:] for row in rows if row and row[0].isdigit()]})
    else:
        if progress:
            progress("Downloading the document…")
        base = f"https://docs.google.com/document/d/{doc_id}"
        try:
            page = _get(base + "/export?format=html")
        except Exception:
            page = _get(base + "/mobilebasic")
        for index, rows in enumerate(_doc_tables(page), 1):
            tables.append({"name": f"Table {index}", "rows": rows})
    return tables


def _doc_tables(page):
    result = []
    for body in re.findall(r"<table[^>]*>(.*?)</table>", page, re.S):
        rows = tracker.table_rows(body)
        if len(rows) >= 2:
            result.append(rows)
    return result


# ------------------------------------------------------------------ reading the table
NAME_HEADERS = (  # (pattern, rank) — the first column whose header matches the best rank is the song's name
    (r"title", 0), (r"song", 1), (r"track", 2), (r"^name", 3), (r"\bname", 4))
NOT_NAME = re.compile(r"file|instrumental|project title|beat|album", re.I)
ERA_HEADER = re.compile(r"^(era|project|album)\b", re.I)


def _clean_header(text):
    return re.sub(r"\s+", " ", str(text)).strip().strip(":").strip()


def name_column(header):
    best = None
    for index, text in enumerate(header):
        low = _clean_header(text).casefold()
        if not low or NOT_NAME.search(low) and "title" not in low:
            continue
        for pattern, rank in NAME_HEADERS:
            if re.search(pattern, low):
                if best is None or rank < best[0]:
                    best = (rank, index)
                break
    return best[1] if best else None


def song_table(rows):
    """(header row index, name column index) of a song table, or None."""
    for index, row in enumerate(rows[:25]):
        filled = [c for c in row if c.strip()]
        if len(filled) < 3 or sum(len(c) > 60 for c in filled) > 1:
            continue
        column = name_column(row)
        if column is not None:
            return index, column
    return None


class Sheet:
    def __init__(self, tables, link="", fetched=0.0):
        self.link, self.fetched, self.raw = link, fetched, tables
        self.tables = []   # {"name", "header", "name_col", "era_col", "rows": [{"row", "names", "cells", "keys"...}]}
        for table in tables:
            found = song_table(table["rows"])
            if not found:
                continue
            header_index, name_col = found
            header = [_clean_header(c) for c in table["rows"][header_index]]
            era_col = next((i for i, h in enumerate(header) if ERA_HEADER.match(h) and i != name_col), None)
            songs = []
            for offset, row in enumerate(table["rows"][header_index + 1:], header_index + 2):
                row = list(row) + [""] * (len(header) - len(row))
                names = tracker.parse_names(row[name_col]) if name_col < len(row) else []
                if not names or names[0].casefold() == header[name_col].casefold():
                    continue
                keys = [sm.song_key(n) for n in names]
                songs.append({"row": offset, "names": names, "cells": row[:len(header)],
                              "titles": {k[0] for k in keys if k[0]}, "aliases": {a for k in keys for a in k[2]},
                              "versions": tracker._numbers(row[name_col]),
                              "era": row[era_col] if era_col is not None and era_col < len(row) else ""})
            if songs:
                self.tables.append({"name": table["name"], "header": header, "name_col": name_col,
                                    "era_col": era_col, "songs": songs})
        self.by_title = {}
        for table in self.tables:
            for song in table["songs"]:
                for title in song["titles"]:
                    self.by_title.setdefault(title, []).append((table, song))

    def __bool__(self):
        return bool(self.tables)

    @property
    def song_count(self):
        return sum(len(t["songs"]) for t in self.tables)

    def find(self, query, limit=12):
        """Every row that could be the song ``query`` (a file name or typed name): [(strength, table, song)], best first.
        strength: 3 = same title, 2 = same alternative name, 1 = similar name."""
        title, _marks, aliases = sm.song_key(query)
        if not title:
            return []
        found = {}
        for table, song in self.by_title.get(title, []):
            found[(table["name"], song["row"])] = (3, table, song)
        wanted = set(aliases)
        for table in self.tables:
            for song in table["songs"]:
                key = (table["name"], song["row"])
                if key in found:
                    continue
                if title in song["aliases"] or wanted & song["titles"] or wanted & song["aliases"]:
                    found[key] = (2, table, song)
        if not found:  # similar names (typos, missing words)
            for table in self.tables:
                for song in table["songs"]:
                    ratio = max((difflib.SequenceMatcher(None, title, t).ratio() for t in song["titles"]), default=0)
                    if ratio >= 0.85:
                        found[(table["name"], song["row"])] = (1, table, song)
        eras = tracker._eras(query) if "." in Path(str(query)).name or "(" in str(query) else set()
        versions = tracker._numbers(Path(str(query)).stem)

        def rank(item):
            strength, _table, song = item
            era_hit = bool(eras and song["era"].strip().upper() in eras)
            version_hit = bool(versions and song["versions"] & versions)
            return (-strength, not era_hit, not version_hit)

        return sorted(found.values(), key=rank)[:limit]

    # ---- cache
    def save(self, path):
        data = {"link": self.link, "fetched": self.fetched,
                "tables": self.raw}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def cache_path(link):
    return CACHE_DIR / f"sheet_{parse_link(link)[1]}.json"


def load_cached(link):
    try:
        data = json.loads(cache_path(link).read_text(encoding="utf-8"))
        return Sheet(data["tables"], link, data.get("fetched", 0))
    except (OSError, ValueError, KeyError):
        return None


def download(link, progress=None):
    """Fetch the link, keep a cache and return the Sheet (an empty Sheet when no tab looks like a song table)."""
    tables = fetch(link, progress)
    sheet = Sheet(tables, link, time.time())
    if sheet:
        sheet.save(cache_path(link))
    return sheet


# ------------------------------------------------------------------ values
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"], 1)}


def collapse_ranges(text):
    """A date range becomes its last day: "July 23-24, 2019" → "July 24, 2019", "July 23 - August 2, 2019" →
    "August 2, 2019", "23-24 July 2019" → "24 July 2019"."""
    month = r"([A-Za-z]{3,9})\.?"
    dash = r"\s*[-–—]\s*"

    def keep_if_month(match, replacement):
        return replacement(match) if _month(match.group("m")) else match.group(0)

    text = re.sub(month.replace("(", "(?P<m>", 1) + r"\s+\d{1,2}(?:st|nd|rd|th)?" + dash
                  + r"(?P<d>\d{1,2})(?:st|nd|rd|th)?\b",
                  lambda m: keep_if_month(m, lambda x: f"{x.group('m')} {x.group('d')}"), text)
    text = re.sub(month.replace("(", "(?P<m>", 1) + r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*(?:19|20)\d{2})?" + dash
                  + r"(?=[A-Za-z]{3,9}\.?\s+\d)", lambda m: keep_if_month(m, lambda x: ""), text)
    text = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)?" + dash + r"(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m>[A-Za-z]{3,9})\b",
                  lambda m: keep_if_month(m, lambda x: f"{x.group('d')} {x.group('m')}"), text)
    return text


def parse_date(text):
    """"2019-09-30" / "2019-09" / "2019" for the first date in a cell (labels like "Recorded /" are ignored), or "".
    A range such as "July 23-24, 2019" gives its last day (2019-07-24)."""
    text = collapse_ranges(str(text or ""))
    labelled = tracker.record_date(text)  # understands the labels ("Recorded", "File Exported" …)
    own = _plain_date(text)
    return labelled if tracker.precision(labelled) >= tracker.precision(own) else own


def _plain_date(text):
    flat = re.sub(r"\s+", " ", text)
    match = re.search(r"\b((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})\b", flat)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.]((?:19|20)\d{2})\b", flat)
    if match:  # month/day/year (the layout of the Juice WRLD tracker and US sheets)
        month, day = int(match.group(1)), int(match.group(2))
        if month > 12:
            month, day = day, month
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{match.group(3)}-{month:02d}-{day:02d}"
    match = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{2})\b", flat)
    if match and _month(match.group(1)):
        return f"{match.group(3)}-{_month(match.group(1)):02d}-{int(match.group(2)):02d}"
    match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+((?:19|20)\d{2})\b", flat)
    if match and _month(match.group(2)):
        return f"{match.group(3)}-{_month(match.group(2)):02d}-{int(match.group(1)):02d}"
    match = re.search(r"\b([A-Za-z]{3,9})\.?,?\s+((?:19|20)\d{2})\b", flat)
    if match and _month(match.group(1)):
        return f"{match.group(2)}-{_month(match.group(1)):02d}"
    match = re.search(r"\b((?:19|20)\d{2})\b", flat)
    return match.group(1) if match else ""


def _month(word):
    word = word.casefold()
    for name, number in MONTHS.items():
        if name == word or (len(word) >= 3 and name.startswith(word)):
            return number
    return 0


def flat(text):
    """A cell on one line: line breaks become ", " (or " / " for labelled cells)."""
    return re.sub(r"\s*\n\s*", ", ", str(text or "")).strip(" ,")


def default_target(header):
    """Where a column most likely belongs: a key of TARGETS."""
    low = _clean_header(header).casefold()
    if re.search(r"record(ed|ing)?\s*date", low):
        return "date"
    if re.search(r"\bdate", low) and not re.search(r"preview|leak|surface|first", low):
        return "date"
    if re.search(r"producer", low):
        return "comment_producer"
    if re.search(r"(recording\s*)?(location|studio)", low):
        return "comment_studio"
    if re.search(r"credited|artist", low):
        return "artist"
    return "skip"


def column_value(header, text, target):
    """The text a column would write for ``target`` (what the window shows and imports)."""
    if target == "date":
        return parse_date(text)
    if target in ("comment_studio", "comment_producer"):
        value = flat(text)
        return "" if value.strip("-– ?[]") == "" or value.casefold() in ("n/a", "unknown") else value
    return "" if flat(text).strip("-– ") == "" else flat(text)


def apply_values(current, chosen):
    """New tag values ({tag key: text}) from ``chosen`` ([(target, text)]) and the song's ``current`` tags.

    Comment targets are appended to the existing comment (only what is not already in it)."""
    new, comment = {}, str(current.get("comment", "") or "")
    for target, text in chosen:
        if not text or target == "skip":
            continue
        if target == "comment_studio":
            comment = tracker.comment_with_extras(comment, location=text)
        elif target == "comment_producer":
            comment = tracker.comment_with_extras(comment, producers=text)
        elif target == "comment":
            comment = comment if text.casefold() in comment.casefold() else (comment + " " + text).strip()
        else:
            new[target] = text
    if comment != str(current.get("comment", "") or ""):
        new["comment"] = comment
    return {k: v for k, v in new.items() if v != str(current.get(k, "") or "")}
