import ctypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "SYNCED-LYRICS-DOWNLOADER-MAIN"))
sys.path.insert(0, str(ROOT / "MAINTENANCE"))
from triplenine_builder_runtime import TranscriptionProcess
import Triplenine_Hybrid_Sessions_Lyrics_Builder as builder


def sleeping_tree(events):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    events.put(("pids", [os.getpid(), child.pid]))
    time.sleep(120)


def completed_task(events):
    events.put(("done", "finished"))


def alive(pid):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    api.OpenProcess.restype = ctypes.c_void_p
    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = api.OpenProcess(0x100000, False, pid)
    if not handle:
        return False
    try:
        return api.WaitForSingleObject(handle, 0) == 258
    finally:
        api.CloseHandle(handle)


@unittest.skipUnless(os.name == "nt", "Windows process containment")
class BuilderRuntimeTests(unittest.TestCase):
    def assert_stopped(self, pids):
        deadline = time.monotonic() + 5
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(any(alive(pid) for pid in pids), pids)

    def test_stop_kills_worker_and_its_descendant_and_allows_restart(self):
        task = TranscriptionProcess(sleeping_tree, ())
        try:
            kind, pids = task.events.get(timeout=15)
            self.assertEqual(kind, "pids")
            self.assertTrue(all(alive(pid) for pid in pids))
        finally:
            task.close()
        self.assert_stopped(pids)
        restarted = TranscriptionProcess(completed_task, ())
        try:
            self.assertEqual(restarted.events.get(timeout=15), ("done", "finished"))
        finally:
            restarted.close()

    def test_owner_exit_kills_transcription_without_stop_callback(self):
        controller = subprocess.Popen([sys.executable, str(Path(__file__)), "--controller"],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        received = queue.Queue()
        threading.Thread(target=lambda: received.put(controller.stdout.readline()), daemon=True).start()
        try:
            line = received.get(timeout=20)
            self.assertTrue(line, "Controller exited before reporting child processes")
            pids = json.loads(line)
            controller.terminate()  # Simulate closing/crashing the job-owning GUI.
            controller.wait(timeout=5)
            self.assert_stopped(pids)
        finally:
            if controller.poll() is None:
                controller.terminate()
            controller.wait(timeout=5)
            controller.stdout.close()
            controller.stderr.close()

    def test_stop_resets_controls_without_clearing_previous_result(self):
        app = builder.App.__new__(builder.App)
        task = Mock()
        app.transcription, app.running = task, True
        app.status_var, app.start_btn, app.stop_btn = Mock(), Mock(), Mock()
        app.q = queue.Queue()
        app.last_result = "previous finished text"
        app.stop()
        task.close.assert_called_once()
        self.assertIsNone(app.transcription)
        self.assertFalse(app.running)
        self.assertEqual(app.last_result, "previous finished text")
        app.status_var.set.assert_called_with("Stopped")
        app.stop_btn.configure.assert_called_with(state="disabled")
        app.stop()  # Idempotent, including when called by on_close afterward.
        task.close.assert_called_once()

    def test_worker_sends_export_metadata_before_terminal_event(self):
        events = queue.Queue()
        def fake_worker(app, *args):
            app.last_engine_mode = "Hybrid + Xingyu"
            app.last_xingyu_device = "CUDA"
            app.last_xingyu_preset = "full-recognition"
            app.last_elapsed_seconds = 4.5
            app.q.put(("result", "finished lyrics"))
            app.q.put(("done", "Done"))
        with patch.object(builder.App, "worker", fake_worker):
            builder._transcribe_in_process(events, ())
        self.assertEqual(events.get(), ("result", "finished lyrics"))
        kind, metadata = events.get()
        self.assertEqual(kind, "metadata")
        self.assertEqual(metadata["last_xingyu_device"], "CUDA")
        self.assertEqual(metadata["last_elapsed_seconds"], 4.5)
        self.assertEqual(events.get(), ("done", "Done"))

    def test_fixed_engine_runs_xingyu_with_or_without_reference_even_for_old_settings(self):
        for reference in ('', '[Verse]\nThese are the words of the song'):
            app = builder.App.__new__(builder.App)
            app.q = queue.Queue()
            # Stop at the engine boundary: this test never loads models or audio.
            with patch.dict(sys.modules, {'syncalong': Mock()}), patch.object(
                    builder, 'run_xingyu_candidate', side_effect=RuntimeError('engine boundary reached')) as run:
                app.worker('song.mp3', reference, 'turbo', 'en', 0.46,
                           'Current V5', 'full-recognition', 'CPU', time.perf_counter())
                run.assert_called_once()
                self.assertEqual(app.last_engine_mode, 'TripleNine Engine')


if __name__ == "__main__":
    if "--controller" in sys.argv:
        task = TranscriptionProcess(sleeping_tree, ())
        print(json.dumps(task.events.get(timeout=15)[1]), flush=True)
        time.sleep(120)
    else:
        unittest.main()
