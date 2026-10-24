"""In-memory stand-in for store.Store, used by the bot tests.

Same public methods and same behavior as the real Store (see
tests/test_store_contract.py), without any network. Not a test module itself.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import (  # noqa: E402
    Alert, Collection, LinkedinSession, QueuedJob, Source, StoreError,
    _Field, _format_date, _parse_date,
)

_SESSION_FIELDS = (_Field.SEARCHED_AT, _Field.EXPIRED_AT, _Field.REMINDED_AT)


def _utc(at: datetime) -> datetime:
    """Same precision and refusal of naive datetimes as the real Store."""
    return _parse_date(_format_date(at))


class FakeStore:
    def __init__(self, *, fail_with: StoreError | None = None) -> None:
        self.fail_with = fail_with
        self.connected = False
        self.sent: dict[str, tuple[str, str]] = {}
        self._next_id = 0
        self._next_seq = 0
        self._queued: list[tuple[int, QueuedJob]] = []
        self._alerts: list[tuple[int, Alert]] = []
        self._last_alert_at: datetime | None = None
        self._brave: dict[str, datetime] = {}
        self._sessions: dict[str, dict[str, datetime | None]] = {}
        self._tenants: dict[str, datetime] = {}
        self._discovery: tuple[datetime, int] | None = None

    # -- helpers -------------------------------------------------------

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def _new_id(self) -> str:
        self._next_id += 1
        return f"fake{self._next_id:011d}"

    def _new_seq(self) -> int:
        self._next_seq += 1
        return self._next_seq

    def count(self, collection: Collection) -> int:
        counts = {
            Collection.SENT_JOBS: lambda: len(self.sent),
            Collection.QUEUED_JOBS: lambda: len(self._queued),
            Collection.ALERTS: lambda: len(self._alerts),
            Collection.ALERT_SENDS: lambda: int(self._last_alert_at is not None),
            Collection.BRAVE_SEARCHES: lambda: len(self._brave),
            Collection.LINKEDIN_SESSIONS: lambda: len(self._sessions),
            Collection.INHIRE_TENANTS: lambda: len(self._tenants),
            Collection.INHIRE_DISCOVERIES: lambda: int(self._discovery is not None),
        }
        if collection not in counts:
            raise ValueError(f"not a data collection: {collection}")
        return counts[collection]()

    # -- login ---------------------------------------------------------

    def connect(self) -> None:
        self._check()
        self.connected = True

    # -- sent_jobs -----------------------------------------------------

    def is_sent(self, link: str) -> bool:
        self._check()
        return link in self.sent

    def mark_sent(self, link: str, published: str, title: str) -> None:
        self._check()
        self.sent.setdefault(link, (published, title))

    # -- queued_jobs ---------------------------------------------------

    def queue_job(self, link: str, title: str, company: str, published: str,
                  message: str, source: Source) -> None:
        self._check()
        if any(job.link == link and job.source is source for _, job in self._queued):
            return
        job = QueuedJob(id=self._new_id(), link=link, title=title, company=company,
                        published=published, message=message, source=source)
        self._queued.append((self._new_seq(), job))

    def queued_jobs(self) -> list[QueuedJob]:
        self._check()
        ordered = sorted(self._queued, key=lambda item: (item[1].source is Source.WEB, item[0]))
        return [QueuedJob(**vars(job)) for _, job in ordered]

    def delete_queued(self, record_id: str) -> None:
        self._check()
        self._queued = [item for item in self._queued if item[1].id != record_id]

    # -- alerts / alert_sends -----------------------------------------

    def add_alerts(self, alerts: list[tuple[str, str]]) -> None:
        self._check()
        for label, text in alerts:
            self._alerts.append((self._new_seq(), Alert(self._new_id(), label, text)))

    def pending_alerts(self) -> list[Alert]:
        self._check()
        return [Alert(alert.id, alert.label, alert.text)
                for _, alert in sorted(self._alerts, key=lambda item: item[0])]

    def delete_alerts(self, ids: list[str]) -> None:
        self._check()
        wanted = set(ids)
        self._alerts = [item for item in self._alerts if item[1].id not in wanted]

    def last_alert_at(self) -> datetime | None:
        self._check()
        return self._last_alert_at

    def set_last_alert_at(self, at: datetime) -> None:
        self._check()
        self._last_alert_at = _utc(at)

    # -- brave_searches ------------------------------------------------

    def brave_searched_at(self, label: str) -> datetime | None:
        self._check()
        return self._brave.get(label)

    def set_brave_searched_at(self, label: str, at: datetime) -> None:
        self._check()
        self._brave[label] = _utc(at)

    # -- linkedin_sessions --------------------------------------------

    def linkedin_session(self, cookie_hash: str) -> LinkedinSession | None:
        self._check()
        row = self._sessions.get(cookie_hash)
        if row is None:
            return None
        return LinkedinSession(searched_at=row.get(_Field.SEARCHED_AT),
                               expired_at=row.get(_Field.EXPIRED_AT),
                               reminded_at=row.get(_Field.REMINDED_AT))

    def save_linkedin_session(self, cookie_hash: str, **fields: datetime | None) -> None:
        self._check()
        allowed = {field.value for field in _SESSION_FIELDS}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown linkedin session fields: {sorted(unknown)}")
        converted = {name: None if value is None else _utc(value)
                     for name, value in fields.items()}
        self._sessions.setdefault(cookie_hash, {}).update(converted)

    def delete_linkedin_sessions_except(self, cookie_hash: str) -> None:
        self._check()
        self._sessions = {key: row for key, row in self._sessions.items() if key == cookie_hash}

    # -- inhire --------------------------------------------------------

    def inhire_tenants(self) -> list[str]:
        self._check()
        return sorted(self._tenants)

    def add_inhire_tenants(self, slugs: list[str], found_at: datetime) -> None:
        self._check()
        if not slugs:
            return
        found = _utc(found_at)
        for slug in slugs:
            self._tenants.setdefault(slug, found)

    def remove_inhire_tenant(self, slug: str) -> None:
        self._check()
        self._tenants.pop(slug, None)

    def inhire_discovery(self) -> tuple[datetime, int | None] | None:
        self._check()
        return self._discovery

    def set_inhire_discovery(self, last_run: datetime, found: int) -> None:
        self._check()
        self._discovery = (_utc(last_run), found)
