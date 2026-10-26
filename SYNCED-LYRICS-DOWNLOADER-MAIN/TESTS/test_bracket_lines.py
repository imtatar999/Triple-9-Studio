import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_resync as r

TXT = """[Chorus: Quavo]
Hold up, hold up (Yeah)
Get right witcha (I'ma get right witcha)
(Yeah) Bad bitches, fuck 'em then dismiss 'em (Bad, woo)

[Post-Chorus: Quavo & Offset]
(Yeah) Get right witcha, I'ma get right witcha (Rah, woo)
(Chorus)

[Chorus: Quavo]
(Yeah) Bad bitches, fuck 'em then dismiss 'em (Bad, woo)
(M-M-M-Murda)
"""


class BracketLineTests(unittest.TestCase):
    def test_headers_and_sung_bracket_lines(self):
        # Only brackets on the line: left out of the LRC (headers and lone ad-libs alike).
        for header in ("[Chorus: Quavo]", "(Chorus)", "(Post-Chorus: Quavo)", "(Bridge 2)", "[Intro]",
                       "(M-M-M-Murda)", "(Woo)", "(Cha-ching, cha-ching, cha-ching, brrt, hello?)", "(Woo) (Yeah)"):
            self.assertTrue(r.is_section_header(header), header)
        # Sung text outside the brackets: a lyric.
        for lyric in ("(Yeah) Get right witcha (Rah, woo)", "(Cha-ching, cha-ching,) I'm gon' drop it in the mix",
                      "Fuck nigga on my phone finna phone in (Hold on, hm)",
                      "(Feel like Cosby with that molly, I'm gon' drop it in the mix"):
            self.assertFalse(r.is_section_header(lyric), lyric)

    def test_allowed_bracket_lines_still_skip_section_headers(self):
        # "Allow sync of lines only in brackets": only real section headers stay out.
        for header in ("[Chorus: Quavo]", "[Intro]", "(Chorus)", "(Post-Chorus: Quavo)", "(Verse 2)"):
            self.assertTrue(r.is_section_name(header), header)
        for lyric in ("(M-M-M-Murda)", "(Woo)", "(Cha-ching, cha-ching, cha-ching, brrt, hello?)", "(Woo) (Yeah)",
                      "(Breakfast at noon)", "(Partying)"):
            self.assertFalse(r.is_section_name(lyric), lyric)

    def test_missing_lines_are_counted_per_occurrence(self):
        lrc = ("[00:01.00] Hold up, hold up (Yeah)\n[00:02.00] Get right witcha (I'ma get right witcha)\n\n"
               "[00:05.00] (Yeah) Bad bitches, fuck ’em then dismiss ’em (Bad, woo)\n")
        missing = r.missing_bracket_lines(TXT, lrc)
        self.assertEqual(sorted(missing), sorted([
            "(Yeah) Bad bitches, fuck 'em then dismiss 'em (Bad, woo)",  # in the TXT twice, the LRC has it once
            "(Yeah) Get right witcha, I'ma get right witcha (Rah, woo)",
        ]))  # "(M-M-M-Murda)" is only brackets: it is meant to stay out of the LRC

    def test_complete_lrc_has_nothing_missing(self):
        lrc = "\n".join(f"[00:{i:02d}.00] {line}" for i, line in enumerate(TXT.splitlines()) if line)
        self.assertEqual(r.missing_bracket_lines(TXT, lrc), [])


if __name__ == "__main__":
    unittest.main()
