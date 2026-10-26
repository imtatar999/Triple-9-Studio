"""Edit a sidecar and replace embedded timed lyrics as one recoverable operation."""
import json
import os
from pathlib import Path
import queue
import re
import shutil
import tempfile
import threading

TIMESTAMP = re.compile(r'(?m)^\s*\[\d{1,3}:[0-5]\d(?:[.:]\d{1,3})?\]')


def stamp(path):
    s = path.stat()
    return s.st_ino, s.st_size, s.st_mtime_ns


def save_and_embed(audio, lrc, text, expected_lrc, expected_audio, backup_root, embed):
    from mutagen.id3 import ID3, ID3NoHeaderError
    audio, lrc = Path(audio), Path(lrc)
    if audio.suffix.lower() != '.mp3' or lrc.suffix.lower() != '.lrc':
        raise ValueError('Select an MP3 and its LRC file.')
    if not TIMESTAMP.search(text):
        raise ValueError('The LRC must contain timestamps such as [00:12.34].')
    if lrc.read_bytes() != expected_lrc or stamp(audio) != expected_audio:
        raise RuntimeError('The MP3 or LRC changed outside this editor. Close and reopen it before saving.')
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='manual-lrc-', dir=backup_root))
    shutil.copy2(audio, backup / audio.name)
    shutil.copy2(lrc, backup / lrc.name)
    try:  # where each copy came from, for Options → Restore from backups
        (backup / 'restore.json').write_text(json.dumps(
            {str(audio): str(backup / audio.name), str(lrc): str(backup / lrc.name)}, ensure_ascii=False),
            encoding='utf-8')
    except OSError:
        pass
    staged_audio = staged_lrc = None
    audio_committed = False
    try:
        fd, name = tempfile.mkstemp(prefix='.t9-lrc-', suffix='.mp3', dir=audio.parent)
        os.close(fd)
        staged_audio = Path(name)
        shutil.copy2(backup / audio.name, staged_audio)
        try:
            tags = ID3(staged_audio)
        except ID3NoHeaderError:
            tags = ID3()
        version = 3 if tags.version[1] == 3 else 4
        # Remove old synchronized variants, retain unrelated metadata and plain translations.
        for key, frame in list(tags.items()):
            timed = frame.FrameID == 'SYLT'
            if frame.FrameID == 'USLT':
                timed = bool(TIMESTAMP.search(frame.text))
            if frame.FrameID == 'TXXX' and 'lyric' in frame.desc.lower():
                timed = any(TIMESTAMP.search(t) for t in frame.text)
            if timed:
                del tags[key]
        tags.save(staged_audio, v2_version=version)
        normalized = text.rstrip() + '\n'
        embed(str(staged_audio), normalized)
        fd, name = tempfile.mkstemp(prefix='.t9-lrc-', suffix='.tmp', dir=lrc.parent)
        os.close(fd)
        staged_lrc = Path(name)
        staged_lrc.write_text(normalized, encoding='utf-8', newline='\n')
        if lrc.read_bytes() != expected_lrc or stamp(audio) != expected_audio:
            raise RuntimeError('Files changed during saving. Nothing was replaced.')
        os.replace(staged_audio, audio)
        audio_committed = True
        os.replace(staged_lrc, lrc)
    except Exception:
        if audio_committed:
            try:
                shutil.copy2(backup / audio.name, audio)
            except OSError as exc:
                raise RuntimeError(f'Could not restore the MP3. Restore it from {backup}: {exc}') from exc
        raise
    finally:
        for path in (staged_audio, staged_lrc):
            if path and path.exists():
                path.unlink()
    return backup


def open_editor(root, audio, lrc, palette, create_window, embed, backup_root, on_saved):
    import tkinter as tk
    from tkinter import messagebox
    audio, lrc = Path(audio), Path(lrc)
    original = lrc.read_bytes()
    baseline = {'bytes': original, 'audio': stamp(audio), 'text': original.decode('utf-8-sig')}
    win = create_window(root)
    win.title('Change LRC Manually — ' + audio.name)
    win.geometry('900x650')
    win.minsize(650, 430)
    win.configure(bg=palette['bg'])
    win.transient(root)
    win.grab_set()
    tk.Label(win, text=str(lrc), wraplength=840, anchor='w', bg=palette['bg'], fg=palette['fg']).pack(fill='x', padx=14, pady=10)
    tk.Label(win, text='Edit lyrics and timestamps. Save updates this LRC and replaces the embedded timed lyrics in the MP3.',
             wraplength=840, bg=palette['bg'], fg=palette['fg']).pack(fill='x', padx=14, pady=(0, 8))
    frame = tk.Frame(win, bg=palette['bg'])
    frame.pack(fill='both', expand=True, padx=14)
    editor = tk.Text(frame, undo=True, wrap='none', font=('Consolas', 11), bg=palette['panel'], fg=palette['fg'],
                     insertbackground=palette['fg'], selectbackground=palette['sel_bg'], selectforeground=palette['sel_fg'])
    vertical = tk.Scrollbar(frame, command=editor.yview)
    horizontal = tk.Scrollbar(frame, orient='horizontal', command=editor.xview)
    vertical.pack(side='right', fill='y')
    horizontal.pack(side='bottom', fill='x')
    editor.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    editor.pack(fill='both', expand=True)
    editor.insert('1.0', baseline['text'])
    editor.edit_reset()
    status = tk.StringVar(value='Ctrl+S: save LRC and update MP3. Originals are backed up.')
    tk.Label(win, textvariable=status, bg=palette['bg'], fg=palette['fg'], wraplength=840).pack(fill='x', padx=14, pady=8)
    bar = tk.Frame(win, bg=palette['bg'])
    bar.pack(fill='x', padx=14, pady=(0, 12))
    state = {'busy': False}
    results = queue.Queue()

    def save(event=None):
        if state['busy']:
            return 'break'
        text = editor.get('1.0', 'end-1c')
        if not TIMESTAMP.search(text):
            messagebox.showerror('Invalid LRC', 'Keep timestamped lines such as [00:12.34].', parent=win)
            return 'break'
        state['busy'] = True
        editor.configure(state='disabled')
        save_btn.configure(state='disabled')
        status.set('Saving LRC and replacing embedded lyrics…')
        def worker():
            try:
                backup = save_and_embed(audio, lrc, text, baseline['bytes'], baseline['audio'], backup_root, embed)
                results.put((backup, None, lrc.read_bytes(), stamp(audio)))
            except Exception as exc:
                results.put((None, str(exc), None, None))
        threading.Thread(target=worker, daemon=True).start()
        win.after(100, poll)
        return 'break'

    def poll():
        try:
            backup, error, raw, audio_stamp = results.get_nowait()
        except queue.Empty:
            win.after(100, poll)
            return
        state['busy'] = False
        editor.configure(state='normal')
        save_btn.configure(state='normal')
        if error:
            status.set('Save failed. See details below.')
            messagebox.showerror('Change LRC Manually', error, parent=win)
        else:
            baseline.update(bytes=raw, audio=audio_stamp, text=raw.decode('utf-8-sig'))
            editor.delete('1.0', 'end')
            editor.insert('1.0', baseline['text'])
            editor.edit_reset()
            status.set('Saved LRC and updated MP3. Backup: ' + str(backup))
            on_saved(str(audio), str(lrc), str(backup))

    def close():
        if state['busy']:
            return
        if editor.get('1.0', 'end-1c') != baseline['text']:
            if not messagebox.askyesno('Discard changes?', 'Close without saving your LRC edits?', parent=win):
                return
        win.destroy()

    button_style = dict(bg=palette['btn_bg'], fg=palette['btn_fg'], activebackground=palette['sel_bg'],
                        activeforeground=palette['sel_fg'], relief='flat', padx=12, pady=7)
    save_btn = tk.Button(bar, text='Save LRC + update MP3', command=save, **button_style)
    save_btn.pack(side='left')
    tk.Button(bar, text='Close', command=close, **button_style).pack(side='right')
    win.bind('<Control-s>', save)
    win.protocol('WM_DELETE_WINDOW', close)
    editor.focus_set()
    return win
