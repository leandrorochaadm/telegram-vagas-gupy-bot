"""Types of the AI extraction of job descriptions, plus the local extraction cache."""
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self

EXTRACTION_MODEL = "claude-haiku-5-5"
JUDGE_MODEL = "claude-sonnet-5-5"
PROMPT_VERSION = 1
MAX_INPUT_CHARS = 12_000


class ExtractionFailure(StrEnum):
    MISSING_KEY = "missing_key"
    API = "api"
    INVALID_RESPONSE = "invalid_response"

    @property
    def api_value(self) -> str:
        return self.value


class ExtractionError(Exception):
    def __init__(self, message: str, kind: ExtractionFailure) -> None:
        super().__init__(message)
        self.kind = kind


class _ApiEnum(StrEnum):
    @property
    def api_value(self) -> str:
        return self.value

    @classmethod
    def from_api(cls, raw: str | None) -> Self:
        """Exact match only; anything else is UNKNOWN."""
        member = cls._value2member_map_.get(raw) if isinstance(raw, str) else None
        return member if member is not None else cls.UNKNOWN  # type: ignore[attr-defined]


class _LevelEnum(_ApiEnum):
    """Enum whose members are declared in ascending order, with UNKNOWN last."""

    @property
    def rank(self) -> int:
        if self.value == "unknown":
            return -1
        return [m for m in type(self) if m.value != "unknown"].index(self)

    def at_least(self, level: Self) -> bool:
        return self.rank >= 0 and level.rank >= 0 and self.rank >= level.rank


class ResumeCoverage(_ApiEnum):
    PRESENT = "present"
    SYNONYM = "synonym"
    MISSING = "missing"
    UNKNOWN = "unknown"


class Requirement(_ApiEnum):
    REQUIRED = "required"
    DESIRED = "desired"
    UNKNOWN = "unknown"


class EnglishLevel(_LevelEnum):
    NONE = "none"
    BASIC = "basic"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    FLUENT = "fluent"
    UNKNOWN = "unknown"


class Education(_LevelEnum):
    NOT_REQUIRED = "not_required"
    TECHNICAL = "technical"
    BACHELOR = "bachelor"
    POSTGRAD = "postgrad"
    UNKNOWN = "unknown"


class WorkModel(_ApiEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class PhraseKind(_ApiEnum):
    TECHNOLOGY = "technology"
    SKILL = "skill"
    RESPONSIBILITY = "responsibility"
    IGNORE = "ignore"

    @classmethod
    def from_api(cls, raw: str | None) -> Self:
        """Exact match only; anything else is IGNORE."""
        member = cls._value2member_map_.get(raw) if isinstance(raw, str) else None
        return member if member is not None else cls.IGNORE


@dataclass(frozen=True)
class Skill:
    name: str
    requirement: Requirement


@dataclass(frozen=True)
class Knockout:
    min_years: int | None
    english: EnglishLevel
    education: Education
    work_model: WorkModel


@dataclass(frozen=True)
class Extraction:
    skills: tuple[Skill, ...]
    responsibilities: tuple[str, ...]
    desired_technologies: tuple[str, ...]
    extra_technologies: tuple[str, ...]
    knockout: Knockout


@dataclass(frozen=True)
class Coverage:
    status: ResumeCoverage
    resume_term: str


@dataclass(frozen=True)
class ResumeProfile:
    # Holds a dict: not hashable despite being frozen.
    years: int | None
    english: EnglishLevel
    education: Education
    coverage: dict[str, Coverage]


class _Key(StrEnum):
    SKILLS = "skills"
    NAME = "name"
    REQUIREMENT = "requirement"
    RESPONSIBILITIES = "responsibilities"
    DESIRED_TECHNOLOGIES = "desired_technologies"
    EXTRA_TECHNOLOGIES = "extra_technologies"
    KNOCKOUT = "knockout"
    MIN_YEARS = "min_years"
    ENGLISH = "english"
    EDUCATION = "education"
    WORK_MODEL = "work_model"


def extraction_to_payload(extraction: Extraction) -> dict:
    knockout = extraction.knockout
    return {
        _Key.SKILLS: [
            {_Key.NAME: s.name, _Key.REQUIREMENT: s.requirement.api_value} for s in extraction.skills
        ],
        _Key.RESPONSIBILITIES: list(extraction.responsibilities),
        _Key.DESIRED_TECHNOLOGIES: list(extraction.desired_technologies),
        _Key.EXTRA_TECHNOLOGIES: list(extraction.extra_technologies),
        _Key.KNOCKOUT: {
            _Key.MIN_YEARS: knockout.min_years,
            _Key.ENGLISH: knockout.english.api_value,
            _Key.EDUCATION: knockout.education.api_value,
            _Key.WORK_MODEL: knockout.work_model.api_value,
        },
    }


def _field(raw: object, key: _Key) -> object:
    if not isinstance(raw, dict) or key.value not in raw:
        raise ValueError(f"missing field: {key.value}")
    return raw[key.value]


def _text_list(raw: object, key: _Key) -> tuple[str, ...]:
    value = _field(raw, key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key.value} must be a list of strings")
    return tuple(value)


def extraction_from_payload(raw: object) -> Extraction:
    raw_skills = _field(raw, _Key.SKILLS)
    if not isinstance(raw_skills, list) or not all(isinstance(item, dict) for item in raw_skills):
        raise ValueError("skills must be a list of objects")
    skills = []
    for item in raw_skills:
        name = _field(item, _Key.NAME)
        if not isinstance(name, str):
            raise ValueError("skill name must be a string")
        skills.append(Skill(name, Requirement.from_api(_field(item, _Key.REQUIREMENT))))

    raw_knockout = _field(raw, _Key.KNOCKOUT)
    if not isinstance(raw_knockout, dict):
        raise ValueError("knockout must be an object")
    min_years = _field(raw_knockout, _Key.MIN_YEARS)
    if min_years is not None and (isinstance(min_years, bool) or not isinstance(min_years, int) or min_years < 0):
        raise ValueError("min_years must be a non-negative integer or null")
    knockout = Knockout(
        min_years=min_years,
        english=EnglishLevel.from_api(_field(raw_knockout, _Key.ENGLISH)),
        education=Education.from_api(_field(raw_knockout, _Key.EDUCATION)),
        work_model=WorkModel.from_api(_field(raw_knockout, _Key.WORK_MODEL)),
    )
    return Extraction(
        skills=tuple(skills),
        responsibilities=_text_list(raw, _Key.RESPONSIBILITIES),
        desired_technologies=_text_list(raw, _Key.DESIRED_TECHNOLOGIES),
        extra_technologies=_text_list(raw, _Key.EXTRA_TECHNOLOGIES),
        knockout=knockout,
    )


class ExtractionCache:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._entries: dict[str, object] = {}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return
        if isinstance(loaded, dict):
            self._entries = loaded

    def key(self, record_id: str, text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return f"{record_id}:{digest}:{PROMPT_VERSION}"

    def get(self, key: str) -> Extraction | None:
        if key not in self._entries:
            return None
        try:
            return extraction_from_payload(self._entries[key])
        except ValueError:
            return None

    def put(self, key: str, extraction: Extraction) -> None:
        self._entries[key] = extraction_to_payload(extraction)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._entries, handle, ensure_ascii=False)
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
