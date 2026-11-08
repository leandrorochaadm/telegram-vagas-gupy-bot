"""Job track (Flutter, other mobile or back-end) decided by the job title.

Each track has its own analysis report. The priority is Flutter > Mobile > Back-end: a title with "flutter"
is always Flutter, even when it also says "mobile" or "back-end", so no report takes another track's job.
The bot sends Flutter and mobile jobs to Telegram; back-end jobs are only kept for the analysis.
"""
import re
from enum import StrEnum

FLUTTER_TITLE_TERMS = ("flutter",)
MOBILE_TITLE_TERMS = ("mobile", "android", "kotlin", "ios", "swift", "react native", "react-native")
# Only the word itself, in any spelling (no technology such as Java or Node): "back" + optional separator
# (hyphen, typographic hyphens or dashes, underscore, space) + "end"
BACKEND_TITLE_PATTERN = r"back[-\u2010\u2011\u2012\u2013\u2014_ ]?end"


class Track(StrEnum):
    """Member = (CLI value, label shown in the report and in the bot log)."""

    FLUTTER = ("flutter", "Flutter")
    MOBILE = ("mobile", "Mobile")
    BACKEND = ("backend", "Back-end")

    label: str

    def __new__(cls, value: str, label: str) -> "Track":
        member = str.__new__(cls, value)
        member._value_ = value
        member.label = label
        return member

    @property
    def api_value(self) -> str:
        return self.value

    @classmethod
    def from_api(cls, raw: str) -> "Track":
        """Exact match only; raises ValueError on an unknown value (the CLI shows it as an argument error)."""
        member = cls._value2member_map_.get(raw)
        if member is None:
            raise ValueError(raw)
        return member


def _whole_words(terms: tuple[str, ...]) -> re.Pattern[str]:
    # A term cannot be glued to another letter or digit ("ios" in "negócios", "swift" in "swiftly")
    alternatives = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    return _whole_word_pattern(alternatives)


def _whole_word_pattern(pattern: str) -> re.Pattern[str]:
    # The underscore is a word character, so "back_end" matches as a whole, but "back_end_dev" does not
    return re.compile(rf"(?<!\w)(?:{pattern})(?!\w)")


_FLUTTER = _whole_words(FLUTTER_TITLE_TERMS)
_MOBILE = _whole_words(MOBILE_TITLE_TERMS)
_BACKEND = _whole_word_pattern(BACKEND_TITLE_PATTERN)


def track_of(title: str) -> Track | None:
    """Track of a job by its title (Flutter > Mobile > Back-end), or None when the title has no term."""
    text = title.lower()
    if _FLUTTER.search(text):
        return Track.FLUTTER
    if _MOBILE.search(text):
        return Track.MOBILE
    if _BACKEND.search(text):
        return Track.BACKEND
    return None


def analysis_track(title: str) -> Track:
    """Track of a saved job in the analysis: the title's track, or Flutter when the title has no term.

    Jobs saved before the mobile search (and web results whose page title has no term) stay in Flutter.
    """
    return track_of(title) or Track.FLUTTER


def is_sent_to_telegram(track: Track) -> bool:
    """Back-end jobs are only analyzed: they never go to the Telegram group."""
    match track:
        case Track.FLUTTER | Track.MOBILE:
            return True
        case Track.BACKEND:
            return False
