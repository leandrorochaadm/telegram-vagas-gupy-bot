# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
# ///
"""One-off import of the old SQLite history into PocketBase.

The superuser password comes from stdin (one line) or from a prompt when stdin is a terminal.
Everything is read and validated before the first record is written.
"""

import argparse
import getpass
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from urllib.parse import quote

import requests

REQUEST_TIMEOUT_SECONDS = 15
PAGE_SIZE = 500
SUPERUSER_COLLECTION = "_superusers"
UNIQUE_ERROR_CODE = "validation_not_unique"
PB_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

EXIT_OK = 0
EXIT_COUNT_MISMATCH = 1
EXIT_FAILURE = 2

SOURCE_BY_OLD_LABEL: dict[str, str] = {
    "GUPY": "gupy",
    "PROGRAMATHOR": "programathor",
    "LINKEDIN": "linkedin",
    "LINKEDIN_POST": "linkedin_posts",
    "LINKEDIN PUBLICAÇÕES": "linkedin_posts",
    "INHIRE": "inhire",
    "SOLIDES": "solides",
    "REMOTAR": "remotar",
    "WEB": "web",
    "BOT": "bot",
    "TELEGRAM": "telegram",
}


class ImportMode(StrEnum):
    UNIQUE = "unique"
    SINGLE_ROW = "single_row"
    MULTISET = "multiset"


class ColumnKind(StrEnum):
    TEXT = "text"
    SOURCE = "source"
    DATE = "date"
    NUMBER = "number"


@dataclass(frozen=True)
class Column:
    sqlite_name: str
    field: str
    kind: ColumnKind
    required: bool = False


@dataclass(frozen=True)
class TableMap:
    sqlite_table: str
    collection: str
    columns: tuple[Column, ...]
    mode: ImportMode


TABLES: tuple[TableMap, ...] = (
    TableMap(
        "vagas_enviadas",
        "sent_jobs",
        (
            Column("link", "link", ColumnKind.TEXT, True),
            Column("data_publicacao", "published", ColumnKind.TEXT),
            Column("titulo", "title", ColumnKind.TEXT),
        ),
        ImportMode.UNIQUE,
    ),
    TableMap(
        "vagas_pendentes",
        "queued_jobs",
        (
            Column("link", "link", ColumnKind.TEXT, True),
            Column("titulo", "title", ColumnKind.TEXT),
            Column("empresa", "company", ColumnKind.TEXT),
            Column("data_publicacao", "published", ColumnKind.TEXT),
            Column("mensagem", "message", ColumnKind.TEXT),
            Column("fonte", "source", ColumnKind.SOURCE, True),
        ),
        ImportMode.UNIQUE,
    ),
    TableMap(
        "avisos_pendentes",
        "alerts",
        (
            Column("fonte", "label", ColumnKind.SOURCE, True),
            Column("texto", "text", ColumnKind.TEXT, True),
        ),
        ImportMode.MULTISET,
    ),
    TableMap(
        "avisos_enviados",
        "alert_sends",
        (Column("enviado_em", "sent_at", ColumnKind.DATE, True),),
        ImportMode.SINGLE_ROW,
    ),
    TableMap(
        "brave_searches",
        "brave_searches",
        (
            Column("source", "label", ColumnKind.SOURCE, True),
            Column("searched_at", "searched_at", ColumnKind.DATE, True),
        ),
        ImportMode.UNIQUE,
    ),
    TableMap(
        "linkedin_session",
        "linkedin_sessions",
        (
            Column("cookie_hash", "cookie_hash", ColumnKind.TEXT, True),
            Column("searched_at", "searched_at", ColumnKind.DATE),
            Column("expired_at", "expired_at", ColumnKind.DATE),
            Column("reminded_at", "reminded_at", ColumnKind.DATE),
        ),
        ImportMode.UNIQUE,
    ),
    TableMap(
        "inhire_tenants",
        "inhire_tenants",
        (
            Column("slug", "slug", ColumnKind.TEXT, True),
            Column("found_at", "found_at", ColumnKind.DATE),
        ),
        ImportMode.UNIQUE,
    ),
    TableMap(
        "inhire_discovery",
        "inhire_discoveries",
        (
            Column("last_run", "last_run", ColumnKind.DATE, True),
            Column("found", "found", ColumnKind.NUMBER),
        ),
        ImportMode.SINGLE_ROW,
    ),
)


@dataclass
class TableResult:
    table: TableMap
    sqlite_count: int | None  # None = table missing in the SQLite file
    created: int
    skipped: int
    pocketbase_count: int


class ImportFailure(Exception):
    """Short pt-BR message; never contains the password or a token."""


def convert_source(raw: str | None) -> str:
    if raw is None or raw not in SOURCE_BY_OLD_LABEL:
        raise ImportFailure(f'rótulo de fonte desconhecido: "{raw}"')
    return SOURCE_BY_OLD_LABEL[raw]


def to_pb_date(raw: str | None) -> str:
    if raw is None or raw == "":
        return ""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ImportFailure(f'data inválida: "{raw}"') from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return f"{parsed.strftime(PB_DATE_FORMAT)}.{parsed.microsecond // 1000:03d}Z"


def _convert_value(table: TableMap, column: Column, raw: object) -> str | int | None:
    where = f"{table.sqlite_table}.{column.sqlite_name}"
    try:
        match column.kind:
            case ColumnKind.SOURCE:
                return convert_source(raw)  # type: ignore[arg-type]
            case ColumnKind.DATE:
                return to_pb_date(raw)  # type: ignore[arg-type]
            case ColumnKind.NUMBER:
                return None if raw is None else int(raw)  # type: ignore[arg-type]
            case ColumnKind.TEXT:
                return "" if raw is None else raw  # type: ignore[return-value]
    except ImportFailure as error:
        # Insert the location after the generic prefix: "<what> em <table.column>: <value>".
        message = str(error)
        head, _, value = message.partition(": ")
        raise ImportFailure(f"{head} em {where}: {value}") from None
    except (TypeError, ValueError):
        raise ImportFailure(f'número inválido em {where}: "{raw}"') from None


def read_table(conn: sqlite3.Connection, table: TableMap) -> list[dict[str, str | int]] | None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table.sqlite_table,)
    ).fetchone()
    if exists is None:
        print(f"aviso: tabela {table.sqlite_table} não existe no arquivo, pulada", file=sys.stderr)
        return None
    actual = [row[1] for row in conn.execute(f'PRAGMA table_info("{table.sqlite_table}")')]
    expected = [column.sqlite_name for column in table.columns]
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise ImportFailure(
            f"colunas inesperadas em {table.sqlite_table}: faltando {missing}, sobrando {extra}"
        )
    select = ", ".join(f'"{name}"' for name in expected)
    rows: list[dict[str, str | int]] = []
    for raw_row in conn.execute(f'SELECT {select} FROM "{table.sqlite_table}"'):
        body: dict[str, str | int] = {}
        for column, raw in zip(table.columns, raw_row):
            value = _convert_value(table, column, raw)
            if column.required and value in ("", None):
                raise ImportFailure(
                    f"campo obrigatório vazio em {table.sqlite_table}.{column.sqlite_name}"
                )
            if value is not None:
                body[column.field] = value
        rows.append(body)
    return rows


class PocketBaseAdmin:
    def __init__(self, url: str, *, session: requests.Session | None = None) -> None:
        self._url = url.rstrip("/")
        self._session = session or requests.Session()
        self._token = ""

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = {"Authorization": self._token} if self._token else {}
        try:
            return self._session.request(
                method,
                f"{self._url}{path}",
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                **kwargs,
            )
        except requests.RequestException:
            raise ImportFailure(f"o PocketBase não respondeu em {self._url}") from None

    @staticmethod
    def _error_codes(response: requests.Response) -> list[str]:
        try:
            data = response.json().get("data", {})
        except ValueError:
            return []
        return [str(value.get("code", "")) for value in data.values() if isinstance(value, dict)]

    def _fail(self, collection: str, response: requests.Response) -> ImportFailure:
        codes = ", ".join(self._error_codes(response)) or "-"
        return ImportFailure(f"erro em {collection}: status {response.status_code}, códigos: {codes}")

    def login(self, email: str, password: str) -> None:
        response = self._request(
            "POST",
            f"/api/collections/{SUPERUSER_COLLECTION}/auth-with-password",
            json={"identity": email, "password": password},
        )
        if response.status_code in (400, 401, 403):
            raise ImportFailure("o login do superusuário foi recusado")
        if response.status_code >= 400:
            raise self._fail(SUPERUSER_COLLECTION, response)
        self._token = response.json()["token"]

    def create(self, collection: str, body: dict[str, str | int]) -> bool:
        response = self._request("POST", f"/api/collections/{collection}/records", json=body)
        if response.status_code == 400 and UNIQUE_ERROR_CODE in self._error_codes(response):
            return False
        if response.status_code >= 400:
            raise self._fail(collection, response)
        return True

    def list_all(self, collection: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"/api/collections/{collection}/records",
                params={"perPage": PAGE_SIZE, "page": page},
            )
            if response.status_code >= 400:
                raise self._fail(collection, response)
            payload = response.json()
            items.extend(payload["items"])
            if page >= payload["totalPages"]:
                return items
            page += 1

    def count(self, collection: str) -> int:
        response = self._request(
            "GET", f"/api/collections/{collection}/records", params={"perPage": 1}
        )
        if response.status_code >= 400:
            raise self._fail(collection, response)
        return response.json()["totalItems"]


def import_table(
    admin: PocketBaseAdmin, table: TableMap, rows: list[dict[str, str | int]]
) -> TableResult:
    created = skipped = 0
    match table.mode:
        case ImportMode.UNIQUE:
            for body in rows:
                if admin.create(table.collection, body):
                    created += 1
                else:
                    skipped += 1
        case ImportMode.SINGLE_ROW:
            if admin.count(table.collection) > 0:
                skipped = len(rows)
            else:
                for body in rows:
                    admin.create(table.collection, body)
                    created += 1
        case ImportMode.MULTISET:
            existing = Counter(
                (record["label"], record["text"]) for record in admin.list_all(table.collection)
            )
            for body in rows:
                key = (body["label"], body["text"])
                if existing[key] > 0:
                    existing[key] -= 1
                    skipped += 1
                else:
                    admin.create(table.collection, body)
                    created += 1
    return TableResult(table, len(rows), created, skipped, admin.count(table.collection))


def read_password() -> str:
    if sys.stdin.isatty():
        password = getpass.getpass("Senha do superusuário: ")
    else:
        password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        raise ImportFailure("a senha do superusuário está vazia")
    return password


def _open_database(path: str) -> sqlite3.Connection:
    try:
        return sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)
    except sqlite3.Error:
        raise ImportFailure(f"não foi possível abrir o arquivo {path}") from None


def _run(args: argparse.Namespace) -> int:
    password = read_password()
    conn = _open_database(args.db)
    try:
        try:
            data = [(table, read_table(conn, table)) for table in TABLES]
        except sqlite3.Error:
            raise ImportFailure(f"não foi possível ler o arquivo {args.db}") from None
    finally:
        conn.close()

    admin = PocketBaseAdmin(args.url)
    admin.login(args.email, password)

    results: list[TableResult] = []
    for table, rows in data:
        if rows is None:
            print(f"{table.collection}: tabela ausente no SQLite, pulada")
            continue
        result = import_table(admin, table, rows)
        results.append(result)
        print(
            f"{table.collection}: SQLite {result.sqlite_count} × PocketBase {result.pocketbase_count}"
            f" (criados {result.created}, já existiam {result.skipped})"
        )

    mismatched = [r.table.collection for r in results if r.sqlite_count != r.pocketbase_count]
    if mismatched:
        print(f"contagem diferente em: {', '.join(mismatched)}", file=sys.stderr)
        return EXIT_COUNT_MISMATCH
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Importa o histórico do SQLite para o PocketBase.")
    parser.add_argument("--db", required=True, help="caminho do arquivo .db")
    parser.add_argument("--url", required=True, help="endereço do PocketBase")
    parser.add_argument("--email", required=True, help="e-mail do superusuário")
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except ImportFailure as error:
        print(f"erro: {error}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
