from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_lyrics_repair as repair


class LyricsRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.audio = self.root / 'Red Moonlight (DRFL).mp3'
        self.audio.write_bytes(b'audio')
        (self.root / 'LYRICS').mkdir()
        (self.root / 'SYNCED LYRICS').mkdir()
        self.txt = self.root / 'LYRICS' / 'darkside.txt'
        self.lrc = self.root / 'SYNCED LYRICS' / 'darkside.lrc'
        self.txt.write_text('Example line about red moonlight, test lyrics only.')
        self.lrc.write_text('[00:01.00]Example line about red moonlight, test lyrics only.')

    def scan(self):
        return repair.scan(self.root, threading.Event())

    def test_past_rename_with_both_missing_formats(self):
        found, missing = self.scan()
        self.assertEqual(missing, [(self.audio, ('.txt', '.lrc'))])
        self.assertEqual(len(found), 1)
        before = [p.read_bytes() for p, _ in found[0].pairs]
        repair.apply(found[0])
        self.assertEqual([p.read_bytes() for _, p in found[0].pairs], before)
        self.assertEqual(self.scan(), ([], []))

    def test_only_missing_lrc_is_changed(self):
        own = self.txt.with_name(self.audio.stem + '.txt')
        own.write_text('existing plain lyrics')
        found, _ = self.scan()
        self.assertEqual(found[0].missing, ('.lrc',))
        self.assertEqual(len(found[0].pairs), 1)
        repair.apply(found[0])
        self.assertTrue(self.txt.exists())
        self.assertEqual(own.read_text(), 'existing plain lyrics')

    def test_existing_old_audio_is_not_an_orphan(self):
        (self.root / 'darkside.flac').write_bytes(b'other audio')
        found, missing = self.scan()
        self.assertEqual(found, [])
        self.assertTrue(missing)

    def test_unrelated_orphan_is_not_proposed(self):
        self.txt.write_text('unrelated text')
        self.lrc.write_text('[00:01.00]unrelated text')
        self.assertEqual(self.scan()[0], [])

    def test_matches_existing_other_format_contents(self):
        lyrics = 'A distinct sample of lyrics without the current title in its text. ' * 3
        self.txt.with_name(self.audio.stem + '.txt').write_text(lyrics)
        self.txt.write_text(lyrics)
        self.lrc.write_text('[00:01.00]' + lyrics)
        found, _ = self.scan()
        self.assertIn('Same lyrics as the existing sidecar / embedded lyrics', found[0].reasons)

    def test_conflict_and_stale_content_leave_sources_untouched(self):
        candidate = self.scan()[0][0]
        target = candidate.pairs[0][1]
        target.write_text('keep')
        with self.assertRaises(FileExistsError):
            repair.apply(candidate)
        self.assertTrue(self.txt.exists())
        self.assertTrue(self.lrc.exists())
        target.unlink()
        self.txt.write_text('changed')
        with self.assertRaises(RuntimeError):
            repair.apply(candidate)
        self.assertTrue(self.lrc.exists())

    def test_ambiguous_phrase_only_candidates_are_suppressed(self):
        self.txt.with_name('another name.txt').write_text(self.txt.read_text())
        found, _ = self.scan()
        self.assertEqual(len(found), 0)

    def test_rollback(self):
        candidate = self.scan()[0][0]
        original = Path.rename
        def fail(path, target):
            if path == candidate.pairs[1][0]:
                raise OSError('locked')
            return original(path, target)
        with patch.object(Path, 'rename', fail), self.assertRaises(OSError):
            repair.apply(candidate)
        self.assertTrue(self.txt.exists())
        self.assertTrue(self.lrc.exists())

    def test_lets_go_uses_normalized_name_not_generic_phrase(self):
        self.audio.unlink()
        self.audio = self.root / "Let's Go.mp3"
        self.audio.write_bytes(b'audio')
        self.txt.write_text("Let's go appears in many songs.")
        self.lrc.write_text("[00:01.00]Let's go appears in many songs.")
        correct = self.txt.with_name('Let_s Go.txt')
        correct.write_text('Completely distinct lyrics for the correct song.')
        found, _ = self.scan()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].pairs[0][0], correct)
        self.assertEqual(found[0].score, 90)

    def test_parenthetical_alternative_title_matches_current_filename(self):
        self.audio.unlink()
        self.audio = self.root / 'Show Stopper (AI).mp3'
        self.audio.write_bytes(b'audio')
        old_txt = self.txt.with_name('Two Cups (Showstopper) (AI).txt')
        old_lrc = self.lrc.with_name('Two Cups (Showstopper) (AI).lrc')
        self.txt.rename(old_txt)
        self.lrc.rename(old_lrc)

        found, missing = self.scan()

        self.assertEqual(missing, [(self.audio, ('.txt', '.lrc'))])
        self.assertEqual(len(found), 1)
        self.assertIn('Alternative title in parentheses matches the current filename', found[0].reasons)
        self.assertEqual({path.name for path, _ in found[0].pairs}, {old_txt.name, old_lrc.name})

    def test_alias_with_existing_current_lrc_is_reviewable_and_backed_up(self):
        self.audio.unlink()
        self.audio = self.root / 'Show Stopper (AI).mp3'
        self.audio.write_bytes(b'audio')
        old = self.lrc.with_name('Two Cups (Showstopper) (AI).lrc')
        self.lrc.rename(old)
        repeated = 'Example line about red moonlight and another distinctive lyric. ' * 3
        old.write_text('[00:01.00]' + repeated)
        current = old.with_name(self.audio.stem + '.lrc')
        current.write_text('[00:02.00]' + repeated)
        self.txt.with_name(self.audio.stem + '.txt').write_text(self.txt.read_text())

        found, missing = self.scan()

        self.assertEqual(missing, [])
        candidate = next(c for c in found if c.pairs[0][0] == old)
        self.assertIsNotNone(candidate.target_hashes[0])
        backup = repair.apply(candidate, backup_root=self.root / 'backups')
        self.assertFalse(old.exists())
        self.assertEqual(current.read_text(), '[00:01.00]' + repeated)
        self.assertTrue(backup.is_dir())

    def test_batch_two_songs_and_conflicting_selections(self):
        first = self.scan()[0][0]
        second_audio = self.root / 'Other Song.mp3'
        second_audio.write_bytes(b'other audio')
        source = self.root / 'LYRICS' / 'Other_Song.txt'
        source.write_text('Different lyrics')
        second = next(c for c in self.scan()[0] if c.audio == second_audio)
        with self.assertRaises(RuntimeError):
            repair.apply_batch([first, first])
        self.assertTrue(self.txt.exists())
        repair.apply_batch([first, second])
        self.assertTrue(source.with_name('Other Song.txt').exists())
        self.assertFalse(self.txt.exists())

    def test_batch_late_failure_rolls_back_all_songs(self):
        first = self.scan()[0][0]
        second_audio = self.root / 'Other Song.mp3'
        second_audio.write_bytes(b'other audio')
        source = self.root / 'LYRICS' / 'Other_Song.txt'
        source.write_text('Different lyrics')
        second = next(c for c in self.scan()[0] if c.audio == second_audio)
        original = Path.rename
        def fail(path, target):
            if path == source:
                raise OSError('locked')
            return original(path, target)
        with patch.object(Path, 'rename', fail), self.assertRaises(OSError):
            repair.apply_batch([first, second])
        self.assertTrue(self.txt.exists())
        self.assertTrue(self.lrc.exists())


if __name__ == '__main__':
    unittest.main()
