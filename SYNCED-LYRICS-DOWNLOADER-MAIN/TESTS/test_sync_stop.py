import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import Mock, patch

SOURCE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_DIR))
from studio_eta import OperationETA

TREE = ast.parse((SOURCE_DIR / 'triplenine_lyrics_downloader.py').read_text(encoding='utf-8'))


def load_function(name, namespace):
    node = next(n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<sync test>', 'exec'), namespace)
    return namespace[name]


class SyncStopTests(unittest.TestCase):
    def test_resync_backs_up_only_started_song_and_runs_without_prompt(self):
        event = threading.Event()
        calls = []
        def mark(*args):
            calls.append(('mark', args[0]))
        def align(*args):
            calls.append(('align', args[1]))
            self.assertFalse(args[6])
            event.set()
            return Path('saved.lrc'), 2, 2, 100, 'embedded'
        ns = dict(Path=Path, warnings=warnings, stop_after_current=event,
                  win=Mock(), slog=Mock(), sync_status_var=Mock(), messagebox=Mock(),
                  set_running=Mock(), _align_one_with_transcriber=align,
                  _ensure_windowless_standard_streams=Mock(), studio_resync=SimpleNamespace(mark=mark),
                  queue_path='queue.json', __file__=str(SOURCE_DIR / 'app.py'), BACKUP_ROOT=Path('BACKUPS'),
                  _find_plain_txt_for_audio=Mock(), remove_embedded_lyrics=Mock(),
                  get_transcriber=Mock(), release_transcriber=Mock())
        jobs = [dict(audio=f'{i}.mp3', lyrics=f'{i}.txt', output=f'{i}.lrc', resync_lrc=f'{i}.lrc') for i in range(2)]
        with patch.dict(sys.modules, syncalong=SimpleNamespace(Transcriber=Mock())):
            load_function('batch_sync_worker', ns)(jobs, 'turbo', 'en', 55, False, False, True, True)
        self.assertEqual(calls, [('mark', '0.lrc'), ('align', '0.mp3')])

    def test_stop_finishes_current_save_and_does_not_start_next_track(self):
        event = threading.Event()
        messages = []
        saved = []
        def align(*args):
            event.set()  # Click Stop while alignment is still in progress.
            saved.append(args[1])
            return Path('saved.lrc'), 2, 2, 100, 'embedded'
        ns = dict(Path=Path, warnings=warnings, stop_after_current=event,
                  win=Mock(), slog=messages.append, sync_status_var=Mock(),
                  messagebox=Mock(), set_running=Mock(), _align_one_with_transcriber=align,
                  _ensure_windowless_standard_streams=Mock(), OperationETA=OperationETA,
                  get_transcriber=Mock(), release_transcriber=Mock(),
                  LYRICS_MATCH=Mock(suspect_entry=Mock(return_value=None)), refresh_track_badges=Mock())
        worker = load_function('batch_sync_worker', ns)
        jobs = [dict(audio=f'{i}.mp3', lyrics=f'{i}.txt', output=f'{i}.lrc') for i in range(3)]
        with patch.dict(sys.modules, syncalong=SimpleNamespace(Transcriber=Mock())):
            worker(jobs, 'turbo', 'en', 55, True, False, True, True)
        self.assertEqual(saved, ['0.mp3'])
        self.assertIn('   Completed: 1', messages)
        self.assertIn('   Not started: 2', messages)

    def test_windowless_launcher_gets_writable_standard_streams(self):
        streams = []
        ns = dict(os=os, sys=sys, _WINDOWLESS_STANDARD_STREAMS=streams)
        ensure_streams = load_function('_ensure_windowless_standard_streams', ns)
        original_stdout, original_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout = None
            sys.stderr = None
            ensure_streams()
            self.assertTrue(callable(sys.stdout.write))
            self.assertTrue(callable(sys.stderr.write))
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            for stream in streams:
                stream.close()

    def test_demucs_output_does_not_inherit_trailing_space_or_long_title(self):
        ns = dict(Path=Path, os=os, sys=sys, shutil=shutil, subprocess=subprocess)
        separate = load_function('_separate_sync_vocals_subprocess', ns)
        separator = SimpleNamespace(_separation_dirs={})
        with tempfile.TemporaryDirectory() as directory:
            song = Path(directory) / '\u200bHey Wonderland (Sessions) (GB&GR).mp3'
            song.write_bytes(b'original audio')
            def run(command, **kwargs):
                template = command[command.index('--filename') + 1]
                output = Path(command[command.index('-o') + 1]) / 'htdemucs' / template.format(stem='vocals', ext='wav')
                self.assertEqual(command[-1], str(song))
                self.assertEqual(command[1:3], ['-X', 'utf8'])
                self.assertEqual(kwargs['encoding'], 'utf-8')
                self.assertEqual(kwargs['env']['PYTHONUTF8'], '1')
                self.assertEqual(kwargs['env']['PYTHONIOENCODING'], 'utf-8')
                self.assertEqual(output.name, 'vocals.wav')
                self.assertNotIn(song.stem, output.parts)
                output.parent.mkdir(parents=True)
                output.write_bytes(b'vocals')
                return SimpleNamespace(returncode=0)
            with patch.dict(sys.modules, syncalong=SimpleNamespace(vocal_separator=separator)), patch.object(subprocess, 'run', side_effect=run):
                vocals = separate(song, Mock())
            self.assertEqual(song.read_bytes(), b'original audio')
            self.assertEqual(vocals.read_bytes(), b'vocals')
            shutil.rmtree(separator._separation_dirs[vocals])

    def test_in_process_demucs_failure_falls_back_to_command_line_once(self):
        calls = []
        def in_process(path, progress):
            calls.append("in-process")
            raise RuntimeError("CUDA out of memory")
        def command_line(path, progress):
            calls.append("command line")
            return Path("vocals.wav")
        ns = dict(_DEMUCS_MODEL={"separator": None, "failed": ""},
                  _separate_sync_vocals_in_process=in_process,
                  _separate_sync_vocals_subprocess=command_line)
        separate = load_function('_separate_sync_vocals', ns)
        messages = []
        self.assertEqual(separate("a.mp3", messages.append), Path("vocals.wav"))
        self.assertEqual(separate("b.mp3", messages.append), Path("vocals.wav"))
        self.assertEqual(calls, ["in-process", "command line", "command line"])
        self.assertIn("CUDA out of memory", ns["_DEMUCS_MODEL"]["failed"])
        self.assertTrue(any("command line" in m for m in messages))


if __name__ == '__main__':
    unittest.main()
