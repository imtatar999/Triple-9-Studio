import ast
import difflib
import re
import unicodedata
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "triplenine_lyrics_downloader.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NAMES = {
    "_norm_match_text", "_extract_version_markers", "_extract_required_edition_markers",
    "_edition_markers_compatible", "_strip_bracketed_parts", "_extract_bracket_aliases",
    "_strip_feature_suffix_for_title_match", "_token_similarity",
    "_near_exact_main_title_typo", "_title_match_details",
    "_strip_artist_prefix_from_title",
    "_strip_local_revision_markers", "_strip_version_labels_for_fallback",
    "_classify_genius_title_parts", "_build_smart_genius_second_query_title",
    "_catalog_noise_key", "_strip_search_noise_brackets",
}
NS = {"re": re, "unicodedata": unicodedata, "difflib": difflib}
exec(compile(ast.Module(
    body=[node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in NAMES],
    type_ignores=[]), "<title-functions>", "exec"), NS)


class SessionEditionMatchingTests(unittest.TestCase):
    def test_session_edit_matches_sessions(self):
        score, reason = NS["_title_match_details"](
            "Split My Brains (Session Edit)", "Split My Brains (Sessions)")
        self.assertGreaterEqual(score, 0.95, reason)

    def test_sessions_matches_session_edit(self):
        score, reason = NS["_title_match_details"](
            "Split My Brains (Sessions)", "Split My Brains (Session Edit)")
        self.assertGreaterEqual(score, 0.95, reason)

    def test_session_edit_still_rejects_unmarked_regular_version(self):
        score, reason = NS["_title_match_details"](
            "Split My Brains (Session Edit)", "Split My Brains")
        self.assertEqual(score, 0.0, reason)

    def test_sessions_still_rejects_unmarked_regular_version(self):
        score, reason = NS["_title_match_details"](
            "Split My Brains (Sessions)", "Split My Brains")
        self.assertEqual(score, 0.0, reason)


class CompoundParentheticalAliasTests(unittest.TestCase):
    def test_lava_girl_matches_compound_genius_second_title(self):
        score, reason = NS["_title_match_details"](
            "Lava Girl (DRFL)", "Stick Talk (Lavagirl)")
        self.assertGreaterEqual(score, 0.95, reason)

    def test_unrelated_girl_title_remains_below_acceptance_threshold(self):
        score, reason = NS["_title_match_details"](
            "Lava Girl (DRFL)", "Girl Of My Dreams")
        self.assertLess(score, 0.51, reason)

    def test_second_query_uses_compound_spelling_without_an_extra_request(self):
        query = NS["_build_smart_genius_second_query_title"](
            "Lava Girl (DRFL)", ["Juice WRLD"], "Lava Girl")
        self.assertEqual(query, "LavaGirl")

    def test_long_typo_fallback_keeps_distinctive_final_word(self):
        query = NS["_build_smart_genius_second_query_title"](
            "Cokkie Crisp", ["Juice WRLD"], "Cokkie Crisp")
        self.assertEqual(query, "Crisp")

    def test_short_alias_fallback_keeps_the_main_title(self):
        query = NS["_build_smart_genius_second_query_title"](
            "Like Shit (Pump) (Fight) (V2) (GB&GR)",
            ["Juice WRLD"],
            "Like Shit V2",
        )
        self.assertEqual(query, "Like Shit Pump")

    def test_correct_like_shit_result_passes_relaxed_title_verification(self):
        relaxed_title = NS["_strip_version_labels_for_fallback"](
            "Like Shit (Pump) (Fight) (V2) (GB&GR)"
        )
        score, reason = NS["_title_match_details"](
            relaxed_title, "Like Shit (Pump)")
        self.assertGreaterEqual(score, 0.51, reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CatalogNoiseTests(unittest.TestCase):
    def test_gb_and_gr_is_catalog_noise_even_with_unbalanced_brackets(self):
        parts = NS["_classify_genius_title_parts"]("Already (V2) ((GB&GR)", ["Juice WRLD"])
        self.assertEqual(parts["aliases"], [])
        self.assertTrue(parts["noise"])
        self.assertEqual(parts["core"], "Already V2")

    def test_second_query_uses_core_title_instead_of_gb_gr(self):
        second = NS["_build_smart_genius_second_query_title"](
            "Already (V2) ((GB&GR)", ["Juice WRLD"], "Already V2")
        self.assertEqual(second, "Already")

    def test_noise_brackets_stripped_from_verification_title(self):
        self.assertEqual(NS["_strip_search_noise_brackets"]("Hurt Me (GB&GR)"), "Hurt Me")
        self.assertEqual(NS["_catalog_noise_key"]("GB & GR"), NS["_catalog_noise_key"]("gb&gr"))

    def test_real_alias_is_still_an_alias(self):
        parts = NS["_classify_genius_title_parts"]("5 Bitches (500 Carats) (GB&GR)", ["Juice WRLD"])
        self.assertEqual(parts["aliases"], ["500 Carats"])
