"""Tests for the description analysis pipeline (`analyze_descriptions.run`), with a fake database and fake AI.

Run: python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyze_descriptions as ad  # noqa: E402
import description_extractor  # noqa: E402
from description_extractor import (  # noqa: E402
    Coverage, Education, EnglishLevel, Extraction, ExtractionCache, ExtractionError, ExtractionFailure, Knockout,
    PhraseKind, Requirement, ResumeCoverage, ResumeProfile, Skill, WorkModel,
)
from fake_store import FakeStore  # noqa: E402
from job_analysis import Category, ItemOrigin  # noqa: E402
from store import Source, StoreError  # noqa: E402

NOW = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)  # 08:00 in Brasília
BASE_TIME = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
KNOCKOUT = Knockout(min_years=None, english=EnglishLevel.UNKNOWN, education=Education.UNKNOWN,
                    work_model=WorkModel.UNKNOWN)
RESUME_TEX = (
    "\\documentclass{article}\n"
    "\\hypersetup{pdfkeywords={Docker, Python}}\n"
    "\\begin{document}\n"
    "Desenvolvedor Flutter com Riverpod e Dart.\n"
    "\\end{document}\n"
)


def extraction(skills=(), responsibilities=(), desired=(), extra=()) -> Extraction:
    return Extraction(
        skills=tuple(Skill(name, requirement) for name, requirement in skills),
        responsibilities=tuple(responsibilities),
        desired_technologies=tuple(desired),
        extra_technologies=tuple(extra),
        knockout=KNOCKOUT,
    )


def api_error() -> ExtractionError:
    return ExtractionError("boom", ExtractionFailure.API)


class FakeExtractor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.extractions: dict[str, Extraction] = {}
        self.fail_titles: set[str] = set()
        self.grouping: dict[str, str] = {}
        self.group_error = False
        self.resume_profile: ResumeProfile | None = None
        self.resume_error = False
        self.phrase_kinds: dict[str, PhraseKind] = {}
        self.phrase_error = False
        self.interrupt_at: int | None = None

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)

    def extract(self, title, text) -> Extraction:
        self.calls.append(("extract", (title, text)))
        if self.interrupt_at is not None and self.count("extract") == self.interrupt_at:
            raise KeyboardInterrupt
        if title in self.fail_titles:
            raise api_error()
        return self.extractions.get(title, extraction())

    def group_labels(self, labels) -> dict[str, str]:
        self.calls.append(("group_labels", tuple(labels)))
        if self.group_error:
            raise api_error()
        return {label: self.grouping.get(label, label) for label in labels}

    def check_resume(self, resume_text, labels) -> ResumeProfile:
        self.calls.append(("check_resume", tuple(labels)))
        if self.resume_error:
            raise api_error()
        if self.resume_profile is not None:
            return self.resume_profile
        return ResumeProfile(years=5, english=EnglishLevel.INTERMEDIATE, education=Education.BACHELOR, coverage={})

    def classify_phrases(self, phrases) -> dict[str, PhraseKind]:
        self.calls.append(("classify_phrases", tuple(phrases)))
        if self.phrase_error:
            raise api_error()
        return {phrase: self.phrase_kinds.get(phrase, PhraseKind.IGNORE) for phrase in phrases}


class AnalyzeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.out_dir = tmp / "reports"
        self.cache_path = tmp / "cache" / "extractions.json"
        self.learned_path = tmp / "cache" / "learned_terms.json"
        self.db = FakeStore()
        self.extractor = FakeExtractor()
        self._counter = 0

    def add_saved(self, title, text, *, company=None, seniority="", published="2026-10-01", link=None) -> str:
        self._counter += 1
        link = link or f"https://example.com/{self._counter}"
        company = f"Empresa {self._counter}" if company is None else company
        self.db.add_job_description(link, title, company, Source.GUPY, published)
        record_id = next(row.id for row in self.db.pending_descriptions() if row.link == link)
        self.db.save_description(record_id, text, seniority, "", BASE_TIME + timedelta(minutes=self._counter))
        return record_id

    def go(self, *, resume_text=None, since=None, seniority=None, max_new=100, now=NOW, extractor=None):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            report = ad.run(
                self.db, extractor or self.extractor, ExtractionCache(self.cache_path),
                resume_text=resume_text, since=since, seniority=seniority, max_new=max_new, now=now,
                out_dir=self.out_dir, learned_path=self.learned_path,
            )
        self.output = output.getvalue()
        return report

    @staticmethod
    def find(report, category, name):
        return next((item for item in report.items if item.category == category and item.name == name), None)


class RunTest(AnalyzeTestCase):
    def test_full_run_writes_report_and_csv(self):
        for number in range(3):
            self.add_saved(f"Dev {number}", "Flutter com Riverpod e Kotlin Multiplatform.")
        report = self.go(resume_text=RESUME_TEX)
        self.assertEqual(report.total_jobs, 3)
        self.assertIsNotNone(report.resume)
        md = self.out_dir / "analise-vagas-2026-11-02-0800.md"
        csv_file = self.out_dir / "analise-vagas-2026-11-02-0800.csv"
        self.assertIn("Análise das vagas", md.read_text(encoding="utf-8"))
        self.assertIn("categoria", csv_file.read_text(encoding="utf-8"))

    def test_report_paths_brasilia_time(self):
        md, csv_file = ad.report_paths(Path("out"), NOW)
        self.assertEqual(md, Path("out/analise-vagas-2026-11-02-0800.md"))
        self.assertEqual(csv_file, Path("out/analise-vagas-2026-11-02-0800.csv"))
        with self.assertRaises(ValueError):
            ad.report_paths(Path("out"), datetime(2026, 11, 2, 11, 0))

    def test_naive_now_raises(self):
        self.db = FakeStore(fail_with=StoreError())
        with self.assertRaises(ValueError):
            self.go(now=datetime(2026, 11, 2, 11, 0))

    def test_describe_filters(self):
        self.assertEqual(ad.describe_filters(None, None), "")
        self.assertEqual(ad.describe_filters(None, "  "), "")
        since = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)  # still Oct 4th in Brasília
        self.assertEqual(ad.describe_filters(since, "pleno"), "desde 04/10/2026, senioridade “pleno”")

    def test_grouped_labels_are_merged(self):
        self.add_saved("A", "Texto a.")
        self.add_saved("B", "Texto b.")
        self.extractor.extractions = {
            "A": extraction(skills=[("Comunicação clara", Requirement.REQUIRED)]),
            "B": extraction(skills=[("Boa comunicação", Requirement.REQUIRED)]),
        }
        self.extractor.grouping = {"Comunicação clara": "Comunicação", "Boa comunicação": "Comunicação"}
        report = self.go()
        item = self.find(report, Category.SKILL, "Comunicação")
        self.assertEqual(item.jobs, 2)
        self.assertEqual(len([i for i in report.items if i.category == Category.SKILL]), 1)

    def test_same_job_labels_in_one_group_keep_required(self):
        self.add_saved("A", "Texto a.")
        self.extractor.extractions = {
            "A": extraction(skills=[("Comunicação clara", Requirement.DESIRED),
                                    ("Boa comunicação", Requirement.REQUIRED)]),
        }
        self.extractor.grouping = {"Comunicação clara": "Comunicação", "Boa comunicação": "Comunicação"}
        item = self.find(self.go(), Category.SKILL, "Comunicação")
        self.assertEqual(item.required_share, 1.0)

    def test_group_labels_failure_uses_identity(self):
        self.add_saved("A", "Texto a.")
        self.extractor.extractions = {"A": extraction(skills=[("Comunicação clara", Requirement.REQUIRED)])}
        self.extractor.group_error = True
        report = self.go()
        self.assertIsNotNone(self.find(report, Category.SKILL, "Comunicação clara"))

    def test_tech_coverage_from_resume(self):
        for number in range(2):
            self.add_saved(f"Dev {number}", "Riverpod, Kotlin Multiplatform e Docker.")
        report = self.go(resume_text=RESUME_TEX)
        riverpod = self.find(report, Category.TECHNOLOGY, "Riverpod")
        kmp = self.find(report, Category.TECHNOLOGY, "Kotlin Multiplatform")
        docker = self.find(report, Category.TECHNOLOGY, "Docker")
        self.assertEqual(riverpod.coverage, ResumeCoverage.PRESENT)
        self.assertEqual(kmp.coverage, ResumeCoverage.MISSING)
        self.assertFalse(kmp.metadata_only)
        self.assertEqual(docker.coverage, ResumeCoverage.MISSING)
        self.assertTrue(docker.metadata_only)
        self.assertIn("só nas palavras-chave", (self.out_dir / "analise-vagas-2026-11-02-0800.md").read_text("utf-8"))

    def test_skill_coverage_comes_from_resume_check(self):
        self.add_saved("A", "Texto a.")
        self.extractor.extractions = {"A": extraction(skills=[("Liderança", Requirement.REQUIRED)])}
        self.extractor.resume_profile = ResumeProfile(
            years=3, english=EnglishLevel.BASIC, education=Education.TECHNICAL,
            coverage={"Liderança": Coverage(ResumeCoverage.SYNONYM, "Gestão de times")},
        )
        report = self.go(resume_text=RESUME_TEX)
        item = self.find(report, Category.SKILL, "Liderança")
        self.assertEqual((item.coverage, item.resume_term), (ResumeCoverage.SYNONYM, "Gestão de times"))
        self.assertEqual(self.extractor.count("check_resume"), 1)

    def test_no_resume_means_no_comparison(self):
        self.add_saved("A", "Flutter.")
        report = self.go()
        self.assertIsNone(report.resume)
        self.assertFalse(report.resume_check_failed)
        self.assertEqual(self.extractor.count("check_resume"), 0)
        self.assertIsNone(self.find(report, Category.TECHNOLOGY, "Flutter").coverage)

    def test_resume_check_failure_still_writes_report(self):
        self.add_saved("A", "Flutter e Riverpod.")
        self.extractor.extractions = {"A": extraction(skills=[("Liderança", Requirement.REQUIRED)])}
        self.extractor.resume_error = True
        report = self.go(resume_text=RESUME_TEX)
        self.assertTrue(report.resume_check_failed)
        self.assertIsNone(report.resume)
        self.assertIsNone(self.find(report, Category.SKILL, "Liderança").coverage)
        self.assertEqual(self.find(report, Category.TECHNOLOGY, "Riverpod").coverage, ResumeCoverage.PRESENT)
        text = (self.out_dir / "analise-vagas-2026-11-02-0800.md").read_text(encoding="utf-8")
        self.assertIn("Não deu para comparar habilidades e responsabilidades", text)

    def test_missing_key_is_not_swallowed(self):
        self.add_saved("A", "Texto.")
        self.extractor.extract = mock.Mock(side_effect=ExtractionError("no key", ExtractionFailure.MISSING_KEY))
        with self.assertRaises(ExtractionError):
            self.go()


class CacheAndLimitsTest(AnalyzeTestCase):
    def test_cache_avoids_new_calls(self):
        for number in range(3):
            self.add_saved(f"Dev {number}", "Flutter e Dart.")
        first = self.go()
        self.assertEqual(self.extractor.count("extract"), 3)
        self.extractor.calls.clear()
        second = self.go()
        self.assertEqual(self.extractor.count("extract"), 0)
        self.assertEqual([(i.name, i.jobs) for i in first.items], [(i.name, i.jobs) for i in second.items])

    def test_max_new_leaves_rest_pending(self):
        for number in range(5):
            self.add_saved(f"Dev {number}", "Flutter.")
        report = self.go(max_new=2)
        self.assertEqual((report.total_jobs, report.pending_jobs), (2, 3))
        self.assertEqual(self.extractor.count("extract"), 2)
        self.assertIn("3 ficaram para a próxima análise.",
                      (self.out_dir / "analise-vagas-2026-11-02-0800.md").read_text(encoding="utf-8"))

    def test_cached_jobs_do_not_count_toward_max_new(self):
        for number in range(3):
            self.add_saved(f"Dev {number}", "Flutter.")
        self.go(max_new=2)
        report = self.go(max_new=2)
        self.assertEqual((report.total_jobs, report.pending_jobs), (3, 0))

    def test_cache_saved_every_25_and_on_interrupt(self):
        for number in range(26):
            self.add_saved(f"Dev {number}", f"Texto {number}.")
        original = ExtractionCache.save
        with mock.patch.object(ExtractionCache, "save", autospec=True, side_effect=original) as save:
            self.go()
        self.assertEqual(save.call_count, 2)

        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        self.cache_path = other / "extractions.json"
        self.extractor = FakeExtractor()
        self.extractor.interrupt_at = 3
        with self.assertRaises(KeyboardInterrupt):
            self.go()
        cache = ExtractionCache(self.cache_path)
        rows = self.db.saved_descriptions()
        cached = [row for row in rows if cache.get(cache.key(row.id, row.description)) is not None]
        self.assertEqual(len(cached), 2)

    def test_api_error_skips_job(self):
        self.add_saved("Boa", "Flutter.")
        self.add_saved("Ruim", "Dart.")
        self.extractor.fail_titles = {"Ruim"}
        report = self.go()
        self.assertEqual((report.total_jobs, report.skipped_jobs), (1, 1))
        cache = ExtractionCache(self.cache_path)
        bad = next(row for row in self.db.saved_descriptions() if row.title == "Ruim")
        self.assertIsNone(cache.get(cache.key(bad.id, bad.description)))
        self.assertIn("1 vaga não pôde ser analisada.",
                      (self.out_dir / "analise-vagas-2026-11-02-0800.md").read_text(encoding="utf-8"))

    def test_all_extractions_failed_writes_nothing(self):
        self.add_saved("Ruim", "Dart.")
        self.extractor.fail_titles = {"Ruim"}
        self.assertIsNone(self.go())
        self.assertIn(ad.MSG_NOTHING_ANALYZED, self.output)
        self.assertFalse(self.out_dir.exists())


class SelectionTest(AnalyzeTestCase):
    def test_no_descriptions_writes_nothing(self):
        self.assertIsNone(self.go())
        self.assertIn(ad.MSG_NO_DESCRIPTIONS, self.output)
        self.assertFalse(self.out_dir.exists())
        self.assertEqual(self.extractor.calls, [])

    def test_filter_that_matches_nothing_writes_nothing(self):
        self.add_saved("Dev", "Flutter.", seniority="Júnior")
        self.assertIsNone(self.go(seniority="pleno"))
        self.assertIn(ad.MSG_NO_DESCRIPTIONS, self.output)
        self.assertEqual(self.extractor.calls, [])

    def test_since_filter_is_passed_to_the_store(self):
        self.add_saved("Dev", "Flutter.")
        self.assertIsNone(self.go(since=BASE_TIME + timedelta(days=30)))
        self.assertIsNotNone(self.go(since=BASE_TIME - timedelta(days=1)))

    def test_empty_description_is_skipped(self):
        self.add_saved("Vazia", "   \n ")
        self.add_saved("Cheia", "Flutter.")
        report = self.go()
        self.assertEqual((report.total_jobs, report.skipped_jobs), (1, 1))
        self.assertEqual(self.extractor.count("extract"), 1)

    def test_duplicates_counted_once_and_not_extracted(self):
        self.add_saved("Dev Flutter", "Flutter curto.", company="Acme")
        self.add_saved("Dev Flutter", "Flutter com um texto bem mais comprido.", company="Acme")
        report = self.go()
        self.assertEqual((report.total_jobs, report.duplicate_jobs), (1, 1))
        self.assertEqual(self.extractor.count("extract"), 1)
        self.assertIn("1 repetida foi juntada",
                      (self.out_dir / "analise-vagas-2026-11-02-0800.md").read_text(encoding="utf-8"))

    def test_same_title_in_different_companies_are_two_jobs(self):
        self.add_saved("Dev Flutter", "Flutter.", company="Acme")
        self.add_saved("Dev Flutter", "Flutter.", company="Beta")
        report = self.go()
        self.assertEqual((report.total_jobs, report.duplicate_jobs), (2, 0))

    def test_duplicate_tie_breaks_by_newest_then_smallest_id(self):
        older = self.add_saved("Dev", "Mesmo texto.", company="Acme")
        newer = self.add_saved("Dev", "Mesmo texto.", company="Acme")
        selection = ad.select_descriptions(self.db.saved_descriptions(), None)
        self.assertEqual([row.id for row in selection.kept], [newer])
        self.assertNotEqual(older, newer)

    def test_seniority_filter_runs_before_dedupe(self):
        self.add_saved("Dev Flutter", "Texto curto.", company="Acme", seniority="Pleno")
        self.add_saved("Dev Flutter", "Texto bem mais comprido que o outro.", company="Acme", seniority="Júnior")
        report = self.go(seniority="pleno")
        self.assertEqual((report.total_jobs, report.duplicate_jobs), (1, 0))


class LearningAndPhrasesTest(AnalyzeTestCase):
    def test_learned_term_found_by_exact_search(self):
        for number in range(3):
            self.add_saved(f"Dev {number}", f"Texto {number}.")
        names = {"Dev 0": ("Lottie", "Rive"), "Dev 1": ("Lottie", "Rive"), "Dev 2": ("Lottie",)}
        self.extractor.extractions = {title: extraction(extra=extra) for title, extra in names.items()}
        first = self.go()
        self.assertEqual(first.learned_now, [("Lottie", 3)])
        self.assertIn("Lottie", self.learned_path.read_text(encoding="utf-8"))
        self.assertNotIn("Rive", self.learned_path.read_text(encoding="utf-8"))
        rive = self.find(first, Category.TECHNOLOGY, "Rive")
        self.assertEqual((rive.jobs, rive.origin), (2, ItemOrigin.AI))
        self.assertIsNone(self.find(first, Category.TECHNOLOGY, "Lottie"))

        self.add_saved("Dev 3", "Animações com Lottie.")
        second = self.go()
        lottie = self.find(second, Category.TECHNOLOGY, "Lottie")
        self.assertEqual((lottie.jobs, lottie.origin), (1, ItemOrigin.LEARNED))
        self.assertEqual(second.learned_now, [])

    def test_ai_technology_desired_when_listed_as_desired(self):
        self.add_saved("A", "Texto.")
        self.extractor.extractions = {"A": extraction(desired=("Rive",), extra=("Rive",))}
        item = self.find(self.go(), Category.TECHNOLOGY, "Rive")
        self.assertEqual(item.required_share, 0.0)
        self.assertEqual(item.share, 1.0)

    def test_classified_phrases_join_ranking(self):
        for number in range(3):
            self.add_saved(f"Dev {number}", "Gestão de estoque. Ambiente dinâmico.")
        self.extractor.phrase_kinds = {"gestao de estoque": PhraseKind.SKILL}
        report = self.go()
        item = self.find(report, Category.SKILL, "gestao de estoque")
        self.assertEqual((item.jobs, item.origin), (3, ItemOrigin.FREQUENT))
        self.assertIn(("ambiente dinamico", 3), report.discarded_phrases)
        self.assertNotIn(("gestao de estoque", 3), report.discarded_phrases)
        self.assertEqual(self.extractor.count("classify_phrases"), 1)

    def test_classify_phrases_failure_and_empty(self):
        for number in range(3):
            self.add_saved(f"Dev {number}", "Gestão de estoque.")
        self.extractor.phrase_error = True
        report = self.go()
        self.assertEqual(report.discarded_phrases, [("gestao de estoque", 3)])
        self.assertIsNone(self.find(report, Category.SKILL, "gestao de estoque"))

        self.db = FakeStore()
        self.extractor = FakeExtractor()
        self.cache_path = self.cache_path.parent / "other.json"
        for number in range(3):
            self.add_saved(f"Dev {number}", f"Palavra{number}.")
        report = self.go()
        self.assertEqual(report.discarded_phrases, [])
        self.assertEqual(self.extractor.count("classify_phrases"), 0)

    def test_known_terms_do_not_become_phrases(self):
        for number in range(3):
            self.add_saved(f"Dev {number}", "Kotlin Multiplatform.")
        self.go()
        self.assertEqual(self.extractor.count("classify_phrases"), 0)


if __name__ == "__main__":
    unittest.main()


def write_env(path: Path, **values: str) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp
        self.default_env = tmp / "etc-bot.env"  # not created: the system file is absent by default
        self.local_env = tmp / "local.env"
        for name, value in (
            ("DEFAULT_ENV_FILE", str(self.default_env)),
            ("LOCAL_ENV_FILE", self.local_env),
            ("REPORTS_DIR", tmp / "reports"),
            ("CACHE_PATH", tmp / "cache" / "extractions.json"),
            ("LEARNED_TERMS_PATH", tmp / "cache" / "learned.json"),
        ):
            patcher = mock.patch.object(ad, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.db = FakeStore()
        self.from_env_calls = 0
        patcher = mock.patch.object(ad.store, "from_env", side_effect=self._from_env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_calls: list[dict] = []
        self.run_result = None
        patcher = mock.patch.object(ad, "run", side_effect=self._run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.make_client = mock.patch.object(ad, "make_client", return_value=object()).start()
        self.addCleanup(mock.patch.stopall)

    def _from_env(self) -> FakeStore:
        self.from_env_calls += 1
        return self.db

    def _run(self, *args, **kwargs):
        if isinstance(self.run_result, Exception):
            raise self.run_result
        self.run_calls.append(kwargs)
        return self.run_result

    def call_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ad.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def alerts(self):
        return self.db.pending_alerts()

    def test_main_prints_report_paths(self) -> None:
        self.run_result = object()
        with mock.patch.object(ad, "datetime") as fake_dt:
            fake_dt.now.return_value = NOW
            code, out, _ = self.call_main()
        markdown, csv = ad.report_paths(ad.REPORTS_DIR, NOW)
        self.assertEqual(code, 0)
        self.assertIn(ad.MSG_REPORT_SAVED.format(path=markdown), out)
        self.assertIn(ad.MSG_CSV_SAVED.format(path=csv), out)

    def test_since_days_becomes_utc_since(self) -> None:
        before = datetime.now(timezone.utc)
        self.call_main("--since-days", "30")
        kwargs = self.run_calls[0]
        self.assertIsNotNone(kwargs["since"].tzinfo)
        self.assertEqual(kwargs["since"], kwargs["now"] - timedelta(days=30))
        self.assertGreaterEqual(kwargs["now"], before)

    def test_since_days_must_be_positive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            ad.main(["--since-days", "0"])
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.run_calls, [])

    def test_max_new_default_and_override(self) -> None:
        self.call_main()
        self.call_main("--max-new", "5")
        self.assertEqual([call["max_new"] for call in self.run_calls], [ad.MAX_NEW_EXTRACTIONS, 5])

    def test_env_file_resolution(self) -> None:
        self.assertEqual(ad.resolve_env_file(None), self.local_env)
        self.default_env.write_text("", encoding="utf-8")
        self.assertEqual(ad.resolve_env_file(None), self.default_env)
        explicit = self.tmp / "missing.env"
        self.assertEqual(ad.resolve_env_file(str(explicit)), explicit)

    def test_env_file_does_not_override_environment(self) -> None:
        write_env(self.local_env, RESUME_PATH="from-file", OTHER="x")
        os.environ["RESUME_PATH"] = ""
        os.environ["OTHER"] = "from-env"
        self.call_main()
        self.assertEqual(os.environ["OTHER"], "from-env")

    def test_unreadable_env_file_exits_1(self) -> None:
        with mock.patch.object(ad, "load_env_file", side_effect=PermissionError):
            code, _, err = self.call_main()
        self.assertEqual(code, 1)
        self.assertIn(ad.MSG_ENV_FILE.format(path=self.local_env), err)
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(self.run_calls, [])

    def test_resume_path_from_env(self) -> None:
        resume = self.tmp / "cv.tex"
        resume.write_text(RESUME_TEX, encoding="utf-8")
        write_env(self.local_env, RESUME_PATH=str(resume))
        code, _, _ = self.call_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.run_calls[0]["resume_text"], RESUME_TEX)
        write_env(self.local_env, RESUME_PATH="")
        os.environ.pop("RESUME_PATH", None)
        self.call_main()
        self.assertIsNone(self.run_calls[1]["resume_text"])

    def test_resume_flag_wins_over_env(self) -> None:
        env_resume = self.tmp / "env.tex"
        flag_resume = self.tmp / "flag.tex"
        env_resume.write_text(RESUME_TEX + "% env\n", encoding="utf-8")
        flag_resume.write_text(RESUME_TEX, encoding="utf-8")
        os.environ["RESUME_PATH"] = str(env_resume)
        self.call_main("--resume", str(flag_resume))
        self.assertEqual(self.run_calls[0]["resume_text"], RESUME_TEX)

    def test_missing_key_exits_1(self) -> None:
        self.make_client.side_effect = ad.ExtractionError("x", ExtractionFailure.MISSING_KEY)
        code, _, err = self.call_main()
        self.assertEqual(code, 1)
        self.assertIn(ad.MSG_MISSING_KEY.format(path=self.local_env), err)
        self.assertEqual(self.run_calls, [])
        self.assertEqual(len(self.alerts()), 1)

    def test_missing_key_with_real_make_client(self) -> None:
        self.make_client.side_effect = None
        with mock.patch.object(ad, "make_client", description_extractor.make_client):
            code, _, err = self.call_main()
        self.assertEqual(code, 1)
        self.assertIn(ad.MSG_MISSING_KEY.format(path=self.local_env), err)

    def test_store_error_exits_1(self) -> None:
        self.db = FakeStore(fail_with=StoreError())
        code, _, err = self.call_main()
        self.assertEqual(code, 1)
        self.assertIn(ad.MSG_STORE.format(error=StoreError()), err)
        self.assertEqual(self.from_env_calls, 1)
        self.assertEqual(self.run_calls, [])

    def test_missing_store_config_exits_1(self) -> None:
        error = StoreError(ad.store.MSG_MISSING_CONFIG, ad.store.StoreFailure.MISSING_CONFIG)
        with mock.patch.object(ad.store, "from_env", side_effect=error):
            code, _, err = self.call_main()
        self.assertEqual(code, 1)
        self.assertIn(ad.MSG_STORE.format(error=ad.store.MSG_MISSING_CONFIG), err)
        self.assertEqual(self.alerts(), [])

    def test_store_error_during_run_exits_1(self) -> None:
        self.run_result = StoreError()
        code, _, err = self.call_main()
        self.assertEqual(code, 1)
        self.assertIn(ad.MSG_STORE.format(error=StoreError()), err)
        self.assertEqual(self.alerts(), [])

    def test_missing_resume_exits_1(self) -> None:
        missing = self.tmp / "nope.tex"
        self.make_client.side_effect = ad.ExtractionError("x", ExtractionFailure.MISSING_KEY)
        code, _, err = self.call_main("--resume", str(missing))
        self.assertEqual(code, 1)
        self.assertIn(ad.MSG_RESUME.format(path=missing), err)
        self.assertNotIn("ANTHROPIC_API_KEY", err)
        self.make_client.assert_not_called()
        self.assertEqual(self.run_calls, [])

    def test_tex_without_document_exits_1(self) -> None:
        bad = self.tmp / "bad.tex"
        bad.write_text("no document here", encoding="utf-8")
        code, _, err = self.call_main("--resume", str(bad))
        self.assertEqual(code, 1)
        self.assertIn(ad.MSG_RESUME.format(path=bad), err)
        self.assertEqual(len(self.alerts()), 1)

    def test_failure_records_alert(self) -> None:
        missing = self.tmp / "nope.tex"
        self.call_main("--resume", str(missing))
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].label, Source.ANALYSIS.api_value)
        self.assertEqual(alerts[0].text, f"A análise semanal das vagas falhou: Não foi possível ler o currículo em {missing}.")

    def test_alert_write_failure_is_ignored(self) -> None:
        missing = self.tmp / "nope.tex"
        self.db = FakeStore(fail_with=StoreError())
        code, _, err = self.call_main("--resume", str(missing))
        self.assertEqual(code, 1)
        self.assertIn(ad.MSG_RESUME.format(path=missing), err)
        self.assertIn(ad.MSG_STORE.format(error=StoreError()), err)
        # connect works but add_alerts fails
        db = FakeStore()
        db.add_alerts = mock.Mock(side_effect=StoreError())
        err_io = io.StringIO()
        with contextlib.redirect_stderr(err_io):
            ad.record_failure_alert(db, "motivo.")
        self.assertIn(ad.MSG_ALERT_NOT_SAVED.format(error=StoreError()), err_io.getvalue())

    def test_unexpected_error_exits_1_and_records_alert(self) -> None:
        self.run_result = RuntimeError("boom")
        code, _, err = self.call_main()
        self.assertEqual(code, 1)
        self.assertIn(ad.MSG_UNEXPECTED.format(kind="RuntimeError"), err)
        self.assertIn("Traceback", err)
        self.assertEqual(len(self.alerts()), 1)

    def test_no_descriptions_exits_0(self) -> None:
        self.run_result = None
        code, out, err = self.call_main()
        self.assertEqual((code, out, err), (0, "", ""))
