"""Pure functions for the job description analysis: resume text, counting, ranking and phrases.

Standard library only; no network, database or file access.
"""
import csv
import io
import re
import statistics
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from description_extractor import (
    Education,
    EnglishLevel,
    Knockout,
    Requirement,
    ResumeCoverage,
    ResumeProfile,
    WorkModel,
)
from tech_terms import MAX_TERM_CHARS, find_technologies, normalize


class Category(StrEnum):
    TECHNOLOGY = "technology"
    SKILL = "skill"
    RESPONSIBILITY = "responsibility"

    @property
    def api_value(self) -> str:
        return self.value


class ItemOrigin(StrEnum):
    DICTIONARY = "dictionary"
    LEARNED = "learned"
    AI = "ai"
    FREQUENT = "frequent"

    @property
    def api_value(self) -> str:
        return self.value


LEARN_MIN_JOBS = 3
DESIRED_WEIGHT = 0.5
PROFILE_SKILLS_MAX = 50
SENIOR_MARKERS = ("senior", "sênior", "sr", "especialista", "lead", "líder", "staff", "principal")
TOP_N = 30
MIN_JOBS_FOR_TRUST = 20
DIFFERENTIAL_LIFT = 1.5
DIFFERENTIAL_MIN_JOBS = 5
NGRAM_MIN_SHARE = 0.2
NGRAM_TOP = 30
SYNONYM_COVERAGE_FACTOR = 0.5
STOPWORDS: frozenset[str] = frozenset(
    "de da do das dos e ou a o as os em no na nos nas para pra por com sem um uma que se ao aos sua seu suas seus "
    "como mais ser the and or of to in on for with an is are be our you your".split()
)
_EPSILON = 1e-9  # tolerance for fraction comparisons
KNOCKOUT_WARNING_SHARE = 0.30
REPORT_TIMEZONE = ZoneInfo("America/Sao_Paulo")

_PRESENT_COVERAGE = (ResumeCoverage.PRESENT, ResumeCoverage.SYNONYM)


@dataclass(frozen=True)
class JobItems:
    record_id: str
    title: str
    seniority: str
    technologies: dict[str, Requirement]
    skills: dict[str, Requirement]
    responsibilities: frozenset[str]
    knockout: Knockout


@dataclass(frozen=True)
class RankedItem:
    category: Category
    name: str
    jobs: int
    share: float
    required_share: float
    weight: float
    senior_share: float
    coverage: ResumeCoverage | None
    resume_term: str
    origin: ItemOrigin
    metadata_only: bool = False


# --- resume text, seniority and duplicates ---------------------------------------------------------------------

_BEGIN_DOCUMENT = "\\begin{document}"
_END_DOCUMENT = "\\end{document}"
_COMMENT = re.compile(r"(?<!\\)%.*")
_ESCAPES = re.compile(r"\\([%_&#$])|\\\\|~")
_ENVIRONMENT = re.compile(r"\\(?:begin|end)\{[^{}]*\}")
_LAYOUT = re.compile(
    r"\\(?:vspace|hspace|setlength|color|fontsize|includegraphics)\*?(?:\[[^\]]*\])?(?:\{[^{}]*\})+"
)
_COMMAND = re.compile(r"\\[a-zA-Z]+\*?")
_KEYWORDS = re.compile(r"pdfkeywords\s*=\s*\{([^}]*)\}")


def tex_to_text(raw: str) -> str:
    start = raw.find(_BEGIN_DOCUMENT)
    if start < 0:
        raise ValueError("LaTeX without \\begin{document}")
    body = raw[start + len(_BEGIN_DOCUMENT):]
    end = body.find(_END_DOCUMENT)
    if end >= 0:
        body = body[:end]
    body = _COMMENT.sub("", body)
    body = _ESCAPES.sub(lambda m: m.group(1) or " ", body)
    body = _ENVIRONMENT.sub("", body)
    body = _LAYOUT.sub("", body)
    body = _COMMAND.sub("", body)
    body = body.replace("{", "").replace("}", "")
    lines = (" ".join(line.split()) for line in body.splitlines())
    return "\n".join(line for line in lines if line)


def tex_keywords(raw: str) -> list[str]:
    match = _KEYWORDS.search(raw)
    if match is None:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def _whole_word(term: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def is_senior(title: str, seniority: str) -> bool:
    haystack = normalize(f"{title} {seniority}")
    return any(_whole_word(normalize(marker), haystack) for marker in SENIOR_MARKERS)


def matches_seniority(filter_text: str, title: str, seniority: str) -> bool:
    wanted = normalize(filter_text)
    if not wanted:
        return True
    return wanted in normalize(seniority) or wanted in normalize(title)


def dedupe_key(company: str, title: str, published: str) -> str:
    if not company.strip():
        return f"{normalize(title)}|{published}"
    return f"{normalize(company)}|{normalize(title)}"


# --- technologies per job and terms to learn -------------------------------------------------------------------


def job_technologies(
    text: str, desired_technologies: tuple[str, ...], terms: dict[str, tuple[str, ...]]
) -> dict[str, Requirement]:
    found = find_technologies(text, terms)
    desired = find_technologies(" ".join(desired_technologies), terms) if desired_technologies else set()
    return {name: Requirement.DESIRED if name in desired else Requirement.REQUIRED for name in found}


def _unknown_with_originals(
    ai_names_per_job: dict[str, set[str]], grouping: dict[str, str], terms: dict[str, tuple[str, ...]]
) -> dict[str, dict[str, set[str]]]:
    """record_id -> canonical name -> original AI names grouped into it."""
    result: dict[str, dict[str, set[str]]] = {}
    for record_id, names in ai_names_per_job.items():
        canonicals: dict[str, set[str]] = defaultdict(set)
        for name in names:
            if find_technologies(name, terms):
                continue
            canonical = grouping.get(name, name)
            if find_technologies(canonical, terms):
                continue
            canonicals[canonical].add(name)
        if canonicals:
            result[record_id] = dict(canonicals)
    return result


def unknown_ai_technologies(
    ai_names_per_job: dict[str, set[str]], grouping: dict[str, str], terms: dict[str, tuple[str, ...]]
) -> dict[str, frozenset[str]]:
    unknown = _unknown_with_originals(ai_names_per_job, grouping, terms)
    return {record_id: frozenset(canonicals) for record_id, canonicals in unknown.items()}


def terms_to_learn(
    ai_names_per_job: dict[str, set[str]],
    grouping: dict[str, str],
    terms: dict[str, tuple[str, ...]],
    skill_labels: set[str],
) -> dict[str, tuple[str, ...]]:
    unknown = _unknown_with_originals(ai_names_per_job, grouping, terms)
    job_counts: dict[str, int] = defaultdict(int)
    originals: dict[str, set[str]] = defaultdict(set)
    for canonicals in unknown.values():
        for canonical, names in canonicals.items():
            job_counts[canonical] += 1
            originals[canonical] |= names
    skill_keys = {normalize(label) for label in skill_labels}
    learned: dict[str, tuple[str, ...]] = {}
    for canonical, count in job_counts.items():
        if count < LEARN_MIN_JOBS or len(canonical) > MAX_TERM_CHARS:
            continue
        if normalize(canonical) in skill_keys or not any(ch.isalpha() for ch in canonical):
            continue
        aliases: dict[str, str] = {}
        for alias in sorted({canonical} | originals[canonical]):
            if len(alias) <= MAX_TERM_CHARS:
                aliases.setdefault(normalize(alias), alias)
        learned[canonical] = tuple(aliases.values())
    return learned


# --- ranking, coverage, differentials and profile --------------------------------------------------------------


def _sort_key(item: RankedItem) -> tuple[float, str, str]:
    return (-item.weight, item.name, item.category.api_value)


def rank(
    jobs: list[JobItems], origins: Mapping[tuple[Category, str], ItemOrigin] | None = None
) -> list[RankedItem]:
    if not jobs:
        return []
    total = len(jobs)
    senior_flags = [is_senior(job.title, job.seniority) for job in jobs]
    senior_total = sum(senior_flags)
    # (category, name) -> [jobs, required jobs, senior jobs]
    counts: dict[tuple[Category, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for job, senior in zip(jobs, senior_flags):
        entries: list[tuple[Category, str, bool]] = []
        for name, requirement in job.technologies.items():
            entries.append((Category.TECHNOLOGY, name, requirement != Requirement.DESIRED))
        for name, requirement in job.skills.items():
            entries.append((Category.SKILL, name, requirement != Requirement.DESIRED))
        for name in job.responsibilities:
            entries.append((Category.RESPONSIBILITY, name, True))
        for category, name, required in entries:
            row = counts[(category, name)]
            row[0] += 1
            row[1] += int(required)
            row[2] += int(senior)
    items: list[RankedItem] = []
    for (category, name), (count, required_count, senior_count) in counts.items():
        share = count / total
        required_share = required_count / total
        items.append(
            RankedItem(
                category=category,
                name=name,
                jobs=count,
                share=share,
                required_share=required_share,
                weight=required_share + DESIRED_WEIGHT * (share - required_share),
                senior_share=senior_count / senior_total if senior_total else 0.0,
                coverage=None,
                resume_term="",
                origin=(origins or {}).get((category, name), ItemOrigin.DICTIONARY),
            )
        )
    return sorted(items, key=_sort_key)


def coverage_score(items: list[RankedItem], category: Category) -> float:
    top = sorted((item for item in items if item.category == category), key=_sort_key)[:TOP_N]
    total = sum(item.weight for item in top)
    if total <= 0:
        return 0.0
    covered = sum(
        item.weight * (1.0 if item.coverage == ResumeCoverage.PRESENT else SYNONYM_COVERAGE_FACTOR)
        for item in top
        if item.coverage in _PRESENT_COVERAGE
    )
    return covered / total


def differentials(items: list[RankedItem], senior_jobs: int) -> list[RankedItem]:
    if senior_jobs <= 0:
        return []
    chosen = [
        item
        for item in items
        if item.senior_share + _EPSILON >= item.share * DIFFERENTIAL_LIFT
        and round(item.senior_share * senior_jobs) >= DIFFERENTIAL_MIN_JOBS
    ]
    return sorted(chosen, key=lambda item: (-item.senior_share, item.name))


def profile_terms(items: list[RankedItem]) -> list[str]:
    owned = [
        item
        for item in items
        if item.category in (Category.TECHNOLOGY, Category.SKILL) and item.coverage in _PRESENT_COVERAGE
    ]
    owned.sort(key=lambda item: (-item.weight, item.name))
    return [item.name for item in owned[:PROFILE_SKILLS_MAX]]


# --- frequent phrases ------------------------------------------------------------------------------------------

_SENTENCE_BREAKS = re.compile(r"[.;:!?()•\n]")
_WORDS = re.compile(r"[a-z0-9]+")


def frequent_ngrams(texts: dict[str, str], known: set[str]) -> dict[str, frozenset[str]]:
    if not texts:
        return {}
    known_terms = [term for term in (normalize(k) for k in known) if term]
    found: dict[str, set[str]] = defaultdict(set)
    for record_id, text in texts.items():
        for chunk in _SENTENCE_BREAKS.split(_normalize_lines(text)):
            words = _WORDS.findall(chunk)
            for size in (2, 3):
                for start in range(len(words) - size + 1):
                    gram = words[start:start + size]
                    if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                        continue
                    found[" ".join(gram)].add(record_id)
    total = len(texts)
    kept = {
        phrase: ids
        for phrase, ids in found.items()
        if len(ids) / total + _EPSILON >= NGRAM_MIN_SHARE
        and not any(_whole_word(term, phrase) for term in known_terms)
    }
    redundant: set[str] = set()
    for phrase, ids in kept.items():
        words = phrase.split()
        if len(words) != 3:
            continue
        for part in (" ".join(words[:2]), " ".join(words[1:])):
            if part in kept and len(kept[part]) == len(ids):
                redundant.add(part)
    ordered = sorted((p for p in kept if p not in redundant), key=lambda p: (-len(kept[p]), p))
    return {phrase: frozenset(kept[phrase]) for phrase in ordered[:NGRAM_TOP]}


def _normalize_lines(text: str) -> str:
    """Like `normalize` but keeps line breaks, which end a phrase."""
    return "\n".join(normalize(line) for line in text.splitlines())


# --- knockout criteria summary ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class KnockoutSummary:
    years_asked: list[int]
    meets_years_share: float | None
    english: dict[EnglishLevel, int]
    education: dict[Education, int]
    work_model: dict[WorkModel, int]
    english_asked_share: float
    education_asked_share: float


def summarize_knockout(jobs: list[JobItems], resume: ResumeProfile | None) -> KnockoutSummary:
    years_asked = sorted(job.knockout.min_years for job in jobs if job.knockout.min_years is not None)
    meets: float | None = None
    if resume is not None and resume.years is not None and years_asked:
        meets = sum(1 for years in years_asked if years <= resume.years) / len(years_asked)
    english = {level: 0 for level in EnglishLevel}
    education = {level: 0 for level in Education}
    work_model = {model: 0 for model in WorkModel}
    for job in jobs:
        english[job.knockout.english] += 1
        education[job.knockout.education] += 1
        work_model[job.knockout.work_model] += 1
    total = len(jobs)
    english_asked = sum(1 for job in jobs if job.knockout.english.at_least(EnglishLevel.BASIC))
    education_asked = sum(1 for job in jobs if job.knockout.education.at_least(Education.TECHNICAL))
    return KnockoutSummary(
        years_asked=years_asked,
        meets_years_share=meets,
        english=english,
        education=education,
        work_model=work_model,
        english_asked_share=english_asked / total if total else 0.0,
        education_asked_share=education_asked / total if total else 0.0,
    )


# --- report ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    generated_at: datetime
    total_jobs: int
    senior_jobs: int
    duplicate_jobs: int
    skipped_jobs: int
    pending_jobs: int
    filters: str
    items: list[RankedItem]
    learned_now: list[tuple[str, int]]
    discarded_phrases: list[tuple[str, int]]
    knockout: KnockoutSummary
    resume: ResumeProfile | None
    resume_check_failed: bool


_ENGLISH_LABELS = {
    EnglishLevel.NONE: "nenhum",
    EnglishLevel.BASIC: "básico",
    EnglishLevel.INTERMEDIATE: "intermediário",
    EnglishLevel.ADVANCED: "avançado",
    EnglishLevel.FLUENT: "fluente",
    EnglishLevel.UNKNOWN: "não informado",
}
_EDUCATION_LABELS = {
    Education.NOT_REQUIRED: "não exigida",
    Education.TECHNICAL: "técnico",
    Education.BACHELOR: "superior completo",
    Education.POSTGRAD: "pós-graduação",
    Education.UNKNOWN: "não informada",
}
_WORK_MODEL_LABELS = {
    WorkModel.REMOTE: "remoto",
    WorkModel.HYBRID: "híbrido",
    WorkModel.ONSITE: "presencial",
    WorkModel.UNKNOWN: "não informado",
}
_COVERAGE_MARKS = {
    ResumeCoverage.PRESENT: "✅",
    ResumeCoverage.SYNONYM: "↔️",
    ResumeCoverage.MISSING: "❌",
    ResumeCoverage.UNKNOWN: "—",
}
_CATEGORY_TITLES = {
    Category.TECHNOLOGY: ("Tecnologias mais pedidas", "Tecnologia"),
    Category.SKILL: ("Habilidades mais pedidas", "Habilidade"),
    Category.RESPONSIBILITY: ("Responsabilidades mais pedidas", "Responsabilidade"),
}
_AI_ORIGINS = (ItemOrigin.AI, ItemOrigin.FREQUENT)
_NOT_COMPARED = "não comparado"


def _pct(fraction: float) -> str:
    return f"{fraction * 100:.0f}%"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _mark(coverage: ResumeCoverage | None) -> str:
    return _COVERAGE_MARKS.get(coverage, "—") if coverage is not None else "—"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


def _top(items: list[RankedItem], category: Category) -> list[RankedItem]:
    return sorted((item for item in items if item.category == category), key=_sort_key)[:TOP_N]


def _share_of(count: int, total: int) -> float:
    return count / total if total else 0.0


def _years_line(summary: KnockoutSummary) -> str:
    years = summary.years_asked
    if not years:
        text = "nenhuma vaga informa."
    elif years[0] == years[-1]:
        text = f"as vagas pedem {_count(years[0], 'ano', 'anos')}."
    else:
        median = statistics.median(years)
        text = f"as vagas pedem de {years[0]} a {years[-1]} anos (mediana: {f'{median:g}'.replace('.', ',')})."
    line = f"- **Anos de experiência:** {text}"
    if summary.meets_years_share is not None:
        line += f" Você atende **{_pct(summary.meets_years_share)}** das vagas em tempo de experiência."
    return line


def _levels_line[L: (EnglishLevel, Education)](
    title: str, asked_share: float, counts: dict[L, int], minimum: L, labels: dict[L, str], total: int
) -> str:
    shown = [(level, count) for level, count in counts.items() if level.at_least(minimum) and count > 0]
    if not shown:
        text = "nenhuma vaga pede."
    elif len(shown) == 1:
        text = f"{_pct(asked_share)} pedem {labels[shown[0][0]]}."
    else:
        parts = ", ".join(f"{labels[level]} {_pct(_share_of(count, total))}" for level, count in shown)
        text = f"{_pct(asked_share)} pedem ({parts})."
    unknown = next(count for level, count in counts.items() if level.rank < 0)
    if unknown > 0:
        text += f" {_count(unknown, 'vaga não informa', 'vagas não informam')}."
    return f"- **{title}:** {text}"


def _work_model_line(summary: KnockoutSummary, total: int) -> str:
    parts = [
        f"{_WORK_MODEL_LABELS[model]} {_pct(_share_of(summary.work_model[model], total))}"
        for model in (WorkModel.REMOTE, WorkModel.HYBRID, WorkModel.ONSITE)
        if summary.work_model[model] > 0
    ]
    if not parts:
        return "- **Modelo de trabalho:** nenhuma vaga informa."
    text = " · ".join(parts) + "."
    unknown = summary.work_model[WorkModel.UNKNOWN]
    if unknown > 0:
        text += f" {_count(unknown, 'vaga não informa', 'vagas não informam')}."
    return f"- **Modelo de trabalho:** {text}"


def _undeclared_warning(criterion: str, share: float) -> str | None:
    if share + _EPSILON < KNOCKOUT_WARNING_SHARE:
        return None
    return f"  ⚠️ Seu currículo não informa {criterion}, e {_pct(share)} das vagas pedem. Declare, se tiver."


def _knockout_section(report: Report) -> str:
    summary = report.knockout
    total = report.total_jobs
    english = _levels_line(
        "Inglês", summary.english_asked_share, summary.english, EnglishLevel.BASIC, _ENGLISH_LABELS, total
    )
    education = _levels_line(
        "Formação", summary.education_asked_share, summary.education, Education.TECHNICAL, _EDUCATION_LABELS, total
    )
    lines = [_years_line(summary), english, education, _work_model_line(summary, total)]
    resume = report.resume
    if resume is not None:
        checks = [
            (1, resume.english == EnglishLevel.UNKNOWN, "o nível de inglês", summary.english_asked_share),
            (2, resume.education == Education.UNKNOWN, "a formação", summary.education_asked_share),
            (0, resume.years is None, "o tempo de experiência", _share_of(len(summary.years_asked), total)),
        ]
        for index, undeclared, criterion, share in sorted(checks, reverse=True):
            warning = _undeclared_warning(criterion, share) if undeclared else None
            if warning is not None:
                lines.insert(index + 1, warning)
    return "## Critérios eliminatórios\n" + "\n".join(lines)


def _adjust_section(report: Report) -> str:
    categories = [Category.TECHNOLOGY] if report.resume_check_failed else list(Category)
    candidates = [
        item
        for category in categories
        for item in _top(report.items, category)
        if item.coverage in (ResumeCoverage.MISSING, ResumeCoverage.SYNONYM)
    ]
    candidates.sort(key=_sort_key)
    rows: list[list[str]] = []
    for number, item in enumerate(candidates, start=1):
        if item.coverage == ResumeCoverage.SYNONYM:
            action = f"{_mark(item.coverage)} troque “{item.resume_term}” por “{item.name}”"
        elif item.metadata_only:
            action = f"{_mark(item.coverage)} está só nas palavras-chave do PDF; coloque no texto"
        else:
            action = f"{_mark(item.coverage)} só inclua se você tiver essa experiência"
        rows.append([str(number), _cell(item.name), _pct(item.share), _pct(item.required_share), _cell(action)])
    body = (
        _table(["#", "Item", "Pedem", "Obrigatório", "Ação"], rows)
        if rows
        else "Nada a ajustar entre os itens mais pedidos."
    )
    return "## O que ajustar primeiro\n" + body


def _differentials_section(report: Report, with_resume: bool) -> str:
    chosen = differentials(report.items, report.senior_jobs)
    lines = [f"{_count(report.senior_jobs, 'vaga sênior', 'vagas sênior')}."]
    if not chosen:
        lines.append("Nenhum diferencial claro nas vagas sênior.")
        return "## Diferenciais das vagas sênior\n" + "\n\n".join(lines)
    headers = ["Item", "Nas sênior", "No geral"] + (["No currículo"] if with_resume else [])
    rows = []
    for item in chosen:
        row = [_cell(item.name), _pct(item.senior_share), _pct(item.share)]
        if with_resume:
            mark = _mark(item.coverage)
            if item.coverage == ResumeCoverage.SYNONYM:
                mark += f" “{_cell(item.resume_term)}”"
            row.append(mark)
        rows.append(row)
    lines.append(_table(headers, rows))
    return "## Diferenciais das vagas sênior\n" + "\n\n".join(lines)


def _ranking_section(report: Report, category: Category, with_resume: bool) -> str:
    title, label = _CATEGORY_TITLES[category]
    top = _top(report.items, category)
    if not top:
        return f"## {title}\n" + "Nenhum item."
    with_required = category != Category.RESPONSIBILITY
    headers = [label, "Vagas", "% das vagas"] + (["% obrigatório"] if with_required else []) + ["% nas sênior"]
    if with_resume:
        headers.append("No currículo")
    rows = []
    for item in top:
        name = _cell(item.name) + (" (detectada pela IA)" if item.origin in _AI_ORIGINS else "")
        row = [name, str(item.jobs), _pct(item.share)]
        if with_required:
            row.append(_pct(item.required_share))
        row.append(_pct(item.senior_share))
        if with_resume:
            row.append(_mark(item.coverage))
        rows.append(row)
    return f"## {title}\n" + _table(headers, rows)


def _counted_list(entries: list[tuple[str, int]], quote: bool) -> str:
    ordered = sorted(entries, key=lambda entry: (-entry[1], entry[0]))
    parts = []
    for index, (name, count) in enumerate(ordered):
        shown = f'"{name}"' if quote else name
        suffix = _count(count, "vaga", "vagas") if index == 0 else str(count)
        parts.append(f"{shown} ({suffix})")
    return " · ".join(parts)


def render_markdown(report: Report) -> str:
    if report.generated_at.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    local = report.generated_at.astimezone(REPORT_TIMEZONE)
    with_resume = report.resume is not None or report.resume_check_failed
    filters = report.filters.strip() or "nenhum"
    sections = [
        f"# Análise das vagas — {local:%d/%m/%Y %H:%M}\n"
        f"**{_count(report.total_jobs, 'vaga analisada', 'vagas analisadas')}** · "
        f"{_count(report.duplicate_jobs, 'repetida foi juntada', 'repetidas foram juntadas')} · filtros: {filters}\n"
        f"{_count(report.skipped_jobs, 'vaga não pôde ser analisada', 'vagas não puderam ser analisadas')}. "
        f"{_count(report.pending_jobs, 'ficou', 'ficaram')} para a próxima análise."
    ]
    if report.total_jobs < MIN_JOBS_FOR_TRUST:
        sections.append(
            f"⚠️ Amostra pequena: com menos de {MIN_JOBS_FOR_TRUST} vagas o ranking ainda muda muito."
        )
    if with_resume:
        failed = report.resume_check_failed
        rows = [["Tecnologias", f"**{_pct(coverage_score(report.items, Category.TECHNOLOGY))}**"]]
        for label, category in (("Habilidades", Category.SKILL), ("Responsabilidades", Category.RESPONSIBILITY)):
            rows.append([label, _NOT_COMPARED if failed else f"**{_pct(coverage_score(report.items, category))}**"])
        parts = ["## Cobertura do seu currículo\n" + _table(["Categoria", "Cobertura"], rows)]
        if failed:
            parts.append(
                "Não deu para comparar habilidades e responsabilidades com o currículo agora. "
                "Rode de novo mais tarde."
            )
        parts.append("Para ficar entre os 10% mais aderentes, mire em 90% ou mais nas tecnologias e habilidades.")
        sections.append("\n\n".join(parts))
    sections.append(_knockout_section(report))
    if with_resume:
        sections.append(_adjust_section(report))
    sections.append(_differentials_section(report, with_resume))
    if with_resume:
        terms = profile_terms(report.items)
        body = (
            "Cadastre estas competências no perfil, com a mesma grafia. "
            "A Gupy compara o perfil preenchido com a vaga, não só o PDF.\n\n"
            + " · ".join(f"`{term}`" for term in terms)
            if terms
            else "Nenhuma competência do seu currículo aparece entre as mais pedidas."
        )
        sections.append("## Para o perfil da Gupy e do LinkedIn\n" + body)
    for category in Category:
        sections.append(_ranking_section(report, category, with_resume))
    if report.learned_now:
        learned = (
            "Já entraram no ranking e passam a ser procuradas sozinhas nas próximas análises.\n\n"
            + _counted_list(report.learned_now, quote=False)
        )
    else:
        learned = "Nenhuma tecnologia nova nesta análise."
    sections.append("## Tecnologias aprendidas nesta análise\n" + learned)
    discarded = (
        _counted_list(report.discarded_phrases, quote=True)
        if report.discarded_phrases
        else "Nenhuma expressão descartada."
    )
    sections.append("## Expressões descartadas\n" + discarded)
    return "\n\n".join(sections) + "\n"


_CSV_HEADER = [
    "categoria", "item", "origem", "vagas", "percentual", "percentual_obrigatorio", "peso",
    "percentual_senior", "no_curriculo", "palavra_no_curriculo",
]


def _csv_pct(fraction: float) -> str:
    return f"{fraction * 100:.1f}"


def render_csv(report: Report) -> str:
    order = list(Category)
    ordered = sorted(report.items, key=lambda item: (order.index(item.category), -item.weight, item.name))
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_HEADER)
    for item in ordered:
        writer.writerow([
            item.category.api_value,
            item.name,
            item.origin.api_value,
            item.jobs,
            _csv_pct(item.share),
            _csv_pct(item.required_share),
            _csv_pct(item.weight),
            _csv_pct(item.senior_share),
            item.coverage.api_value if item.coverage is not None else "",
            item.resume_term,
        ])
    return buffer.getvalue()
