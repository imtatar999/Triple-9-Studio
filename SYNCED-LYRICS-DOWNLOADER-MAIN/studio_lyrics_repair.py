"""Review orphan lyric files as candidates for audio whose name changed."""
from collections import defaultdict
from dataclasses import dataclass
import difflib
import hashlib
import os
from pathlib import Path
import queue
import re
import shutil
import tempfile
import threading
import unicodedata


def words(text):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold()))


def filename_key(text):
    # Apostrophes / underscores / spaces are common filename substitutions.
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def filename_aliases(text):
    """Return meaningful base/parenthetical title aliases without spacing."""
    text = unicodedata.normalize('NFKC', text)
    parts = [re.split(r'[([]', text, 1)[0]]
    parts.extend(re.findall(r'\(([^)]*)\)|\[([^]]*)\]', text))
    flattened = [part if isinstance(part, str) else next((value for value in part if value), '')
                 for part in parts]
    ignored = {'ai', 'sessions', 'sessionedit', 'session', 'extended', 'remaster',
               'v1', 'v2', 'v3', 'drfl', 'wod', 'jw3', 'gbgr', '999ep'}
    aliases = {filename_key(part) for part in flattened}
    return {alias for alias in aliases if len(alias) >= 5 and alias not in ignored}


def lyric_text(text):
    return words(re.sub(r'\[[^\]]*\]', '', text))


def read_text(path):
    return path.read_text(encoding='utf-8-sig', errors='replace')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class Candidate:
    audio: Path
    missing: tuple
    pairs: list
    reasons: list
    hashes: list
    audio_stamp: tuple
    target_hashes: list
    score: int = 0


def audio_stamp(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def scan(folder, cancel):
    import studio_audio_renames
    audios = [Path(path) for path in studio_audio_renames.snapshot(folder)]
    albums = defaultdict(list)
    for audio in audios:
        albums[audio.parent].append(audio)
    results, missing_songs = [], []
    for album, tracks in albums.items():
        if cancel.is_set():
            break
        names = {p.stem.casefold() for p in tracks}
        sidecars = []
        for directory in (album, album / 'LYRICS', album / 'SYNCED LYRICS'):
            if directory.is_dir():
                sidecars.extend(p for p in directory.iterdir()
                                if p.is_file() and not p.is_symlink() and p.suffix.lower() in ('.txt', '.lrc'))
        groups = defaultdict(list)
        for path in sidecars:
            if path.stem.casefold() not in names:
                groups[path.stem].append(path)
        texts = {p: read_text(p) for group in groups.values() for p in group}
        for audio in tracks:
            if cancel.is_set():
                break
            own = [p for p in sidecars if p.stem.casefold() == audio.stem.casefold()]
            missing = tuple(ext for ext in ('.txt', '.lrc') if not any(p.suffix.lower() == ext for p in own))
            if missing:
                missing_songs.append((audio, missing))
            references = [lyric_text(read_text(p)) for p in own]
            # Embedded lyrics are independent evidence when the original filename is gone.
            try:
                if audio.suffix.lower() == '.mp3':
                    from mutagen.id3 import ID3
                    tags = ID3(audio)
                    references += [lyric_text(frame.text) for frame in tags.getall('USLT')]
                else:
                    from mutagen.flac import FLAC
                    tags = FLAC(audio)
                    references += [lyric_text(t) for t in tags.get('lyrics', [])]
            except Exception:
                pass
            title = words(re.sub(r'\([^)]*\)|\[[^]]*\]', '', audio.stem))
            suggestions = []
            for old_name, paths in groups.items():
                candidates = [p for p in paths if p.suffix.lower() in missing]
                reasons = set()
                score = 0
                if filename_key(old_name) == filename_key(audio.stem):
                    reasons.add('Filename matches after punctuation / spacing normalization')
                    score = 90
                elif filename_aliases(old_name) & filename_aliases(audio.stem):
                    reasons.add('Alternative title in parentheses matches the current filename')
                    score = 90
                elif difflib.SequenceMatcher(None, words(old_name), words(audio.stem)).ratio() >= .82:
                    reasons.add('Similar filename (suggestion only)')
                    score = 60
                for path in paths:
                    text = texts[path]
                    normal = lyric_text(text)
                    if len(normal) >= 80 and normal in references:
                        reasons.add('Same lyrics as the existing sidecar / embedded lyrics')
                        score = max(score, 100)
                    if title and any(words(t) == title for t in re.findall(r'\[ti:([^]]+)\]', text, re.I)):
                        reasons.add('LRC title matches the current audio title')
                        score = max(score, 80)
                    if len(title.split()) >= 2 and len(filename_key(title)) >= 10 and f' {title} ' in f' {normal} ':
                        reasons.add('Current title occurs in lyrics (suggestion only)')
                        score = max(score, 10)
                # An old-name sidecar can remain even when a current-name file
                # already exists. Offer it only when both the filename alias and
                # normalized lyrics strongly agree, then require explicit review.
                if not candidates and score >= 90:
                    for source in paths:
                        target = source.with_name(audio.stem + source.suffix)
                        if not target.is_file():
                            continue
                        source_text = lyric_text(texts[source])
                        target_text = lyric_text(read_text(target))
                        similarity = difflib.SequenceMatcher(None, source_text, target_text).ratio()
                        if min(len(source_text), len(target_text)) >= 80 and similarity >= .98:
                            candidates.append(source)
                            reasons.add(f'Existing current-name file has {similarity:.1%} matching lyric text; review which timing is correct')
                            score = max(score, 95)
                if not candidates:
                    continue
                target_hashes = []
                pairs = []
                for source in candidates:
                    target = source.with_name(audio.stem + source.suffix)
                    pairs.append((source, target))
                    target_hashes.append(digest(target) if target.exists() else None)
                if reasons:
                    suggestions.append(Candidate(audio, missing,
                        pairs, sorted(reasons), [digest(p) for p in candidates],
                        audio_stamp(audio), target_hashes, score))
            strong = [c for c in suggestions if c.score >= 80]
            if strong:
                results.extend(strong)
            elif any(c.score >= 60 for c in suggestions):
                results.extend(c for c in suggestions if c.score >= 60)
            elif len(suggestions) == 1:
                results.extend(suggestions)  # Phrase-only matches must be unique.
    return results, missing_songs


def validate(candidate):
    if audio_stamp(candidate.audio) != candidate.audio_stamp:
        raise RuntimeError('Audio changed after scanning. Scan again.')
    for index, ((source, target), expected) in enumerate(zip(candidate.pairs, candidate.hashes)):
        expected_target = candidate.target_hashes[index]
        if target.exists() and expected_target is None:
            raise FileExistsError(f'Nothing changed: destination already exists:\n{target}')
        if target.exists() and digest(target) != expected_target:
            raise RuntimeError(f'Current-name lyrics changed after scanning:\n{target}')
        if not target.exists() and expected_target is not None:
            raise RuntimeError(f'Current-name lyrics disappeared after scanning:\n{target}')
        if digest(source) != expected:
            raise RuntimeError(f'Lyrics changed after scanning:\n{source}')
        # A file with the old name may have appeared since the scan.
        if any(p.suffix.lower() in ('.mp3', '.flac') and p.stem.casefold() == source.stem.casefold()
               for p in candidate.audio.parent.iterdir()):
            raise RuntimeError('The old name now belongs to an audio file. Scan again.')


def validate_batch(candidates):
    audios, sources, targets = set(), set(), set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.audio.resolve()))
        if key in audios:
            raise RuntimeError('Select only one proposed old name for each song.')
        audios.add(key)
        for source, target in candidate.pairs:
            source_key = os.path.normcase(str(source.resolve()))
            target_key = os.path.normcase(str(target.resolve()))
            if source_key in sources or target_key in targets:
                raise RuntimeError('The selection assigns the same lyrics file more than once. Choose one match.')
            sources.add(source_key)
            targets.add(target_key)
        validate(candidate)


def apply_batch(candidates, backup_root=None):
    validate_batch(candidates)
    moved = []
    replacements = sum(expected is not None for candidate in candidates
                       for expected in candidate.target_hashes)
    backup = None
    if replacements:
        backup_root = (Path(backup_root) if backup_root is not None else
                       Path(__file__).resolve().parent.parent / 'BACKUPS' / 'LYRICS-NAME-REPAIR')
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix='replace_', dir=backup_root))
    try:
        for candidate in candidates:
            for index, (source, target) in enumerate(candidate.pairs):
                saved = None
                if candidate.target_hashes[index] is not None:
                    saved = backup / str(len(moved)) / target.name
                    saved.parent.mkdir(parents=True)
                    shutil.copy2(target, saved)
                    source.replace(target)
                else:
                    source.rename(target)
                moved.append((source, target, saved))
    except OSError as exc:
        errors = []
        for source, target, saved in reversed(moved):
            try:
                target.rename(source)
                if saved is not None:
                    shutil.copy2(saved, target)
            except OSError as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError(f'{exc}\nRollback errors: {errors}') from exc
        raise
    return backup


def apply(candidate, backup_root=None):
    return apply_batch([candidate], backup_root=backup_root)


def open_review(root, folder, refresh, report, palette=None, create_window=None):
    import tkinter as tk
    from tkinter import ttk, messagebox
    win = create_window(root) if create_window else tk.Toplevel(root)
    colors = {'bg': '#141414', 'panel': '#1c1c1c', 'fg': '#eaeaea',
              'btn_bg': '#2a2a2a', 'sel_bg': '#3b82f6', 'sel_fg': '#ffffff'}
    colors.update(palette or {})
    win.configure(bg=colors['bg'])
    style = ttk.Style(win)
    # Scoped styles leave the rest of Studio unchanged.
    for name, original in [('Repair.field', 'Treeview.field'), ('Repair.heading', 'Treeheading.border')]:
        if name not in style.element_names():
            style.element_create(name, 'from', 'clam', original)
    style.layout('Repair.Treeview', [('Repair.field', {'sticky': 'nswe', 'children': [
        ('Treeview.padding', {'sticky': 'nswe', 'children': [('Treeview.treearea', {'sticky': 'nswe'})]})]})])
    style.layout('Repair.Treeview.Heading', [('Repair.heading', {'sticky': 'nswe', 'children': [
        ('Treeheading.padding', {'sticky': 'nswe', 'children': [('Treeheading.text', {'sticky': 'we'})]})]})])
    for kind in ('TFrame', 'TLabel', 'TButton'):
        style.configure('Repair.' + kind, background=colors['bg'] if kind != 'TButton' else colors['btn_bg'], foreground=colors['fg'])
    style.configure('Repair.Treeview', background=colors['panel'], fieldbackground=colors['panel'], foreground=colors['fg'], rowheight=25)
    style.map('Repair.Treeview', background=[('selected', colors['sel_bg'])], foreground=[('selected', colors['sel_fg'])])
    style.configure('Repair.Treeview.Heading', background=colors['btn_bg'], foreground=colors['fg'])
    style.map('Repair.Treeview.Heading', background=[('active', colors['btn_bg'])])
    win.title('Check missing TXT / LRC — previous filenames')
    win.geometry('1000x650')
    win.transient(root)
    win.grab_set()
    status = tk.StringVar(value='Scanning audio and orphan lyrics…')
    ttk.Label(win, textvariable=status, wraplength=960, style='Repair.TLabel').pack(fill='x', padx=12, pady=10)
    ttk.Label(win, text='Ctrl / Shift: select several songs. Review suggestions before confirming.', style='Repair.TLabel').pack(anchor='w', padx=12)
    tree = ttk.Treeview(win, columns=('audio', 'missing', 'old'), show='headings', selectmode='extended', height=10, style='Repair.Treeview')
    for key, title in [('audio', 'Current song'), ('missing', 'Missing'), ('old', 'Possible old filename')]:
        tree.heading(key, text=title)
        tree.column(key, width=300 if key != 'missing' else 110)
    tree.pack(fill='both', expand=True, padx=12, pady=8)
    detail = tk.Text(win, height=12, wrap='word', state='disabled', bg=colors['panel'], fg=colors['fg'], insertbackground=colors['fg'], selectbackground=colors['sel_bg'], selectforeground=colors['sel_fg'], relief='flat')
    detail.pack(fill='both', expand=True, padx=12)
    bar = ttk.Frame(win, style='Repair.TFrame')
    bar.pack(fill='x', padx=12, pady=10)
    cancel, messages = threading.Event(), queue.Queue()
    candidates, rows = [], {}
    state = {'busy': False, 'closed': False}

    def describe(event=None):
        selection = tree.selection()
        detail.configure(state='normal')
        detail.delete('1.0', 'end')
        candidate = rows.get(selection[0]) if selection else None
        chosen = [rows[row] for row in selection if row in rows]
        if len(chosen) > 1:
            detail.insert('end', f'{len(chosen)} songs selected:\n\n')
            for c in chosen:
                detail.insert('end', f'{c.audio.name} ← {c.pairs[0][0].stem}\n')
            detail.insert('end', '\nFirst selected suggestion:\n\n')
        if candidate:
            detail.insert('end', str(candidate.audio) + '\n\n' + '\n'.join(candidate.reasons) + '\n\n')
            for index, (source, target) in enumerate(candidate.pairs):
                action = 'REPLACE existing file (backup will be created)' if candidate.target_hashes[index] else 'rename'
                detail.insert('end', f'{source}\n→ {target.name}  [{action}]\n\n')
            detail.insert('end', 'Preview (first 800 characters):\n')
            try:
                detail.insert('end', read_text(candidate.pairs[0][0])[:800])
            except OSError as exc:
                detail.insert('end', str(exc))
        elif selection:
            detail.insert('end', 'No supported match found. A past rename cannot always be recovered without filename history or matching lyrics.')
        detail.configure(state='disabled')
        apply_btn.configure(state='normal' if chosen and not state['busy'] else 'disabled', text=f'Apply selected matches ({len(chosen)})…')

    def start():
        if state['busy']:
            return
        state['busy'] = True
        scan_btn.configure(state='disabled')
        apply_btn.configure(state='disabled')
        status.set('Scanning audio and orphan lyrics…')
        tree.delete(*tree.get_children())
        rows.clear()
        def worker():
            try:
                messages.put((scan(folder, cancel), None))
            except Exception as exc:
                messages.put((None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll():
        if state['closed']:
            return
        try:
            result, error = messages.get_nowait()
        except queue.Empty:
            win.after(100, poll)
            return
        state['busy'] = False
        scan_btn.configure(state='normal')
        if error:
            status.set('Scan failed: ' + error)
        else:
            found, missing = result
            candidates[:] = found
            matched = {c.audio for c in found}
            for c in found:
                operation = ', '.join(c.missing) if c.missing else 'Review existing file'
                row = tree.insert('', 'end', values=(c.audio.name, operation, c.pairs[0][0].stem))
                rows[row] = c
            for audio, extensions in missing:
                if audio not in matched:
                    tree.insert('', 'end', values=(audio.name, ', '.join(extensions), 'No match found'))
            status.set(f'{len(missing)} songs missing TXT / LRC; {len(found)} suggestions. Review each match; title similarity is not proof.')
        win.after(100, poll)

    def confirm():
        selected = tree.selection()
        chosen = [rows[row] for row in selected if row in rows]
        if not chosen or state['busy']:
            return
        try:
            validate_batch(chosen)
        except (OSError, RuntimeError) as exc:
            messagebox.showerror('Conflicting selection', str(exc), parent=win)
            return
        preview = '\n'.join(f'{c.pairs[0][0].stem} → {c.audio.stem}' for c in chosen)
        replacements = sum(value is not None for c in chosen for value in c.target_hashes)
        warning = (f'\n\n{replacements} existing current-name file(s) will be replaced. '
                   'Their previous versions will be backed up.' if replacements else '')
        if not messagebox.askyesno('Confirm lyrics matches',
                f'Apply lyrics-name matches for {len(chosen)} songs ({sum(len(c.pairs) for c in chosen)} files)?'
                f'\n\n{preview}{warning}\n\nAudio files and tags will stay unchanged.', parent=win):
            return
        try:
            backup = apply_batch(chosen)
        except (OSError, RuntimeError) as exc:
            messagebox.showerror('Lyrics not renamed', str(exc), parent=win)
            return
        for c in chosen:
            report(f'Recovered lyrics for {c.audio.name}: {len(c.pairs)} filename match(es) applied.')
        if backup:
            report(f'Replaced lyric-file backup: {backup}')
        refresh()
        start()

    def close():
        state['closed'] = True
        cancel.set()
        win.destroy()

    button_colors = dict(bg=colors['btn_bg'], fg=colors['fg'], activebackground=colors['sel_bg'],
                         activeforeground=colors['sel_fg'], disabledforeground='#888888', relief='flat', padx=10, pady=6)
    apply_btn = tk.Button(bar, text='Apply selected matches…', command=confirm, state='disabled', **button_colors)
    apply_btn.pack(side='left')
    scan_btn = tk.Button(bar, text='Scan again', command=start, **button_colors)
    scan_btn.pack(side='left', padx=8)
    tk.Button(bar, text='Close', command=close, **button_colors).pack(side='right')
    tree.bind('<<TreeviewSelect>>', describe)
    win.protocol('WM_DELETE_WINDOW', close)
    start()
    win.after(100, poll)
    return win
