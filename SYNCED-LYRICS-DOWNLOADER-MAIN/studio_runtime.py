"""The program's own Python and tools, kept in one folder next to the program.

``Install Triple 9 Studio.cmd`` creates::

    runtime\\uv\\uv.exe          uv: downloads Python and installs packages (astral.sh)
    runtime\\python\\            Python 3.11 managed by uv (with Tkinter) — nothing is installed into Windows
    runtime\\app\\               environment for Downloader + Lyrics Sync (+ the Builder window)
    runtime\\builder\\           environment for Xingyu, made by Builder → Check / Setup Builder
    runtime\\ffmpeg\\            ffmpeg.exe / ffprobe.exe

One Python version, two environments: Lyrics Sync and Xingyu need different versions of torch / numpy,
so they must not share packages — but they no longer need two different Pythons. Removing the
``runtime`` folder removes everything the installer added. Without it the program keeps working with
the Pythons installed in Windows (the older setup).
"""
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent              # SYNCED-LYRICS-DOWNLOADER-MAIN
ROOT = APP_DIR.parent                                  # the folder with Triplenine_Lyrics_Studio.py
RUNTIME = ROOT / "runtime"
UV = RUNTIME / "uv" / "uv.exe"
PYTHON_DIR = RUNTIME / "python"
APP_ENV = RUNTIME / "app"
BUILDER_ENV = RUNTIME / "builder"
FFMPEG_DIR = RUNTIME / "ffmpeg"
PYTHON_VERSION = "3.11"
PYTORCH_CUDA_INDEX_URL = "https://download.pytorch.org/whl/cu128"
FFMPEG_URLS = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def env_python(env):
    return Path(env) / "Scripts" / "python.exe"


def env_pythonw(env):
    return Path(env) / "Scripts" / "pythonw.exe"


def active():
    """True when the installer made the app environment."""
    return env_python(APP_ENV).is_file()


def running_in_runtime():
    try:
        return Path(sys.prefix).resolve() == APP_ENV.resolve()
    except OSError:
        return False


def uv_env():
    """Environment for uv: keep its Python and cache inside runtime\\."""
    env = dict(os.environ)
    env["UV_PYTHON_INSTALL_DIR"] = str(PYTHON_DIR)
    env["UV_CACHE_DIR"] = str(RUNTIME / "cache")
    env["UV_PYTHON_PREFERENCE"] = "only-managed"
    env.pop("VIRTUAL_ENV", None)
    return env


def prepare_environment():
    """Put the program's ffmpeg in front of PATH (also for every program started from here) and give the
    process its own taskbar identity, so Windows shows the triplenine icon instead of Python's."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Triplenine.Studio")
        except Exception:
            pass
    if (FFMPEG_DIR / "ffmpeg.exe").is_file():
        path = os.environ.get("PATH", "")
        if str(FFMPEG_DIR).lower() not in path.lower():
            os.environ["PATH"] = str(FFMPEG_DIR) + os.pathsep + path


def run(command, log=None, timeout=3600, env=None):
    """Run a command, stream its output to ``log`` line by line, raise on failure."""
    process = subprocess.Popen([str(c) for c in command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", env=env, creationflags=NO_WINDOW)
    tail = []
    for line in process.stdout:
        line = line.rstrip()
        if line:
            tail = (tail + [line])[-60:]
            if log:
                log(line)
    process.wait(timeout=timeout)
    if process.returncode:
        raise RuntimeError("\n".join(tail[-25:]) or f"{Path(str(command[0])).name} exited with code {process.returncode}")


def pip_install(python, packages, log=None, extra=()):
    """Install packages into the environment of ``python`` — with uv when it is there, else pip."""
    if UV.is_file():
        run([UV, "pip", "install", "--python", python, *extra, *packages], log, env=uv_env())
    else:
        run([python, "-m", "ensurepip", "--upgrade"], None)
        run([python, "-m", "pip", "install", "--disable-pip-version-check", *extra, *packages], log)


def create_env(env_dir, log=None):
    """A new environment with the program's Python 3.11 (needs uv)."""
    if env_python(env_dir).is_file():
        return env_python(env_dir)
    if not UV.is_file():
        raise RuntimeError("runtime\\uv\\uv.exe is missing — run Install Triple 9 Studio.cmd first.")
    run([UV, "python", "install", PYTHON_VERSION, "--no-bin"], log, env=uv_env())
    run([UV, "venv", env_dir, "--python", PYTHON_VERSION], log, env=uv_env())
    return env_python(env_dir)


def nvidia_gpu():
    tool = shutil.which("nvidia-smi")
    if not tool and os.name == "nt":
        candidate = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        tool = str(candidate) if candidate.is_file() else None
    if not tool:
        return False
    try:
        result = subprocess.run([tool, "-L"], capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
        return result.returncode == 0 and "GPU" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def _download(url, target, log=None):
    request = urllib.request.Request(url, headers={"User-Agent": "Triplenine-Studio-Setup"})
    with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done, step = 0, 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if log and total and done * 10 // total > step:
                step = done * 10 // total
                log(f"  {done >> 20} / {total >> 20} MB")


def install_ffmpeg(log=None):
    """ffmpeg.exe + ffprobe.exe into runtime\\ffmpeg (no WinGet, nothing added to Windows)."""
    if (FFMPEG_DIR / "ffmpeg.exe").is_file():
        return FFMPEG_DIR / "ffmpeg.exe"
    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    archive = RUNTIME / "ffmpeg-download.zip"
    errors = []
    for url in FFMPEG_URLS:
        try:
            if log:
                log("Downloading FFmpeg: " + url)
            _download(url, archive, log)
            with zipfile.ZipFile(archive) as zf:
                for name in zf.namelist():
                    if name.lower().endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe")):
                        (FFMPEG_DIR / Path(name).name).write_bytes(zf.read(name))
            if (FFMPEG_DIR / "ffmpeg.exe").is_file():
                break
        except Exception as exc:  # try the next mirror
            errors.append(f"{url}: {exc}")
        finally:
            archive.unlink(missing_ok=True)
    if not (FFMPEG_DIR / "ffmpeg.exe").is_file():
        raise RuntimeError("FFmpeg could not be downloaded:\n" + "\n".join(errors))
    prepare_environment()
    return FFMPEG_DIR / "ffmpeg.exe"


def install_torch(python, log=None, cuda=None):
    """PyTorch: the CUDA build when an NVIDIA card is present, else the normal (CPU) build."""
    cuda = nvidia_gpu() if cuda is None else cuda
    if cuda:
        if log:
            log("NVIDIA card found — installing PyTorch with CUDA (large download)…")
        pip_install(python, ["torch"], log, extra=("--index-url", PYTORCH_CUDA_INDEX_URL))
    else:
        if log:
            log("No NVIDIA card — installing PyTorch for the processor (CPU)…")
        pip_install(python, ["torch"], log)
