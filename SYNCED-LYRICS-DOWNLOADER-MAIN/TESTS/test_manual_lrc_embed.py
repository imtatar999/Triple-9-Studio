import ast
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import Mock
from mutagen.id3 import ID3, TIT2, TPE1, USLT


SOURCE = Path(__file__).resolve().parents[1] / 'triplenine_lyrics_downloader.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))


class ManualEmbedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.audio = self.folder / 'Song.mp3'
        tags = ID3()
        tags.add(TIT2(encoding=3, text=['Keep title']))
        tags.add(TPE1(encoding=3, text=['Keep artist']))
        tags.add(USLT(encoding=3, lang='eng', desc='', text='[00:01.00]Old'))
        tags.add(USLT(encoding=3, lang='pol', desc='translation', text='Keep translation'))
        tags.save(self.audio, v2_version=3)
        with self.audio.open('ab') as stream:
            stream.write(b'unchanged-audio-payload')
        self.original = self.audio.read_bytes()
        (self.folder / 'SYNCED LYRICS').mkdir()
        self.lrc = self.folder / 'SYNCED LYRICS' / 'Song.lrc'
        self.lrc.write_text('[00:03.25]Edited line\n[00:08.10]Next line\n', encoding='utf-8')
        self.ns = dict(Path=Path, os=os, shutil=shutil, datetime=datetime,
                       __file__=str(self.folder / 'app.py'), BACKUP_ROOT=self.folder / 'BACKUPS',
                       studio_resync=__import__('studio_resync'), config={'sync_language': 'en'},
                       _SYNCED_LYRICS_CACHE={}, _SYNC_LANG_TO_ID3={'en': 'eng'},
                       _LRC_TIMESTAMP_RE=re.compile(r'(?m)^\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]'))
        names = {'embed_existing_lrc', '_embed_synced_lrc_as_mp3tag_unsyncedlyrics', '_sync_id3_language',
                 '_write_restore_map'}
        self.ns['json'] = __import__('json')
        exec(compile(ast.Module(body=[n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[]), '<embed>', 'exec'), self.ns)

    def test_embeds_edited_timing_preserves_other_tags_payload_and_lrc(self):
        source, backup = self.ns['embed_existing_lrc'](self.audio)
        tags = ID3(self.audio)
        self.assertEqual(tags['USLT::eng'].text, self.lrc.read_text().rstrip())
        self.assertEqual(tags['USLT:translation:pol'].text, 'Keep translation')
        self.assertEqual(str(tags['TIT2']), 'Keep title')
        self.assertEqual(str(tags['TPE1']), 'Keep artist')
        self.assertEqual(tags.version[1], 3)
        self.assertTrue(self.audio.read_bytes().endswith(b'unchanged-audio-payload'))
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(source, self.lrc)
        self.assertTrue(self.lrc.read_text().endswith('\n'))

    def test_missing_plain_and_ambiguous_lrc_do_not_modify_audio(self):
        self.lrc.write_text('No timestamps')
        with self.assertRaises(ValueError):
            self.ns['embed_existing_lrc'](self.audio)
        self.lrc.write_text('[00:01.00]First')
        self.audio.with_suffix('.lrc').write_text('[00:02.00]Different')
        with self.assertRaises(ValueError):
            self.ns['embed_existing_lrc'](self.audio)
        self.assertEqual(self.audio.read_bytes(), self.original)

    def test_failed_tag_write_keeps_original(self):
        self.ns['_embed_synced_lrc_as_mp3tag_unsyncedlyrics'] = Mock(side_effect=OSError('write failed'))
        with self.assertRaises(OSError):
            self.ns['embed_existing_lrc'](self.audio)
        self.assertEqual(self.audio.read_bytes(), self.original)
        self.assertEqual(list(self.folder.glob('.t9-embed-*')), [])


if __name__ == '__main__':
    unittest.main()
