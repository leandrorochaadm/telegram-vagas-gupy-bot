"""Unit tests for the extraction types and the extraction cache.

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

import description_extractor  # noqa: E402
from description_extractor import (  # noqa: E402
    Education,
    EnglishLevel,
    Extraction,
    ExtractionCache,
    ExtractionError,
    ExtractionFailure,
    Knockout,
    PhraseKind,
    Requirement,
    ResumeCoverage,
    Skill,
    WorkModel,
    extraction_from_payload,
    extraction_to_payload,
)

NO_ANTHROPIC_AT_IMPORT = "anthropic" not in sys.modules


def sample_extraction() -> Extraction:
    return Extraction(
        skills=(Skill("Comunicação", Requirement.REQUIRED), Skill("Mentoria", Requirement.DESIRED)),
        responsibilities=("Desenvolver aplicativos",),
        desired_technologies=("Firebase",),
        extra_technologies=("Flutter", "Dart"),
        knockout=Knockout(3, EnglishLevel.INTERMEDIATE, Education.BACHELOR, WorkModel.REMOTE),
    )


def raw_payload() -> dict:
    return {
        "skills": [{"name": "Comunicação", "requirement": "required"}],
        "responsibilities": ["Desenvolver aplicativos"],
        "desired_technologies": [],
        "extra_technologies": ["Flutter"],
        "knockout": {"min_years": 2, "english": "basic", "education": "technical", "work_model": "hybrid"},
    }


class EnumTest(unittest.TestCase):
    ENUMS = (ResumeCoverage, Requirement, EnglishLevel, Education, WorkModel, PhraseKind)

    def test_from_api_round_trip_all_enums(self):
        for enum in self.ENUMS:
            for member in enum:
                self.assertIs(enum.from_api(member.api_value), member)

    def test_from_api_unknown_falls_back_to_unknown(self):
        for enum in (ResumeCoverage, Requirement, EnglishLevel, Education, WorkModel):
            for raw in ("c1", None, 3, ""):
                self.assertIs(enum.from_api(raw), enum.UNKNOWN)

    def test_phrase_kind_unknown_is_ignore(self):
        for raw in ("other", None, 1):
            self.assertIs(PhraseKind.from_api(raw), PhraseKind.IGNORE)

    def test_levels_at_least(self):
        self.assertTrue(EnglishLevel.INTERMEDIATE.at_least(EnglishLevel.BASIC))
        self.assertTrue(EnglishLevel.BASIC.at_least(EnglishLevel.BASIC))
        self.assertFalse(EnglishLevel.NONE.at_least(EnglishLevel.BASIC))
        self.assertFalse(EnglishLevel.UNKNOWN.at_least(EnglishLevel.NONE))
        self.assertTrue(Education.BACHELOR.at_least(Education.TECHNICAL))
        self.assertFalse(Education.NOT_REQUIRED.at_least(Education.TECHNICAL))
        self.assertEqual(EnglishLevel.FLUENT.rank, 4)
        self.assertEqual(Education.POSTGRAD.rank, 3)
        self.assertEqual(Education.UNKNOWN.rank, -1)

    def test_extraction_error_keeps_kind(self):
        error = ExtractionError("boom", ExtractionFailure.API)
        self.assertIs(error.kind, ExtractionFailure.API)
        self.assertEqual(str(error), "boom")

    def test_module_does_not_import_anthropic(self):
        self.assertTrue(NO_ANTHROPIC_AT_IMPORT)
        self.assertNotIn("anthropic", sys.modules)
        self.assertIsNotNone(description_extractor)


class PayloadTest(unittest.TestCase):
    def test_payload_round_trip(self):
        extraction = sample_extraction()
        self.assertEqual(extraction_from_payload(extraction_to_payload(extraction)), extraction)

    def test_payload_unknown_values(self):
        raw = raw_payload()
        raw["knockout"].update({"min_years": None, "english": "c1"})
        extraction = extraction_from_payload(raw)
        self.assertIsNone(extraction.knockout.min_years)
        self.assertIs(extraction.knockout.english, EnglishLevel.UNKNOWN)

    def test_payload_malformed_raises_value_error(self):
        def with_knockout(**changes):
            raw = raw_payload()
            raw["knockout"].update(changes)
            return raw

        no_knockout = raw_payload()
        del no_knockout["knockout"]
        text_skills = raw_payload()
        text_skills["skills"] = "Python"
        for bad in (
            [],
            no_knockout,
            text_skills,
            with_knockout(min_years="3"),
            with_knockout(min_years=True),
            with_knockout(min_years=-1),
        ):
            with self.assertRaises(ValueError):
                extraction_from_payload(bad)


class ExtractionCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = self.tmp / "cache" / "extractions.json"

    def test_cache_key_format(self):
        cache = ExtractionCache(self.path)
        key = cache.key("abc", "texto")
        self.assertRegex(key, r"^abc:[0-9a-f]{16}:1$")
        self.assertEqual(key, cache.key("abc", "texto"))

    def test_cache_key_changes_with_text_and_version(self):
        cache = ExtractionCache(self.path)
        base = cache.key("abc", "texto")
        self.assertNotEqual(base, cache.key("abc", "texto novo"))
        with mock.patch.object(description_extractor, "PROMPT_VERSION", 2):
            self.assertNotEqual(base, cache.key("abc", "texto"))

    def test_cache_round_trip_through_file(self):
        cache = ExtractionCache(self.path)
        cache.put("k", sample_extraction())
        cache.save()
        self.assertTrue(self.path.parent.is_dir())
        self.assertEqual(ExtractionCache(self.path).get("k"), sample_extraction())

    def test_missing_cache_file_starts_empty(self):
        self.assertIsNone(ExtractionCache(self.path).get("k"))

    def test_corrupt_cache_starts_empty(self):
        self.path.parent.mkdir(parents=True)
        for content in ("not json {", "[]"):
            self.path.write_text(content, encoding="utf-8")
            cache = ExtractionCache(self.path)
            self.assertIsNone(cache.get("k"))
            cache.put("k", sample_extraction())
            cache.save()
            self.assertEqual(ExtractionCache(self.path).get("k"), sample_extraction())

    def test_malformed_entry_returns_none(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"bad": {"skills": "x"}, "good": extraction_to_payload(sample_extraction())}),
            encoding="utf-8",
        )
        cache = ExtractionCache(self.path)
        self.assertIsNone(cache.get("bad"))
        self.assertEqual(cache.get("good"), sample_extraction())

    def test_put_does_not_write(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{}", encoding="utf-8")
        ExtractionCache(self.path).put("k", sample_extraction())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{}")

    def test_save_is_atomic(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{}", encoding="utf-8")
        cache = ExtractionCache(self.path)
        cache.put("k", sample_extraction())
        with mock.patch("os.replace", side_effect=OSError):
            with self.assertRaises(OSError):
                cache.save()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{}")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["extractions.json"])


if __name__ == "__main__":
    unittest.main()
