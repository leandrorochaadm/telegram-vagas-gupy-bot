"""PocketBase client: the only place in the bot that talks to the job database."""
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

import requests

ENV_URL = "PB_URL"
ENV_BOT_EMAIL = "PB_BOT_EMAIL"
ENV_BOT_PASSWORD = "PB_BOT_PASSWORD"

# Slightly over a multiple of 3 s, the TCP retransmission window (requests docs, "Timeouts")
CONNECT_TIMEOUT_S = 6.05
READ_TIMEOUT_S = 15
# Pause before each new attempt: a network blip between the runner and the VPS can last ~30 s
RETRY_DELAYS_S = (2, 5, 10)
PAGE_SIZE = 500

MSG_UNAVAILABLE = "o banco de vagas não respondeu"
MSG_LOGIN_REFUSED = "o login do bot no banco de vagas foi recusado"
MSG_REJECTED = "o banco de vagas recusou um pedido"
MSG_MISSING_CONFIG = "faltam os dados de acesso ao banco de vagas"

_RECORDS_PATH = "/api/collections/{collection}/records"
_AUTH_PATH = "/api/collections/{collection}/auth-with-password"
_NOT_UNIQUE_CODE = "validation_not_unique"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_BAD_REQUEST = 400
_HTTP_NOT_FOUND = 404
_HTTP_NO_CONTENT = 204
_HTTP_SERVER_ERROR = 500
_HTTP_CLIENT_ERROR = 400


class Collection(StrEnum):
    BOT_ACCOUNTS = "bot_accounts"
    SENT_JOBS = "sent_jobs"
    QUEUED_JOBS = "queued_jobs"
    ALERTS = "alerts"
    ALERT_SENDS = "alert_sends"
    BRAVE_SEARCHES = "brave_searches"
    LINKEDIN_SESSIONS = "linkedin_sessions"
    INHIRE_TENANTS = "inhire_tenants"
    INHIRE_DISCOVERIES = "inhire_discoveries"


class _Field(StrEnum):
    ID = "id"
    LINK = "link"
    PUBLISHED = "published"
    TITLE = "title"
    COMPANY = "company"
    MESSAGE = "message"
    SOURCE = "source"
    LABEL = "label"
    TEXT = "text"
    CREATED = "created"
    SENT_AT = "sent_at"
    SEARCHED_AT = "searched_at"
    COOKIE_HASH = "cookie_hash"
    EXPIRED_AT = "expired_at"
    REMINDED_AT = "reminded_at"
    SLUG = "slug"
    FOUND_AT = "found_at"
    LAST_RUN = "last_run"
    FOUND = "found"


_SESSION_FIELDS = (_Field.SEARCHED_AT, _Field.EXPIRED_AT, _Field.REMINDED_AT)


class Source(StrEnum):
    """Member = (stored value / CLI key, alert label, is a search source)."""

    GUPY = ("gupy", "GUPY", True)
    LINKEDIN = ("linkedin", "LINKEDIN", True)
    LINKEDIN_POSTS = ("linkedin_posts", "LINKEDIN PUBLICAÇÕES", True)
    INHIRE = ("inhire", "INHIRE", True)
    SOLIDES = ("solides", "SOLIDES", True)
    REMOTAR = ("remotar", "REMOTAR", True)
    WEB = ("web", "WEB", True)
    PROGRAMATHOR = ("programathor", "PROGRAMATHOR", False)
    BOT = ("bot", "BOT", False)
    TELEGRAM = ("telegram", "TELEGRAM", False)
    UNKNOWN = ("unknown", "FONTE DESCONHECIDA", False)

    label: str
    is_search_source: bool

    def __new__(cls, value: str, label: str, is_search_source: bool) -> "Source":
        member = str.__new__(cls, value)
        member._value_ = value
        member.label = label
        member.is_search_source = is_search_source
        return member

    @property
    def api_value(self) -> str:
        return self.value

    @classmethod
    def from_api(cls, raw: str | None) -> "Source":
        """Exact match only; anything else (including old labels) is UNKNOWN."""
        member = cls._value2member_map_.get(raw) if isinstance(raw, str) else None
        return member if member is not None else cls.UNKNOWN

    @classmethod
    def search_sources(cls) -> list["Source"]:
        return [source for source in cls if source.is_search_source]


class StoreFailure(StrEnum):
    UNAVAILABLE = "unavailable"
    LOGIN_REFUSED = "login_refused"
    REJECTED = "rejected"
    MISSING_CONFIG = "missing_config"


class StoreError(RuntimeError):
    def __init__(self, message: str = MSG_UNAVAILABLE,
                 kind: StoreFailure = StoreFailure.UNAVAILABLE) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class QueuedJob:
    id: str
    link: str
    title: str
    company: str
    published: str
    message: str
    source: Source


@dataclass(frozen=True)
class Alert:
    id: str
    label: str
    text: str


@dataclass(frozen=True)
class LinkedinSession:
    searched_at: datetime | None
    expired_at: datetime | None
    reminded_at: datetime | None


class _NotUnique(Exception):
    """A POST was refused by a unique index."""


def _quote(value: str) -> str:
    """Filter string literal, with quotes, escaping backslash and double quote."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_date(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    utc = at.astimezone(timezone.utc)
    return f"{utc.strftime(_DATE_FORMAT)}.{utc.microsecond // 1000:03d}Z"


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _date_or_empty(at: datetime | None) -> str:
    return "" if at is None else _format_date(at)


class Store:
    def __init__(self, url: str, email: str, password: str, *,
                 session: requests.Session | None = None) -> None:
        self._url = url.rstrip("/")
        self._email = email
        self._password = password
        self._session = session or requests.Session()
        self._token: str | None = None
        self._sent_links: set[str] | None = None

    def __repr__(self) -> str:
        return f"Store(url={self._url!r})"

    # -- login ---------------------------------------------------------

    def connect(self) -> None:
        path = _AUTH_PATH.format(collection=Collection.BOT_ACCOUNTS)
        try:
            body = self._request("POST", path, auth=False, safe_to_repeat=True,
                                 json={"identity": self._email, "password": self._password})
        except StoreError as error:
            if error.kind is StoreFailure.REJECTED:
                raise StoreError(MSG_LOGIN_REFUSED, StoreFailure.LOGIN_REFUSED) from error
            raise
        token = (body or {}).get("token")
        if not token:
            raise StoreError(MSG_LOGIN_REFUSED, StoreFailure.LOGIN_REFUSED)
        self._token = token

    # -- sent_jobs -----------------------------------------------------

    def is_sent(self, link: str) -> bool:
        if self._sent_links is None:
            rows = self._list_all(Collection.SENT_JOBS, fields=_Field.LINK)
            self._sent_links = {row[_Field.LINK] for row in rows}
        return link in self._sent_links

    def mark_sent(self, link: str, published: str, title: str) -> None:
        try:
            self._request("POST", self._records(Collection.SENT_JOBS), json={
                _Field.LINK: link, _Field.PUBLISHED: published, _Field.TITLE: title})
        except _NotUnique:
            pass
        if self._sent_links is not None:
            self._sent_links.add(link)

    # -- queued_jobs ---------------------------------------------------

    def queue_job(self, link: str, title: str, company: str, published: str,
                  message: str, source: Source) -> None:
        try:
            self._request("POST", self._records(Collection.QUEUED_JOBS), json={
                _Field.LINK: link, _Field.TITLE: title, _Field.COMPANY: company,
                _Field.PUBLISHED: published, _Field.MESSAGE: message,
                _Field.SOURCE: source.api_value})
        except _NotUnique:
            pass

    def queued_jobs(self) -> list[QueuedJob]:
        rows = self._list_all(Collection.QUEUED_JOBS, sort=_Field.CREATED)
        jobs = [(Source.from_api(row.get(_Field.SOURCE)), row) for row in rows]
        jobs.sort(key=lambda item: (item[0] is Source.WEB, item[1].get(_Field.CREATED, "")))
        return [QueuedJob(
            id=row[_Field.ID], link=row.get(_Field.LINK, ""), title=row.get(_Field.TITLE, ""),
            company=row.get(_Field.COMPANY, ""), published=row.get(_Field.PUBLISHED, ""),
            message=row.get(_Field.MESSAGE, ""), source=source) for source, row in jobs]

    def delete_queued(self, record_id: str) -> None:
        self._delete(Collection.QUEUED_JOBS, record_id)

    # -- alerts / alert_sends -----------------------------------------

    def add_alerts(self, alerts: list[tuple[str, str]]) -> None:
        for label, text in alerts:
            self._request("POST", self._records(Collection.ALERTS),
                          json={_Field.LABEL: label, _Field.TEXT: text})

    def pending_alerts(self) -> list[Alert]:
        rows = self._list_all(Collection.ALERTS, sort=_Field.CREATED)
        rows.sort(key=lambda row: row.get(_Field.CREATED, ""))
        return [Alert(id=row[_Field.ID], label=row.get(_Field.LABEL, ""),
                      text=row.get(_Field.TEXT, "")) for row in rows]

    def delete_alerts(self, ids: list[str]) -> None:
        for record_id in ids:
            self._delete(Collection.ALERTS, record_id)

    def last_alert_at(self) -> datetime | None:
        row = self._first(Collection.ALERT_SENDS, sort=f"-{_Field.SENT_AT}")
        return _parse_date(row.get(_Field.SENT_AT)) if row else None

    def set_last_alert_at(self, at: datetime) -> None:
        self._set_single(Collection.ALERT_SENDS, _Field.SENT_AT,
                         {_Field.SENT_AT: _format_date(at)})

    # -- brave_searches ------------------------------------------------

    def brave_searched_at(self, label: str) -> datetime | None:
        row = self._first(Collection.BRAVE_SEARCHES,
                          filter=f"{_Field.LABEL} = {_quote(label)}")
        return _parse_date(row.get(_Field.SEARCHED_AT)) if row else None

    def set_brave_searched_at(self, label: str, at: datetime) -> None:
        self._upsert(Collection.BRAVE_SEARCHES, _Field.LABEL, label,
                     {_Field.SEARCHED_AT: _format_date(at)})

    # -- linkedin_sessions --------------------------------------------

    def linkedin_session(self, cookie_hash: str) -> LinkedinSession | None:
        row = self._first(Collection.LINKEDIN_SESSIONS,
                          filter=f"{_Field.COOKIE_HASH} = {_quote(cookie_hash)}")
        if row is None:
            return None
        return LinkedinSession(
            searched_at=_parse_date(row.get(_Field.SEARCHED_AT)),
            expired_at=_parse_date(row.get(_Field.EXPIRED_AT)),
            reminded_at=_parse_date(row.get(_Field.REMINDED_AT)))

    def save_linkedin_session(self, cookie_hash: str, **fields: datetime | None) -> None:
        allowed = {field.value for field in _SESSION_FIELDS}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown linkedin session fields: {sorted(unknown)}")
        body = {name: _date_or_empty(value) for name, value in fields.items()}
        self._upsert(Collection.LINKEDIN_SESSIONS, _Field.COOKIE_HASH, cookie_hash, body)

    def delete_linkedin_sessions_except(self, cookie_hash: str) -> None:
        rows = self._list_all(Collection.LINKEDIN_SESSIONS,
                              filter=f"{_Field.COOKIE_HASH} != {_quote(cookie_hash)}")
        for row in rows:
            self._delete(Collection.LINKEDIN_SESSIONS, row[_Field.ID])

    # -- inhire --------------------------------------------------------

    def inhire_tenants(self) -> list[str]:
        rows = self._list_all(Collection.INHIRE_TENANTS)
        return sorted(row[_Field.SLUG] for row in rows)

    def add_inhire_tenants(self, slugs: list[str], found_at: datetime) -> None:
        if not slugs:
            return
        found = _format_date(found_at)
        for slug in slugs:
            try:
                self._request("POST", self._records(Collection.INHIRE_TENANTS),
                              json={_Field.SLUG: slug, _Field.FOUND_AT: found})
            except _NotUnique:
                pass

    def remove_inhire_tenant(self, slug: str) -> None:
        row = self._first(Collection.INHIRE_TENANTS, filter=f"{_Field.SLUG} = {_quote(slug)}")
        if row is not None:
            self._delete(Collection.INHIRE_TENANTS, row[_Field.ID])

    def inhire_discovery(self) -> tuple[datetime, int | None] | None:
        row = self._first(Collection.INHIRE_DISCOVERIES, sort=f"-{_Field.LAST_RUN}")
        if row is None:
            return None
        last_run = _parse_date(row.get(_Field.LAST_RUN))
        if last_run is None:
            return None
        found = row.get(_Field.FOUND)
        return last_run, (int(found) if isinstance(found, (int, float)) else None)

    def set_inhire_discovery(self, last_run: datetime, found: int) -> None:
        self._set_single(Collection.INHIRE_DISCOVERIES, _Field.LAST_RUN, {
            _Field.LAST_RUN: _format_date(last_run), _Field.FOUND: found})

    # -- transport -----------------------------------------------------

    @staticmethod
    def _records(collection: Collection, record_id: str | None = None) -> str:
        path = _RECORDS_PATH.format(collection=collection)
        return path if record_id is None else f"{path}/{record_id}"

    def _fail(self, method: str, path: str, status: object, message: str,
              kind: StoreFailure, cause: BaseException | None = None) -> StoreError:
        print(f"[store] {method} {path} failed: {status}")
        error = StoreError(message, kind)
        error.__cause__ = cause
        return error

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: dict | None = None, auth: bool = True,
                 safe_to_repeat: bool = False) -> dict | None:
        headers = {}
        if auth and self._token:
            headers["Authorization"] = self._token
        # A POST may have been saved even if the reply was lost: repeating it could
        # duplicate the row, so it is only repeated when the connection never opened
        safe_to_repeat = safe_to_repeat or method != "POST"
        response = None
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            last = attempt == len(RETRY_DELAYS_S)
            try:
                response = self._session.request(
                    method, self._url + path, params=params, json=json,
                    headers=headers, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
            except requests.RequestException as error:
                if last or not (safe_to_repeat or isinstance(error, requests.ConnectTimeout)):
                    raise self._fail(method, path, type(error).__name__, MSG_UNAVAILABLE,
                                     StoreFailure.UNAVAILABLE, error) from error
                time.sleep(RETRY_DELAYS_S[attempt])
                continue
            if response.status_code >= _HTTP_SERVER_ERROR:
                if last or not safe_to_repeat:
                    raise self._fail(method, path, response.status_code, MSG_UNAVAILABLE,
                                     StoreFailure.UNAVAILABLE)
                time.sleep(RETRY_DELAYS_S[attempt])
                continue
            break

        status = response.status_code
        if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
            raise self._fail(method, path, status, MSG_LOGIN_REFUSED, StoreFailure.LOGIN_REFUSED)
        if method == "POST" and status == _HTTP_BAD_REQUEST and self._is_not_unique(response):
            raise _NotUnique()
        if status == _HTTP_NOT_FOUND and method == "DELETE":
            return None
        if status >= _HTTP_CLIENT_ERROR:
            raise self._fail(method, path, status, MSG_REJECTED, StoreFailure.REJECTED)
        if status == _HTTP_NO_CONTENT:
            return None
        try:
            body = response.json()
        except ValueError as error:
            raise self._fail(method, path, "invalid JSON", MSG_UNAVAILABLE,
                             StoreFailure.UNAVAILABLE, error) from error
        if not isinstance(body, dict):
            raise self._fail(method, path, "unexpected body", MSG_UNAVAILABLE,
                             StoreFailure.UNAVAILABLE)
        return body

    @staticmethod
    def _is_not_unique(response) -> bool:
        try:
            data = response.json().get("data") or {}
        except (ValueError, AttributeError):
            return False
        return any(isinstance(entry, dict) and entry.get("code") == _NOT_UNIQUE_CODE
                   for entry in data.values())

    def _delete(self, collection: Collection, record_id: str) -> None:
        self._request("DELETE", self._records(collection, record_id))

    def _list_all(self, collection: Collection, *, filter: str | None = None,
                  sort: str | None = None, fields: str | None = None) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            params: dict = {"page": page, "perPage": PAGE_SIZE}
            if filter:
                params["filter"] = filter
            if sort:
                params["sort"] = sort
            if fields:
                params["fields"] = fields
            body = self._request("GET", self._records(collection), params=params) or {}
            items.extend(body.get("items", []))
            if page >= body.get("totalPages", 0):
                return items
            page += 1

    def _first(self, collection: Collection, *, filter: str | None = None,
               sort: str | None = None) -> dict | None:
        params: dict = {"page": 1, "perPage": 1, "skipTotal": 1}
        if filter:
            params["filter"] = filter
        if sort:
            params["sort"] = sort
        body = self._request("GET", self._records(collection), params=params) or {}
        items = body.get("items", [])
        return items[0] if items else None

    def _upsert(self, collection: Collection, key_field: _Field, key: str, body: dict) -> None:
        condition = f"{key_field} = {_quote(key)}"
        row = self._first(collection, filter=condition)
        if row is None:
            try:
                self._request("POST", self._records(collection), json={key_field: key, **body})
                return
            except _NotUnique:
                row = self._first(collection, filter=condition)
                if row is None:
                    raise self._fail("POST", self._records(collection), "not unique",
                                     MSG_REJECTED, StoreFailure.REJECTED) from None
        self._request("PATCH", self._records(collection, row[_Field.ID]), json=body)

    def _set_single(self, collection: Collection, sort_field: _Field, body: dict) -> None:
        row = self._first(collection, sort=f"-{sort_field}")
        if row is None:
            self._request("POST", self._records(collection), json=body)
        else:
            self._request("PATCH", self._records(collection, row[_Field.ID]), json=body)


def from_env() -> Store:
    url = os.environ.get(ENV_URL, "")
    email = os.environ.get(ENV_BOT_EMAIL, "")
    password = os.environ.get(ENV_BOT_PASSWORD, "")
    if not (url and email and password):
        raise StoreError(MSG_MISSING_CONFIG, StoreFailure.MISSING_CONFIG)
    return Store(url, email, password)
