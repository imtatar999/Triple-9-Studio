import sys
from pathlib import Path
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'SYNCED-LYRICS-DOWNLOADER-MAIN'))
import studio_sync_setup as setup


class SetupTests(unittest.TestCase):
    def test_ready_environment_is_not_modified(self):
        ready = dict.fromkeys(('FFmpeg', 'PyTorch', 'Whisper', 'Syncalong'), True)
        with patch.object(setup, 'check', return_value=ready), patch.object(setup, 'refresh_path'), patch.object(setup, 'run') as run:
            self.assertEqual(setup.setup(Mock()), ready)
            run.assert_not_called()

    def test_missing_whisper_installed_into_current_interpreter(self):
        ready = dict.fromkeys(('FFmpeg', 'PyTorch', 'Whisper', 'Syncalong'), True)
        missing = dict(ready, Whisper=False)
        with patch.object(setup, 'check', side_effect=[missing, ready]), patch.object(setup, 'refresh_path'), patch.object(setup, 'run') as run:
            setup.setup(Mock())
            command = run.call_args.args[0]
            self.assertEqual(command[:4], [sys.executable, '-m', 'pip', 'install'])
            self.assertIn('openai-whisper', command)
            self.assertNotIn('torch', command)

    def test_failed_verification_does_not_claim_success(self):
        missing = dict(FFmpeg=True, PyTorch=True, Whisper=False, Syncalong=True)
        with patch.object(setup, 'check', return_value=missing), patch.object(setup, 'refresh_path'), patch.object(setup, 'run'):
            with self.assertRaisesRegex(RuntimeError, 'Still unavailable: Whisper'):
                setup.setup(Mock())

if __name__ == '__main__':
    unittest.main()
