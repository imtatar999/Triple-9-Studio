import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_audio_renames as renames


class AudioRenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.old = self.folder / 'Song.mp3'
        self.old.write_bytes(b'audio bytes')
        self.new = self.folder / 'Song (new title).mp3'

    def rename(self):
        before = renames.snapshot(self.folder)
        self.old.rename(self.new)
        after = renames.snapshot(self.folder)
        return before, after

    def test_detects_same_file_and_renames_exact_sidecars_only(self):
        for folder in (self.folder, self.folder / 'LYRICS', self.folder / 'SYNCED LYRICS'):
            folder.mkdir(exist_ok=True)
            for ext in ('.txt', '.lrc'):
                (folder / ('Song' + ext)).write_bytes(b'lyrics unchanged')
        unrelated = self.folder / 'Song live.txt'
        unrelated.write_text('leave me')
        before, after = self.rename()
        self.assertEqual(renames.find_renames(before, after), [(self.old, self.new)])
        pairs = renames.sidecar_pairs(self.old, self.new)
        self.assertEqual(len(pairs), 6)
        renames.rename_sidecars(self.old, self.new, after[str(self.new)], pairs)
        self.assertEqual(self.new.read_bytes(), b'audio bytes')
        self.assertTrue(unrelated.exists())
        for source, target in pairs:
            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), b'lyrics unchanged')

    def test_does_not_match_titles_or_duplicate_identities(self):
        before, after = self.rename()
        after[str(self.new)][1] += 1
        self.assertEqual(renames.find_renames(before, after), [])
        stamp = before[str(self.old)]
        self.assertEqual(renames.find_renames(before, {str(self.new): stamp, 'duplicate.mp3': stamp}), [])

    def test_existing_destination_leaves_every_file_untouched(self):
        self.old.with_suffix('.txt').write_text('plain')
        self.old.with_suffix('.lrc').write_text('timed')
        self.new.with_suffix('.txt').write_text('existing')
        _, after = self.rename()
        with self.assertRaises(FileExistsError):
            renames.rename_sidecars(self.old, self.new, after[str(self.new)], renames.sidecar_pairs(self.old, self.new))
        self.assertEqual(self.new.with_suffix('.txt').read_text(), 'existing')
        self.assertTrue(self.old.with_suffix('.lrc').exists())

    def test_rolls_back_first_sidecar_when_second_fails(self):
        self.old.with_suffix('.txt').write_text('plain')
        self.old.with_suffix('.lrc').write_text('timed')
        _, after = self.rename()
        pairs = renames.sidecar_pairs(self.old, self.new)
        original = Path.rename
        def fail_second(path, target):
            if path == pairs[1][0]:
                raise OSError('locked file')
            return original(path, target)
        with patch.object(Path, 'rename', fail_second), self.assertRaises(OSError):
            renames.rename_sidecars(self.old, self.new, after[str(self.new)], pairs)
        for source, target in pairs:
            self.assertTrue(source.exists())
            self.assertFalse(target.exists())

    def test_revalidates_audio_before_applying(self):
        self.old.with_suffix('.txt').write_text('plain')
        _, after = self.rename()
        self.new.write_bytes(b'changed audio')
        with self.assertRaises(RuntimeError):
            renames.rename_sidecars(self.old, self.new, after[str(self.new)], renames.sidecar_pairs(self.old, self.new))
        self.assertTrue(self.old.with_suffix('.txt').exists())

    def test_restart_detects_rename_and_decline_is_remembered(self):
        self.old.with_suffix('.txt').write_text('plain')
        before, after = self.rename()
        root, ask = Mock(), Mock(return_value=False)
        folder = str(self.folder.resolve())
        monitor = renames.RenameMonitor(root, lambda: folder, lambda: False, ask, Mock(), Mock(), self.folder / 'state')
        monitor.state_dir.mkdir()
        monitor.state_path(folder).write_text(json.dumps(before))
        monitor.scan(folder)
        monitor.scanning = True
        monitor.tick()
        ask.assert_called_once()
        self.assertTrue(self.old.with_suffix('.txt').exists())
        self.assertEqual(json.loads(monitor.state_path(folder).read_text()), after)
        monitor.scan(folder)
        monitor.scanning = True
        monitor.tick()
        ask.assert_called_once()

    def test_case_only_rename(self):
        stamp = [1, 2, 3, 4, 5]
        old, new = self.folder / 'Song.mp3', self.folder / 'SONG.mp3'
        self.assertEqual(renames.find_renames({str(old): stamp}, {str(new): stamp}), [(old, new)])

    def test_monitor_accepts_and_refreshes_only_after_confirmation(self):
        self.old.with_suffix('.txt').write_text('plain')
        before, after = self.rename()
        folder = str(self.folder.resolve())
        ask, refresh = Mock(return_value=True), Mock()
        busy = Mock(return_value=True)
        monitor = renames.RenameMonitor(Mock(), lambda: folder, busy, ask, Mock(), refresh, self.folder / 'state')
        monitor.results.put((folder, after, renames.find_renames(before, after), None))
        monitor.scanning = True
        monitor.tick()
        ask.assert_not_called()
        busy.return_value = False
        monitor.tick()
        ask.assert_called_once()
        refresh.assert_called_once()
        self.assertEqual(self.new.with_suffix('.txt').read_text(), 'plain')


if __name__ == '__main__':
    unittest.main()
