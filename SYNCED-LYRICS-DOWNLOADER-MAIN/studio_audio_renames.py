"""Detect external audio renames by filesystem identity, never fuzzy titles."""
import hashlib
import json
import os
from pathlib import Path
import queue
import threading


def identity(path):
    stat = Path(path).stat()
    if not stat.st_ino:
        return None
    # ctime changes on rename on POSIX; only Windows exposes creation time here.
    birth = getattr(stat, 'st_birthtime_ns', stat.st_ctime_ns if os.name == 'nt' else 0)
    return [stat.st_dev, stat.st_ino, birth, stat.st_size, stat.st_mtime_ns]


def snapshot(folder):
    root = Path(folder).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    result = {}
    def failed(error):
        raise error  # An incomplete scan must never replace a good baseline.
    for directory, dirs, files in os.walk(root, onerror=failed):
        dirs[:] = [name for name in dirs if name.upper() not in
                   {'BACKUPS', '.GIT', '__PYCACHE__'} and not Path(directory, name).is_symlink()]
        for name in files:
            path = Path(directory, name)
            if path.suffix.lower() in ('.mp3', '.flac') and not path.is_symlink():
                result[str(path)] = identity(path)
    return result


def find_renames(previous, current):
    def index(items):
        result = {}
        for name, stamp in items.items():
            if stamp:
                result.setdefault(tuple(stamp), []).append(name)
        return result
    old_index, new_index = index(previous), index(current)
    pairs = []
    for stamp, old_names in old_index.items():
        new_names = new_index.get(stamp, [])
        if len(old_names) != 1 or len(new_names) != 1:
            continue  # Hardlinks and ambiguous identities are not safe matches.
        old, new = Path(old_names[0]), Path(new_names[0])
        if (str(old) != str(new) and old.parent == new.parent and old.stem != new.stem
                and old.suffix.lower() == new.suffix.lower()):
            pairs.append((old, new))
    return pairs


def sidecar_pairs(old, new):
    pairs = []
    for folder in (old.parent, old.parent / 'LYRICS', old.parent / 'SYNCED LYRICS'):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.is_file() and path.stem == old.stem and path.suffix.lower() in ('.txt', '.lrc'):
                pairs.append((path, folder / (new.stem + path.suffix)))
    return sorted(pairs)


def rename_sidecars(old, new, expected_identity, pairs):
    if identity(new) != expected_identity:
        raise RuntimeError('The audio file changed again. Refresh and try again.')
    if old.exists() and not os.path.samefile(old, new):
        raise RuntimeError('The old audio filename exists again. Nothing changed.')
    if sidecar_pairs(old, new) != pairs:
        raise RuntimeError('The lyrics files changed while the question was open. Nothing changed.')
    for source, target in pairs:
        if target.exists() and not os.path.samefile(source, target):
            raise FileExistsError(f'Lyrics already exist; nothing changed:\n{target}')
    moved = []
    try:
        for source, target in pairs:
            # Windows rename refuses to overwrite a concurrently created destination.
            if target.exists() and not os.path.samefile(source, target):
                raise FileExistsError(target)
            source.rename(target)
            moved.append((source, target))
    except OSError as exc:
        errors = []
        for source, target in reversed(moved):
            try:
                target.rename(source)
            except OSError as rollback_error:
                errors.append(str(rollback_error))
        if errors:
            raise RuntimeError(f'Rename failed: {exc}; rollback needs attention: {errors}') from exc
        raise


class RenameMonitor:
    """Scan off the UI thread; ask and change files only on the Tk thread."""
    def __init__(self, root, get_folder, is_busy, ask, report, refresh, state_dir):
        self.root, self.get_folder, self.is_busy = root, get_folder, is_busy
        self.ask, self.report, self.refresh = ask, report, refresh
        self.state_dir = Path(state_dir)
        self.results = queue.Queue()
        self.scanning = False
        self.folder = None
        self.previous = {}
        self.root.after(1000, self.tick)

    def state_path(self, folder):
        key = hashlib.sha256(os.path.normcase(folder).encode('utf-8')).hexdigest()
        return self.state_dir / (key + '.json')

    def scan(self, folder):
        try:
            current = snapshot(folder)
            previous = self.previous
            if folder != self.folder:
                try:
                    saved = json.loads(self.state_path(folder).read_text(encoding='utf-8'))
                    previous = saved if isinstance(saved, dict) else {}
                except (OSError, ValueError):
                    previous = {}
            changes = find_renames(previous, current)
            self.results.put((folder, current, changes, None))
        except Exception as exc:
            self.results.put((folder, {}, [], exc))

    def tick(self):
        try:
            if self.is_busy():
                return
            folder = self.get_folder()
            folder = str(Path(folder).resolve()) if folder else None
            if self.scanning:
                try:
                    scanned, current, changes, error = self.results.get_nowait()
                except queue.Empty:
                    return
                self.scanning = False
                if scanned != folder:
                    return
                if error:
                    self.report(f'Audio rename scan skipped: {error}')
                    return
                changed = False
                for old, new in changes:
                    try:
                        pairs = sidecar_pairs(old, new)
                        if not pairs:
                            continue
                        preview = '\n'.join(f'{a.relative_to(old.parent)} → {b.name}' for a, b in pairs)
                        if self.ask('Audio filename changed',
                                    f'This is the same audio file with a new name:\n\n{old.name}\n→ {new.name}\n\n'
                                    f'Rename its TXT / LRC lyrics too?\n\n{preview}\n\n'
                                    'Only filenames will change. Lyrics and audio tags stay unchanged.'):
                            rename_sidecars(old, new, current[str(new)], pairs)
                            self.report(f'Renamed {len(pairs)} lyrics files: {old.name} → {new.name}')
                            changed = True
                    except (OSError, RuntimeError) as exc:
                        self.report(f'Lyrics rename skipped: {exc}')
                self.folder, self.previous = folder, current
                try:
                    self.state_dir.mkdir(parents=True, exist_ok=True)
                    path = self.state_path(folder)
                    temporary = path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(current), encoding='utf-8')
                    temporary.replace(path)
                except OSError as exc:
                    self.report(f'Could not save audio rename history: {exc}')
                if changed:
                    self.refresh()
            elif folder:
                self.scanning = True
                threading.Thread(target=self.scan, args=(folder,), daemon=True).start()
        finally:
            self.root.after(2000 if self.scanning else 8000, self.tick)
