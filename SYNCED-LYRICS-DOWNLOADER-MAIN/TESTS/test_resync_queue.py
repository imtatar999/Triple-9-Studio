import tempfile
import unittest
from pathlib import Path
import studio_resync as r

class ResyncTests(unittest.TestCase):
    def test_incremental_scan(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); song=p/'x.lrc'; song.write_text('first')
            calls=[]
            def detect(text):
                calls.append(text)
                return []
            r.scan(p,p/'cache.json',detect)
            self.assertEqual(r.scan(p,p/'cache.json',detect)['reused'],1)
            song.write_text('changed text')
            r.scan(p,p/'cache.json',detect)
            self.assertEqual(calls,['first','changed text'])

    def test_backup_queue_and_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); audio=p/'song.mp3'; txt=p/'song.txt'; lrc=p/'song.lrc'
            audio.write_bytes(b'audio'); txt.write_text('plain'); lrc.write_text('timed')
            queue=p/'queue.json'
            def broken(path):
                Path(path).write_bytes(b'changed')
                raise ValueError('test failure')
            with self.assertRaises(ValueError):
                r.mark(lrc,queue,p/'backups',lambda _:str(txt),broken)
            self.assertEqual(audio.read_bytes(),b'audio')
            self.assertTrue(lrc.exists())
            r.mark(lrc,queue,p/'backups',lambda _:str(txt),lambda _:None)
            self.assertFalse(lrc.exists())
            self.assertEqual(txt.read_text(),'plain')
            self.assertEqual(len(r.read(queue)),1)
            r.complete(queue,audio)
            self.assertEqual(r.read(queue),{})

    def test_clear_sync_after_new_lyrics(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); audio=p/'song.mp3'; txt=p/'LYRICS'/'song.txt'
            synced=p/'SYNCED LYRICS'/'song.lrc'; txt.parent.mkdir(); synced.parent.mkdir()
            audio.write_bytes(b'audio'); txt.write_text('new plain'); synced.write_text('old timed')
            queue=p/'queue.json'
            def broken(path):
                Path(path).write_bytes(b'changed')
                raise ValueError('test failure')
            with self.assertRaises(ValueError):
                r.clear_sync(audio,txt,queue,p/'backups',broken)
            self.assertEqual(audio.read_bytes(),b'audio')
            self.assertTrue(synced.exists())
            backup,removed,embedded=r.clear_sync(audio,txt,queue,p/'backups',lambda _:True)
            self.assertFalse(synced.exists())
            self.assertEqual(removed,[synced])
            self.assertTrue(embedded)
            self.assertEqual((backup/'1'/'song.lrc').read_text(),'old timed')
            self.assertEqual(txt.read_text(),'new plain')
            self.assertEqual(list(r.read(queue).values())[0]['lyrics'],str(txt))
            self.assertEqual(r.clear_sync(audio,txt,queue,p/'backups',lambda _:False),(None,[],False))

    def test_ignored_warnings_hidden_until_timings_change(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); ignored=p/'ignored.json'
            warning=dict(path=str(p/'a.lrc'),kind='dense_lines',timestamp='00:10.00',count=4,first_text='hey')
            other=dict(warning,path=str(p/'b.lrc'))
            r.ignore([warning],ignored)
            visible,count=r.split_ignored([warning,other],ignored)
            self.assertEqual((visible,count),([other],1))
            resynced=dict(warning,timestamp='00:11.50')
            self.assertEqual(r.split_ignored([resynced],ignored),([resynced],0))

    def test_fix_history_starts_from_backups_and_lists_newest_first(self):
        import json, os, time
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); backups=p/'BACKUPS'; history=p/'state'/'fix_history.json'
            old_fix=backups/'fragment_abc'; old_fix.mkdir(parents=True)
            (old_fix/'Old Song.lrc').write_text('x')
            old_resync=backups/'resync_def'; (old_resync/'0').mkdir(parents=True)
            (old_resync/'restore.json').write_text(json.dumps({str(p/'music'/'Other.mp3'): 'x',
                                                               str(p/'music'/'SYNCED LYRICS'/'Other.lrc'): 'y'}))
            (backups/'BUILDER_X').mkdir()
            past=time.time()-3600
            os.utime(old_fix,(past,past)); os.utime(old_resync,(past-10,past-10))
            r.record_history(history,'Fix fragment — manual timing',p/'music'/'SYNCED LYRICS'/'New.lrc','2 line(s) retimed',backups)
            items=r.history(history,backups)
            self.assertEqual([Path(i['path']).name for i in items],['New.lrc','Old Song.lrc','Other.lrc'])
            self.assertEqual(items[0]['detail'],'2 line(s) retimed')
            self.assertIn('from backup',items[1]['action'])
            self.assertEqual(r.song_key(items[2]['path']),'other')
            r.record_history(history,'Resync 2 Pass',p/'x.lrc')
            self.assertEqual(len(r.history(history,backups)),4)  # past events are not added twice
            removed=r.remove_history(history,{'old song','x'},backups)
            self.assertEqual(removed,2)
            self.assertEqual([Path(i['path']).name for i in r.history(history,backups)],['New.lrc','Other.lrc'])
            self.assertTrue((backups/'fragment_abc'/'Old Song.lrc').exists())  # backups are untouched

    def test_old_scan_rules_invalidate_cached_results(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); song = p / 'x.lrc'; song.write_text('text')
            cache = p / 'cache.json'
            r.scan(p, cache, lambda _: [])
            data = r.read(cache)
            for record in data.values():
                record.pop('version')
            r.write(cache, data)
            calls = []
            result = r.scan(p, cache, lambda text: calls.append(text) or [])
            self.assertEqual(result['reused'], 0)
            self.assertEqual(calls, ['text'])

    def test_fragment_repair_is_backed_up(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lrc = root / 'song.lrc'
            lrc.write_text('[00:01.00] old', encoding='utf-8')
            saved = r.save_fragment_repair(lrc, '[00:02.00] new', root / 'backups')
            self.assertEqual(saved.read_text(encoding='utf-8'), '[00:01.00] old')
            self.assertEqual(lrc.read_text(encoding='utf-8'), '[00:02.00] new')

if __name__=='__main__': unittest.main()
