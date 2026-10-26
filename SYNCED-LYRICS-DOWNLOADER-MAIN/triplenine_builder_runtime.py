"""An isolated transcription process whose descendants share its lifetime."""
import ctypes
import multiprocessing
import os


class WindowsProcessJob:
    """Windows closes all job members if the owning GUI exits, even unexpectedly."""
    def __init__(self):
        from ctypes import wintypes as wt

        class Basic(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                        ("Flags", wt.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcesses", wt.DWORD),
                        ("Affinity", ctypes.c_size_t), ("Priority", wt.DWORD), ("Scheduling", wt.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class Limits(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", ctypes.c_size_t),
                        ("JobMemory", ctypes.c_size_t), ("PeakProcessMemory", ctypes.c_size_t),
                        ("PeakJobMemory", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wt.LPCWSTR], wt.HANDLE),
            "SetInformationJobObject": ([wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD], wt.BOOL),
            "OpenProcess": ([wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
            "AssignProcessToJobObject": ([wt.HANDLE, wt.HANDLE], wt.BOOL),
            "CloseHandle": ([wt.HANDLE], wt.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Limits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, pid):
        process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _run_task(target, events, gate, args):
    # No subprocess/model may start before containment has been installed.
    if not gate.wait(15):
        return
    if os.name != "nt":
        os.setsid()
    target(events, *args)


class TranscriptionProcess:
    def __init__(self, target, args):
        context = multiprocessing.get_context("spawn")
        self.events = context.Queue()
        self.gate = context.Event()
        self.process = context.Process(target=_run_task, args=(target, self.events, self.gate, args))
        self.job = None
        try:
            if os.name == "nt":
                self.job = WindowsProcessJob()
            self.process.start()
            if self.job:
                self.job.assign(self.process.pid)
            self.gate.set()
        except Exception:
            self.close()
            raise

    def close(self):
        if self.job:
            self.job.close()
            self.job = None
        if self.process.pid:
            if os.name != "nt" and self.process.is_alive():
                import signal
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    self.process.terminate()
            self.process.join(timeout=0.5)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=0.5)
        self.events.close()
