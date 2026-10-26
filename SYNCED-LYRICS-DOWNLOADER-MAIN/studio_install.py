"""Second half of Install Triple 9 Studio.cmd: runs inside runtime\\app and installs everything the
Downloader and Lyrics Sync need. Lyric Builder's Xingyu part is installed later by Check / Setup Builder.

Safe to run again: what is already there is kept, missing or broken parts are installed.
"""
import json
import subprocess
import sys
import time

import studio_runtime as rt

PACKAGES = [
    "syncedlyrics", "mutagen", "pillow", "numpy",
    "syncalong[whisper]",        # Lyrics Sync engine + OpenAI Whisper
    "demucs==4.1.0",             # "Separate vocals with Demucs"
]
CHECK = (
    "import json, tkinter, PIL, mutagen, syncedlyrics, syncalong, whisper, torch, demucs\n"
    "print(json.dumps({'tk': tkinter.TkVersion, 'torch': torch.__version__, 'cuda_build': torch.version.cuda,"
    " 'cuda': bool(torch.cuda.is_available()), 'device': torch.cuda.get_device_name(0) if torch.cuda.is_available() else ''}))"
)


def say(text=""):
    print(text, flush=True)


def step(number, total, text):
    say("")
    say(f"[{number}/{total}] {text}")


def probe():
    result = subprocess.run([sys.executable, "-c", CHECK], capture_output=True, text=True)
    if result.returncode:
        return None, (result.stderr or result.stdout).strip().splitlines()[-1:] or ["unknown error"]
    return json.loads(result.stdout.strip().splitlines()[-1]), None


def main():
    started = time.time()
    total = 4
    cuda = rt.nvidia_gpu()
    step(1, total, "PyTorch" + (" with NVIDIA CUDA" if cuda else " (CPU)"))
    info, _error = probe()
    if info and (info["cuda_build"] or not cuda):
        say(f"  already installed: torch {info['torch']}")
    else:
        rt.install_torch(sys.executable, say, cuda=cuda)

    step(2, total, "Lyrics downloader, Lyrics Sync (Whisper) and Demucs")
    rt.pip_install(sys.executable, PACKAGES, say)

    step(3, total, "FFmpeg (reads the audio for Whisper)")
    say("  " + str(rt.install_ffmpeg(say)))

    step(4, total, "Checking that everything works")
    info, error = probe()
    if info and cuda and not info["cuda_build"]:  # a package pulled in the CPU build: put CUDA back
        say("  PyTorch lost its CUDA build — reinstalling the CUDA build…")
        rt.pip_install(sys.executable, ["torch"], say,
                       extra=("--index-url", rt.PYTORCH_CUDA_INDEX_URL, "--reinstall-package", "torch"))
        info, error = probe()
    if not info:
        raise SystemExit("Something did not install correctly: " + " ".join(error))
    ffmpeg = subprocess.run([str(rt.FFMPEG_DIR / "ffmpeg.exe"), "-version"], capture_output=True, text=True)
    if ffmpeg.returncode:
        raise SystemExit("FFmpeg does not start.")
    say(f"  Python {sys.version.split()[0]} • Tkinter {info['tk']} • torch {info['torch']}"
        + (f" • GPU: {info['device']}" if info["cuda"] else " • CPU mode"))
    (rt.RUNTIME / "installed.json").write_text(json.dumps(
        {"python": sys.version.split()[0], **info, "time": time.strftime("%Y-%m-%d %H:%M")}, indent=2),
        encoding="utf-8")
    say("")
    say(f"Done in {int(time.time() - started) // 60} min.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        say("")
        say("ERROR: " + str(exc))
        raise SystemExit(1)
