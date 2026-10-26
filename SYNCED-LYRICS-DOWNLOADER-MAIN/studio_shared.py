"""Download → Get synced lyrics from the shared sheet: match the songs in Tracks with the public
"SYNCED LYRICS DATABASE" sheet (made by studio_export + fill_links.gs) and download their LRC files.

* ``fetch_rows(link)`` reads every tab of the public sheet: Song, Album, Artist, Length and the Download link.
* ``plan(songs, rows)`` pairs each local song with a row (same file name; else the same name without brackets
  when that is unique) and compares the lengths so a different version is not imported by mistake.
* ``download(url)`` fetches one LRC (it must contain timestamps).
"""
import difflib
import html
import re
import urllib.parse
import urllib.request
from pathlib import Path

import studio_metadata as sm
import studio_resync
import studio_sheet

DEFAULT_LINK = "https://docs.google.com/spreadsheets/d/1Qn-od3N33qSeKpqkOMz0gu863WWvt6t5F2OrVlQur-c/edit"
LENGTH_TOLERANCE = 3  # seconds a song may differ from the sheet and still count as the same version
TIMESTAMP = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")


def _get(url, limit=None):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(limit) if limit else response.read()


def _cells(row_html):
    """[(text, link)] of one table row; the link is the real target (Google's redirect removed)."""
    cells = []
    for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S):
        link = ""
        found = re.search(r'href="([^"]+)"', cell)
        if found:
            link = html.unescape(found.group(1))
            if "google.com/url" in link:
                link = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("q", [link])[0]
        text = re.sub(r"<br\s*/?>", "\n", cell)
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ").strip()
        cells.append((text, link))
    return cells


def parse_tab(page):
    """Rows of one tab: [{"song", "album", "artist", "length", "url"}] (header row found by its column names)."""
    rows, columns = [], None
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = _cells(row_html)
        texts = [t.casefold().strip(" :") for t, _l in cells]
        if columns is None:
            if "song" in texts and any("download" in t for t in texts):
                columns = {"song": texts.index("song"),
                           "url": next(i for i, t in enumerate(texts) if "download" in t)}
                for key in ("album", "artist", "length"):
                    if key in texts:
                        columns[key] = texts.index(key)
            continue
        def value(key, part=0):
            index = columns.get(key)
            return cells[index][part] if index is not None and index < len(cells) else ""
        song = value("song").strip()
        url = value("url", 1)
        if song and url:
            rows.append({"song": song, "album": value("album"), "artist": value("artist"),
                         "length": value("length"), "url": url})
    return rows


def fetch_rows(link, progress=None):
    kind, doc_id, gid = studio_sheet.parse_link(link)
    if kind != "sheet":
        raise ValueError("Paste the link of the Google Sheet with the songs.")
    base = f"https://docs.google.com/spreadsheets/d/{doc_id}"
    tabs = studio_sheet.sheet_tabs(_get(base + "/htmlview").decode("utf-8", "replace")) or [("Sheet", gid or "0")]
    rows = []
    for index, (name, tab_gid) in enumerate(tabs, 1):
        if progress:
            progress(f"Reading the sheet ({index}/{len(tabs)}): {name}…")
        page = _get(f"{base}/htmlview/sheet?headers=true&gid={tab_gid}").decode("utf-8", "replace")
        rows.extend(parse_tab(page))
    return rows


def seconds(text):
    """"2:21" / "02:21" / "1:02:03" → seconds (None when it is not a length)."""
    parts = str(text or "").strip().split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    if len(parts) == 3 and parts[2] == "00" and int(parts[0]) >= 10:
        # Google Sheets reads a long "40:04" (min:s) as 40 h 4 min and shows "40:04:00": undo that
        return int(parts[0]) * 60 + int(parts[1])
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def _name(text):
    return re.sub(r"\s+", " ", str(text)).strip().casefold()


def variants(stem):
    """The ways a file name may hide the song name: as is, without a leading track number ("01 - "), without the
    artist in front ("Juice WRLD - Swerve (JW3)" → "Swerve (JW3)"); underscores count as spaces."""
    text = re.sub(r"\s+", " ", str(stem).replace("_", " ")).strip()
    found = [text]
    numberless = re.sub(r"^\d{1,3}\s*[-.)]+\s*", "", text).strip()
    if numberless and numberless not in found:
        found.append(numberless)
    for base in list(found):
        for separator in (" - ", " – ", " — "):
            if separator in base:
                rest = base.split(separator, 1)[1].strip()
                if rest and rest not in found:
                    found.append(rest)
    return found


def plan(songs, rows, length_of):
    """Pair every song with a row of the sheet. Each item:
    {"song", "row" or None, "how", "sure", "local", "sheet", "same_length"}.

    how: "name" (the same name), "cleaned" (the same after dropping a track number / "Artist - " in front),
    "similar" (the same title, other brackets) or "fuzzy" (a close spelling). ``sure`` is True only for the first two:
    the window asks the user to confirm the others."""
    by_name, by_key, titles = {}, {}, {}
    for row in rows:
        by_name.setdefault(_name(row["song"]), []).append(row)
        title, marks, _aliases = sm.song_key(row["song"])
        by_key.setdefault((title, marks), []).append(row)
        titles.setdefault(title, []).append(row)
    result = []
    for song in songs:
        stem = Path(song).stem
        local = length_of(song)
        options = variants(stem)
        found, how = [], ""
        for index, text in enumerate(options):
            hit = by_name.get(_name(text), [])
            if hit:
                found, how = hit, "name" if index == 0 else "cleaned"
                break
        if not found:
            for text in options:
                title, marks, _aliases = sm.song_key(text)
                candidates = by_key.get((title, marks), []) if title else []
                if candidates and len({_name(r["song"]) for r in candidates}) == 1:
                    found, how = candidates, "similar"
                    break
        if not found:
            best, best_ratio = [], 0.0
            for text in options:
                title = sm.song_key(text)[0]
                if not title:
                    continue
                for known, hit in titles.items():
                    ratio = difflib.SequenceMatcher(None, title, known).ratio()
                    if ratio > best_ratio:
                        best, best_ratio = hit, ratio
            if best_ratio >= 0.88 and len({_name(r["song"]) for r in best}) == 1:
                found, how = best, "fuzzy"
        row = None
        if found:  # the same name twice (e.g. a released and a session version): the closest length wins
            row = min(found, key=lambda r: abs((seconds(r["length"]) or 10 ** 6) - (local or 0)))
        sheet = seconds(row["length"]) if row else None
        same = bool(row) and (local is None or sheet is None or abs(local - sheet) <= LENGTH_TOLERANCE)
        result.append({"song": song, "row": row, "how": how if row else "", "sure": how in ("name", "cleaned"),
                       "local": local, "sheet": sheet, "same_length": same})
    return result


def download(url):
    """The text of one LRC from its link; ValueError when it is not a synced LRC."""
    data = _get(url, limit=2_000_000)
    text = data.decode("utf-8-sig", "replace")
    if "<html" in text[:500].casefold():
        raise ValueError("the link did not return a file (is the Drive folder shared with “Anyone with the link”?)")
    if not TIMESTAMP.search(text):
        raise ValueError("the file has no timestamps")
    return text.replace("\r\n", "\n").rstrip() + "\n"


def clock(value):
    return f"{value // 60}:{value % 60:02d}" if isinstance(value, int) else "?"


def local_length(song):
    length = studio_resync.song_length(song)
    return int(round(length)) if length else None
