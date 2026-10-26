import ast
import difflib
from pathlib import Path
import re
import unicodedata
import unittest
import urllib.parse


SOURCE = Path(__file__).resolve().parents[1] / "triplenine_lyrics_downloader.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))


class GeniusRateLimitError(RuntimeError):
    pass


def load_functions(**stubs):
    names = {
        "_norm_match_text", "_split_artist_names", "_artist_matches",
        "_strip_bracketed_parts", "_strip_version_labels_for_fallback",
        "_validated_genius_url", "_genius_hits_from_payload",
        "_lyrics_body_similarity", "_genius_alternative_candidates",
        "_classify_genius_title_parts", "_build_smart_genius_first_query_title",
        "_build_smart_genius_second_query_title", "_strip_local_revision_markers",
        "_extract_version_markers", "_extract_required_edition_markers", "_catalog_noise_key",
    }
    nodes = [node for node in TREE.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"re": re, "difflib": difflib, "unicodedata": unicodedata,
                 "urllib": urllib, "GeniusRateLimitError": GeniusRateLimitError}
    namespace.update(stubs)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<different lyrics>", "exec"), namespace)
    return namespace


def hit(title, artist, slug):
    return {"type": "song", "result": {"title": title, "artist_names": artist,
                                       "url": f"https://genius.com/{slug}-lyrics"}}


class DifferentLyricsTests(unittest.TestCase):
    def make(self, hits):
        queries = []

        def search(query, _token):
            queries.append(query)
            return {"response": {"hits": hits}}, True

        ns = load_functions(
            get_track_metadata=lambda song: ("Lucid Dreams (V2)", "Juice WRLD"),
            get_artist_candidates=lambda song, title, artist: [artist],
            _strip_artist_prefix_from_title=lambda title, artists: title,
            _title_match_details=lambda expected, actual: (
                (1.0, "exact") if expected.casefold() == actual.casefold() else (0.0, "missing version marker")),
            get_genius_api_token=lambda: "token",
            _genius_search_request=search,
        )
        return ns, queries

    def test_keeps_other_versions_ranks_artist_match_and_excludes_current(self):
        ns, queries = self.make([
            hit("Lucid Dreams (V2)", "Juice WRLD", "Juice-wrld-lucid-dreams-v2"),
            hit("Lucid Dreams", "Juice WRLD", "Juice-wrld-lucid-dreams"),
            hit("Lucid Dreams (Remix)", "Someone Else", "Someone-else-lucid-dreams-remix"),
            hit("Completely Other Song", "Juice WRLD", "Juice-wrld-other-song"),
        ])
        candidates, notes = ns["_genius_alternative_candidates"](
            "song.mp3", exclude_url="https://genius.com/Juice-wrld-lucid-dreams-v2-lyrics")
        urls = [item["url"] for item in candidates]
        self.assertEqual(notes, [])
        self.assertLessEqual(len(queries), 3)
        self.assertNotIn("https://genius.com/Juice-wrld-lucid-dreams-v2-lyrics", urls)
        self.assertNotIn("https://genius.com/Juice-wrld-other-song-lyrics", urls)
        self.assertEqual(urls[0], "https://genius.com/Juice-wrld-lucid-dreams-lyrics")
        self.assertIn("https://genius.com/Someone-else-lucid-dreams-remix-lyrics", urls)
        self.assertFalse(candidates[-1]["artist_match"])

    def test_already_v2_uses_downloader_queries_plus_bare_main_title(self):
        queries = []

        def search(query, _token):
            queries.append(query)
            hits = [hit("Already Dead (V2)", "Juice WRLD", "Juice-wrld-already-dead-v2")]
            if query == "Juice WRLD Already":
                hits = [hit("Already Dead", "Juice WRLD", "Juice-wrld-already-dead"),
                        hit("Already", "Juice WRLD", "Juice-wrld-already")]
            return {"response": {"hits": hits}}, True

        ns = load_functions(
            get_track_metadata=lambda song: ("Already (V2) ((GB&GR)", "Juice WRLD"),
            get_artist_candidates=lambda song, title, artist: [artist],
            _strip_artist_prefix_from_title=lambda title, artists: title,
            _title_match_details=lambda expected, actual: (0.0, "missing version marker"),
            get_genius_api_token=lambda: "token",
            _genius_search_request=search,
        )
        candidates, _ = ns["_genius_alternative_candidates"](
            "song.mp3", exclude_url="https://genius.com/Juice-wrld-already-dead-v2-lyrics")
        self.assertEqual(queries[0], "Juice WRLD Already V2")
        self.assertEqual(queries[-1], "Juice WRLD Already")
        self.assertLessEqual(len(queries), 3)
        self.assertEqual(candidates[0]["url"], "https://genius.com/Juice-wrld-already-lyrics")

    def test_limit_and_duplicates(self):
        ns, _ = self.make([hit("Lucid Dreams", "Juice WRLD", f"Juice-wrld-lucid-dreams-{i}")
                           for i in range(8)])
        candidates, _ = ns["_genius_alternative_candidates"]("song.mp3", limit=3)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(len({item["url"] for item in candidates}), 3)

    def test_lyrics_similarity_ignores_headers_and_footer(self):
        ns = load_functions()
        similar = ns["_lyrics_body_similarity"](
            "[Verse 1]\nI still see your shadows in my room\n\nhttps://genius.com/A-lyrics",
            "[Intro]\nI still see your shadows in my room")
        different = ns["_lyrics_body_similarity"](
            "I still see your shadows in my room", "Something totally unrelated here")
        self.assertEqual(similar, 1.0)
        self.assertLess(different, 0.3)


if __name__ == "__main__":
    unittest.main()
