import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "SYNCED-LYRICS-DOWNLOADER-MAIN"))
sys.path.insert(0, str(ROOT / "MAINTENANCE"))
import Triplenine_Hybrid_Sessions_Lyrics_Builder as builder
import triplenine_reference_queue as queue
from validate_builder_replay import load_trace


class BuilderSectionTests(unittest.TestCase):
    def test_newgirl_continuation_uses_verses_and_local_word_checks(self):
        fixture = ROOT / 'BUILDER_REPLAY' / 'REGRESSION_INPUTS'
        ref = builder.parse_reference(queue.reference_text(fixture / 'newgirl_reference.txt'))
        w, x = load_trace(fixture / 'newgirl_6min09sec.txt')
        output, *_ = builder.hybridize(ref, w, xingyu_lines=x)
        blocks = builder._reference_section_blocks(builder.parse_reference('\n'.join(output)))
        self.assertFalse(any(b['kind'] == 'outro' for b in blocks))
        tail = blocks[-2]['lines']
        self.assertTrue(any("I do Xan's, I do lean" in s for s in tail))
        self.assertFalse(any('small max' in s or 'Zins' in s for s in tail))
        self.assertTrue(any('big mac' in s.lower() for s in tail))
        self.assertTrue(all(len(s.split()) <= 16 for s in tail))

    def test_word_check_keeps_unsupported_variants_and_rap_forms(self):
        units = [{'text': 'we work all day until the sun sets'}]
        row = dict(text="we workin' all day until the sun sets", start=0, end=4)
        self.assertEqual(builder._check_session_wording(row, [], units, []), row)
        novel = dict(text='a completely different story with new words', start=0, end=4)
        self.assertEqual(builder._check_session_wording(novel, [], units, []), novel)

    def test_never_ever_land_keeps_all_early_xingyu_phrases(self):
        fixture = ROOT / 'BUILDER_REPLAY' / 'REGRESSION_INPUTS'
        ref = builder.parse_reference(queue.reference_text(fixture / 'never_reference.txt'))
        w, x = load_trace(fixture / 'never_5min04sec.txt')
        output, *_ = builder.hybridize(ref, w, xingyu_lines=x)
        blocks = builder._reference_section_blocks(builder.parse_reference('\n'.join(output)))
        verses = [b for b in blocks if b['kind'] == 'verse']
        self.assertEqual(verses[0]['lines'], [r['text'] for r in x[:39]])
        self.assertEqual(verses[0]['header'], '[Verse 1]')
        self.assertEqual(verses[1]['header'], '[Verse 2]')
        self.assertTrue(verses[1]['lines'][0].startswith("Workin'"))

    def test_lotti_merged_edges_keep_novel_opening_and_garage_ending(self):
        fixture = ROOT / 'BUILDER_REPLAY' / 'REGRESSION_INPUTS'
        ref = builder.parse_reference(queue.reference_text(fixture / 'lotti_reference.txt'))
        w, x = load_trace(fixture / 'lotti_3min05sec.txt')
        output, *_ = builder.hybridize(ref, w, xingyu_lines=x)
        blocks = builder._reference_section_blocks(builder.parse_reference('\n'.join(output)))
        verses = [b['lines'] for b in blocks if b['kind'] == 'verse']
        self.assertEqual(len(verses), 3)
        self.assertEqual(verses[2][0], "I'm a phenomenon with the Montclair on")
        self.assertEqual(verses[2][-1], 'He got me foreign cars and a big-ass garage')
        self.assertFalse(any('lurking' in s or 'America' in s for s in verses[2]))
        self.assertEqual([len(b['lines']) for b in blocks if b['kind'] == 'chorus'], [4, 12, 12, 12])

    def test_boundary_checker_preserves_unrelated_short_verse(self):
        chorus = ['we walk beside the river', 'flowers open in the garden']
        rows = [dict(start=10, end=13, text='a new story begins today'),
                dict(start=13, end=16, text='we walk beside the river tomorrow in my story')]
        result = builder._session_lines_between_choruses(10, 17, rows, rows, [], [], chorus)
        self.assertEqual(result, [r['text'] for r in rows])

    def test_lotti_timed_cycles_restore_verses_and_three_long_choruses(self):
        fixture = ROOT / "BUILDER_REPLAY" / "REGRESSION_INPUTS"
        ref = builder.parse_reference(queue.reference_text(fixture / "lotti_reference.txt"))
        whisper, xingyu = load_trace(fixture / "lotti_3min16sec.txt")
        output, diagnostics, *_ = builder.hybridize(ref, whisper, xingyu_lines=xingyu)
        blocks = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
        reference = builder._reference_section_blocks(ref)
        short_chorus = [b for b in reference if b["kind"] == "chorus"][0]["lines"]
        long_chorus = [b for b in reference if b["kind"] == "chorus"][-1]["lines"]

        self.assertEqual([b["lines"] for b in blocks if b["kind"] == "chorus"],
                         [short_chorus, long_chorus, long_chorus, long_chorus])
        verses = [b["lines"] for b in blocks if b["kind"] == "verse"]
        self.assertEqual(len(verses), 3)
        self.assertEqual(verses[1][0], "Non-stop, workin' all day, non-stop")
        self.assertEqual(verses[1][-1], "I'm the only one that could break the rules.")
        self.assertEqual(verses[2][0], "I'm a phenomenon,")
        self.assertEqual(verses[2][-1], "He got me four cars in a big ass garage.")
        self.assertFalse(any("thank you" in line.lower() for line in output))
        self.assertFalse(any(line.strip() == "." for line in output))
        self.assertFalse(any("I'm American" in line for line in output))
        self.assertTrue(any("TIMED CHORUS CYCLES" in line for line in diagnostics))

    def test_kirby_session_gap_preserves_whisper_opening_and_following_xingyu(self):
        fixture = ROOT / "BUILDER_REPLAY" / "REGRESSION_INPUTS"
        ref = builder.parse_reference(queue.reference_text(fixture / "kirby_reference.txt"))
        w, x = load_trace(fixture / "kirby_4min57sec.txt")
        output, diagnostics, *_ = builder.hybridize(ref, w, xingyu_lines=x)
        blocks = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
        verses = [b["lines"] for b in blocks if b["kind"] == "verse"]
        self.assertEqual(len(verses), 3)
        self.assertEqual(verses[2][0], "Count it up,")
        expected = [s["text"] for s in w if 168.70 <= s["start"] <= 192.10]
        self.assertEqual(verses[2][1:1 + len(expected)], expected)
        self.assertTrue(verses[2][1 + len(expected)].startswith("Crack, brand new bitch"))
        self.assertEqual([len(b["lines"]) for b in blocks if b["kind"] == "chorus"], [12] * 4)
        self.assertTrue(any("XINGYU GAP FILLED" in d for d in diagnostics))

    def test_session_gap_merge_keeps_sources_and_trims_only_confirmed_overlap(self):
        x = [dict(start=0, end=3, text="known refrain ending here"),
             dict(start=8, end=12, text="next recognized passage")]
        w = [dict(start=0, end=2, text="same covered audio"),
             dict(start=2.5, end=5, text="refrain ending here brand new words"),
             dict(start=5, end=8, text="more new words in the gap"),
             dict(start=9, end=11, text="covered again")]
        merged = builder._merge_session_gap_sources(w, x, [], [])
        self.assertEqual([s["text"] for s in merged],
                         [x[0]["text"], "brand new words", w[2]["text"], x[1]["text"]])
        self.assertEqual(merged[1]["start"], 3)
        self.assertNotIn("engine", x[0])
        self.assertEqual(w[1]["text"], "refrain ending here brand new words")

    def test_overlapping_xingyu_segments_do_not_double_count_time_coverage(self):
        x = [dict(start=0, end=2, text="one covered section"), dict(start=0, end=2, text="overlapping section")]
        w = [dict(start=0, end=6, text="new independent words after the section")]
        merged = builder._merge_session_gap_sources(w, x, [], [])
        self.assertIn(w[0]["text"], [s["text"] for s in merged])

    def test_hectic_merged_phrase_trace_restores_tail_and_splits_verse_boundary(self):
        fixture = ROOT / "BUILDER_REPLAY" / "REGRESSION_INPUTS"
        ref = builder.parse_reference(queue.reference_text(fixture / "hectic_reference.txt"))
        w, x = load_trace(fixture / "hectic_2min08sec.txt")
        output, diagnostics, *_ = builder.hybridize(ref, w, xingyu_lines=x)
        blocks = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
        trusted = next(b["lines"] for b in builder._reference_section_blocks(ref) if b["kind"] == "chorus")
        self.assertEqual([b["lines"] for b in blocks if b["kind"] == "chorus"], [trusted] * 3)
        verse = [b["lines"] for b in blocks if b["kind"] == "verse"][1]
        self.assertEqual(verse[-1], "Try me if you want, it get ugly")
        self.assertTrue(verse[0].startswith("I'm the type"))
        self.assertTrue(verse[1].startswith("I'm the type"))
        self.assertFalse(any("hectic" in line.lower() for line in verse))

    def test_verse_formatting_preserves_words_and_reference_lines(self):
        text = "I'm walking along the river on a sunny morning I'm watching all the birds fly above the old garden"
        output = ["[Verse]", text]
        split = builder._format_novel_verse_phrases(output, [], [])
        self.assertGreater(len(split), 2)
        self.assertEqual(" ".join(split[1:]), text)
        ref = builder.parse_reference("\n".join(output))
        self.assertEqual(builder._format_novel_verse_phrases(output, ref, []), output)

    def test_chorus_tail_is_not_added_without_local_audio_evidence(self):
        chorus = ["silver moon above the garden gate", "walking home along the quiet river", "flowers bloom beneath the old oak tree", "morning birds are singing in the sunlight"]
        ref = builder.parse_reference("\n".join(["[Chorus]", *chorus]))
        output = ["[Chorus]", *chorus[:2], "[Verse]", "my new story starts right here today"]
        result = builder._restore_evidenced_chorus_tail(output, ref, [], [])
        self.assertEqual(builder._reference_section_blocks(builder.parse_reference("\n".join(result))),
                         builder._reference_section_blocks(builder.parse_reference("\n".join(output))))

    def test_hectic_new_trace_keeps_chorus_out_of_both_verse_boundaries(self):
        fixture = ROOT / "BUILDER_REPLAY" / "REGRESSION_INPUTS"
        ref = builder.parse_reference(queue.reference_text(fixture / "hectic_reference.txt"))
        w, x = load_trace(fixture / "hectic_2min09sec.txt")
        output, diagnostics, *_ = builder.hybridize(ref, w, xingyu_lines=x)
        blocks = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
        trusted = next(b["lines"] for b in builder._reference_section_blocks(ref) if b["kind"] == "chorus")
        self.assertEqual([b["lines"] for b in blocks if b["kind"] == "chorus"], [trusted] * 3)
        verses = [b["lines"] for b in blocks if b["kind"] == "verse"]
        self.assertEqual(len(verses), 2)
        self.assertTrue(verses[1][0].startswith("I'm the type"))
        self.assertIn("try me if you want, it get ugly", verses[1][-1].lower())
        self.assertTrue(any("BOUNDARY ECHO" in d for d in diagnostics))

    def test_boundary_word_is_preserved_without_merged_audio_confirmation(self):
        ref = builder.parse_reference("[Chorus]\nwe send a message")
        output = ["unsectioned opening", "[Chorus]", "we send a message", "[Verse]", "message from my brother arrived today"]
        result = builder._remove_confirmed_boundary_echo(output, ref, [], [])
        self.assertIn("message from my brother arrived today", result)
        self.assertEqual(result[0], "unsectioned opening")

    def test_hectic_frozen_trace_restores_three_complete_choruses(self):
        fixture = ROOT / "BUILDER_REPLAY" / "REGRESSION_INPUTS"
        ref = builder.parse_reference(queue.reference_text(fixture / "hectic_reference.txt"))
        w, x = load_trace(fixture / "hectic_2min06sec.txt")
        output, diagnostics, *_ = builder.hybridize(ref, w, xingyu_lines=x)
        blocks = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
        trusted = next(b["lines"] for b in builder._reference_section_blocks(ref) if b["kind"] == "chorus")
        self.assertEqual([b["lines"] for b in blocks if b["kind"] == "chorus"], [trusted] * 3)
        verses = [b["lines"] for b in blocks if b["kind"] == "verse"]
        self.assertEqual(len(verses), 2)
        self.assertEqual(verses[1][-1], "Try me if you want, it get ugly")
        self.assertTrue(verses[1][0].startswith("I'm the type"))
        self.assertTrue(any("ASR COVERAGE REPAIR" in d for d in diagnostics))
        self.assertTrue(any("COMPLETE CHORUS PATTERN" in d for d in diagnostics))

    def test_repetitive_whisper_is_kept_without_better_secondary_evidence(self):
        rows = [dict(start=0, end=20, text="yeah " * 20)]
        ref = builder.parse_reference("[Chorus]\nwalking home beside the river tonight\nsilver moon above the garden gate\nflowers bloom underneath the old oak tree")
        self.assertEqual(builder._release_uninformative_whisper_spans(rows, [], ref, []), rows)
        unrelated = [dict(start=0, end=10, text="the train leaves at noon"), dict(start=10, end=20, text="buy a ticket at the station")]
        self.assertEqual(builder._release_uninformative_whisper_spans(rows, unrelated, ref, []), rows)

    def test_complete_pattern_does_not_discard_unknown_verse_material(self):
        chorus = ["silver moon above the garden gate", "walking home along the quiet river", "flowers bloom beneath the old oak tree", "morning birds are singing in the sunlight"]
        ref = builder.parse_reference("\n".join(["[Chorus]", *chorus]))
        output = ["[Verse]", "keep this new verse line", chorus[0], "[Chorus]", *chorus[1:], "a train leaves the busy station at noon"]
        result = builder._complete_fragmented_chorus_patterns(output, ref, [])
        self.assertEqual(builder._reference_section_blocks(builder.parse_reference("\n".join(result))),
                         builder._reference_section_blocks(builder.parse_reference("\n".join(output))))

    def test_frozen_different_story_variants_preserve_complete_sections(self):
        reference = ROOT / "BUILDER_REPLAY" / "REGRESSION_INPUTS" / "different_story_reference.txt"
        self.assertTrue(reference.exists())
        ref = builder.parse_reference(queue.reference_text(reference))
        trusted = next(b["lines"] for b in builder._reference_section_blocks(ref) if b["kind"] == "chorus")
        for filename in ("different_story_1min58sec.txt", "different_story_2min01sec.txt"):
            with self.subTest(trace=filename):
                w, x = load_trace(ROOT / "BUILDER_REPLAY" / "REGRESSION_INPUTS" / filename)
                output, diagnostics, *_ = builder.hybridize(ref, w, xingyu_lines=x)
                blocks = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
                self.assertEqual([b["lines"] for b in blocks if b["kind"] == "chorus"], [trusted] * 3)
                verses = [b["lines"] for b in blocks if b["kind"] == "verse"]
                self.assertEqual(len(verses), 2)
                self.assertTrue(verses[0][0].startswith("Wockhardt"))
                self.assertTrue(verses[1][0].startswith("You a target"))
                self.assertTrue(any(line.startswith("DBS on my bro") for line in verses[1]))
                self.assertNotIn("Thank you.", output)
                self.assertFalse(any("REVIEW NEEDED" in d for d in diagnostics))

    def test_long_block_comparison_keeps_common_letters_as_evidence(self):
        ref = ["walking home by the quiet river under the silver moon",
               "flowers bloom beside the garden and the birds are singing",
               "the evening breeze carries a familiar melody through town",
               "we follow the winding road until the morning comes again"]
        heard = " ".join(ref).replace("quiet", "silent").replace("silver", "bright").replace("winding", "old")
        score, sequence, *_ = builder._trusted_block_score(ref, [heard])
        self.assertGreater(sequence, 0.75)
        self.assertGreater(score, 0.75)

    def test_chorus_fingerprint_does_not_consume_previous_verse_line(self):
        chorus = ["silver moon above the garden gate", "walking home along the quiet river", "flowers bloom beneath the old oak tree"]
        lines = ["the station train departs at noon", *chorus]
        self.assertEqual(builder._chorus_start_fingerprint(lines, 0, chorus), 0)
        self.assertGreater(builder._chorus_start_fingerprint(lines, 1, chorus), 0.58)

    def test_tail_filter_applies_to_both_engines_and_preserves_reference_words(self):
        ref = builder.parse_reference("[Verse]\nwalking home along the river")
        rows = [dict(start=0, end=3, text="walking home along the river"), dict(start=4, end=5, text="Thank you.")]
        output, diagnostics, *_ = builder.hybridize(ref, rows, xingyu_lines=rows)
        self.assertNotIn("Thank you.", output)
        self.assertEqual(sum("ASR TAIL FILTER" in d for d in diagnostics), 2)
        self.assertEqual(len(rows), 2)
        protected = builder.parse_reference("[Verse]\nwalking home along the river\nThank you.")
        output, diagnostics, *_ = builder.hybridize(protected, rows, xingyu_lines=rows)
        self.assertIn("Thank you.", output)
        self.assertFalse(any("ASR TAIL FILTER" in d for d in diagnostics))

    def test_incomplete_chorus_is_reported_but_not_invented(self):
        chorus = ["silver moon above the garden gate", "walking home along the quiet river", "flowers bloom beneath the old oak tree", "morning birds are singing"]
        ref = builder.parse_reference("\n".join(["[Chorus]", *chorus]))
        diagnostics = []
        builder._audit_reference_choruses(["[Chorus]", *chorus[:2]], ref, diagnostics)
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("REVIEW NEEDED", diagnostics[0])

    def test_reference_intro_is_exact_even_without_any_audio_match(self):
        ref = builder.parse_reference("[Intro]\nfirst opening line\nsecond opening line\n[Chorus]\nchorus words")
        output = ["unreliable ASR opening", "[Intro]", "wrong words", "[Chorus]", "chorus words", "[Verse]", "new audio words"]
        actual = builder._copy_reference_intro(output, ref, [])
        blocks = builder._reference_section_blocks(builder.parse_reference("\n".join(actual)))
        self.assertEqual(blocks[0]["lines"], ["first opening line", "second opening line"])
        self.assertNotIn("wrong words", actual)
        self.assertNotIn("unreliable ASR opening", actual)
        self.assertIn("new audio words", actual)
        no_intro = builder.parse_reference("[Chorus]\nchorus words")
        self.assertEqual(builder._copy_reference_intro(output, no_intro, []), output)

    def test_bracketed_chorus_fragments_do_not_swallow_an_unrelated_verse(self):
        chorus = ["silver moon above the garden gate", "walking home along the quiet river",
                  "flowers bloom beneath the old oak tree", "morning birds are singing in the sunlight",
                  "warm summer breezes fill the air", "the hills are shining in the distance"]
        ref = builder.parse_reference("\n".join(["[Chorus]", *chorus]))
        output = ["[Chorus]", *chorus[:2], "[Verse]", "the train leaves at noon", "buy a ticket at the station", "[Chorus]", *chorus[-2:]]
        before = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
        after = builder._reference_section_blocks(builder.parse_reference("\n".join(builder._repair_chorus_across_headers(output, ref, []))))
        self.assertEqual(before, after)

    def test_different_story_verified_excerpt_restores_all_three_choruses(self):
        trace = ROOT / "BUILDER_REPLAY" / "different_story_verified" / "diagnostics.txt"
        reference = queue.existing_marker(ROOT / "Juice WRLD" / "Juice WRLD" / "Different Story (Sessions) (WOD).mp3")
        if not trace.exists() or reference is None:
            self.skipTest("Local recovered audio fixture unavailable")
        ref = builder.parse_reference(queue.reference_text(reference))
        whisper, xingyu = load_trace(trace)
        output, *_ = builder.hybridize(ref, whisper, xingyu_lines=xingyu)
        blocks = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
        choruses = [s["lines"] for s in blocks if s["kind"] == "chorus"]
        trusted = next(s["lines"] for s in builder._reference_section_blocks(ref) if s["kind"] == "chorus")
        self.assertEqual(choruses, [trusted, trusted, trusted])

    def test_different_story_chorus_boundaries_keep_whisper_verse_opening(self):
        logs = list(ROOT.glob("Different Story*1min58sec*.txt"))
        reference = queue.existing_marker(ROOT / "Juice WRLD" / "Juice WRLD" / "Different Story (Sessions) (WOD).mp3")
        if not logs or reference is None:
            self.skipTest("Local user replay fixture unavailable")
        ref = builder.parse_reference(queue.reference_text(reference))
        whisper, xingyu = load_trace(logs[0])
        output, *_ = builder.hybridize(ref, whisper, xingyu_lines=xingyu)
        blocks = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
        choruses = [s for s in blocks if s["kind"] == "chorus"]
        self.assertEqual([len(s["lines"]) for s in choruses], [12, 12, 12])
        verses = [s for s in blocks if s["kind"] == "verse"]
        self.assertEqual(len(verses), 2)
        self.assertTrue(verses[1]["lines"][0].startswith("You a target"))
        self.assertTrue(verses[0]["lines"][0].startswith("Wockhardt"))
        self.assertIn("I acknowledge that", " ".join(verses[1]["lines"][:2]))
        self.assertTrue(any(line.startswith("Send my") for line in verses[1]["lines"][:3]))
        self.assertEqual(blocks[0]["lines"], builder._reference_section_blocks(ref)[0]["lines"])

    def test_split_chorus_does_not_absorb_an_unrelated_verse(self):
        chorus = ["silver moon above the garden gate", "walking home along the quiet river",
                  "flowers bloom beneath the old oak tree", "morning birds are singing in the sunlight"]
        ref = builder.parse_reference("\n".join(["[Chorus]", *chorus]))
        output = ["[Chorus]", *chorus[:2], "[Verse]", "the train leaves at noon", "buy a ticket at the station"]
        before = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
        after = builder._reference_section_blocks(builder.parse_reference("\n".join(builder._repair_chorus_across_headers(output, ref, []))))
        self.assertEqual(before, after)

    def test_latest_verse_uses_later_reference_verse_and_preserves_new_line(self):
        logs = list(ROOT.glob("Come On*3min54sec*.txt"))
        reference = queue.existing_marker(ROOT / "Juice WRLD" / "Juice WRLD" / "Come On Wit' It (Sessions) (WOD).mp3")
        if not logs or reference is None:
            self.skipTest("Local user replay fixture unavailable")
        ref = builder.parse_reference(queue.reference_text(reference))
        whisper, xingyu = load_trace(logs[0])
        result, diagnostics, *_ = builder.hybridize(ref, whisper, xingyu_lines=xingyu)
        verses = [s for s in builder._reference_section_blocks(builder.parse_reference("\n".join(result))) if s["kind"] == "verse"]
        trusted = [s for s in builder._reference_section_blocks(ref) if s["kind"] == "verse"]
        self.assertEqual(len(verses), 2)
        self.assertTrue(verses[1]["lines"][0].startswith("My cup's"))
        self.assertEqual(verses[1]["lines"][1:], trusted[2]["lines"][1:])
        self.assertNotIn(trusted[2]["lines"][0], result)  # Unheard opening is not copied.
        self.assertTrue(any("LOCAL VERSE REFERENCE" in line for line in diagnostics))

    def test_local_verse_recheck_requires_multiple_anchors(self):
        lines = ["walking slowly along the quiet river", "silver moon above the garden gate",
                 "flowers bloom beneath the old oak tree", "morning birds are singing in the sunlight"]
        ref = builder.parse_reference("\n".join(["[Verse 3]", *lines]))
        output = ["[Verse 1]", lines[0], "the station train departs at noon", "take a ticket for the bus", "a suitcase waits beside the door"]
        self.assertEqual(builder._repair_locally_supported_verses(output, ref, []), output)

    def test_alignment_cannot_validate_a_verse_line_using_the_next_chorus(self):
        ref, _ = builder.build_reference_units(builder.parse_reference("[Verse]\nunrelated distant story\n[Chorus]\nsilver moon above the garden gate"))
        matches = builder.align_reference_to_transcript(ref, [dict(text="silver moon above the garden gate", start=1, end=5)])
        self.assertTrue(matches)
        self.assertTrue(all(match["ref_start"] == 1 and match["ref_count"] == 1 for match in matches))

    def test_intro_requires_distinctive_early_evidence(self):
        ref = builder.parse_reference("[Intro]\nUh\nwelcome to this little garden party\nroses bloom tonight\n[Chorus]\nwalking home beside the river\nsilver moon across the sky")
        output = ["[Intro]", "Uh", "[Chorus]", "walking home beside the river", "silver moon across the sky"]
        main = dict(start=12, end=20, text="walking home beside the river silver moon across the sky")
        unrelated = dict(start=1, end=6, text="trains are waiting at the station platform")
        self.assertEqual(builder._restore_supported_intro(output, ref, [unrelated, main], [], []), output)
        related = dict(start=1, end=6, text="welcome to this little garden party")
        restored = builder._restore_supported_intro(output, ref, [related, main], [], [])
        self.assertIn("roses bloom tonight", restored)
        self.assertEqual(restored[0], "[Intro]")

    def test_intro_retry_recovers_only_missing_prefix_without_reference_prompt(self):
        ref = builder.parse_reference("[Intro]\nopening words\n[Verse]\nother words")
        original = [dict(start=20, end=24, text="existing transcript")]
        xingyu = [dict(start=12, end=16, text="earlier transcript")]
        tx = Mock()
        tx.transcribe.return_value = [SimpleNamespace(raw="opening", start=1, end=2),
                                     SimpleNamespace(raw="words", start=2, end=3)]
        with patch.object(builder.subprocess, "run") as ffmpeg:
            result = builder._retry_missing_intro_audio("original.mp3", ref, original, xingyu, tx, "en", Mock())
        self.assertEqual([line["start"] for line in result], [1, 20])
        self.assertEqual(len(original), 1)
        self.assertIsNone(tx.transcribe.call_args.kwargs["initial_prompt"])
        command = ffmpeg.call_args.args[0]
        self.assertEqual(command[command.index("-t") + 1], "12.0")
        self.assertTrue(command[-1].endswith("intro.wav"))

    def test_intro_retry_skips_covered_opening_and_handles_failure(self):
        ref = builder.parse_reference("[Intro]\nopening words")
        tx = Mock()
        covered = [dict(start=1, end=4, text="early audio")]
        result = builder._retry_missing_intro_audio("a.mp3", ref, covered, [], tx, "en", Mock())
        self.assertIs(result, covered)
        tx.transcribe.assert_not_called()
        missing = [dict(start=12, end=16, text="late audio")]
        with patch.object(builder.subprocess, "run", side_effect=OSError("test failure")):
            result = builder._retry_missing_intro_audio("a.mp3", ref, missing, [], tx, "en", Mock())
        self.assertIs(result, missing)

    def test_time_gap_recovers_xingyu_without_duplicating_covered_segments(self):
        whisper = [dict(start=0, end=10, text="covered"), dict(start=20, end=25, text="next")]
        missing = dict(start=12, end=20.2, text="unheard by Whisper")
        covered = dict(start=2, end=7, text="already heard")
        result, added = builder._fill_whisper_time_gaps(whisper, [covered, missing])
        self.assertEqual(added, [missing])
        self.assertEqual([x["text"] for x in result], ["covered", "unheard by Whisper", "next"])
        self.assertEqual(len(whisper), 2)

    def test_unrelated_verse_after_chorus_is_unchanged(self):
        ref = builder.parse_reference("[Chorus]\nknown song refrain\n[Post-Chorus]\nred roses flower\nmoonlit garden path\nwalking home tonight")
        output = ["[Chorus]", "known song refrain", "[Verse]", "a completely different verse", "unique new material"]
        self.assertEqual(builder._repair_split_post_after_chorus(output, ref, []), output)

    def test_buck_saved_trace_preserves_four_complete_post_choruses(self):
        logs = list(ROOT.glob("Buck 50*2min43sec*.txt"))
        reference = queue.existing_marker(ROOT / "Juice WRLD" / "Juice WRLD" / "Buck 50 (Buck Fifty) (Sessions).mp3")
        if not logs or reference is None:
            self.skipTest("Local user replay fixture unavailable")
        whisper, xingyu = load_trace(logs[0])
        parsed = builder.parse_reference(queue.reference_text(reference))
        ref, count = [], 0
        for item in parsed:
            if count == 80:  # Original log explicitly states 80 reference lines.
                break
            ref.append(item)
            count += item["kind"] == "lyric"
        result, diagnostics, *_ = builder.hybridize(ref, whisper, xingyu_lines=xingyu)
        sections = builder._reference_section_blocks(builder.parse_reference("\n".join(result)))
        posts = [s for s in sections if s["kind"] == "post-chorus"]
        self.assertEqual([len(s["lines"]) for s in posts], [8, 8, 8, 8])
        verses = [s for s in sections if s["kind"] == "verse"]
        self.assertEqual(len(verses), 3)
        self.assertTrue(verses[2]["lines"][0].startswith("Fresh up out the jet"))
        self.assertEqual(sum("Fresh up out the jet" in line for line in result), 1)
        self.assertTrue(any("LOCAL POST-CHORUS TAIL" in line for line in diagnostics))

    def test_consensus_guard_preserves_novel_material_and_verse_order(self):
        chorus = ["silver moon across the sky", "walking home along the river"]
        ref = builder.parse_reference("\n".join(["[Chorus]", *chorus, "[Verse 1]", "first verse", "[Chorus]", *chorus]))
        output = ["[Chorus]", *chorus, "brand new unrelated story about a dog", "[Verse 3]", "third verse", "[Verse 1]", "first verse"]
        result = builder._guard_trusted_repeated_sections(output, ref, [])
        self.assertIn("brand new unrelated story about a dog", result)
        self.assertLess(result.index("third verse"), result.index("first verse"))

    def test_come_on_trace_uses_reference_refrains_without_extra_lines(self):
        logs = list(ROOT.glob("Come On*1min41sec*.txt"))
        reference = queue.existing_marker(ROOT / "Juice WRLD" / "Juice WRLD" / "Come On Wit' It (Sessions) (WOD).mp3")
        if not logs or reference is None:
            self.skipTest("Local user replay fixture unavailable")
        whisper, xingyu = load_trace(logs[0])
        ref = builder.parse_reference(queue.reference_text(reference))
        output, *_ = builder.hybridize(ref, whisper, xingyu_lines=xingyu)
        sections = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
        self.assertEqual(output[0], "[Intro: Juice WRLD]")
        self.assertEqual(sections[0]["lines"], builder._reference_section_blocks(ref)[0]["lines"])
        self.assertNotIn("Running up bands, then I blow them on mirror", output)
        choruses = [s for s in sections if s["kind"] == "chorus"]
        posts = [s for s in sections if s["kind"] == "post-chorus"]
        self.assertEqual([len(s["lines"]) for s in choruses], [4, 4, 4])
        self.assertEqual([len(s["lines"]) for s in posts], [4, 4, 4])
        verses = [s for s in sections if s["kind"] == "verse"]
        self.assertEqual(len(verses), 2)
        self.assertTrue(verses[-1]["lines"][0].startswith("Hi-Tech"))
        self.assertNotIn("I hit any city, trend like Twitter", output)

    def test_latest_trace_moves_novel_post_tail_into_next_verse(self):
        logs = list(ROOT.glob("Come On*1min45sec*.txt"))
        reference = queue.existing_marker(ROOT / "Juice WRLD" / "Juice WRLD" / "Come On Wit' It (Sessions) (WOD).mp3")
        if not logs or reference is None:
            self.skipTest("Local user replay fixture unavailable")
        whisper, xingyu = load_trace(logs[0])
        output, *_ = builder.hybridize(builder.parse_reference(queue.reference_text(reference)), whisper, xingyu_lines=xingyu)
        sections = builder._reference_section_blocks(builder.parse_reference("\n".join(output)))
        self.assertEqual([len(s["lines"]) for s in sections if s["kind"] == "post-chorus"], [4, 4, 4])
        verses = [s for s in sections if s["kind"] == "verse"]
        self.assertTrue(verses[1]["lines"][0].startswith("My cup's"))
        self.assertEqual(sum(line.startswith("My cup's") for line in output), 1)


if __name__ == "__main__":
    unittest.main()
