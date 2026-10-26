"""Smoke-test the release in a temporary copy, then create its ZIP."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

root = Path(__file__).resolve().parent.parent
package = root / 'T9 FRIEND TEST'
for path in package.rglob('*.py'):
    ast.parse(path.read_text(encoding='utf-8'))
with tempfile.TemporaryDirectory(dir=root) as tmp:
    isolated = Path(tmp)
    shutil.copytree(package, isolated / 'APP')
    env = os.environ.copy()
    env['APPDATA'] = str(isolated / 'PROFILE')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    script = '''import tkinter as tk, runpy
from pathlib import Path
tk.Tk.mainloop = lambda self, *a, **k: None
ns = runpy.run_path(str(Path('SYNCED-LYRICS-DOWNLOADER-MAIN/triplenine_lyrics_downloader.py')), run_name='__main__')
r = tk._default_root
assert r.title() == 'Triple 9 Studio - Downloader'
ns['open_syncalong_window']()
assert any(w.title() == 'Triple 9 Studio - Lyrics Sync' for w in r.winfo_children() if isinstance(w, tk.Toplevel))
r.destroy()
import Triplenine_Hybrid_Sessions_Lyrics_Builder as builder
r = tk.Tk()
builder.App(r)
r.update_idletasks()
assert r.title() == 'Triple 9 Studio - Lyric Builder'
r.destroy()
print('Clean settings: all three windows initialized successfully.')
'''
    subprocess.run([sys.executable, '-c', script], cwd=isolated / 'APP', env=env, check=True, timeout=60)
archive = root / 'T9 FRIEND TEST.zip'
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for path in sorted(package.rglob('*')):
        if path.is_file():
            z.write(path, path.relative_to(root))
with zipfile.ZipFile(archive, 'r') as z:
    assert z.testzip() is None
files = [p for p in package.rglob('*') if p.is_file()]
print(f'Files: {len(files)}; folder bytes: {sum(p.stat().st_size for p in files)}; ZIP bytes: {archive.stat().st_size}')
