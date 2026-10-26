"""Find and delete old data backups (lyrics, LRC, MP3 copies) made by the app.

Only backups the app creates for songs are considered. Code backups in the
same folder (``*.py.bak``, ``BUILDER_*``, ``STUDIO_*`` …) are never touched.
"""
import os
import shutil
import time
from pathlib import Path

# Top-level backup folders created per operation (tempfile prefixes).
DATA_PREFIXES = ("deleted_lyrics_", "edit_embedded_", "fragment_", "new_lyrics_", "resync_",
                 "DELETED_REFERENCE_")
# Category folders whose children are one backup each.
DATA_CATEGORIES = ("LRC-EMBED", "LYRICS-EDITS", "SONG-RENAMES", "MARKED-FOR-TRANSCRIPTION",
                   "LRC-EDITS-AND-EMBED", "LYRICS-LIBRARY-IMPORT", "LYRICS-NAME-REPAIR")


def _size_and_mtime(path):
    if path.is_file():
        stat = path.stat()
        return stat.st_size, stat.st_mtime
    # Age comes from the files inside; a folder's own mtime changes whenever
    # an entry is added, so it is used only for an empty backup folder.
    size, newest = 0, None
    for directory, _dirs, files in os.walk(path):
        for name in files:
            try:
                stat = (Path(directory) / name).stat()
            except OSError:
                continue
            size += stat.st_size
            newest = stat.st_mtime if newest is None else max(newest, stat.st_mtime)
    return size, newest if newest is not None else path.stat().st_mtime


def data_backups(roots):
    """Every app-made data backup in ``roots`` as (path, size_bytes, newest_mtime)."""
    found = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.is_dir() and entry.name in DATA_CATEGORIES:
                children = list(entry.iterdir())
            elif entry.name.startswith(DATA_PREFIXES):
                children = [entry]
            else:
                continue
            for child in children:
                try:
                    size, mtime = _size_and_mtime(child)
                except OSError:
                    continue
                found.append((child, size, mtime))
    return found


FILE_TYPES = (("txt", "Lyrics text (.txt)"), ("lrc", "Synced lyrics (.lrc)"),
              ("mp3", "Audio (.mp3)"), ("other", "Other (restore lists, other files)"))


def file_type(path):
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix if suffix in ("txt", "lrc", "mp3") else "other"


def _backup_files(roots, days=0, now=None):
    """(file, size, type) for every file inside app-made backups older than ``days`` (0 = all)."""
    limit = (now if now is not None else time.time()) - days * 86400 if days else None
    for backup, _size, mtime in data_backups(roots):
        if limit is not None and mtime >= limit:
            continue
        files = [backup] if backup.is_file() else [p for p in backup.rglob("*") if p.is_file()]
        for path in files:
            try:
                yield path, path.stat().st_size, file_type(path), backup
            except OSError:
                continue


def usage_by_type(roots, days=0, now=None):
    """{type: [file_count, bytes]} for app-made backups (older than ``days`` if given)."""
    usage = {key: [0, 0] for key, _label in FILE_TYPES}
    for _path, size, kind, _backup in _backup_files(roots, days, now):
        usage[kind][0] += 1
        usage[kind][1] += size
    return usage


def delete_types(roots, kinds, days=0, now=None):
    """Delete backed-up files of the chosen types; drop backups with no song files left.

    Returns (deleted_files, freed_bytes, errors).
    """
    kinds = set(kinds)
    deleted, freed, errors, touched = 0, 0, [], set()
    for path, size, kind, backup in list(_backup_files(roots, days, now)):
        if kind not in kinds:
            continue
        try:
            path.unlink()
            deleted += 1
            freed += size
            touched.add(backup)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    for backup in touched:
        if not backup.is_dir():
            continue
        left = [p for p in backup.rglob("*") if p.is_file()]
        if all(p.name == "restore.json" for p in left):  # nothing to restore any more
            for leftover in left:
                try:
                    freed += leftover.stat().st_size
                    leftover.unlink()
                except OSError:
                    pass
            shutil.rmtree(backup, ignore_errors=True)
    return deleted, freed, errors


def format_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
