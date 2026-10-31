"""Unit tests for the pure job analysis functions.

Run: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import job_analysis as ja  # noqa: E402
from description_extractor import Education, EnglishLevel, Knockout, Requirement, ResumeCoverage, WorkModel  # noqa: E402
from job_analysis import Category, ItemOrigin, JobItems, RankedItem  # noqa: E402

KNOCKOUT = Knockout(min_years=None, english=EnglishLevel.UNKNOWN, education=Education.UNKNOWN,
                    work_model=WorkModel.UNKNOWN)
REQ, DES, UNK = Requirement.REQUIRED, Requirement.DESIRED, Requirement.UNKNOWN
TERMS = {"Flutter": ("flutter",), "Dart": ("dart",)}


def job(record_id, title="Dev Flutter", seniority="", technologies=None, skills=None, responsibilities=(),
        knockout=KNOCKOUT):
    return JobItems(record_id, title, seniority, technologies or {}, skills or {}, frozenset(responsibilities),
                    knockout)


def item(name, category=Category.TECHNOLOGY, share=0.5, weight=0.5, senior_share=0.0, coverage=None,
         resume_term=""):
    return RankedItem(category, name, 1, share, weight, weight, senior_share, coverage, resume_term,
                      ItemOrigin.DICTIONARY)


class TexTests(unittest.TestCase):
    def test_tex_to_text(self):
        raw = ("\\documentclass{article}\n\\definecolor{x}{rgb}{0,0,0}\n\\setmainfont{/usr/fonts/Foo.ttf}\n"
               "\\begin{document}\n% comment line\nGrowth of 100\\% with \\textbf{Riverpod} % trailing\n"
               "\\vspace{2pt}\n\\begin{itemize}\n\\item Dart\\\\ Flutter\n\\end{itemize}\n\\end{document}\nafter")
        text = ja.tex_to_text(raw)
        self.assertIn("100% with Riverpod", text)
        self.assertIn("Dart Flutter", text)
        for banned in ("definecolor", "Foo.ttf", "comment", "trailing", "itemize", "2pt", "after", "{", "\\"):
            self.assertNotIn(banned, text)
        with self.assertRaises(ValueError):
            ja.tex_to_text("no document here")

    def test_tex_keywords(self):
        self.assertEqual(ja.tex_keywords("\\hypersetup{pdfkeywords={Flutter, Dart , }}"), ["Flutter", "Dart"])
        self.assertEqual(ja.tex_keywords("\\begin{document}x"), [])


class SeniorityTests(unittest.TestCase):
    def test_is_senior_whole_word(self):
        self.assertTrue(ja.is_senior("Desenvolvedor Flutter Sr.", ""))
        self.assertFalse(ja.is_senior("Srta Maria", ""))
        self.assertTrue(ja.is_senior("Tech Lead", ""))
        self.assertFalse(ja.is_senior("Leadership Coach", ""))
        self.assertTrue(ja.is_senior("Dev", "Sênior"))

    def test_seniority_filter_uses_title_too(self):
        self.assertTrue(ja.matches_seniority("pleno", "Dev Flutter Pleno", ""))
        self.assertTrue(ja.matches_seniority("pleno", "Dev Flutter", "Pleno"))
        self.assertFalse(ja.matches_seniority("pleno", "Dev Flutter", "Junior"))

    def test_matches_seniority_empty_filter(self):
        self.assertTrue(ja.matches_seniority("", "Dev", "Junior"))
        self.assertTrue(ja.matches_seniority("   ", "Dev", "Junior"))

    def test_dedupe_key_without_company(self):
        self.assertEqual(ja.dedupe_key("", "Dev Flutter", "2026-01-01"), ja.dedupe_key(" ", "Dev Flutter", "2026-01-01"))
        self.assertNotEqual(ja.dedupe_key("", "Dev", "2026-01-01"), ja.dedupe_key("", "Dev", "2026-01-02"))
        self.assertEqual(ja.dedupe_key("Acmé", "Dev Flutter", "a"), ja.dedupe_key("acme", "dev flutter", "b"))


class TechnologyTests(unittest.TestCase):
    def test_job_technologies_requirement(self):
        terms = {"Flutter": ("flutter",), "Dart": ("dart",), "Go": ("go",), "Rust": ("rust",)}
        result = ja.job_technologies("Flutter e Dart, diferencial: Rust", ("Rust", "Dart", "Go"), terms)
        self.assertEqual(result, {"Flutter": REQ, "Dart": DES, "Rust": DES})
        self.assertEqual(ja.job_technologies("Flutter", (), terms), {"Flutter": REQ})

    def test_enums_api_value(self):
        self.assertEqual(Category.RESPONSIBILITY.api_value, "responsibility")
        self.assertEqual(ItemOrigin.FREQUENT.api_value, "frequent")


class LearnTests(unittest.TestCase):
    def test_unknown_ai_technologies(self):
        result = ja.unknown_ai_technologies({"1": {"Flutter", "Lottie"}, "2": {"Flutter"}}, {}, TERMS)
        self.assertEqual(result, {"1": frozenset({"Lottie"})})

    def test_terms_to_learn_skips_known(self):
        ai = {str(i): {"Flutter", "Lottie", "Flutter SDK"} for i in range(3)}
        learned = ja.terms_to_learn(ai, {"Flutter SDK": "Flutter"}, TERMS, set())
        self.assertEqual(learned, {"Lottie": ("Lottie",)})

    def test_terms_to_learn_threshold(self):
        ai = {"1": {"Lottie", "Rive"}, "2": {"Lottie", "Rive"}, "3": {"Lottie"}}
        self.assertEqual(ja.terms_to_learn(ai, {}, TERMS, set()), {"Lottie": ("Lottie",)})
        self.assertIn("Rive", ja.unknown_ai_technologies(ai, {}, TERMS)["1"])

    def test_terms_to_learn_uses_grouping(self):
        ai = {"1": {"Node"}, "2": {"Node"}, "3": {"NodeJS"}}
        grouping = {"Node": "Node.js", "NodeJS": "Node.js"}
        self.assertEqual(ja.terms_to_learn(ai, grouping, TERMS, set()), {"Node.js": ("Node", "Node.js", "NodeJS")})

    def test_terms_to_learn_rejects_noise(self):
        names = {"a" * 41, "b" * 40, "Comunicação", "2.0"}
        ai = {str(i): set(names) for i in range(3)}
        learned = ja.terms_to_learn(ai, {}, TERMS, {"comunicacao"})
        self.assertEqual(set(learned), {"b" * 40})


class RankTests(unittest.TestCase):
    def test_item_counts_once_per_job(self):
        jobs = [job("1", technologies={"Flutter": REQ}), job("2")]
        ranked = ja.rank(jobs)
        self.assertEqual((ranked[0].jobs, ranked[0].share), (1, 0.5))

    def test_weight_favors_required(self):
        jobs = [
            job("1", technologies={"A": DES, "B": REQ}),
            job("2", technologies={"A": DES, "B": DES}),
            job("3", responsibilities=("Liderar",), skills={"S": UNK}),
            job("4"),
        ]
        ranked = {r.name: r for r in ja.rank(jobs)}
        self.assertAlmostEqual(ranked["A"].weight, 0.25)
        self.assertAlmostEqual(ranked["B"].weight, 0.375)
        self.assertAlmostEqual(ranked["S"].required_share, 0.25)
        self.assertEqual(ranked["Liderar"].required_share, ranked["Liderar"].share)
        names = [r.name for r in ja.rank(jobs)]
        self.assertLess(names.index("B"), names.index("A"))

    def test_rank_senior_share_zero_without_senior_jobs(self):
        ranked = ja.rank([job("1", technologies={"A": REQ})])
        self.assertTrue(all(r.senior_share == 0.0 for r in ranked))
        self.assertEqual(ja.rank([]), [])

    def test_rank_origins_and_senior_share(self):
        jobs = [job("1", title="Dev Sênior", technologies={"A": REQ}), job("2", technologies={"A": REQ})]
        ranked = ja.rank(jobs, {(Category.TECHNOLOGY, "A"): ItemOrigin.AI})
        self.assertEqual(ranked[0].origin, ItemOrigin.AI)
        self.assertEqual(ranked[0].senior_share, 1.0)

    def test_coverage_score(self):
        P, S, M = ResumeCoverage.PRESENT, ResumeCoverage.SYNONYM, ResumeCoverage.MISSING
        items = [item("a", weight=0.4, coverage=P), item("b", weight=0.4, coverage=S), item("c", weight=0.2, coverage=M)]
        self.assertAlmostEqual(ja.coverage_score(items, Category.TECHNOLOGY), 0.6)
        many = [item(f"i{i:02d}", weight=1.0 - i / 100, coverage=M) for i in range(30)]
        many.append(item("last", weight=0.01, coverage=P))
        self.assertEqual(ja.coverage_score(many, Category.TECHNOLOGY), 0.0)
        self.assertEqual(ja.coverage_score([], Category.TECHNOLOGY), 0.0)
        self.assertEqual(ja.coverage_score(items, Category.SKILL), 0.0)

    def test_differentials_limits(self):
        self.assertEqual(ja.differentials([item("a", share=0.2, senior_share=0.3)], 20)[0].name, "a")
        self.assertEqual(ja.differentials([item("a", share=0.2, senior_share=0.3)], 0), [])
        self.assertEqual(len(ja.differentials([item("a", share=0.1, senior_share=0.5)], 10)), 1)
        self.assertEqual(ja.differentials([item("a", share=0.1, senior_share=0.8)], 5), [])  # 4 senior jobs

    def test_profile_terms_only_owned_and_capped(self):
        P, S, M = ResumeCoverage.PRESENT, ResumeCoverage.SYNONYM, ResumeCoverage.MISSING
        items = [item(f"t{i:02d}", weight=1.0 - i / 100, coverage=P) for i in range(60)]
        items.append(item("Syn", weight=1.5, coverage=S, resume_term="synonym in resume"))
        items.append(item("Miss", weight=2.0, coverage=M))
        items.append(item("Resp", category=Category.RESPONSIBILITY, weight=3.0, coverage=P))
        result = ja.profile_terms(items)
        self.assertEqual(len(result), 50)
        self.assertEqual(result[0], "Syn")
        self.assertNotIn("Miss", result)
        self.assertNotIn("Resp", result)
        self.assertNotIn("synonym in resume", result)


class NgramTests(unittest.TestCase):
    def test_frequent_ngrams_rules(self):
        texts = {str(i): f"x{i}." for i in range(5)}
        texts["0"] = "metodologias ágeis. metodologias ágeis. Flutter avançado. de gestão. gestão e"
        result = ja.frequent_ngrams(texts, {"flutter"})
        self.assertEqual(result, {"metodologias ageis": frozenset({"0"})})
        texts = {str(i): f"x{i}." for i in range(100)}
        for i in range(19):
            texts[str(i)] = "metodologias ágeis"
        self.assertEqual(ja.frequent_ngrams(texts, set()), {})
        self.assertEqual(ja.frequent_ngrams({}, set()), {})
        crossing = {"1": "alta gestao. times grandes"}
        self.assertNotIn("gestao times", ja.frequent_ngrams(crossing, set()))

    def test_frequent_ngrams_prefers_longer(self):
        texts = {"1": "gestao de times", "2": "gestao de times"}
        self.assertEqual(set(ja.frequent_ngrams({"1": "gestão de times"}, set())), {"gestao de times"})
        self.assertEqual(set(ja.frequent_ngrams(texts, set())), {"gestao de times"})
        texts = {"1": "gestao de times", "2": "times ageis"}
        texts["3"] = "times ageis"
        result = ja.frequent_ngrams({**texts, "4": "gestao de times", "5": "x"}, set())
        self.assertIn("gestao de times", result)
        many = {"1": " ".join(f"w{i} x{i}" for i in range(40))}
        self.assertEqual(len(ja.frequent_ngrams(many, set())), 30)


if __name__ == "__main__":
    unittest.main()
