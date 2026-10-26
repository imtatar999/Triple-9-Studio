import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_waveform as w


class _FakeWinmm:
    """A sound card whose position counter can misbehave (never reset, bytes instead of samples)."""

    def __init__(self, player, counter, unit=2):
        self.player, self.counter, self.unit = player, counter, unit

    def waveOutGetPosition(self, _handle, pointer, _size):
        when = pointer._obj
        when.wType, when.sample = self.unit, int(self.counter())
        return 0


class PlayerPositionTests(unittest.TestCase):
    """The playhead follows the device, but a confused counter can never run it past the song."""

    def make(self, counter, unit=2, start=150.0, clock_elapsed=1.0):
        import ctypes
        player = w.WaveOutPlayer.__new__(w.WaveOutPlayer)

        class MMTIME(ctypes.Structure):
            _fields_ = [("wType", ctypes.c_uint), ("sample", ctypes.c_uint), ("_pad", ctypes.c_uint)]

        player._MMTIME, player._ctypes = MMTIME, ctypes
        player._winmm = _FakeWinmm(player, counter, unit)
        player._handle = None
        player.rate, player.block, player._size = 44100, 4, int(193.65 * 44100) * 4
        player._start, player.playing, player._header = start, True, None
        player._clock = w.time.perf_counter() - clock_elapsed
        player._base = player._device_seconds() - 1.0 if unit == 2 else 0.0
        return player

    def test_a_counter_that_is_not_reset_by_pause_is_measured_from_play(self):
        # The driver still counts the 120 s played before the last pause.
        player = self.make(lambda: (120.0 + 1.0) * 44100)
        self.assertAlmostEqual(player.position(), 151.0, places=2)

    def test_bytes_instead_of_samples_are_converted(self):
        player = self.make(lambda: 1.0 * 44100 * 4, unit=4)  # TIME_BYTES: 1 s of 16-bit stereo
        self.assertAlmostEqual(player._device_seconds(), 1.0, places=3)

    def test_a_runaway_counter_falls_back_to_the_clock_and_never_passes_the_end(self):
        runaway = self.make(lambda: (1.0 + 30.0) * 44100)  # 30 s ahead of what really played
        self.assertAlmostEqual(runaway.position(), 151.0, delta=0.05)  # the clock: 1 s after Play
        late = self.make(lambda: 0, start=193.5, clock_elapsed=5.0)
        self.assertLessEqual(late.position(), 193.65)


class WaveformTests(unittest.TestCase):
    def test_active_line_is_the_latest_start_at_or_before_the_playhead(self):
        times = {10: 5.0, 11: 7.5, 12: 9.0}
        self.assertIsNone(w.active_line(times, 4.99))
        self.assertEqual(w.active_line(times, 5.0), 10)
        self.assertEqual(w.active_line(times, 8.0), 11)
        self.assertEqual(w.active_line(times, 99.0), 12)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_decode_gives_matching_duration_and_quiet_then_loud_peaks(self):
        with tempfile.TemporaryDirectory() as d:
            mp3 = Path(d) / "tone ż.mp3"
            # 1 s of silence followed by 2 s of a loud tone.
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=1",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            "-filter_complex", "[0][1]concat=n=2:v=0:a=1", str(mp3)], check=True)
            wav, peaks, duration = w.decode_audio(mp3)
            try:
                self.assertAlmostEqual(duration, 3.0, delta=0.1)
                self.assertAlmostEqual(len(peaks), duration * w.PEAKS_PER_SECOND, delta=2)
                self.assertLess(peaks[: w.PEAKS_PER_SECOND // 2].max(), 0.05)   # silence
                self.assertGreater(peaks[2 * w.PEAKS_PER_SECOND:].max(), 0.9)  # tone
            finally:
                os.remove(wav)


if __name__ == "__main__":
    unittest.main()
