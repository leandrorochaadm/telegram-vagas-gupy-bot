"""Types of the AI extraction of job descriptions, plus the local extraction cache."""
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self

EXTRACTION_MODEL = "claude-haiku-5-5"
JUDGE_MODEL = "claude-sonnet-5-5"
PROMPT_VERSION = 1
MAX_INPUT_CHARS = 12_000
ENV_API_KEY = "ANTHROPIC_API_KEY"
CLIENT_MAX_RETRIES = 4
EXTRACTION_MAX_TOKENS = 4_096
JUDGE_MAX_TOKENS = 16_000
MAX_LABELS_PER_LIST = 15
GROUP_BATCH_SIZE = 1_500

# Boundary with the SDK.
_STOP_MAX_TOKENS = "max_tokens"
_STOP_REFUSAL = "refusal"
_BLOCK_TEXT = "text"
_ROLE_USER = "user"


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


class _JsonKey(StrEnum):
    GROUPS = "groups"
    CANONICAL = "canonical"
    MEMBERS = "members"
    YEARS = "years"
    COVERAGE = "coverage"
    LABEL = "label"
    STATUS = "status"
    RESUME_TERM = "resume_term"
    PHRASES = "phrases"
    PHRASE = "phrase"
    KIND = "kind"


def _enum_values(enum_type: type[_ApiEnum]) -> list[str]:
    return [member.api_value for member in enum_type]


def _object(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": {str(key): value for key, value in properties.items()},
        "required": [str(key) for key in properties],
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_STRING_LIST = {"type": "array", "items": _STRING}
_NULLABLE_INT = {"type": ["integer", "null"]}


def _enum_schema(enum_type: type[_ApiEnum]) -> dict:
    return {"type": "string", "enum": _enum_values(enum_type)}


_EXTRACTION_SCHEMA = _object(
    {
        _Key.SKILLS: {
            "type": "array",
            "items": _object({_Key.NAME: _STRING, _Key.REQUIREMENT: _enum_schema(Requirement)}),
        },
        _Key.RESPONSIBILITIES: _STRING_LIST,
        _Key.DESIRED_TECHNOLOGIES: _STRING_LIST,
        _Key.EXTRA_TECHNOLOGIES: _STRING_LIST,
        _Key.KNOCKOUT: _object(
            {
                _Key.MIN_YEARS: _NULLABLE_INT,
                _Key.ENGLISH: _enum_schema(EnglishLevel),
                _Key.EDUCATION: _enum_schema(Education),
                _Key.WORK_MODEL: _enum_schema(WorkModel),
            }
        ),
    }
)

_GROUPING_SCHEMA = _object(
    {
        _JsonKey.GROUPS: {
            "type": "array",
            "items": _object({_JsonKey.CANONICAL: _STRING, _JsonKey.MEMBERS: _STRING_LIST}),
        }
    }
)

_RESUME_SCHEMA = _object(
    {
        _JsonKey.YEARS: _NULLABLE_INT,
        _Key.ENGLISH: _enum_schema(EnglishLevel),
        _Key.EDUCATION: _enum_schema(Education),
        _JsonKey.COVERAGE: {
            "type": "array",
            "items": _object(
                {
                    _JsonKey.LABEL: _STRING,
                    _JsonKey.STATUS: _enum_schema(ResumeCoverage),
                    _JsonKey.RESUME_TERM: _STRING,
                }
            ),
        },
    }
)

_PHRASES_SCHEMA = _object(
    {
        _JsonKey.PHRASES: {
            "type": "array",
            "items": _object({_JsonKey.PHRASE: _STRING, _JsonKey.KIND: _enum_schema(PhraseKind)}),
        }
    }
)

_EXTRACT_SYSTEM = f"""You extract structured data from a job description (any language).
Write every label in Brazilian Portuguese, even when the job description is in English.
Labels are short: 2 to 6 words.
- skills: soft and hard skills as a noun phrase ("Comunicação", "Trabalho em equipe"). No technologies here.
- responsibilities: in the infinitive ("Desenvolver novas funcionalidades"). No technologies here.
- At most {MAX_LABELS_PER_LIST} items per list (skills and responsibilities).
- extra_technologies: ALL technologies seen in the text, named as they appear in the job description.
- desired_technologies: only technologies cited solely as a differential.
Skill requirement: a skill in a differential section or sentence ("desejável", "diferencial", "nice to have",
"será um plus", "Informações adicionais") is "{Requirement.DESIRED.api_value}". A skill in the requirements
section, or with no indication, is "{Requirement.REQUIRED.api_value}".
Knockout criteria:
- min_years: the smallest number of years asked for the main stack ("3+ anos" -> 3; "3 a 5 anos" -> 3);
  null when not mentioned.
- english, education and work_model: "{EnglishLevel.UNKNOWN.api_value}" when not mentioned (never guess).
  A language asked only as a differential is "{EnglishLevel.NONE.api_value}".
"""

_GROUP_SYSTEM = """You receive a list of short labels in Brazilian Portuguese. Group labels that mean the same thing.
Return only the groups with 2 or more labels, or whose canonical name differs from the label.
The canonical name uses the most common or official spelling ("Node", "NodeJS" -> "Node.js";
"Comunicação clara", "Boa comunicação" -> "Comunicação"). Members must be copied exactly from the input.
"""

_RESUME_SYSTEM = f"""You read a resume and compare it with a list of labels.
Report only what the resume declares: anything not declared is null (years) or "unknown" (english, education);
"mais de 6 anos" -> 6. years is the total years of professional experience.
For each label, status is "{ResumeCoverage.PRESENT.api_value}" when the resume uses the same word,
"{ResumeCoverage.SYNONYM.api_value}" when it expresses the same idea with another word (return that word in
resume_term), and "{ResumeCoverage.MISSING.api_value}" when it does not appear (resume_term empty).
"""

_PHRASES_SYSTEM = f"""You classify frequent phrases taken from job descriptions.
kind is "{PhraseKind.TECHNOLOGY.api_value}", "{PhraseKind.SKILL.api_value}" or
"{PhraseKind.RESPONSIBILITY.api_value}". Generic text ("ambiente de producao", "experiencia comprovada") and any
doubt is "{PhraseKind.IGNORE.api_value}".
"""


class ClaudeExtractor:
    def __init__(self, client: object) -> None:
        self._client = client

    def _ask(self, model: str, max_tokens: int, system: str, user_text: str, schema: dict) -> object:
        try:
            response = self._client.messages.create(  # type: ignore[attr-defined]
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": _ROLE_USER, "content": user_text}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except Exception as error:
            raise ExtractionError(f"API call failed: {error}", ExtractionFailure.API) from error
        if response.stop_reason in (_STOP_MAX_TOKENS, _STOP_REFUSAL):
            raise ExtractionError(f"unusable response: {response.stop_reason}", ExtractionFailure.INVALID_RESPONSE)
        text = next((block.text for block in response.content if block.type == _BLOCK_TEXT), None)
        if text is None:
            raise ExtractionError("response without text block", ExtractionFailure.INVALID_RESPONSE)
        try:
            return json.loads(text)
        except ValueError as error:
            raise ExtractionError("response is not valid JSON", ExtractionFailure.INVALID_RESPONSE) from error

    def _ask_and_build[T](
        self, model: str, max_tokens: int, system: str, user_text: str, schema: dict, build: Callable[[object], T]
    ) -> T:
        raw = self._ask(model, max_tokens, system, user_text, schema)
        try:
            return build(raw)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise ExtractionError(f"response out of format: {error}", ExtractionFailure.INVALID_RESPONSE) from error

    def extract(self, title: str, text: str) -> Extraction:
        user_text = f"Title: {title}\n\n{text[:MAX_INPUT_CHARS]}"
        extraction = self._ask_and_build(
            EXTRACTION_MODEL, EXTRACTION_MAX_TOKENS, _EXTRACT_SYSTEM, user_text, _EXTRACTION_SCHEMA,
            extraction_from_payload,
        )
        return Extraction(
            skills=tuple(
                Skill(s.name.strip(), s.requirement) for s in extraction.skills if s.name.strip()
            )[:MAX_LABELS_PER_LIST],
            responsibilities=tuple(r.strip() for r in extraction.responsibilities if r.strip())[
                :MAX_LABELS_PER_LIST
            ],
            desired_technologies=extraction.desired_technologies,
            extra_technologies=extraction.extra_technologies,
            knockout=extraction.knockout,
        )

    def group_labels(self, labels: list[str]) -> dict[str, str]:
        distinct = list(dict.fromkeys(labels))
        result: dict[str, str] = {}
        for start in range(0, len(distinct), GROUP_BATCH_SIZE):
            batch = distinct[start : start + GROUP_BATCH_SIZE]
            result.update(self._group_batch(batch))
        return result

    def _group_batch(self, batch: list[str]) -> dict[str, str]:
        def build(raw: object) -> dict[str, str]:
            groups = _field(raw, _JsonKey.GROUPS)
            if not isinstance(groups, list):
                raise ValueError("groups must be a list")
            known = set(batch)
            mapping: dict[str, str] = {}
            for group in groups:
                canonical = _field(group, _JsonKey.CANONICAL)
                members = _text_list(group, _JsonKey.MEMBERS)
                if not isinstance(canonical, str):
                    raise ValueError("canonical must be a string")
                for member in members:
                    if member in known and member not in mapping:
                        mapping[member] = canonical.strip() or member
            return mapping

        mapping = self._ask_and_build(
            JUDGE_MODEL, JUDGE_MAX_TOKENS, _GROUP_SYSTEM, json.dumps(batch, ensure_ascii=False), _GROUPING_SCHEMA,
            build,
        )
        return {label: mapping.get(label, label) for label in batch}

    def check_resume(self, resume_text: str, labels: list[str]) -> ResumeProfile:
        user_text = (
            f"Resume:\n{resume_text[:MAX_INPUT_CHARS]}\n\nLabels:\n{json.dumps(labels, ensure_ascii=False)}"
        )

        def build(raw: object) -> ResumeProfile:
            years = _field(raw, _JsonKey.YEARS)
            if years is not None and (isinstance(years, bool) or not isinstance(years, int) or years < 0):
                raise ValueError("years must be a non-negative integer or null")
            items = _field(raw, _JsonKey.COVERAGE)
            if not isinstance(items, list):
                raise ValueError("coverage must be a list")
            returned: dict[str, Coverage] = {}
            for item in items:
                label = _field(item, _JsonKey.LABEL)
                term = _field(item, _JsonKey.RESUME_TERM)
                if not isinstance(label, str) or not isinstance(term, str):
                    raise ValueError("coverage label and term must be strings")
                status = ResumeCoverage.from_api(_field(item, _JsonKey.STATUS))
                term = term.strip()
                if status is ResumeCoverage.SYNONYM and not term:
                    status = ResumeCoverage.UNKNOWN
                if label not in returned:
                    returned[label] = Coverage(status, term)
            unknown = Coverage(ResumeCoverage.UNKNOWN, "")
            return ResumeProfile(
                years=years,
                english=EnglishLevel.from_api(_field(raw, _Key.ENGLISH)),
                education=Education.from_api(_field(raw, _Key.EDUCATION)),
                coverage={label: returned.get(label, unknown) for label in labels},
            )

        return self._ask_and_build(
            JUDGE_MODEL, JUDGE_MAX_TOKENS, _RESUME_SYSTEM, user_text, _RESUME_SCHEMA, build
        )

    def classify_phrases(self, phrases: list[str]) -> dict[str, PhraseKind]:
        if not phrases:
            return {}

        def build(raw: object) -> dict[str, PhraseKind]:
            items = _field(raw, _JsonKey.PHRASES)
            if not isinstance(items, list):
                raise ValueError("phrases must be a list")
            kinds: dict[str, PhraseKind] = {}
            for item in items:
                phrase = _field(item, _JsonKey.PHRASE)
                if not isinstance(phrase, str):
                    raise ValueError("phrase must be a string")
                kinds.setdefault(phrase, PhraseKind.from_api(_field(item, _JsonKey.KIND)))
            return kinds

        kinds = self._ask_and_build(
            JUDGE_MODEL, JUDGE_MAX_TOKENS, _PHRASES_SYSTEM, json.dumps(phrases, ensure_ascii=False),
            _PHRASES_SCHEMA, build,
        )
        return {phrase: kinds.get(phrase, PhraseKind.IGNORE) for phrase in phrases}


def make_client() -> object:
    api_key = os.environ.get(ENV_API_KEY, "").strip()
    if not api_key:
        raise ExtractionError(f"{ENV_API_KEY} is not set", ExtractionFailure.MISSING_KEY)
    import anthropic

    return anthropic.Anthropic(api_key=api_key, max_retries=CLIENT_MAX_RETRIES)
