"""Single entry point for Triplenine's Download, Build and Sync tools."""
from pathlib import Path
import runpy
import sys
import subprocess
import os

HERE = Path(__file__).resolve().parent
APP_DIR = HERE / 'SYNCED-LYRICS-DOWNLOADER-MAIN'


def main():
    sys.path.insert(0, str(APP_DIR))
    import studio_runtime
    script = [str(Path(__file__).resolve()), *sys.argv[1:]]
    # After "Install Triple 9 Studio.cmd" the program always runs in its own runtime\app environment,
    # also when this file is opened with another Python.
    if os.name == 'nt' and studio_runtime.active() and not studio_runtime.running_in_runtime():
        subprocess.Popen([str(studio_runtime.env_pythonw(studio_runtime.APP_ENV)), *script], cwd=str(HERE))
        return
    if os.name == 'nt' and Path(sys.executable).name.lower() != 'pythonw.exe':
        interpreter = Path(sys.executable).with_name('pythonw.exe')
        if interpreter.is_file():
            subprocess.Popen([str(interpreter), *script], cwd=str(HERE))
            return
    studio_runtime.prepare_environment()
    downloader = APP_DIR / 'triplenine_lyrics_downloader.py'
    runpy.run_path(str(downloader), run_name='__main__')


if __name__ == '__main__':
    main()
