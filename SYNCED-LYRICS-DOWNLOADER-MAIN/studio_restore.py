"""List the app's song backups and put their files back where they came from.

Newer backups contain ``restore.json`` ({original path: backup copy}). Older
ones only keep file names; their place in the library is found by name:
an LRC goes to the song's SYNCED LYRICS folder, a TXT to LYRICS, an MP3 next
to the song. Before anything is overwritten, the current files are saved as a
new backup (``before_restore_*``), so every restore can itself be undone.
"""
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

KINDS = (  # (folder or prefix, label); a trailing "/" means "every entry inside this folder"
    ("deleted_lyrics_", "Deleted lyrics"),
    ("fragment_", "Before Fix fragment / lyrics edit"),
    ("edit_embedded_", "Before lyrics edit (MP3)"),
    ("new_lyrics_", "Before new lyrics were downloaded"),
    ("resync_", "Before resync / marked for sync"),
    ("before_restore_", "Before a restore"),
    ("LYRICS-EDITS/", "Before lyrics text edit (TXT)"),
    ("LRC-EMBED/", "Before embedding LRC into MP3"),
    ("LRC-EDITS-AND-EMBED/", "Before manual LRC edit"),
    ("MARKED-FOR-TRANSCRIPTION/", "Before marked for transcription"),
    ("LYRICS-NAME-REPAIR/", "Before lyrics name repair"),
)
SONG_FILES = (".lrc", ".txt", ".mp3")
_EDIT_PREFIX = re.compile(r"^\d{8}_\d{6}_[0-9A-Fa-f]{8}_")
_MARK_PREFIX = re.compile(r"^\d{2}_")


def library_index(folder):
    """{"names": {file name: [paths]}, "songs": {audio stem: folder}} for the music library."""
    names, songs = {}, {}
    if folder and os.path.isdir(folder):
        for directory, _dirs, files in os.walk(folder):
            for name in files:
                lower = name.casefold()
                if lower.endswith(SONG_FILES):
                    names.setdefault(lower, []).append(Path(directory) / name)
                if lower.endswith(".mp3"):
                    songs.setdefault(Path(name).stem.casefold(), Path(directory))
    return {"names": names, "songs": songs}


def _clean_name(backup_file, kind_folder, index):
    """The original file name hidden in a backup file name (some kinds add prefixes)."""
    name = backup_file.name
    if kind_folder == "LYRICS-EDITS/":
        return _EDIT_PREFIX.sub("", name), None
    if kind_folder == "MARKED-FOR-TRANSCRIPTION/":
        rest = _MARK_PREFIX.sub("", name)
        for folder in ("SYNCED LYRICS", "LYRICS"):
            if rest.casefold().startswith(folder.casefold() + "_"):
                return rest[len(folder) + 1:], folder
        for position, char in enumerate(rest):  # "<song folder>_<file name>": find the part that is a song
            if char == "_" and Path(rest[position + 1:]).stem.casefold() in index["songs"]:
                return rest[position + 1:], ""
        return rest, None
    return name, None


def guess_target(name, index, folder_hint=None):
    """Where a file with this original name belongs in the library (None when unknown)."""
    existing = index["names"].get(name.casefold(), [])
    suffix = Path(name).suffix.lower()
    wanted = {".lrc": "SYNCED LYRICS", ".txt": "LYRICS"}.get(suffix, "")
    if folder_hint is not None:
        wanted = folder_hint
    for path in existing:  # prefer the usual folder for this file type
        if (path.parent.name.casefold() == wanted.casefold()) if wanted else True:
            return path
    if existing:
        return existing[0]
    song_folder = index["songs"].get(Path(name).stem.casefold())
    if song_folder is None:
        return None
    return song_folder / wanted / name if wanted else song_folder / name


def _read_restore_map(folder):
    try:
        data = json.loads((folder / "restore.json").read_text(encoding="utf-8"))
        return {Path(original): Path(copy) for original, copy in data.items()}
    except (OSError, ValueError):
        return None


def list_backups(roots, index):
    """Every restorable song backup, newest first.

    Each item: {"path", "kind", "time", "files": [(backup_file, target or None)]}.
    """
    items = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for kind_key, label in KINDS:
            if kind_key.endswith("/"):
                base = root / kind_key[:-1]
                entries = list(base.iterdir()) if base.is_dir() else []
            else:
                entries = [p for p in root.iterdir() if p.name.startswith(kind_key)]
            for entry in entries:
                try:
                    stamp = entry.stat().st_mtime
                except OSError:
                    continue
                pairs = []
                mapping = _read_restore_map(entry) if entry.is_dir() else None
                if mapping:
                    pairs = [(copy, original) for original, copy in mapping.items() if copy.is_file()]
                else:
                    files = [entry] if entry.is_file() else [p for p in entry.rglob("*") if p.is_file()]
                    for backup_file in files:
                        if backup_file.suffix.lower() not in SONG_FILES:
                            continue
                        name, hint = _clean_name(backup_file, kind_key, index)
                        pairs.append((backup_file, guess_target(name, index, hint)))
                if pairs:
                    items.append({"path": entry, "kind": label, "time": stamp, "files": pairs})
    items.sort(key=lambda item: item["time"], reverse=True)
    return items


def restore(items, backup_root):
    """Copy the backup files of ``items`` back; save what they replace first.

    Returns (restored_targets, skipped_messages, safety_backup_folder or None).
    """
    pairs = [(copy, target) for item in items for copy, target in item["files"]]
    skipped = [f"{copy.name}: its place in the library is unknown" for copy, target in pairs if target is None]
    pairs = [(copy, target) for copy, target in pairs if target is not None]
    if not pairs:
        return [], skipped, None
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    safety = Path(tempfile.mkdtemp(prefix="before_restore_", dir=backup_root))
    saved = {}
    for number, (_copy, target) in enumerate(pairs):
        if target.is_file() and str(target) not in saved:
            keep = safety / str(number) / target.name
            keep.parent.mkdir(parents=True)
            shutil.copy2(target, keep)
            saved[str(target)] = str(keep)
    (safety / "restore.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    restored = []
    for copy, target in pairs:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copy, target)
            restored.append(target)
        except OSError as exc:
            skipped.append(f"{target.name}: {exc}")
    if not saved:
        shutil.rmtree(safety, ignore_errors=True)
        safety = None
    return restored, skipped, safety


def describe(item):
    """One list row: date, kind and the song names."""
    names = sorted({Path(str(target or copy)).stem for copy, target in item["files"]})
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["time"]))
    return f"{when}   {item['kind']:<36}   {', '.join(names)[:90]}"
