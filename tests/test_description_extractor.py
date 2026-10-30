"""Unit tests for the extraction types and the extraction cache.

Run: python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
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
    GROUP_BATCH_SIZE,
    MAX_INPUT_CHARS,
    make_client,
    Knockout,
    PhraseKind,
    Requirement,
    ResumeCoverage,
    Coverage,
    ClaudeExtractor,
    ResumeProfile,
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


class FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.queue: list[object] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, *responses: object) -> None:
        self.messages = FakeMessages()
        self.messages.queue.extend(responses)


def api_response(payload: object, stop_reason: str = "end_turn") -> object:
    block = SimpleNamespace(type="text", text=json.dumps(payload))
    return SimpleNamespace(content=[block], stop_reason=stop_reason)


def raw_extraction(**overrides) -> dict:
    payload = {
        "skills": [{"name": "Comunicação", "requirement": "required"}, {"name": "Mentoria", "requirement": "desired"}],
        "responsibilities": ["Desenvolver aplicativos", "Revisar código"],
        "desired_technologies": ["Docker"],
        "extra_technologies": ["Flutter", "Docker"],
        "knockout": {"min_years": 3, "english": "intermediate", "education": "unknown", "work_model": "remote"},
    }
    payload.update(overrides)
    return payload


def extractor_with(*responses: object) -> tuple[ClaudeExtractor, FakeClient]:
    client = FakeClient(*responses)
    return ClaudeExtractor(client), client


def user_text(client: FakeClient, index: int = 0) -> str:
    return client.messages.calls[index]["messages"][0]["content"]


class ClaudeExtractorTest(unittest.TestCase):
    def test_extract_parses_payload(self):
        extractor, client = extractor_with(api_response(raw_extraction()))
        result = extractor.extract("Dev Flutter", "texto")
        self.assertEqual(result.skills[1], Skill("Mentoria", Requirement.DESIRED))
        self.assertEqual(result.responsibilities, ("Desenvolver aplicativos", "Revisar código"))
        self.assertEqual(result.knockout, Knockout(3, EnglishLevel.INTERMEDIATE, Education.UNKNOWN, WorkModel.REMOTE))
        kwargs = client.messages.calls[0]
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertIn("Dev Flutter", user_text(client))

    def test_models_per_call(self):
        extractor, client = extractor_with(
            api_response(raw_extraction()),
            api_response({"groups": []}),
            api_response({"years": None, "english": "unknown", "education": "unknown", "coverage": []}),
            api_response({"phrases": [{"phrase": "a", "kind": "skill"}]}),
        )
        extractor.extract("t", "x")
        extractor.group_labels(["a"])
        extractor.check_resume("cv", [])
        extractor.classify_phrases(["a"])
        models = [call["model"] for call in client.messages.calls]
        self.assertEqual(
            models,
            [description_extractor.EXTRACTION_MODEL] + [description_extractor.JUDGE_MODEL] * 3,
        )

    def test_extract_prompt_asks_portuguese_labels(self):
        extractor, client = extractor_with(api_response(raw_extraction()))
        extractor.extract("t", "x")
        self.assertIn("Brazilian Portuguese", client.messages.calls[0]["system"])

    def test_skill_without_marker_is_required(self):
        payload = raw_extraction(skills=[{"name": "A", "requirement": "required"}, {"name": "B", "requirement": "nice"}])
        extractor, client = extractor_with(api_response(payload))
        result = extractor.extract("t", "x")
        self.assertEqual([s.requirement for s in result.skills], [Requirement.REQUIRED, Requirement.UNKNOWN])
        self.assertIn("no indication", client.messages.calls[0]["system"])

    def test_min_years_parsing_in_payload(self):
        knockout = {"min_years": None, "english": "unknown", "education": "unknown", "work_model": "unknown"}
        extractor, client = extractor_with(
            api_response(raw_extraction()), api_response(raw_extraction(knockout=knockout))
        )
        self.assertEqual(extractor.extract("t", "x").knockout.min_years, 3)
        self.assertIsNone(extractor.extract("t", "x").knockout.min_years)
        self.assertIn("smallest number of years", client.messages.calls[0]["system"])

    def test_input_is_truncated(self):
        extractor, client = extractor_with(api_response(raw_extraction()), api_response(raw_extraction()))
        extractor.extract("t", "a" * MAX_INPUT_CHARS + "Z")
        extractor.extract("t", "a" * (MAX_INPUT_CHARS - 1) + "Z")
        self.assertNotIn("Z", user_text(client, 0))
        self.assertIn("Z", user_text(client, 1))

    def test_extract_caps_lists(self):
        skills = [{"name": f"S{i}", "requirement": "required"} for i in range(16)] + [
            {"name": "  ", "requirement": "required"}
        ]
        payload = raw_extraction(skills=skills, responsibilities=[f"R{i}" for i in range(16)] + [""])
        extractor, _ = extractor_with(api_response(payload))
        result = extractor.extract("t", "x")
        self.assertEqual(len(result.skills), 15)
        self.assertEqual(len(result.responsibilities), 15)

    def test_api_exception_raises_api_error(self):
        extractor, _ = extractor_with(RuntimeError("boom"))
        with self.assertRaises(ExtractionError) as ctx:
            extractor.extract("t", "x")
        self.assertEqual(ctx.exception.kind, ExtractionFailure.API)

    def test_invalid_response_raises(self):
        no_text = SimpleNamespace(content=[SimpleNamespace(type="thinking", text="")], stop_reason="end_turn")
        not_json = SimpleNamespace(content=[SimpleNamespace(type="text", text="oops")], stop_reason="end_turn")
        bad_years = raw_extraction(
            knockout={"min_years": "3", "english": "unknown", "education": "unknown", "work_model": "unknown"}
        )
        for response in (
            not_json,
            no_text,
            api_response(bad_years),
            api_response(raw_extraction(), stop_reason="max_tokens"),
            api_response(raw_extraction(), stop_reason="refusal"),
        ):
            extractor, _ = extractor_with(response)
            with self.assertRaises(ExtractionError) as ctx:
                extractor.extract("t", "x")
            self.assertEqual(ctx.exception.kind, ExtractionFailure.INVALID_RESPONSE)

    def test_group_labels_missing_maps_to_itself(self):
        groups = {"groups": [{"canonical": "Node.js", "members": ["Node", "NodeJS"]}]}
        extractor, _ = extractor_with(api_response(groups))
        self.assertEqual(
            extractor.group_labels(["Node", "NodeJS", "Flutter"]),
            {"Node": "Node.js", "NodeJS": "Node.js", "Flutter": "Flutter"},
        )

    def test_group_labels_ignores_noise(self):
        groups = {
            "groups": [
                {"canonical": "A1", "members": ["A", "Stranger"]},
                {"canonical": "  ", "members": ["B"]},
                {"canonical": "Other", "members": ["A"]},
            ]
        }
        extractor, _ = extractor_with(api_response(groups))
        self.assertEqual(extractor.group_labels(["A", "B"]), {"A": "A1", "B": "B"})

    def test_group_labels_batches(self):
        empty = api_response({"groups": []})
        for count, calls in ((GROUP_BATCH_SIZE, 1), (GROUP_BATCH_SIZE + 1, 2)):
            extractor, client = extractor_with(empty, empty)
            result = extractor.group_labels([f"L{i}" for i in range(count)])
            self.assertEqual(len(client.messages.calls), calls)
            self.assertEqual(len(result), count)
        extractor, client = extractor_with()
        self.assertEqual(extractor.group_labels([]), {})
        self.assertEqual(client.messages.calls, [])

    def test_coverage_unknown_fallback(self):
        payload = {
            "years": None,
            "english": "unknown",
            "education": "unknown",
            "coverage": [
                {"label": "a", "status": "synonym", "resume_term": " "},
                {"label": "b", "status": "maybe", "resume_term": ""},
                {"label": "d", "status": "synonym", "resume_term": "git"},
                {"label": "extra", "status": "present", "resume_term": ""},
            ],
        }
        extractor, _ = extractor_with(api_response(payload))
        coverage = extractor.check_resume("cv", ["a", "b", "c", "d"]).coverage
        unknown = Coverage(ResumeCoverage.UNKNOWN, "")
        self.assertEqual(coverage, {"a": unknown, "b": unknown, "c": unknown, "d": Coverage(ResumeCoverage.SYNONYM, "git")})

    def test_check_resume_profile(self):
        payload = {"years": 6, "english": "advanced", "education": "c1", "coverage": []}
        extractor, client = extractor_with(api_response(payload))
        profile = extractor.check_resume("c" * (MAX_INPUT_CHARS + 500), [])
        self.assertEqual(profile, ResumeProfile(6, EnglishLevel.ADVANCED, Education.UNKNOWN, {}))
        self.assertLessEqual(user_text(client).count("c"), MAX_INPUT_CHARS + 10)
        self.assertNotIn("c" * (MAX_INPUT_CHARS + 1), user_text(client))

    def test_check_resume_invalid_years(self):
        for years in (-1, True):
            payload = {"years": years, "english": "unknown", "education": "unknown", "coverage": []}
            extractor, _ = extractor_with(api_response(payload))
            with self.assertRaises(ExtractionError) as ctx:
                extractor.check_resume("cv", [])
            self.assertEqual(ctx.exception.kind, ExtractionFailure.INVALID_RESPONSE)

    def test_classify_phrases_fallbacks(self):
        payload = {
            "phrases": [
                {"phrase": "flutter", "kind": "technology"},
                {"phrase": "equipe", "kind": "skill"},
                {"phrase": "liderar", "kind": "responsibility"},
                {"phrase": "weird", "kind": "other"},
                {"phrase": "stranger", "kind": "skill"},
            ]
        }
        extractor, client = extractor_with(api_response(payload))
        result = extractor.classify_phrases(["flutter", "equipe", "liderar", "weird", "missing"])
        self.assertEqual(
            result,
            {
                "flutter": PhraseKind.TECHNOLOGY,
                "equipe": PhraseKind.SKILL,
                "liderar": PhraseKind.RESPONSIBILITY,
                "weird": PhraseKind.IGNORE,
                "missing": PhraseKind.IGNORE,
            },
        )
        empty_extractor, empty_client = extractor_with()
        self.assertEqual(empty_extractor.classify_phrases([]), {})
        self.assertEqual(empty_client.messages.calls, [])

    def test_schemas_are_strict(self):
        def check(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node["additionalProperties"], False)
                    self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)

        schemas = (
            description_extractor._EXTRACTION_SCHEMA,
            description_extractor._GROUPING_SCHEMA,
            description_extractor._RESUME_SCHEMA,
            description_extractor._PHRASES_SCHEMA,
        )
        for schema in schemas:
            check(schema)
        knockout = description_extractor._EXTRACTION_SCHEMA["properties"]["knockout"]["properties"]
        self.assertEqual(knockout["english"]["enum"], [m.api_value for m in EnglishLevel])
        phrase_kind = description_extractor._PHRASES_SCHEMA["properties"]["phrases"]["items"]["properties"]["kind"]
        self.assertEqual(phrase_kind["enum"], [m.api_value for m in PhraseKind])


class MakeClientTest(unittest.TestCase):
    def test_make_client_missing_key(self):
        for value in ("", "   "):
            with mock.patch.dict(os.environ, {description_extractor.ENV_API_KEY: value}):
                sys.modules.pop("anthropic", None)
                with self.assertRaises(ExtractionError) as ctx:
                    make_client()
                self.assertEqual(ctx.exception.kind, ExtractionFailure.MISSING_KEY)
                self.assertNotIn("anthropic", sys.modules)

    def test_make_client_builds_sdk_client(self):
        fake_module = SimpleNamespace(Anthropic=mock.Mock(return_value="client"))
        with mock.patch.dict(os.environ, {description_extractor.ENV_API_KEY: " sk-test \n"}):
            with mock.patch.dict(sys.modules, {"anthropic": fake_module}):
                self.assertEqual(make_client(), "client")
        fake_module.Anthropic.assert_called_once_with(
            api_key="sk-test", max_retries=description_extractor.CLIENT_MAX_RETRIES
        )


if __name__ == "__main__":
    unittest.main()
