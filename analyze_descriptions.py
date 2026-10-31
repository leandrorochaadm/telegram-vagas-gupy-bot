#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests[socks]", "anthropic"]
# ///
"""Analisa as descrições de vagas salvas e grava um relatório (.md) e uma planilha (.csv).

Lê as descrições `saved` do banco, extrai habilidades e requisitos de cada vaga com a IA (com cache local),
compara com o currículo e grava os arquivos em `reports/` com a data e hora de Brasília no nome.
Este módulo tem o `run`; a linha de comando (argumentos, chave da API, currículo) fica no `main`.
"""
import argparse
import dataclasses
import os
import re
import sys
import traceback
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import store
from description_extractor import (
    ClaudeExtractor,
    Coverage,
    Extraction,
    ExtractionCache,
    ExtractionError,
    ExtractionFailure,
    PhraseKind,
    Requirement,
    ResumeCoverage,
    ResumeProfile,
    Skill,
    make_client,
)
from env_file import load_env_file
from job_analysis import (
    REPORT_TIMEZONE,
    TOP_N,
    Category,
    ItemOrigin,
    JobItems,
    RankedItem,
    Report,
    dedupe_key,
    frequent_ngrams,
    is_senior,
    job_technologies,
    matches_seniority,
    rank,
    render_csv,
    render_markdown,
    summarize_knockout,
    terms_to_learn,
    tex_keywords,
    tex_to_text,
    unknown_ai_technologies,
)
from store import SavedDescription, Source, StoreError
from tech_terms import (
    LEARNED_TERMS_PATH,
    TECH_TERMS,
    all_terms,
    find_technologies,
    load_learned,
    normalize,
    save_learned,
)

REPORTS_DIR = Path("reports")
CACHE_PATH = REPORTS_DIR / "cache" / "extractions.json"
MAX_NEW_EXTRACTIONS = 400
DEFAULT_ENV_FILE = "/etc/vagas-bot/bot.env"
ENV_RESUME_PATH = "RESUME_PATH"
ALERT_LABEL = "Análise das vagas"  # texto de tela; o passo 09 grava o aviso com Source.ANALYSIS.api_value
CACHE_SAVE_EVERY = 25
REPORT_FILE_PREFIX = "analise-vagas-"
REPORT_STAMP_FORMAT = "%Y-%m-%d-%H%M"
MSG_NO_DESCRIPTIONS = "Nenhuma descrição salva para analisar com esses filtros."
MSG_NOTHING_ANALYZED = "Nenhuma vaga pôde ser analisada agora. Rode de novo mais tarde."
LOCAL_ENV_FILE = Path(__file__).resolve().parent / ".env"
LOG_HINT = "Detalhes: journalctl -u vagas-analysis.service -n 50"
MSG_MISSING_KEY = "Falta a chave da API do Claude (ANTHROPIC_API_KEY) no arquivo de configuração {path}."
MSG_STORE = "Não foi possível ler o banco de vagas: {error}"
MSG_RESUME = "Não foi possível ler o currículo em {path}."
MSG_ENV_FILE = "Não foi possível ler o arquivo de configuração {path}."
MSG_UNEXPECTED = "Erro inesperado ({kind}). " + LOG_HINT
MSG_ALL_EXTRACTIONS_FAILED = "Nenhuma vaga foi analisada porque todas as chamadas à API do Claude falharam. " + LOG_HINT
MSG_ALERT_TEXT = "A análise semanal das vagas falhou: {reason}."
MSG_ALERT_NOT_SAVED = "Não foi possível guardar o aviso da falha: {error}"
MSG_REPORT_SAVED = "Relatório gravado em {path}"
MSG_CSV_SAVED = "Planilha gravada em {path}"

_REPORT_DATE_FORMAT = "%d/%m/%Y"
_SOFT_FAILURES = (ExtractionFailure.API, ExtractionFailure.INVALID_RESPONSE)


# --- file names and filters ------------------------------------------------------------------------------------


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None:
        raise ValueError("datetime without time zone")


def report_paths(out_dir: Path, now: datetime) -> tuple[Path, Path]:
    _require_aware(now)
    stem = REPORT_FILE_PREFIX + now.astimezone(REPORT_TIMEZONE).strftime(REPORT_STAMP_FORMAT)
    return out_dir / f"{stem}.md", out_dir / f"{stem}.csv"


def describe_filters(since: datetime | None, seniority: str | None) -> str:
    parts: list[str] = []
    if since is not None:
        _require_aware(since)
        parts.append(f"desde {since.astimezone(REPORT_TIMEZONE).strftime(_REPORT_DATE_FORMAT)}")
    if seniority is not None and seniority.strip():
        parts.append(f"senioridade “{seniority.strip()}”")
    return ", ".join(parts)


# --- selection -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    kept: list[SavedDescription]
    empty_jobs: int
    duplicate_jobs: int


def _better_duplicate_key(row: SavedDescription) -> tuple[int, float, str]:
    """Sort key: smaller is better (longer text, then newer, then smaller id)."""
    finished = row.finished_at.timestamp() if row.finished_at is not None else float("-inf")
    return (-len(row.description.strip()), -finished, row.id)


def select_descriptions(rows: list[SavedDescription], seniority: str | None) -> Selection:
    empty_jobs = 0
    candidates: list[SavedDescription] = []
    for row in rows:
        if row.description.strip() == "":
            empty_jobs += 1
            continue
        if seniority and not matches_seniority(seniority, row.title, row.seniority):
            continue
        candidates.append(row)
    groups: dict[str, list[SavedDescription]] = defaultdict(list)
    for row in candidates:
        groups[dedupe_key(row.company, row.title, row.published)].append(row)
    winners = {min(group, key=_better_duplicate_key).id for group in groups.values()}
    kept = [row for row in candidates if row.id in winners]
    return Selection(kept=kept, empty_jobs=empty_jobs, duplicate_jobs=len(candidates) - len(kept))


# --- extraction with cache -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionBatch:
    extracted: dict[str, Extraction]
    skipped_jobs: int
    pending_jobs: int
    new_calls: int
    last_error: ExtractionError | None = None


class AllExtractionsFailed(Exception):
    """Every extraction attempted in this run failed, so no job could be analyzed."""

    def __init__(self, cause: ExtractionError) -> None:
        super().__init__(str(cause))
        self.cause = cause


def extract_all(kept: list[SavedDescription], extractor, cache: ExtractionCache, max_new: int) -> ExtractionBatch:
    extracted: dict[str, Extraction] = {}
    skipped = pending = new_calls = new_puts = 0
    last_error: ExtractionError | None = None
    try:
        for row in kept:
            key = cache.key(row.id, row.description)
            cached = cache.get(key)
            if cached is not None:
                extracted[row.id] = cached
                continue
            if new_calls >= max_new:
                pending += 1
                continue
            new_calls += 1
            try:
                extraction = extractor.extract(row.title, row.description)
            except ExtractionError as error:
                if error.kind not in _SOFT_FAILURES:
                    raise
                skipped += 1
                last_error = error
                continue
            cache.put(key, extraction)
            extracted[row.id] = extraction
            new_puts += 1
            if new_puts % CACHE_SAVE_EVERY == 0:
                cache.save()
    finally:
        if new_puts > 0:
            cache.save()
    return ExtractionBatch(
        extracted=extracted, skipped_jobs=skipped, pending_jobs=pending, new_calls=new_calls, last_error=last_error
    )


# --- grouping and items per job --------------------------------------------------------------------------------


def safe_group(extractor, labels: set[str]) -> dict[str, str]:
    if not labels:
        return {}
    ordered = sorted(labels)
    try:
        grouping = extractor.group_labels(ordered)
    except ExtractionError as error:
        if error.kind not in _SOFT_FAILURES:
            raise
        grouping = {}
    return {label: grouping.get(label, label) for label in ordered}


def _contains_word(term: str, text: str) -> bool:
    """Whole-word search over already normalized strings (no letter or digit glued to the match)."""
    if not term:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _merge_requirement(current: Requirement | None, new: Requirement) -> Requirement:
    if current is None:
        return new
    return current if current != Requirement.DESIRED else new


def _skill_map(skills: tuple[Skill, ...], grouping: dict[str, str]) -> dict[str, Requirement]:
    result: dict[str, Requirement] = {}
    for skill in skills:
        canonical = grouping.get(skill.name, skill.name)
        result[canonical] = _merge_requirement(result.get(canonical), skill.requirement)
    return result


def build_jobs(
    kept: list[SavedDescription],
    extracted: dict[str, Extraction],
    extractor,
    learned_path: Path,
) -> tuple[
    list[JobItems],
    dict[tuple[Category, str], ItemOrigin],
    list[tuple[str, int]],
    dict[str, tuple[str, ...]],
    dict[str, set[str]],
]:
    rows = [row for row in kept if row.id in extracted]
    extractions = {row.id: extracted[row.id] for row in rows}

    skill_grouping = safe_group(extractor, {s.name for e in extractions.values() for s in e.skills})
    resp_grouping = safe_group(extractor, {r for e in extractions.values() for r in e.responsibilities})
    tech_grouping = safe_group(extractor, {t for e in extractions.values() for t in e.extra_technologies})

    learned = load_learned(learned_path)
    terms = all_terms(learned)
    ai_names_per_job = {record_id: set(e.extra_technologies) for record_id, e in extractions.items()}
    skill_labels = set(skill_grouping) | set(skill_grouping.values())
    new = terms_to_learn(ai_names_per_job, tech_grouping, terms, skill_labels)
    learned_now: list[tuple[str, int]] = []
    if new:
        counts: dict[str, int] = defaultdict(int)
        for canonicals in unknown_ai_technologies(ai_names_per_job, tech_grouping, terms).values():
            for canonical in canonicals:
                if canonical in new:
                    counts[canonical] += 1
        learned_now = sorted(((name, counts[name]) for name in new), key=lambda entry: (-entry[1], entry[0]))
        learned.update(new)
        save_learned(learned_path, learned)
        terms = all_terms(learned)

    ai_unknown = unknown_ai_technologies(ai_names_per_job, tech_grouping, terms)
    ai_aliases: dict[str, set[str]] = defaultdict(set)
    for extraction in extractions.values():
        for name in extraction.extra_technologies:
            ai_aliases[tech_grouping.get(name, name)].add(name)

    origins: dict[tuple[Category, str], ItemOrigin] = {}
    jobs: list[JobItems] = []
    for row in rows:
        extraction = extractions[row.id]
        technologies = job_technologies(row.description, extraction.desired_technologies, terms)
        for name in technologies:
            origin = ItemOrigin.DICTIONARY if name in TECH_TERMS else ItemOrigin.LEARNED
            origins.setdefault((Category.TECHNOLOGY, name), origin)
        desired_text = normalize(" ".join(extraction.desired_technologies))
        for canonical in sorted(ai_unknown.get(row.id, frozenset())):
            if canonical in technologies:
                continue
            names = {canonical} | ai_aliases.get(canonical, set())
            desired = any(_contains_word(normalize(name), desired_text) for name in names)
            technologies[canonical] = Requirement.DESIRED if desired else Requirement.REQUIRED
            origins.setdefault((Category.TECHNOLOGY, canonical), ItemOrigin.AI)
        jobs.append(
            JobItems(
                record_id=row.id,
                title=row.title,
                seniority=row.seniority,
                technologies=technologies,
                skills=_skill_map(extraction.skills, skill_grouping),
                responsibilities=frozenset(resp_grouping.get(r, r) for r in extraction.responsibilities),
                knockout=extraction.knockout,
            )
        )
    return jobs, origins, learned_now, terms, dict(ai_aliases)


# --- frequent phrases ------------------------------------------------------------------------------------------


def add_frequent_phrases(
    jobs: list[JobItems],
    texts: dict[str, str],
    known: set[str],
    extractor,
    origins: dict[tuple[Category, str], ItemOrigin],
) -> tuple[list[JobItems], list[tuple[str, int]]]:
    phrases = frequent_ngrams(texts, known)
    if not phrases:
        return jobs, []
    kinds: dict[str, PhraseKind] | None
    try:
        kinds = extractor.classify_phrases(sorted(phrases))
    except ExtractionError as error:
        if error.kind not in _SOFT_FAILURES:
            raise
        kinds = None

    discarded: list[tuple[str, int]] = []
    technologies: dict[str, list[str]] = defaultdict(list)  # record id -> phrases
    skills: dict[str, list[str]] = defaultdict(list)
    responsibilities: dict[str, list[str]] = defaultdict(list)
    for phrase, ids in phrases.items():
        kind = PhraseKind.IGNORE if kinds is None else kinds.get(phrase, PhraseKind.IGNORE)
        if kind == PhraseKind.IGNORE:
            discarded.append((phrase, len(ids)))
            continue
        target, category = {
            PhraseKind.TECHNOLOGY: (technologies, Category.TECHNOLOGY),
            PhraseKind.SKILL: (skills, Category.SKILL),
            PhraseKind.RESPONSIBILITY: (responsibilities, Category.RESPONSIBILITY),
        }[kind]
        origins.setdefault((category, phrase), ItemOrigin.FREQUENT)
        for record_id in ids:
            target[record_id].append(phrase)
    discarded.sort(key=lambda entry: (-entry[1], entry[0]))

    updated: list[JobItems] = []
    for job in jobs:
        extra_tech = technologies.get(job.record_id, [])
        extra_skills = skills.get(job.record_id, [])
        extra_resp = responsibilities.get(job.record_id, [])
        if not (extra_tech or extra_skills or extra_resp):
            updated.append(job)
            continue
        updated.append(
            dataclasses.replace(
                job,
                technologies={**job.technologies, **{p: Requirement.REQUIRED for p in extra_tech if p not in job.technologies}},
                skills={**job.skills, **{p: Requirement.REQUIRED for p in extra_skills if p not in job.skills}},
                responsibilities=job.responsibilities | frozenset(extra_resp),
            )
        )
    return updated, discarded


# --- resume comparison -----------------------------------------------------------------------------------------


def apply_resume(
    items: list[RankedItem],
    resume_tex: str,
    terms: dict[str, tuple[str, ...]],
    ai_aliases: dict[str, set[str]],
    extractor,
) -> tuple[list[RankedItem], ResumeProfile | None, bool]:
    plain = tex_to_text(resume_tex)
    keywords = " ".join(tex_keywords(resume_tex))
    plain_found = find_technologies(plain, terms)
    keywords_found = find_technologies(keywords, terms)
    plain_norm = normalize(plain)
    keywords_norm = normalize(keywords)

    top: dict[Category, list[str]] = {Category.SKILL: [], Category.RESPONSIBILITY: []}
    for item in items:
        if item.category in top and len(top[item.category]) < TOP_N:
            top[item.category].append(item.name)
    labels = list(dict.fromkeys(top[Category.SKILL] + top[Category.RESPONSIBILITY]))
    profile: ResumeProfile | None
    try:
        profile = extractor.check_resume(plain, labels)
        check_failed = False
    except ExtractionError as error:
        if error.kind not in _SOFT_FAILURES:
            raise
        profile = None
        check_failed = True

    result: list[RankedItem] = []
    for item in items:
        if item.category == Category.TECHNOLOGY:
            if item.origin in (ItemOrigin.AI, ItemOrigin.FREQUENT):
                names = {normalize(item.name)} | {normalize(n) for n in ai_aliases.get(item.name, set())}
                present = any(_contains_word(name, plain_norm) for name in names)
                in_keywords = any(_contains_word(name, keywords_norm) for name in names)
            else:
                present = item.name in plain_found
                in_keywords = item.name in keywords_found
            result.append(
                dataclasses.replace(
                    item,
                    coverage=ResumeCoverage.PRESENT if present else ResumeCoverage.MISSING,
                    resume_term="",
                    metadata_only=(not present) and in_keywords,
                )
            )
        elif profile is not None and item.name in top[item.category]:
            covered: Coverage | None = profile.coverage.get(item.name)
            result.append(
                dataclasses.replace(
                    item,
                    coverage=covered.status if covered is not None else ResumeCoverage.UNKNOWN,
                    resume_term=covered.resume_term if covered is not None else "",
                )
            )
        else:
            result.append(dataclasses.replace(item, coverage=None, resume_term=""))
    return result, profile, check_failed


# --- run -------------------------------------------------------------------------------------------------------


def run(
    db,
    extractor,
    cache: ExtractionCache,
    *,
    resume_text: str | None,
    since: datetime | None,
    seniority: str | None,
    max_new: int,
    now: datetime,
    out_dir: Path,
    learned_path: Path = LEARNED_TERMS_PATH,
) -> Report | None:
    _require_aware(now)
    rows = db.saved_descriptions(since)
    selection = select_descriptions(rows, seniority)
    if not selection.kept:
        print(MSG_NO_DESCRIPTIONS)
        return None

    batch = extract_all(selection.kept, extractor, cache, max_new)
    if not batch.extracted:
        if batch.last_error is not None:
            raise AllExtractionsFailed(batch.last_error)
        print(MSG_NOTHING_ANALYZED)
        return None

    jobs, origins, learned_now, terms, ai_aliases = build_jobs(
        selection.kept, batch.extracted, extractor, learned_path
    )

    known: set[str] = set()
    for name, aliases in terms.items():
        known.add(name)
        known.update(aliases)
    for extraction in batch.extracted.values():
        known.update(skill.name for skill in extraction.skills)
        known.update(extraction.responsibilities)
        known.update(extraction.extra_technologies)
    for job in jobs:
        known.update(job.technologies)
        known.update(job.skills)
        known.update(job.responsibilities)
    texts = {row.id: row.description for row in selection.kept if row.id in batch.extracted}
    jobs, discarded = add_frequent_phrases(jobs, texts, known, extractor, origins)

    items = rank(jobs, origins)
    profile: ResumeProfile | None = None
    check_failed = False
    if resume_text is not None:
        items, profile, check_failed = apply_resume(items, resume_text, terms, ai_aliases, extractor)

    report = Report(
        generated_at=now,
        total_jobs=len(jobs),
        senior_jobs=sum(1 for job in jobs if is_senior(job.title, job.seniority)),
        duplicate_jobs=selection.duplicate_jobs,
        skipped_jobs=selection.empty_jobs + batch.skipped_jobs,
        pending_jobs=batch.pending_jobs,
        filters=describe_filters(since, seniority),
        items=items,
        learned_now=learned_now,
        discarded_phrases=discarded,
        knockout=summarize_knockout(jobs, profile),
        resume=profile,
        resume_check_failed=check_failed,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown_path, csv_path = report_paths(out_dir, now)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(render_csv(report))
    return report


# --- command line ----------------------------------------------------------------------------------------------


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("precisa ser 1 ou mais")
    return value


def _non_negative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("precisa ser 0 ou mais")
    return value


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analisa as descrições de vagas salvas e grava um relatório.")
    parser.add_argument("--resume", default=None, help="caminho do currículo em .tex")
    parser.add_argument("--since-days", type=_positive_int, default=None, help="só vagas salvas nos últimos N dias")
    parser.add_argument("--seniority", default=None, help="só vagas dessa senioridade (texto)")
    parser.add_argument(
        "--max-new", type=_non_negative_int, default=MAX_NEW_EXTRACTIONS,
        help=f"máximo de vagas novas para a IA ler (padrão {MAX_NEW_EXTRACTIONS})",
    )
    parser.add_argument("--env-file", default=None, help="arquivo de configuração (KEY=valor)")
    return parser.parse_args(argv)


def resolve_env_file(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    default = Path(DEFAULT_ENV_FILE)
    return default if default.exists() else LOCAL_ENV_FILE


def resolve_resume_path(explicit: str | None) -> Path | None:
    if explicit is not None and explicit.strip():
        return Path(explicit)
    from_env = os.environ.get(ENV_RESUME_PATH, "").strip()
    return Path(from_env) if from_env else None


def record_failure_alert(db: store.Store | None, reason: str) -> None:
    text = MSG_ALERT_TEXT.format(reason=reason.rstrip("."))
    try:
        if db is None:
            db = store.from_env()
            db.connect()
    except StoreError as error:
        print(MSG_STORE.format(error=error), file=sys.stderr)
        return
    try:
        db.add_alerts([(Source.ANALYSIS.api_value, text)])
    except StoreError as error:
        print(MSG_ALERT_NOT_SAVED.format(error=error), file=sys.stderr)


def _fail(message: str, db: store.Store | None) -> int:
    print(message, file=sys.stderr)
    record_failure_alert(db, message)
    return 1


def main(argv: Sequence[str] = ()) -> int:
    args = parse_args(argv)
    env_path = resolve_env_file(args.env_file)
    try:
        load_env_file(str(env_path))
    except OSError:
        return _fail(MSG_ENV_FILE.format(path=env_path), None)

    resume_path = resolve_resume_path(args.resume)
    raw: str | None = None
    if resume_path is not None:
        try:
            raw = resume_path.read_text(encoding="utf-8")
            tex_to_text(raw)
        except (OSError, UnicodeDecodeError, ValueError):
            return _fail(MSG_RESUME.format(path=resume_path), None)

    missing_key = MSG_MISSING_KEY.format(path=env_path)
    try:
        client = make_client()
    except ExtractionError as error:
        if error.kind is ExtractionFailure.MISSING_KEY:
            return _fail(missing_key, None)
        raise

    try:
        db = store.from_env()
        db.connect()
    except StoreError as error:
        print(MSG_STORE.format(error=error), file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.since_days) if args.since_days is not None else None
    try:
        report = run(
            db, ClaudeExtractor(client), ExtractionCache(CACHE_PATH),
            resume_text=raw, since=since, seniority=args.seniority, max_new=args.max_new, now=now,
            out_dir=REPORTS_DIR, learned_path=LEARNED_TERMS_PATH,
        )
    except StoreError as error:
        print(MSG_STORE.format(error=error), file=sys.stderr)
        return 1
    except AllExtractionsFailed as error:
        print(error.cause, file=sys.stderr)
        return _fail(MSG_ALL_EXTRACTIONS_FAILED, db)
    except ExtractionError as error:
        if error.kind is ExtractionFailure.MISSING_KEY:
            return _fail(missing_key, db)
        traceback.print_exc()
        return _fail(MSG_UNEXPECTED.format(kind=type(error).__name__), db)
    except Exception as error:
        traceback.print_exc()
        return _fail(MSG_UNEXPECTED.format(kind=type(error).__name__), db)

    if report is None:
        return 0
    markdown_path, csv_path = report_paths(REPORTS_DIR, now)
    print(MSG_REPORT_SAVED.format(path=markdown_path))
    print(MSG_CSV_SAVED.format(path=csv_path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
