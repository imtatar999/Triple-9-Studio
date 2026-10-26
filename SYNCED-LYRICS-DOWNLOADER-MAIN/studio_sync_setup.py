"""Check/repair the current Sync interpreter; never installs into Xingyu's Python."""
import os
import studio_runtime
import json
import shutil
import subprocess
import sys


PYTORCH_CUDA_INDEX_URL = studio_runtime.PYTORCH_CUDA_INDEX_URL
SYNC_PYTHONS = ((3, 10), (3, 11))  # 3.11 = the program's own runtimepp; 3.10 = the older Windows setup


def run(command, timeout=180):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stdout[-4000:] or f"Process exited with code {result.returncode}")
    return result.stdout


def refresh_path():
    if os.name != "nt":
        return
    import winreg
    for hive, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value = os.path.expandvars(winreg.QueryValueEx(handle, "Path")[0])
                os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + value
        except OSError:
            pass


def nvidia_gpu_available():
    """Return True when the NVIDIA driver exposes at least one GPU."""
    tool = shutil.which("nvidia-smi")
    if not tool:
        return False
    try:
        run([tool, "--list-gpus"], timeout=15)
        return True
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return False


def torch_cuda_info():
    """Inspect PyTorch in a fresh process so a repaired wheel is seen immediately."""
    probe = (
        "import json\n"
        "try:\n"
        " import torch\n"
        " print(json.dumps({'installed': True, 'version': torch.__version__, "
        "'cuda_build': torch.version.cuda, 'cuda_available': bool(torch.cuda.is_available()), "
        "'device': torch.cuda.get_device_name(0) if torch.cuda.is_available() else ''}))\n"
        "except Exception as exc:\n"
        " print(json.dumps({'installed': False, 'error': str(exc)}))\n"
    )
    try:
        output = run([sys.executable, "-c", probe], timeout=45).strip().splitlines()[-1]
        return json.loads(output)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        return {"installed": False, "error": str(exc)}


def install_pytorch_gpu(log):
    """Replace CPU-only PyTorch with the official CUDA build in the Lyrics Sync Python."""
    if sys.version_info[:2] not in SYNC_PYTHONS:
        raise RuntimeError("PyTorch GPU repair must run in the Studio's own Python. Reopen the Studio launcher.")
    if not nvidia_gpu_available():
        raise RuntimeError("NVIDIA GPU was not detected by nvidia-smi.")

    before = torch_cuda_info()
    if before.get("cuda_available") and before.get("cuda_build"):
        log(f"PyTorch CUDA is already ready ({before['version']}, CUDA {before['cuda_build']}).")
        return before

    log("Installing official PyTorch CUDA 12.8 for Lyrics Sync…")
    log("This is a large download. Existing CPU-only torch/torchaudio packages will be replaced.")
    if studio_runtime.UV.is_file() and studio_runtime.running_in_runtime():
        studio_runtime.pip_install(sys.executable, ["torch"], log,
                                   extra=("--index-url", PYTORCH_CUDA_INDEX_URL, "--reinstall-package", "torch"))
    else:
        run([sys.executable, "-m", "ensurepip", "--upgrade"])
        output = run([
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--upgrade", "--force-reinstall", "torch", "torchaudio",
            "--index-url", PYTORCH_CUDA_INDEX_URL,
        ], timeout=3600)
        log(output)

    after = torch_cuda_info()
    if not after.get("installed"):
        raise RuntimeError("PyTorch did not load after GPU repair: " + after.get("error", "unknown error"))
    if not after.get("cuda_build") or not after.get("cuda_available"):
        raise RuntimeError(
            "A CUDA PyTorch package was installed, but CUDA is still unavailable. "
            "Restart Windows and retry; if it persists, check the NVIDIA driver."
        )
    log(f"READY: PyTorch {after['version']} • CUDA {after['cuda_build']} • {after['device']}")
    return after


def check(include_demucs=False):
    status = {}
    for module, name in (("torch", "PyTorch"), ("whisper", "Whisper"), ("syncalong", "Syncalong")):
        try:
            run([sys.executable, "-c", f"import {module}"])
            status[name] = True
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            status[name] = False
    try:
        run([shutil.which("ffmpeg") or "ffmpeg", "-version"], timeout=15)
        status["FFmpeg"] = True
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        status["FFmpeg"] = False
    if include_demucs:
        try:
            run([sys.executable, "-m", "demucs", "--help"])
            status["Demucs"] = True
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            status["Demucs"] = False
    return status


def setup(log, include_demucs=False):
    if sys.version_info[:2] not in SYNC_PYTHONS:
        raise RuntimeError("Lyrics Sync setup must run in the Studio's own Python. Reopen the Studio launcher.")
    refresh_path()
    studio_runtime.prepare_environment()
    status = check(include_demucs=include_demucs)
    for name, okay in status.items():
        log(f"{name}: {'OK' if okay else 'missing or failed to load'}")
    cuda = torch_cuda_info()
    if nvidia_gpu_available() and cuda.get("installed") and not cuda.get("cuda_build"):
        log("PyTorch: CPU-only build. Use Install/Repair PyTorch GPU to enable CUDA.")
    elif cuda.get("cuda_available"):
        log(f"PyTorch CUDA: {cuda.get('cuda_build')} • {cuda.get('device')}")
    packages = []
    if not status["PyTorch"]:
        packages.append("torch")
    if not status["Whisper"]:
        packages.append("openai-whisper")
    if not status["Syncalong"]:
        packages.append("syncalong[whisper]")
    if include_demucs and not status["Demucs"]:
        packages.append("demucs==4.1.0")
        if status["PyTorch"]:
            version = run([sys.executable, "-c", "import torch; print(torch.__version__)"]).strip().splitlines()[-1]
            packages.append("torch==" + version)
    if packages:
        log(f"Installing required packages in Python {sys.version_info[0]}.{sys.version_info[1]}: "
            + ", ".join(packages) + ". This may take several minutes.")
        if "torch" in packages and studio_runtime.running_in_runtime():
            packages.remove("torch")
            studio_runtime.install_torch(sys.executable, log)  # the CUDA build when there is an NVIDIA card
        if packages:
            studio_runtime.pip_install(sys.executable, packages, log)
    if not status["FFmpeg"]:
        log("Installing FFmpeg into the program folder (runtime\ffmpeg)…")
        studio_runtime.install_ffmpeg(log)
    final = check(include_demucs=include_demucs)
    missing = [name for name, okay in final.items() if not okay]
    if missing:
        raise RuntimeError("Still unavailable: " + ", ".join(missing) + ". See the setup log; installed but broken packages may need repair.")
    log("READY: FFmpeg, Whisper, Syncalong and PyTorch verified.")
    return final
