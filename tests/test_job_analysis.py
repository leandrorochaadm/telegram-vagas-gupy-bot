"""Unit tests for the pure job analysis functions.

Run: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import job_analysis as ja  # noqa: E402
from description_extractor import (  # noqa: E402
    Education, EnglishLevel, Knockout, Requirement, ResumeCoverage, ResumeProfile, WorkModel,
)
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


# --- knockout summary and report -------------------------------------------------------------------------------

EL, ED, WM = EnglishLevel, Education, WorkModel
P, S, M, U = ResumeCoverage.PRESENT, ResumeCoverage.SYNONYM, ResumeCoverage.MISSING, ResumeCoverage.UNKNOWN


def knockout(years=None, english=EL.UNKNOWN, education=ED.UNKNOWN, work_model=WM.UNKNOWN):
    return Knockout(min_years=years, english=english, education=education, work_model=work_model)


def profile(years=None, english=EL.UNKNOWN, education=ED.UNKNOWN):
    return ResumeProfile(years=years, english=english, education=education, coverage={})


def report(**overrides):
    values = dict(
        generated_at=datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc), total_jobs=20, senior_jobs=0,
        duplicate_jobs=0, skipped_jobs=0, pending_jobs=0, filters="", items=[], learned_now=[],
        discarded_phrases=[], knockout=ja.summarize_knockout([], None), resume=profile(), resume_check_failed=False,
    )
    values.update(overrides)
    return ja.Report(**values)


def ranked(name, category=Category.TECHNOLOGY, share=0.5, required_share=None, weight=None, senior_share=0.0,
           coverage=None, resume_term="", origin=ItemOrigin.DICTIONARY, jobs=1, metadata_only=False):
    required = share if required_share is None else required_share
    return RankedItem(category, name, jobs, share, required, share if weight is None else weight, senior_share,
                      coverage, resume_term, origin, metadata_only)


def knockout_report(jobs_knockouts, resume, **overrides):
    jobs = [job(str(i), knockout=k) for i, k in enumerate(jobs_knockouts)]
    return report(total_jobs=len(jobs), knockout=ja.summarize_knockout(jobs, resume), resume=resume, **overrides)


class KnockoutSummaryTests(unittest.TestCase):
    def test_meets_years_share_limit(self):
        jobs = [job("1", knockout=knockout(years=y)) for y in (8, 3, 6)] + [job("4")]
        summary = ja.summarize_knockout(jobs, profile(years=6))
        self.assertEqual(summary.years_asked, [3, 6, 8])
        self.assertAlmostEqual(summary.meets_years_share, 2 / 3)
        text = ja.render_markdown(report(knockout=summary, total_jobs=4))
        self.assertIn("Você atende **67%** das vagas em tempo de experiência.", text)

    def test_meets_years_share_none(self):
        no_years = [job("1")]
        with_years = [job("1", knockout=knockout(years=3))]
        self.assertIsNone(ja.summarize_knockout(no_years, profile(years=5)).meets_years_share)
        self.assertIsNone(ja.summarize_knockout(with_years, profile(years=None)).meets_years_share)
        self.assertIsNone(ja.summarize_knockout(with_years, None).meets_years_share)
        text = ja.render_markdown(report(knockout=ja.summarize_knockout(no_years, profile(years=5))))
        self.assertIn("**Anos de experiência:** nenhuma vaga informa.", text)
        self.assertNotIn("Você atende", text)

    def test_knockout_unknown_values(self):
        jobs = [job("1"), job("2")]
        summary = ja.summarize_knockout(jobs, None)
        self.assertEqual(summary.english[EL.UNKNOWN], 2)
        self.assertEqual(summary.education[ED.UNKNOWN], 2)
        self.assertEqual(summary.work_model[WM.UNKNOWN], 2)
        self.assertEqual(summary.english_asked_share, 0.0)
        text = ja.render_markdown(report(knockout=summary, total_jobs=2))
        self.assertIn("2 vagas não informam.", text)
        empty = ja.summarize_knockout([], None)
        self.assertEqual((empty.years_asked, empty.english_asked_share, empty.education_asked_share), ([], 0.0, 0.0))
        self.assertEqual(set(empty.english), set(EL))

    def test_knockout_share_base_is_all_jobs(self):
        ks = [knockout(english=EL.INTERMEDIATE)] * 30 + [knockout(english=EL.NONE)] * 10 + [knockout()] * 60
        summary = ja.summarize_knockout([job(str(i), knockout=k) for i, k in enumerate(ks)], None)
        self.assertAlmostEqual(summary.english_asked_share, 0.3)
        text = ja.render_markdown(knockout_report(ks, profile()))
        self.assertIn("**Inglês:** 30% pedem intermediário. 60 vagas não informam.", text)
        self.assertIn("⚠️ Seu currículo não informa o nível de inglês, e 30% das vagas pedem.", text)
        ks = [knockout(education=ED.NOT_REQUIRED)] * 5
        summary = ja.summarize_knockout([job(str(i), knockout=k) for i, k in enumerate(ks)], None)
        self.assertEqual(summary.education_asked_share, 0.0)
        self.assertIn("**Formação:** nenhuma vaga pede.", ja.render_markdown(knockout_report(ks, None)))

    def test_undeclared_knockout_warning_threshold(self):
        def build(asking, resume):
            ks = [knockout(english=EL.ADVANCED)] * asking + [knockout()] * (100 - asking)
            return ja.render_markdown(knockout_report(ks, resume))

        self.assertIn("⚠️ Seu currículo não informa o nível de inglês, e 30% das vagas pedem. Declare, se tiver.",
                      build(30, profile()))
        self.assertNotIn("não informa o nível de inglês", build(29, profile()))
        ks = ([knockout(education=ED.BACHELOR, years=3)] * 30 + [knockout(education=ED.BACHELOR)] * 10
              + [knockout(english=EL.BASIC)] * 60)
        text = ja.render_markdown(knockout_report(ks, profile(english=EL.FLUENT)))
        self.assertIn("⚠️ Seu currículo não informa a formação, e 40% das vagas pedem.", text)
        self.assertIn("⚠️ Seu currículo não informa o tempo de experiência, e 30% das vagas pedem.", text)
        self.assertNotIn("nível de inglês", text)


class ReportTests(unittest.TestCase):
    def full_report(self):
        items = [
            ranked("Flutter", share=0.9, coverage=P),
            ranked("Dart", share=0.8, coverage=P),
            ranked("Trabalho em equipe", Category.SKILL, share=0.5, weight=0.5, coverage=S,
                   resume_term="trabalho em grupo"),
            ranked("GraphQL", share=0.4, weight=0.4, coverage=M),
            ranked("Mentorar", Category.RESPONSIBILITY, share=0.2, senior_share=0.6, coverage=S,
                   resume_term="code review"),
        ]
        return report(items=items, senior_jobs=10, learned_now=[("Rive", 4), ("Lottie", 7)],
                      discarded_phrases=[("ambiente de producao", 41), ("experiencia comprovada", 33)],
                      resume=profile(years=5), filters="pleno")

    def test_render_markdown_sections_in_order(self):
        text = ja.render_markdown(self.full_report())
        self.assertTrue(text.startswith("# Análise das vagas — 20/11/2026 09:30\n"))
        self.assertTrue(text.endswith("\n"))
        titles = ["## Cobertura do seu currículo", "## Critérios eliminatórios", "## O que ajustar primeiro",
                  "## Diferenciais das vagas sênior", "## Para o perfil da Gupy e do LinkedIn",
                  "## Tecnologias mais pedidas", "## Habilidades mais pedidas", "## Responsabilidades mais pedidas",
                  "## Tecnologias aprendidas nesta análise", "## Expressões descartadas"]
        positions = [text.index(title) for title in titles]
        self.assertEqual(positions, sorted(positions))
        with self.assertRaises(ValueError):
            ja.render_markdown(report(generated_at=datetime(2026, 11, 20, 12, 30)))

    def test_header_counts_and_filters(self):
        text = ja.render_markdown(report(total_jobs=148, duplicate_jobs=12, skipped_jobs=3, pending_jobs=0))
        self.assertIn("**148 vagas analisadas** · 12 repetidas foram juntadas · filtros: nenhum", text)
        self.assertIn("3 vagas não puderam ser analisadas. 0 ficaram para a próxima análise.", text)
        text = ja.render_markdown(report(total_jobs=1, duplicate_jobs=1, skipped_jobs=1, pending_jobs=1, filters=" x "))
        self.assertIn("**1 vaga analisada** · 1 repetida foi juntada · filtros: x", text)
        self.assertIn("1 vaga não pôde ser analisada. 1 ficou para a próxima análise.", text)

    def test_small_sample_warning(self):
        warning = "⚠️ Amostra pequena: com menos de 20 vagas o ranking ainda muda muito."
        self.assertIn(warning, ja.render_markdown(report(total_jobs=19)))
        self.assertNotIn(warning, ja.render_markdown(report(total_jobs=20)))

    def test_synonym_suggests_vacancy_wording(self):
        text = ja.render_markdown(self.full_report())
        self.assertIn("↔️ troque “trabalho em grupo” por “Trabalho em equipe”", text)
        self.assertIn("❌ só inclua se você tiver essa experiência", text)
        adjust = text.split("## O que ajustar primeiro")[1].split("## Diferenciais")[0]
        self.assertNotIn("Flutter", adjust)
        self.assertLess(adjust.index("Trabalho em equipe"), adjust.index("GraphQL"))
        self.assertIn("| 1 | Trabalho em equipe |", adjust)
        outside = [ranked(f"T{i:02d}", weight=1.0 - i / 100, coverage=P) for i in range(30)]
        outside.append(ranked("Fora", weight=0.01, coverage=M))
        adjust = ja.render_markdown(report(items=outside)).split("## O que ajustar primeiro")[1]
        self.assertIn("Nada a ajustar entre os itens mais pedidos.", adjust.split("##")[0])

    def test_metadata_only_technology(self):
        text = ja.render_markdown(report(items=[ranked("Codemagic", coverage=M, metadata_only=True)]))
        self.assertIn("❌ está só nas palavras-chave do PDF; coloque no texto", text)

    def test_report_without_resume(self):
        items = [ranked("Flutter", coverage=None)]
        ks = ja.summarize_knockout([job("1", knockout=knockout(english=EL.ADVANCED))], None)
        text = ja.render_markdown(report(items=items, resume=None, knockout=ks, total_jobs=1))
        for absent in ("## Cobertura do seu currículo", "## O que ajustar primeiro", "## Para o perfil",
                       "No currículo", "Você atende", "⚠️ Seu currículo"):
            self.assertNotIn(absent, text)
        self.assertIn("## Critérios eliminatórios", text)
        self.assertIn("| Flutter | 1 | 50% | 50% | 0% |", text)

    def test_coverage_not_compared_when_resume_check_failed(self):
        items = [ranked("Flutter", coverage=P), ranked("Comunicação", Category.SKILL, coverage=U)]
        text = ja.render_markdown(report(items=items, resume=None, resume_check_failed=True))
        self.assertIn("| Habilidades | não comparado |", text)
        self.assertIn("| Responsabilidades | não comparado |", text)
        self.assertIn("| Tecnologias | **100%** |", text)
        self.assertIn("Não deu para comparar habilidades e responsabilidades com o currículo agora. "
                      "Rode de novo mais tarde.", text)
        for title in ("## O que ajustar primeiro", "## Para o perfil da Gupy e do LinkedIn"):
            self.assertIn(title, text)
        self.assertNotIn("⚠️ Seu currículo", text)

    def test_ai_marker_in_tables(self):
        items = [ranked("Melos", origin=ItemOrigin.AI), ranked("Rive", origin=ItemOrigin.LEARNED),
                 ranked("Flutter", origin=ItemOrigin.DICTIONARY),
                 ranked("Gestão de times", Category.RESPONSIBILITY, origin=ItemOrigin.FREQUENT)]
        items += [ranked(f"H{i:02d}", Category.SKILL, weight=1.0 - i / 100) for i in range(31)]
        text = ja.render_markdown(report(items=items))
        self.assertIn("| Melos (detectada pela IA) |", text)
        self.assertIn("| Gestão de times (detectada pela IA) |", text)
        self.assertIn("| Rive |", text)
        self.assertIn("| Flutter |", text)
        skills = text.split("## Habilidades mais pedidas")[1].split("## Responsabilidades")[0]
        self.assertEqual(skills.count("| H0") + skills.count("| H1") + skills.count("| H2") + skills.count("| H3"), 30)
        self.assertEqual(ja.render_markdown(report()).count("Nenhum item."), 3)

    def test_pipe_in_name_is_escaped(self):
        text = ja.render_markdown(report(items=[ranked("A|B")]))
        self.assertIn("| A\\|B |", text)

    def test_learned_and_discarded_sections(self):
        text = ja.render_markdown(report())
        self.assertIn("Nenhuma tecnologia nova nesta análise.", text)
        self.assertIn("Nenhuma expressão descartada.", text)
        text = ja.render_markdown(self.full_report())
        self.assertIn("Lottie (7 vagas) · Rive (4)", text)
        self.assertIn('"ambiente de producao" (41 vagas) · "experiencia comprovada" (33)', text)

    def test_differentials_section(self):
        text = ja.render_markdown(report(senior_jobs=0, items=[ranked("A", senior_share=0.0)]))
        self.assertIn("0 vagas sênior.", text)
        self.assertIn("Nenhum diferencial claro nas vagas sênior.", text)
        text = ja.render_markdown(self.full_report())
        self.assertIn("10 vagas sênior.", text)
        self.assertIn("| Mentorar | 60% | 20% | ↔️ “code review” |", text)

    def test_profile_section(self):
        text = ja.render_markdown(report(items=[ranked("Flutter", coverage=M)]))
        self.assertIn("Nenhuma competência do seu currículo aparece entre as mais pedidas.", text)
        text = ja.render_markdown(self.full_report())
        self.assertIn("`Flutter` · `Dart`", text)


class CsvTests(unittest.TestCase):
    def test_render_csv(self):
        items = [
            ranked("Flutter", share=0.375, weight=0.4, coverage=S, resume_term="flutter sdk"),
            ranked("Comunicação", Category.SKILL, share=0.2, weight=0.2),
            ranked("Dart", share=0.1, weight=0.9, coverage=None),
        ]
        lines = ja.render_csv(report(items=items)).split("\n")
        self.assertEqual(lines[0], "categoria,item,origem,vagas,percentual,percentual_obrigatorio,peso,"
                                   "percentual_senior,no_curriculo,palavra_no_curriculo")
        self.assertEqual(lines[1], "technology,Dart,dictionary,1,10.0,10.0,90.0,0.0,,")
        self.assertEqual(lines[2], "technology,Flutter,dictionary,1,37.5,37.5,40.0,0.0,synonym,flutter sdk")
        self.assertTrue(lines[3].startswith("skill,Comunicação"))
        many = [ranked(f"T{i:02d}") for i in range(35)]
        self.assertEqual(len(ja.render_csv(report(items=many)).splitlines()), 36)

    def test_csv_includes_single_ai_technology(self):
        text = ja.render_csv(report(items=[ranked("Melos", origin=ItemOrigin.AI, jobs=1)]))
        self.assertIn("technology,Melos,ai,1,", text)


if __name__ == "__main__":
    unittest.main()
