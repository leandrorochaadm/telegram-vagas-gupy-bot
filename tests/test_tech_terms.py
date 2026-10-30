"""Unit tests for the technology dictionary and exact-match search.

Run: python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tech_terms  # noqa: E402
from tech_terms import TECH_TERMS, all_terms, find_technologies, load_learned, normalize, save_learned  # noqa: E402


class TechTermsTest(unittest.TestCase):
    def tmp_dir(self) -> Path:
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path)
        return Path(path)

    def test_aliases_map_to_one_name_once(self):
        text = "Experiência com CI/CD, integração contínua e INTEGRAÇÃO CONTÍNUA."
        self.assertEqual(find_technologies(text), {"CI/CD"})

    def test_short_and_symbol_terms(self):
        self.assertEqual(find_technologies("Fechamos o go-live", TECH_TERMS), set())
        self.assertNotIn("Go", find_technologies("go-live, go e Go-live"))
        self.assertIn("Go", find_technologies("Experiência com Go."))
        found = find_technologies("Usamos C#, C++, Node.js e NodeJS")
        self.assertTrue({"C#", "C++", "Node.js"} <= found)
        own = {"C": ("C",), "R": ("R",), "Go": ("Go",)}
        self.assertEqual(find_technologies("Usamos C# e C++", own), set())
        self.assertEqual(find_technologies("C e Java", own), {"C"})

    def test_whole_word_boundaries(self):
        self.assertNotIn("Java", find_technologies("Javascript"))
        self.assertNotIn("Swift", find_technologies("SwiftUI"))
        self.assertIn("Flutter", find_technologies("flutter-based apps"))
        found = find_technologies("flutter_bloc")
        self.assertIn("BLoC", found)
        self.assertNotIn("Flutter", found)

    def test_long_terms_ignore_case_and_accents(self):
        for text in ("RIVERPOD", "Riverpod", "riverpod"):
            self.assertEqual(find_technologies(text), {"Riverpod"})

    def test_ambiguous_canonical_not_searched(self):
        self.assertNotIn("REST", find_technologies("the rest of the team"))
        self.assertIn("REST", find_technologies("consumo de APIs REST"))

    def test_empty_text_finds_nothing(self):
        self.assertEqual(find_technologies(""), set())
        self.assertEqual(find_technologies("  \n\t "), set())

    def test_normalize(self):
        self.assertEqual(normalize("  Integração\n  Contínua "), "integracao continua")
        self.assertEqual(normalize("CI/CD C#"), "ci/cd c#")

    def test_catalog_size_groups_and_unique_aliases(self):
        self.assertGreaterEqual(len(TECH_TERMS), 80)
        for name in ("Flutter", "BLoC", "Kotlin", "Firestore", "REST", "Clean Architecture", "CI/CD",
                     "Sentry", "AWS", "Scrum", "Google Play", "LLM", "Testes unitários"):
            self.assertIn(name, TECH_TERMS)
        names = [normalize(name) for name in TECH_TERMS]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("testes", names)
        self.assertNotIn("teste", names)
        owners: dict[str, str] = {}
        for name, aliases in TECH_TERMS.items():
            self.assertTrue(aliases)
            for alias in aliases:
                key = normalize(alias)
                self.assertTrue(key)
                self.assertNotIn(key, owners, f"{alias!r} owned by {owners.get(key)} and {name}")
                owners[key] = name

    def test_learned_terms_load_and_conflicts(self):
        folder = self.tmp_dir()
        path = folder / "learned.json"
        self.assertEqual(load_learned(path), {})
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(load_learned(path), {})
        path.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(load_learned(path), {})
        path.write_text(json.dumps({"Ok": ["ok", " "], "Bad": "x", "Bad2": [1]}), encoding="utf-8")
        self.assertEqual(load_learned(path), {"Ok": ("ok",)})

        learned = {"flutter": ("fluttr",), "Lottie": ("nodejs",)}
        merged = all_terms(learned)
        self.assertEqual(merged["Flutter"], TECH_TERMS["Flutter"])
        self.assertNotIn("flutter", merged)
        self.assertEqual(merged["Lottie"], ("Lottie",))
        self.assertEqual(find_technologies("nodejs", merged), {"Node.js"})

    def test_save_learned_round_trip_is_atomic(self):
        path = self.tmp_dir() / "a" / "b" / "learned.json"
        terms = {"Lottie": ("lottie", "lottie-flutter")}
        save_learned(path, terms)
        self.assertEqual(load_learned(path), terms)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["learned.json"])

    def test_save_learned_failure_keeps_old_file(self):
        path = self.tmp_dir() / "learned.json"
        save_learned(path, {"Old": ("old",)})
        with mock.patch.object(tech_terms.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                save_learned(path, {"New": ("new",)})
        self.assertEqual(load_learned(path), {"Old": ("old",)})
        self.assertEqual([p.name for p in path.parent.iterdir()], ["learned.json"])

    def test_find_technologies_uses_learned_terms(self):
        before = dict(TECH_TERMS)
        merged = all_terms({"Lottie": ("lottie-flutter",)})
        # "-" is a word boundary, so "flutter" inside "lottie-flutter" also matches Flutter
        self.assertIn("Lottie", find_technologies("usamos lottie-flutter", merged))
        self.assertEqual(TECH_TERMS, before)
        self.assertNotIn("Lottie", TECH_TERMS)


if __name__ == "__main__":
    unittest.main()
