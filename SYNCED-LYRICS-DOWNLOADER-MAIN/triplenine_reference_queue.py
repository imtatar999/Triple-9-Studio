"""Disk-only bridge between Downloader and Builder; Python 3.10 compatible."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import shutil

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "MARKED FOR TRANSCRIPTION" / ".REFERENCES"
SESSION = re.compile(r"\b(?:sessions?(?:\s+edit)?|extended)\b", re.I)
FOUND_SUFFIX = " - reference found"
MISSING_SUFFIX = " - reference not found"
READY_FOR_SYNC_SUFFIX = " - Ready for Lyric Sync"
LEGACY_DONE_SUFFIX = " - done"


def base_title(title):
    def bracket(match):
        return "" if SESSION.search(match.group()) else match.group()
    title = re.sub(r"\([^)]*\)|\[[^]]*\]", bracket, title)
    return " ".join(SESSION.sub("", title).split()).strip(" -_")


def entry_path(audio):
    key = os.path.normcase(str(Path(audio).resolve()))
    return QUEUE / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")


def marker_paths(audio, destination=None):
    folder = Path(destination) if destination is not None else QUEUE.parent
    stem = Path(audio).stem
    return (folder / (stem + FOUND_SUFFIX + ".txt"),
            folder / (stem + MISSING_SUFFIX + ".txt"),
            folder / (stem + ".txt"))


def ready_for_sync_marker_path(audio, destination=None):
    folder = Path(destination) if destination is not None else QUEUE.parent
    return folder / (Path(audio).stem + READY_FOR_SYNC_SUFFIX + ".txt")


def existing_marker(audio, destination=None):
    found, missing, legacy = marker_paths(audio, destination)
    done = ready_for_sync_marker_path(audio, destination)
    old_done = done.with_name(Path(audio).stem + LEGACY_DONE_SUFFIX + ".txt")
    return next((path for path in (done, old_done, found, missing, legacy) if path.is_file()), None)


def ensure_marker(audio, destination=None):
    """Return a marker, migrating old exact-name markers to a status name."""
    found, missing, legacy = marker_paths(audio, destination)
    done = ready_for_sync_marker_path(audio, destination)
    found.parent.mkdir(parents=True, exist_ok=True)
    if done.is_file():
        return done
    if found.is_file():
        return found
    if missing.is_file():
        return missing
    if legacy.is_file():
        target = found if reference_text(legacy) else missing
        os.replace(legacy, target)
        return target
    with missing.open("x", encoding="utf-8"):
        pass
    return missing


def read_entry(audio):
    try:
        return json.loads(entry_path(audio).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"audio": str(Path(audio).resolve())}


def write_entry(entry):
    QUEUE.mkdir(parents=True, exist_ok=True)
    destination = entry_path(entry["audio"])
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=QUEUE,
                                         delete=False, suffix=".tmp") as f:
            name = f.name
            json.dump(entry, f, ensure_ascii=False, indent=2)
        os.replace(name, destination)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def read_text(path):
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def reference_text(path):
    text = read_text(path)
    return "\n".join(line for line in text.splitlines() if not re.match(
        r"^\s*(?:https?://(?:www\.)?genius\.com/|Match accuracy:|Source (?:confirmed|selected))",
        line)).strip()


def assign_reference(entry, source_path, source_label):
    """Put the editable reference in the visible marker and update its JSON link."""
    text = read_text(source_path).strip()
    if not reference_text(source_path):
        raise ValueError("The reference TXT is empty.")
    found, missing, legacy = marker_paths(entry["audio"])
    found.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="\n",
                                         dir=found.parent, delete=False, suffix=".tmp") as stream:
            temporary = stream.name
            stream.write(text.rstrip() + "\n")
        os.replace(temporary, found)
        temporary = None
        for obsolete in (missing, legacy):
            if obsolete.is_file():
                obsolete.unlink()
        entry.update(marker=str(found), reference=str(found), source=source_label)
        write_entry(entry)
        return found
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def delete_reference(audio, destination=None):
    """Clear only queue-owned files, retaining a recoverable backup and marker."""
    marker = existing_marker(audio, destination)
    if marker is None or marker == ready_for_sync_marker_path(audio, destination):
        return None
    entry = read_entry(audio)
    found, missing, legacy = marker_paths(audio, destination)
    cached = entry_path(audio).with_suffix(".txt")
    if not any(p.is_file() and p.stat().st_size for p in (found, legacy, cached)) and not entry.get("reference"):
        return None
    backup_root = ROOT / "BACKUPS"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="DELETED_REFERENCE_", dir=backup_root))
    owned = (found, missing, legacy, cached, entry_path(audio))
    snapshots = {}
    for path in owned:
        if path.is_file():
            shutil.copy2(path, backup / path.name)
            snapshots[path] = backup / path.name
    try:
        missing.write_text("", encoding="utf-8")
        for key in ("reference", "source"):
            entry.pop(key, None)
        entry.update(marker=str(missing), status="missing")
        write_entry(entry)
        for path in (found, legacy, cached):
            if path.is_file():
                path.unlink()
    except Exception:
        for path in owned:
            if path in snapshots:
                shutil.copy2(snapshots[path], path)
            elif path.is_file():
                path.unlink()
        raise
    return backup


def is_ready_for_sync(entry):
    marker = existing_marker(entry["audio"])
    return bool(marker and marker.stem.casefold().endswith((READY_FOR_SYNC_SUFFIX.casefold(), LEGACY_DONE_SUFFIX)))


def mark_ready_for_sync(entry):
    """Rename only the visible reference marker after a successful TXT save."""
    marker = existing_marker(entry["audio"])
    done = ready_for_sync_marker_path(entry["audio"])
    if done.is_file():
        entry.update(marker=str(done), reference=str(done), status="ready_for_lyric_sync")
        write_entry(entry)
        return done
    if marker is None:
        raise ValueError("The marked track no longer has a marker.")
    os.replace(marker, done)
    entry.update(marker=str(done), reference=str(done), status="ready_for_lyric_sync")
    write_entry(entry)
    return done


def completed_lyrics_path(audio):
    audio = Path(audio)
    return audio.parent / "LYRICS" / (audio.stem + ".txt")


def reconcile_completed():
    """Mark reviewed text Ready for Lyric Sync from an exact, non-empty Lyrics TXT."""
    changed = []
    for entry in entries():
        marker = existing_marker(entry["audio"])
        if marker is None or is_ready_for_sync(entry):
            continue
        lyrics = completed_lyrics_path(entry["audio"])
        try:
            if not lyrics.is_file() or not read_text(lyrics).strip():
                continue
            changed.append(str(mark_ready_for_sync(entry)))
        except (OSError, UnicodeError, ValueError):
            continue
    return changed


def ready(entry):
    try:
        marker = existing_marker(entry["audio"])
        reference = entry.get("reference")
        if marker and marker.stem.endswith(FOUND_SUFFIX) and reference_text(marker):
            return True
        return bool(reference and reference_text(reference))
    except (OSError, UnicodeError):
        return False


def entries():
    result = []
    for path in sorted(QUEUE.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            marker = existing_marker(entry["audio"])
            if Path(entry["audio"]).is_file() and marker:
                entry["marker"] = str(marker)
                if marker.stem.endswith((FOUND_SUFFIX, READY_FOR_SYNC_SUFFIX, LEGACY_DONE_SUFFIX)):
                    entry["reference"] = str(marker)
                if marker.stem.endswith((READY_FOR_SYNC_SUFFIX, LEGACY_DONE_SUFFIX)):
                    entry["status"] = "ready_for_lyric_sync"
                result.append(entry)
        except (OSError, ValueError, KeyError):
            continue
    # Old empty TXT markers predate the bridge. Show them too, without requiring
    # a search or writing anything to the music library.
    known = {os.path.normcase(e["audio"]) for e in result}
    try:
        config_path = ROOT / "SYNCED-LYRICS-DOWNLOADER-MAIN" / "lyrics_gui_config.json"
        library = json.loads(config_path.read_text(encoding="utf-8-sig")).get("music_dir", "")
        marker_dir = QUEUE.parent
        if not marker_dir.is_dir():
            marker_dir = ROOT / "SYNCED-LYRICS-DOWNLOADER-MAIN" / "MARKED FOR TRANSCRIPTION"
        markers = {}
        for marker in marker_dir.glob("*.txt"):
            stem = marker.stem
            for suffix in (READY_FOR_SYNC_SUFFIX, LEGACY_DONE_SUFFIX, FOUND_SUFFIX, MISSING_SUFFIX):
                if stem.endswith(suffix):
                    stem = stem[:-len(suffix)]
                    break
            markers[stem] = marker
        if markers and library and Path(library).is_dir():
            for directory, _, files in os.walk(library):
                for name in files:
                    audio = Path(directory) / name
                    if audio.suffix.lower() not in (".mp3", ".flac") or audio.stem not in markers:
                        continue
                    if os.path.normcase(str(audio.resolve())) not in known:
                        entry = {"audio": str(audio.resolve()), "marker": str(markers[audio.stem]),
                                 "base_title": base_title(audio.stem)}
                        if markers[audio.stem].stem.endswith((FOUND_SUFFIX, READY_FOR_SYNC_SUFFIX, LEGACY_DONE_SUFFIX)):
                            entry["reference"] = str(markers[audio.stem])
                            entry["source"] = "Reference marker TXT"
                        if markers[audio.stem].stem.endswith((READY_FOR_SYNC_SUFFIX, LEGACY_DONE_SUFFIX)):
                            entry["status"] = "ready_for_lyric_sync"
                        result.append(entry)
    except (OSError, ValueError):
        pass
    return sorted(result, key=lambda e: Path(e["audio"]).name.casefold())
