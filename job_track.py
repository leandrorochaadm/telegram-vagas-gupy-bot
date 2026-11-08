"""Job track (Flutter or other mobile) decided by the job title.

The bot sends both tracks, and each one has its own analysis report. A title with "flutter" is always
Flutter, even when it also says "mobile", so the mobile report never takes a Flutter job.
"""
import re
from enum import StrEnum

FLUTTER_TITLE_TERMS = ("flutter",)
MOBILE_TITLE_TERMS = ("mobile", "android", "kotlin", "ios", "swift", "react native", "react-native")


class Track(StrEnum):
    """Member = (CLI value, label shown in the report and in the bot log)."""

    FLUTTER = ("flutter", "Flutter")
    MOBILE = ("mobile", "Mobile")

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
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


_FLUTTER = _whole_words(FLUTTER_TITLE_TERMS)
_MOBILE = _whole_words(MOBILE_TITLE_TERMS)


def track_of(title: str) -> Track | None:
    """Track of a job by its title, or None when the title is neither Flutter nor mobile."""
    text = title.lower()
    if _FLUTTER.search(text):
        return Track.FLUTTER
    if _MOBILE.search(text):
        return Track.MOBILE
    return None


def analysis_track(title: str) -> Track:
    """Track of a saved job in the analysis: Mobile only when the title says so, Flutter otherwise.

    Jobs saved before the mobile search (and web results whose page title has no term) stay in Flutter.
    """
    return Track.MOBILE if track_of(title) is Track.MOBILE else Track.FLUTTER
