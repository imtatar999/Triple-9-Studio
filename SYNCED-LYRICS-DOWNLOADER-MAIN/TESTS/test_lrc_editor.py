import sys
from pathlib import Path
from unittest.mock import patch
from mutagen.id3 import ID3, SYLT, TXXX
from test_manual_lrc_embed import ManualEmbedTests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_lrc_editor as editor


class LrcEditorTests(ManualEmbedTests):
    def save(self, text='[00:09.50]New corrected line\n'):
        return editor.save_and_embed(self.audio, self.lrc, text, self.lrc.read_bytes(),
            editor.stamp(self.audio), self.folder / 'backups',
            lambda path, value: self.ns['_embed_synced_lrc_as_mp3tag_unsyncedlyrics'](path, value, 'en'))

    def test_save_replaces_all_old_timed_variants_and_keeps_translation(self):
        tags = ID3(self.audio)
        tags.add(SYLT(encoding=3, lang='eng', format=2, type=1, text=[('old', 1000)]))
        tags.add(TXXX(encoding=3, desc='LYRICS', text=['[00:01.00]old']))
        tags.save(self.audio, v2_version=3)
        original = self.audio.read_bytes()
        backup = self.save()
        tags = ID3(self.audio)
        self.assertFalse(tags.getall('SYLT'))
        self.assertNotIn('TXXX:LYRICS', tags)
        self.assertEqual(tags['USLT::eng'].text, self.lrc.read_text().rstrip())
        self.assertEqual(tags['USLT:translation:pol'].text, 'Keep translation')
        self.assertEqual((backup / self.audio.name).read_bytes(), original)
        self.assertTrue(self.audio.read_bytes().endswith(b'unchanged-audio-payload'))

    def test_failed_lrc_commit_restores_audio_and_leaves_original_lrc(self):
        old_lrc = self.lrc.read_bytes()
        original_replace = editor.os.replace
        def fail(source, target):
            if Path(target) == self.lrc:
                raise OSError('LRC locked')
            return original_replace(source, target)
        with patch.object(editor.os, 'replace', fail), self.assertRaises(OSError):
            self.save()
        self.assertEqual(self.audio.read_bytes(), self.original)
        self.assertEqual(self.lrc.read_bytes(), old_lrc)

    def test_stale_lrc_and_invalid_text_are_rejected(self):
        with self.assertRaises(ValueError):
            self.save('plain text')
        with self.assertRaises(RuntimeError):
            editor.save_and_embed(self.audio, self.lrc, '[00:01.00]line', b'stale',
                editor.stamp(self.audio), self.folder / 'backups', lambda *args: None)
        self.assertEqual(self.audio.read_bytes(), self.original)
