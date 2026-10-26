"""Persistent LRC inspection cache and explicit resynchronization queue."""
import json
import re
import os
import shutil
import tempfile
import time
from pathlib import Path

SCAN_VERSION = 7  # bump whenever the failure rules change; cached results are redone


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def scan(folder, cache_path, detect, detect_file=None):
    cache = read(cache_path)
    fresh = {}
    result = dict(folder=str(folder), files_scanned=0, failures=[], unreadable=[], reused=0)
    for directory, _, names in os.walk(folder, onerror=lambda e: result['unreadable'].append((e.filename, str(e)))):
        for name in names:
            if not name.lower().endswith('.lrc'):
                continue
            path = Path(directory) / name
            key = str(path.resolve())
            result['files_scanned'] += 1
            try:
                stat = path.stat()
                signature = [stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns]
                old = cache.get(key, {})
                if old.get('signature') == signature and old.get('version') == SCAN_VERSION:
                    runs = old['runs']
                    result['reused'] += 1
                else:
                    text = path.read_text(encoding='utf-8-sig')
                    runs = detect_file(path, text) if detect_file else detect(text)
                fresh[key] = dict(signature=signature, runs=runs, version=SCAN_VERSION)
                result['failures'].extend(dict(run, path=key) for run in runs)
            except (OSError, UnicodeError) as error:
                result['unreadable'].append((key, str(error)))
    write(cache_path, fresh)
    return result


def ignore_key(item):
    """Identify one scan warning. Timestamps are part of the key, so a warning
    ignored in one sync comes back if the song is resynced and fails again."""
    return '|'.join([os.path.normcase(str(Path(item['path']).resolve())), item.get('kind', 'same_time'),
                     str(item['timestamp']), str(item['count']), item.get('first_text', '')])


def split_ignored(failures, ignored_path):
    """Return (visible_failures, ignored_count) using the saved ignore list."""
    ignored = read(ignored_path)
    visible = [item for item in failures if ignore_key(item) not in ignored]
    return visible, len(failures) - len(visible)


def ignore(items, ignored_path):
    ignored = read(ignored_path)
    for item in items:
        ignored[ignore_key(item)] = dict(path=item['path'], timestamp=item['timestamp'],
                                         count=item['count'], first_text=item.get('first_text', ''))
    write(ignored_path, ignored)


HISTORY_LIMIT = 5000


def record_history(history_path, action, path, detail="", backup_root=None):
    """Append one Edit-and-Fix event (newest last in the file)."""
    data = read(history_path)
    items = data.get("items")
    if items is None:  # first event ever: start from the past reconstructed from backups
        # Skip backups from the last minute: the event being recorded made one of them.
        cutoff = time.time() - 60
        items = [item for item in (history_from_backups(backup_root) if backup_root else [])
                 if item["time"] < cutoff]
    items.append(dict(time=time.time(), action=action, path=str(path), detail=detail))
    data["items"] = items[-HISTORY_LIMIT:]
    write(history_path, data)


def history(history_path, backup_root=None):
    """All recorded events, newest first. The first call rebuilds the past from backups."""
    data = read(history_path)
    if "items" not in data:
        data["items"] = history_from_backups(backup_root) if backup_root else []
        try:
            write(history_path, data)
        except OSError:
            pass
    return sorted(data["items"], key=lambda item: item.get("time", 0), reverse=True)


def history_from_backups(backup_root):
    """Reconstruct past fixes from backup folders made before history existed."""
    items = []
    root = Path(backup_root)
    if not root.is_dir():
        return items
    kinds = {"fragment_": "Fix fragment / text edit (from backup)",
             "resync_": "Resync or marked for Lyrics Sync (from backup)",
             "new_lyrics_": "New lyrics — old sync removed (from backup)"}
    for folder in root.iterdir():
        action = next((label for prefix, label in kinds.items() if folder.name.startswith(prefix)), None)
        if action is None or not folder.is_dir():
            continue
        try:
            stamp = folder.stat().st_mtime
            restore = read(folder / "restore.json")
            originals = list(restore) or [str(p.name) for p in folder.rglob("*.lrc")]
        except OSError:
            continue
        lrc = next((p for p in originals if str(p).lower().endswith(".lrc")), None)
        target = lrc or next((p for p in originals if str(p).lower().endswith(".mp3")), None)
        if target:
            items.append(dict(time=stamp, action=action, path=str(target), detail=""))
    return items


def remove_history(history_path, song_keys, backup_root=None):
    """Forget every history entry of the given songs (files and backups are not touched)."""
    keys = set(song_keys)
    items = history(history_path, backup_root)  # makes sure the reconstructed past exists first
    kept = [item for item in items if song_key(item["path"]) not in keys]
    data = read(history_path)
    data["items"] = sorted(kept, key=lambda item: item.get("time", 0))
    write(history_path, data)
    return len(items) - len(kept)


def song_key(path):
    """Group history entries of one song: the file stem without extension."""
    return Path(str(path)).stem.casefold()


def lrc_locations(audio):
    """Every place a song's LRC may live, in priority order (Lyrics Sync writes the first)."""
    audio = Path(audio)
    return [audio.parent / 'SYNCED LYRICS' / (audio.stem + '.lrc'),
            audio.parent / 'LYRICS' / (audio.stem + '.lrc'),
            audio.with_suffix('.lrc')]


def audio_for_lrc(path):
    path = Path(path)
    folder = path.parent.parent if path.parent.name.casefold() in {'lyrics', 'synced lyrics'} else path.parent
    candidates = [p for p in folder.iterdir() if p.is_file() and p.stem == path.stem and p.suffix.lower() == '.mp3']
    if len(candidates) != 1:
        raise ValueError('Cannot identify one matching MP3: ' + str(path))
    return candidates[0]


def song_for_lrc(path):
    """The one MP3 / FLAC that belongs to an LRC (same name in the song's folder), else None."""
    path = Path(path)
    folder = path.parent.parent if path.parent.name.casefold() in {'lyrics', 'synced lyrics'} else path.parent
    try:
        found = [p for p in folder.iterdir() if p.is_file() and p.stem == path.stem and p.suffix.lower() in ('.mp3', '.flac')]
    except OSError:
        return None
    return found[0] if len(found) == 1 else None


def song_length(audio):
    """Length of a song in seconds, or None when it cannot be read."""
    try:
        from mutagen import File
        info = File(str(audio)).info
        return float(info.length) or None
    except Exception:
        return None


def mark(lrc, queue_path, backup_root, find_txt, remove_tags):
    audio = audio_for_lrc(lrc)
    txt = find_txt(str(audio))
    if not txt or not Path(txt).is_file():
        raise ValueError('Missing plain TXT for ' + audio.name)
    sidecars = {Path(lrc), *lrc_locations(audio)}
    originals = [audio] + sorted((p for p in sidecars if p.is_file()), key=str)
    Path(backup_root).mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='resync_', dir=backup_root))
    copies = []
    for index, path in enumerate(originals):
        target = backup / str(index) / path.name
        target.parent.mkdir()
        shutil.copy2(path, target)
        copies.append((path, target))
    write(backup / 'restore.json', {str(p): str(b) for p, b in copies})
    try:
        remove_tags(str(audio))
        for path in sidecars:
            if path.is_file():
                path.unlink()
        queue = read(queue_path)
        queue[str(audio.resolve())] = dict(audio=str(audio), lyrics=str(txt), backup=str(backup))
        write(queue_path, queue)
    except Exception:
        for path, copy in copies:
            shutil.copy2(copy, path)
        raise


def clear_sync(audio, txt, queue_path, backup_root, remove_tags):
    """Back up, then delete LRC sidecars and embedded lyrics after new lyrics arrive.

    Returns (backup_folder, removed_paths, embedded_removed); backup_folder is
    None when there was nothing to remove. The song is queued for Lyrics Sync.
    """
    audio = Path(audio)
    sidecars = [p for p in lrc_locations(audio) if p.is_file()]
    is_mp3 = audio.suffix.lower() == '.mp3'
    originals = ([audio] if is_mp3 else []) + sidecars
    Path(backup_root).mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='new_lyrics_', dir=backup_root))
    copies = []
    for index, path in enumerate(originals):
        target = backup / str(index) / path.name
        target.parent.mkdir()
        shutil.copy2(path, target)
        copies.append((path, target))
    write(backup / 'restore.json', {str(p): str(b) for p, b in copies})
    try:
        embedded = bool(remove_tags(str(audio))) if is_mp3 else False
        for path in sidecars:
            path.unlink()
        queue = read(queue_path)
        queue[str(audio.resolve())] = dict(audio=str(audio), lyrics=str(txt), backup=str(backup))
        write(queue_path, queue)
    except Exception:
        for path, copy in copies:
            shutil.copy2(copy, path)
        raise
    if not sidecars and not embedded:
        shutil.rmtree(backup, ignore_errors=True)
        return None, [], False
    return backup, sidecars, embedded


_BRACKET_GROUP = re.compile(r"\[[^\[\]]*\]|\([^()]*\)")


def is_section_header(line):
    """True when the line is made only of bracket groups: "[Chorus: Quavo]", "(Cha-ching, cha-ching, brrt, hello?)",
    "(Woo) (Yeah)". Such lines stay out of the LRC. A line with sung text outside the brackets is a lyric:
    "(Cha-ching, cha-ching,) I'm gon' drop it in the mix", "(Yeah) Bad bitches … (Bad, woo)"."""
    rest = str(line).strip()
    if not rest:
        return False
    while True:
        shorter = _BRACKET_GROUP.sub(" ", rest)
        if shorter == rest:
            break
        rest = shorter
    return not re.search(r"\w", rest)


_SECTION_IN_PARENS = re.compile(
    r"(?i)^\(\s*(?:intro|verse|chorus|hook|bridge|outro|refrain|pre[- ]?chorus|post[- ]?chorus|pre[- ]?hook|"
    r"post[- ]?hook|interlude|break|instrumental|spoken|skit|part|segue|breakdown)\b[^()]*\)$")


def is_section_name(line):
    """Only a real section header: "[Chorus: Quavo]", "[Intro]" or "(Verse 2)". Used when Lyrics Sync may
    sync lines made only of brackets, such as "(Cha-ching, brrt, hello?)"."""
    line = str(line).strip()
    return bool(re.fullmatch(r"\[[^\[\]]*\]", line) or _SECTION_IN_PARENS.match(line))


def _line_key(text):
    return re.sub(r"[^\w]+", " ", str(text).casefold().replace("’", "'").replace("'", "")).strip()


def missing_bracket_lines(txt_text, lrc_text):
    """TXT lines that start and end with brackets but have sung text in between ("(Yeah) Get right witcha
    … (Rah, woo)") that the LRC does not have — older syncs dropped them as if they were section headers.
    Lines made only of brackets ("(Cha-ching, brrt, hello?)") are left out on purpose. Repeated lines count
    as often as they appear in the TXT."""
    from collections import Counter
    lrc_lines = Counter(_line_key(re.sub(r"^\s*(?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]\s*)+", "", line))
                        for line in lrc_text.splitlines())
    wanted = Counter()
    first = {}
    for raw in txt_text.splitlines():
        line = raw.strip()
        if re.fullmatch(r"[\[\(].*[\]\)]", line) and not is_section_header(line) and _line_key(line):
            key = _line_key(line)
            wanted[key] += 1
            first.setdefault(key, line)
    missing = []
    for key, count in wanted.items():
        missing.extend([first[key]] * max(0, count - lrc_lines.get(key, 0)))
    return missing


def complete(queue_path, audio):
    queue = read(queue_path)
    queue.pop(str(Path(audio).resolve()), None)
    write(queue_path, queue)


def save_fragment_repair(lrc, text, backup_root):
    """Back up one LRC and atomically save a locally repaired version."""
    lrc = Path(lrc)
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='fragment_', dir=backup_root))
    saved = backup / lrc.name
    shutil.copy2(lrc, saved)
    try:
        write(backup / 'restore.json', {str(lrc): str(saved)})
    except OSError:
        pass  # restore falls back to finding the file by name
    temporary = lrc.with_name(lrc.name + '.repair.tmp')
    try:
        payload = text.encode('utf-8')
        if lrc.read_bytes().startswith(b'\xef\xbb\xbf'):
            payload = b'\xef\xbb\xbf' + payload
        temporary.write_bytes(payload)
        os.replace(temporary, lrc)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        shutil.copy2(saved, lrc)
        raise
    return saved


# ----- Remove Line from song (Fix fragment) -----------------------------------
_TIMESTAMP = re.compile(r'^\s*(?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]\s*)+')


def lyric_key(text):
    """Compare lyric lines loosely: no timestamps, case, extra spaces or curly quotes."""
    text = _TIMESTAMP.sub('', text or '').replace('’', "'").replace('‘', "'")
    return ' '.join(text.split()).casefold()


def remove_lrc_lines(lrc_text, file_lines):
    """Drop the given 1-based file lines; every other line stays byte-identical."""
    drop = set(file_lines)
    lines = (lrc_text or '').splitlines(keepends=True)
    return ''.join(line for number, line in enumerate(lines, 1) if number not in drop)


def remove_txt_lines(txt_text, lrc_text, file_lines):
    """Remove from the plain lyrics the same lines that are removed from the LRC.

    A line that appears several times (a chorus) is matched by occurrence: the
    2nd "Get geeked" in the LRC removes the 2nd "Get geeked" in the TXT.
    Returns ``(new_text, missing)`` where ``missing`` lists LRC lines whose
    text was not found in the TXT (those are left alone).
    """
    drop = set(file_lines)
    lrc = (lrc_text or '').splitlines()
    wanted = {}  # lyric key -> occurrence numbers (0-based) to remove
    seen = {}
    texts = {}
    for number, raw in enumerate(lrc, 1):
        key = lyric_key(raw)
        if not key:
            continue
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        if number in drop:
            wanted.setdefault(key, set()).add(occurrence)
            texts[(key, occurrence)] = number
    lines = (txt_text or '').splitlines(keepends=True)
    kept, counts, found = [], {}, set()
    for line in lines:
        key = lyric_key(line)
        occurrence = counts.get(key, 0)
        if key:
            counts[key] = occurrence + 1
        if key and occurrence in wanted.get(key, ()):
            found.add((key, occurrence))
            continue
        kept.append(line)
    missing = sorted(number for pair, number in texts.items() if pair not in found)
    return ''.join(kept), missing


def _split_ending(line):
    body = line.rstrip('\r\n')
    return body, line[len(body):]


def edit_lrc_line(lrc_text, file_line, new_text):
    """Replace the lyric text of one LRC line; its timestamp(s) and line ending stay."""
    lines = (lrc_text or '').splitlines(keepends=True)
    body, ending = _split_ending(lines[file_line - 1])
    stamp = _TIMESTAMP.match(body)
    prefix = stamp.group(0) if stamp else ''
    if prefix and not prefix.endswith(' ') and new_text:
        prefix = prefix.rstrip() + ' '
    lines[file_line - 1] = prefix + ' '.join(str(new_text).split()) + ending
    return ''.join(lines)


def edit_txt_line(txt_text, lrc_text, file_line, new_text):
    """Change in the TXT the line matching LRC ``file_line`` (same occurrence for repeated lines).

    Returns ``(new_text, found)``; when the line is not in the TXT the text is returned unchanged.
    """
    lrc = (lrc_text or '').splitlines()
    key = lyric_key(lrc[file_line - 1])
    occurrence = sum(1 for raw in lrc[:file_line - 1] if lyric_key(raw) == key)
    lines = (txt_text or '').splitlines(keepends=True)
    seen = 0
    for index, line in enumerate(lines):
        if key and lyric_key(line) == key:
            if seen == occurrence:
                body, ending = _split_ending(line)
                indent = body[:len(body) - len(body.lstrip())]
                lines[index] = indent + ' '.join(str(new_text).split()) + ending
                return ''.join(lines), True
            seen += 1
    return txt_text, False


def write_keeping_bom(path, text):
    """Atomically replace a text file, keeping a UTF-8 BOM when the file had one."""
    path = Path(path)
    payload = text.encode('utf-8')
    if path.exists() and path.read_bytes().startswith(b'\xef\xbb\xbf'):
        payload = b'\xef\xbb\xbf' + payload
    temporary = path.with_name(path.name + '.edit.tmp')
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def backup_song_files(files, backup_root, prefix='fragment_'):
    """Copy the given files into one new backup folder with a restore.json; returns the folder."""
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=prefix, dir=backup_root))
    mapping = {}
    for original in files:
        original = Path(original)
        copy = backup / original.name
        shutil.copy2(original, copy)
        mapping[str(original)] = str(copy)
    try:
        write(backup / 'restore.json', mapping)
    except OSError:
        pass  # restore falls back to finding the files by name
    return backup
