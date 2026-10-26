"""Warnings shown next to songs in the Tracks list.

* ``WarningIndex`` — sync errors of a song's LRC (from the Edit and Fix scan cache, minus the ignored
  warnings). A stale cache entry (the LRC changed since the last scan) is checked again on the spot.
* ``LyricsMatchStore`` — how much of the lyrics TXT Whisper really heard while syncing; a very low share
  usually means the TXT is the wrong text for the song.
"""
import json
import os
import time
from pathlib import Path

LYRICS_MATCH_WARN_RATIO = 0.30  # fewer directly heard lines than this share → "lyrics might be wrong"
LYRICS_MATCH_MIN_LINES = 8      # too few sung lines say nothing

ERROR_KINDS = ("dense_lines", "even_spacing", "ends_early")
KIND_PHRASES = {
    "same_time": "{n} line(s) at the same time",
    "dense_lines": "{n} line(s) squeezed together too tightly to be sung",
    "even_spacing": "{n} line(s) with guessed timing (spread evenly)",
    "ends_early": "the lyrics end long before the song does (the song goes on without any lyric line)",
}


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def run_level(run):
    """'error' or 'minor' for one scan warning (SAME TIME is always an error)."""
    if run.get("kind") not in ERROR_KINDS:
        return "error"
    return "error" if (run.get("severity") or "minor") == "error" else "minor"


class WarningIndex:
    """Sync warnings per song. Call ``begin()`` before a batch of ``for_audio()`` lookups."""

    def __init__(self, scan_cache_path, ignored_path, detect, resync, detect_file=None):
        self.scan_cache_path, self.ignored_path = scan_cache_path, ignored_path
        self.detect, self.resync, self.detect_file = detect, resync, detect_file
        self._cache, self._ignored = {}, {}
        self._fresh = {}  # key -> (signature, runs) for LRCs changed after the last scan

    def begin(self):
        self._cache = _read_json(self.scan_cache_path)
        self._ignored = _read_json(self.ignored_path)

    def forget(self, lrc_path=None):
        """Drop what was computed for one LRC (or all) — e.g. right after it was saved."""
        if lrc_path is None:
            self._fresh.clear()
        else:
            self._fresh.pop(str(Path(lrc_path).resolve()), None)

    def lrc_for(self, audio):
        for candidate in self.resync.lrc_locations(audio):
            if candidate.is_file():
                return candidate
        return None

    def runs_for_lrc(self, lrc):
        key = str(Path(lrc).resolve())
        try:
            stat = Path(lrc).stat()
        except OSError:
            return key, []
        signature = [stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns]
        entry = self._cache.get(key) or {}
        if entry.get("signature") == signature and entry.get("version") == self.resync.SCAN_VERSION:
            return key, entry.get("runs", [])
        fresh = self._fresh.get(key)
        if fresh and fresh[0] == signature:
            return key, fresh[1]
        try:
            text = Path(lrc).read_text(encoding="utf-8-sig")
            runs = self.detect_file(Path(lrc), text) if self.detect_file else self.detect(text)
        except (OSError, UnicodeError):
            runs = []
        self._fresh[key] = (signature, runs)
        return key, runs

    def for_audio(self, audio):
        """None when the song has no (visible) warning, else a dict describing them."""
        lrc = self.lrc_for(audio)
        if lrc is None:
            return None
        key, runs = self.runs_for_lrc(lrc)
        runs = [dict(run, path=key) for run in runs]
        runs = [run for run in runs if run.get("kind") != "ends_early"  # own tab in Edit and Fix, no ⚠ in Tracks
                and self.resync.ignore_key(run) not in self._ignored]
        if not runs:
            return None
        errors = [run for run in runs if run_level(run) == "error"]
        first = min(errors or runs, key=lambda run: run["first_file_line"])
        kinds = {}
        for run in runs:
            kind = run.get("kind", "same_time")
            kinds[kind] = kinds.get(kind, 0) + int(run.get("count", 0))
        return {"level": "error" if errors else "minor", "passages": len(runs),
                "lines": sum(int(run.get("count", 0)) for run in runs), "kinds": kinds,
                "lrc": str(lrc), "first_line": first["first_file_line"], "last_line": first["last_file_line"]}


def sync_tooltip(info):
    """The text shown when the mouse is over the ⚠ / ❕ of a song."""
    parts = [KIND_PHRASES[kind].format(n=count) for kind, count in info["kinds"].items() if kind in KIND_PHRASES]
    head = ("⚠ This song has possible sync errors" if info["level"] == "error"
            else "❕ This song has small possible timing issues")
    return (f"{head}: {info['passages']} passage(s), {info['lines']} line(s) — " + "; ".join(parts) + ".\n"
            "Right-click the song → Fix this song, or open Repair library → Edit and Fix Synced Lyrics.")


class LyricsMatchStore:
    """How well the TXT matched the audio the last time the song was synced."""

    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def _key(audio):
        return os.path.normcase(str(Path(audio).resolve()))

    @staticmethod
    def _txt_stamp(txt):
        try:
            return Path(txt).stat().st_mtime_ns if txt else 0
        except OSError:
            return 0

    def record(self, audio, direct, sung, txt=""):
        ratio = direct / sung if sung else 1.0
        data = _read_json(self.path)
        data[self._key(audio)] = {"direct": int(direct), "sung": int(sung), "ratio": round(ratio, 4),
                                  "txt_stamp": self._txt_stamp(txt), "time": time.time()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)
        return ratio

    def forget(self, audio):
        data = _read_json(self.path)
        if data.pop(self._key(audio), None) is not None:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temporary, self.path)

    @staticmethod
    def is_suspect(entry):
        return bool(entry) and entry.get("sung", 0) >= LYRICS_MATCH_MIN_LINES \
            and entry.get("ratio", 1.0) < LYRICS_MATCH_WARN_RATIO

    def suspect_entry(self, audio, txt="", data=None):
        """The stored entry when it says the lyrics look wrong and the TXT was not changed since."""
        data = _read_json(self.path) if data is None else data
        entry = data.get(self._key(audio))
        if not self.is_suspect(entry):
            return None
        if txt and entry.get("txt_stamp") and self._txt_stamp(txt) != entry["txt_stamp"]:
            return None  # the lyrics were edited or replaced after that sync
        return entry

    def load(self):
        return _read_json(self.path)


def lyrics_tooltip(entry):
    return (f"❓ The lyrics might be wrong: at the last sync only {entry['direct']} of {entry['sung']} lines "
            f"({entry['ratio'] * 100:.0f}%) matched what Whisper heard in the audio.\n"
            "Check the TXT: right-click the song → View/Change Lyrics.")
