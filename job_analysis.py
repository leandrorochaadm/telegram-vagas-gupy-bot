"""Pure functions for the job description analysis: resume text, counting, ranking and phrases.

Standard library only; no network, database or file access.
"""
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from description_extractor import Knockout, Requirement, ResumeCoverage
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
