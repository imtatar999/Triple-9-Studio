"""Library helpers for the Metadata tab and Repair library.

* Song duplicates: songs whose names say they are the same recording.
  Album marks ((JW3), (WOD), (GB&GR) …), featured artists ((feat. X), (with Y)) and alternative names
  ((Forever)) do not make a song different; version marks do — (Sessions), (Session Edit), (V2), (OG),
  (Stem), (Remaster) … — so "Song" and "Song (Sessions)" are NOT duplicates.
* Year from comment: "(Recorded July 15, 2019)" in the MP3 comment → Year 2019. "Leaked" dates are never
  used, and "(Recorded ?)" or no year leaves the song as it is.
"""
import os
import re
from collections import defaultdict
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".wma"}

# Marks that make a different recording or cut of a song (compared after lower-casing).
VERSION_MARK = re.compile(
    r"^(?:sessions?|session edit|sessions edit|stems?|v\s?\d+|version \d+|og|alt|alternate|ai|solo|"
    r"remaster(?:ed)?|master|remix(?:ed)?|.* remix|snippet|extended|live|demo|instrumental|acapella|a cappella|"
    r"slowed|sped up|reverb|edit|cdq|hq|lq|full|full freestyle|freestyle|official video|music video|"
    r"radio edit|clean|explicit|interlude|outro|intro|leak|og file|unfinished|finished|mixed|unmixed|"
    r"mastered|unmastered|\d+ ?bpm)$")
FEATURE = re.compile(r"^(?:feat\.?|ft\.?|featuring|with|prod\.?|prod by|produced by)\b")


def _clean(text):
    text = str(text).replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^\w']+", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


ALBUM_MARK = re.compile(r"^[A-Z0-9&]{2,6}$")  # (JW3) (WOD) (GB&GR) (DRFL) (TPNE) — written in capitals


def song_key(name):
    """(title, version marks, alternative names) of a song file name.

    Album marks and featured artists are left out; version marks ((Sessions), (V2) …) and
    alternative names ((Hell Girl), (Forever) …) are kept separately."""
    stem = Path(name).stem if Path(name).suffix.lower() in AUDIO_EXTENSIONS else str(name)
    marks, aliases = [], []
    for group in re.findall(r"[(\[]([^()\[\]]*)[)\]]", stem):
        raw = group.strip()
        mark = re.sub(r"\s+", " ", raw.casefold())
        if not mark or FEATURE.match(mark):
            continue
        if VERSION_MARK.match(mark):
            marks.append(_clean(mark))
        elif not ALBUM_MARK.match(raw):
            aliases.append(_clean(mark))
    title = stem
    while re.search(r"[(\[][^()\[\]]*[)\]]", title):  # nested brackets too
        title = re.sub(r"[(\[][^()\[\]]*[)\]]", " ", title)
    title = re.sub(r"[()\[\]]", " ", title)
    title = re.split(r"\s(?:feat\.?|ft\.?|featuring)\s", title, maxsplit=1, flags=re.I)[0]
    return _clean(title), tuple(sorted(set(marks))), tuple(sorted(set(aliases)))


def audio_files(folders):
    seen = set()
    for folder in folders:
        for directory, _dirs, names in os.walk(folder):
            if "backup" in directory.casefold():
                continue
            for name in names:
                path = Path(directory) / name
                key = os.path.normcase(str(path))
                if path.suffix.lower() in AUDIO_EXTENSIONS and key not in seen:
                    seen.add(key)
                    yield path


def find_duplicates(folders):
    """[("same" | "possible", [paths])]: "same" = identical names once album marks and featured artists
    are left out; "possible" = the same song name, but an alternative name in brackets differs."""
    return group_duplicates(audio_files(folders))


def group_duplicates(files):
    """Like find_duplicates, for a given list of song files."""
    by_title = defaultdict(list)
    for path in files:
        path = Path(path)
        title, marks, aliases = song_key(path.name)
        if title:
            by_title[(title, marks)].append((aliases, path))
    found = []
    for entries in by_title.values():
        if len(entries) < 2:
            continue
        exact = defaultdict(list)
        for aliases, path in entries:
            exact[aliases].append(path)
        same = [paths for paths in exact.values() if len(paths) > 1]
        for paths in same:
            found.append(("same", sorted(paths, key=lambda p: str(p).casefold())))
        if len(exact) > 1:  # different alternative names: maybe the same song, maybe not
            found.append(("possible", sorted((p for _a, p in entries), key=lambda p: str(p).casefold())))
    order = {"same": 0, "possible": 1}
    return sorted(found, key=lambda item: (order[item[0]], item[1][0].name.casefold()))


# ---------------------------------------------------------------- year from comment
_RECORDED = re.compile(r"\brecorded\b(?P<rest>[^()\[\]]*)", re.I)
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
           "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH = re.compile(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\b\.?", re.I)
_DAY = re.compile(r"(?<![\d-])([0-3]?\d)(?:st|nd|rd|th)?(?![\d-]|\s*-\s*\d)")
_ISO = re.compile(r"\b(19\d{2}|20\d{2})-(\d{1,2})-(\d{1,2})\b")


def _recorded_text(comment):
    """The text after the first usable "Recorded" (cut at "Leaked"), or None."""
    for match in _RECORDED.finditer(str(comment or "")):
        rest = re.split(r"\bleak", match.group("rest"), maxsplit=1, flags=re.I)[0]
        if "?" in rest.split(",")[0] and not _YEAR.search(rest):
            continue
        if _YEAR.search(rest):
            return rest
    return None


def recorded_date(comment):
    """"2019-09-30" for an exact recorded date, "2019-09" when the day is unsure, "2019" when only the year
    (or no month) is known; None when the comment gives no recorded year ("Leaked" dates are ignored)."""
    rest = _recorded_text(comment)
    if rest is None:
        return None
    iso = _ISO.search(rest)
    if iso and 1 <= int(iso.group(2)) <= 12 and 1 <= int(iso.group(3)) <= 31:
        return f"{iso.group(1)}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
    year = _YEAR.search(rest)
    month = _MONTH.search(rest)
    if not month:
        return year.group(1)
    stem = f"{year.group(1)}-{_MONTHS[month.group(1).lower()]:02d}"
    if "?" in rest or re.search(r"\d\s*-\s*\d", rest.replace(year.group(1), "")):
        return stem  # "2018?" or "19-20": the day is not exact
    around = rest.replace(year.group(1), " ")
    day = _DAY.search(around)
    if day and 1 <= int(day.group(1)) <= 31:
        return f"{stem}-{int(day.group(1)):02d}"
    return stem


def recorded_year(comment):
    """The year after "Recorded" in a comment, or None ("Leaked" dates and "Recorded ?" are ignored)."""
    date = recorded_date(comment)
    return int(date[:4]) if date else None


def read_comments(path):
    """Every comment text of an MP3 (all COMM frames)."""
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        return []
    texts = []
    for frame in tags.getall("COMM"):
        texts.extend(str(t) for t in frame.text)
    return texts


def read_year(path):
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        return ""
    for frame_id in ("TDRC", "TYER", "TDOR"):
        frame = tags.get(frame_id)
        if frame is not None and frame.text:
            return str(frame.text[0])
    return ""


def plan_years(paths):
    """[(path, comment, old Year, new Year/date)] for songs whose comment gives a recorded year that differs."""
    plan = []
    for path in paths:
        if Path(path).suffix.lower() != ".mp3":
            continue
        try:
            comments = read_comments(path)
        except Exception:
            continue
        for comment in comments:
            date = recorded_date(comment)
            if date is None:
                continue
            old = read_year(path)
            # a tag that already holds a more exact date of the same year is left alone
            if old != date and not (old.startswith(date) and len(old) > len(date)):
                plan.append((Path(path), comment, old, date))
            break
    return plan


def write_year(path, year):
    """Set the MP3's Year (ID3 TDRC; saved as TYER for ID3v2.3 files, like Mp3tag does)."""
    from mutagen.id3 import ID3, TDRC
    tags = ID3(str(path))
    version = 3 if tags.version[:2] == (2, 3) else 4
    tags.delall("TYER")
    tags.setall("TDRC", [TDRC(encoding=3, text=[str(year)])])
    tags.save(str(path), v2_version=version)


def restore_year(path, old_year):
    """Put back the Year a song had before (empty = no Year at all)."""
    from mutagen.id3 import ID3
    if old_year:
        write_year(path, old_year)
        return
    tags = ID3(str(path))
    version = 3 if tags.version[:2] == (2, 3) else 4
    for frame_id in ("TDRC", "TYER"):
        tags.delall(frame_id)
    tags.save(str(path), v2_version=version)


def song_length(path):
    try:
        from mutagen import File
        info = File(str(path))
        return float(info.info.length) if info is not None else None
    except Exception:
        return None
