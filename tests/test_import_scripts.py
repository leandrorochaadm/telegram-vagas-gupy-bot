import importlib.util
import os
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import types
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
REAL_DB_PATH = os.path.join(ROOT_DIR, "vagas_gupy.db")
SCRIPT_TIMEOUT_SECONDS = 120
IMPORT_EMAIL = "importer@test.local"
BOT_EMAIL = "bot@test.local"

sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, ROOT_DIR)

from pocketbase_server import PocketBaseServer, pocketbase_bin  # noqa: E402

FIXTURE_SCHEMA = """
CREATE TABLE vagas_enviadas (
    link TEXT PRIMARY KEY,
    data_publicacao TEXT,
    titulo TEXT
);
CREATE TABLE inhire_tenants (slug TEXT PRIMARY KEY, found_at TEXT);
CREATE TABLE inhire_discovery (last_run TEXT, found INTEGER);
CREATE TABLE avisos_pendentes (fonte TEXT, texto TEXT);
CREATE TABLE avisos_enviados (enviado_em TEXT);
CREATE TABLE brave_searches (source TEXT PRIMARY KEY, searched_at TEXT);
CREATE TABLE linkedin_session (cookie_hash TEXT PRIMARY KEY, searched_at TEXT, expired_at TEXT, reminded_at TEXT);
CREATE TABLE vagas_pendentes (link TEXT PRIMARY KEY, titulo TEXT, empresa TEXT, data_publicacao TEXT, mensagem TEXT, fonte TEXT);
"""
FIXTURE_DATA = [
    ("INSERT INTO vagas_enviadas VALUES (?, ?, ?)", [("https://a/1", "há 2 dias", "Dev A"), ("https://a/2", None, "Dev B")]),
    (
        "INSERT INTO vagas_pendentes VALUES (?, ?, ?, ?, ?, ?)",
        [("https://q/1", "Dev Q", "Acme", "hoje", "msg", "LINKEDIN_POST")],
    ),
    (
        "INSERT INTO avisos_pendentes VALUES (?, ?)",
        [("GUPY", "aviso repetido"), ("GUPY", "aviso repetido"), ("LINKEDIN PUBLICAÇÕES", "outro")],
    ),
    ("INSERT INTO avisos_enviados VALUES (?)", [("2026-10-23T23:06:08+00:00",)]),
    (
        "INSERT INTO brave_searches VALUES (?, ?)",
        [("WEB", "2026-10-23T20:00:00-03:00"), ("BOT", "2026-10-23T23:06:08+00:00")],
    ),
    (
        "INSERT INTO linkedin_session VALUES (?, ?, ?, ?)",
        [("hash1", "2026-10-23T23:06:08+00:00", "2026-10-23T23:06:08+00:00", None)],
    ),
    (
        "INSERT INTO inhire_tenants VALUES (?, ?)",
        [("acme", "2026-09-29T16:14:36"), ("beta", "2026-09-29T16:14:37")],
    ),
    ("INSERT INTO inhire_discovery VALUES (?, ?)", [("2026-10-23T23:06:08+00:00", 0)]),
]
# SQLite table → (PocketBase collection, expected row count of the fixture)
FIXTURE_COUNTS = {
    "sent_jobs": 2,
    "queued_jobs": 1,
    "alerts": 3,
    "alert_sends": 1,
    "brave_searches": 2,
    "linkedin_sessions": 1,
    "inhire_tenants": 2,
    "inhire_discoveries": 1,
}
DATA_COLLECTIONS = tuple(FIXTURE_COUNTS)
SQLITE_TABLE_BY_COLLECTION = {
    "sent_jobs": "vagas_enviadas",
    "queued_jobs": "vagas_pendentes",
    "alerts": "avisos_pendentes",
    "alert_sends": "avisos_enviados",
    "brave_searches": "brave_searches",
    "linkedin_sessions": "linkedin_session",
    "inhire_tenants": "inhire_tenants",
    "inhire_discoveries": "inhire_discovery",
}


def load_script(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS_DIR, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(name: str, args: list[str], password: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, f"{name}.py"), *args],
        input=password + "\n",
        text=True,
        capture_output=True,
        timeout=SCRIPT_TIMEOUT_SECONDS,
    )


def make_fixture_db(path: str, skip_tables: tuple[str, ...] = ()) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(FIXTURE_SCHEMA)
        for statement, rows in FIXTURE_DATA:
            conn.executemany(statement, rows)
        for table in skip_tables:
            conn.execute(f"DROP TABLE {table}")
        conn.commit()
    finally:
        conn.close()


def unused_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


class ServerTestCase(unittest.TestCase):
    """Starts a throwaway PocketBase per test, with its own superuser whose password we know."""

    def setUp(self) -> None:
        self.server = PocketBaseServer(pocketbase_bin())
        self.server.start()
        self.addCleanup(self.server.stop)
        self.tmp = tempfile.mkdtemp(prefix="import_scripts_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.password = secrets.token_urlsafe(24)
        self.server.create_record(
            "_superusers",
            {"email": IMPORT_EMAIL, "password": self.password, "passwordConfirm": self.password},
        )

    def counts(self) -> dict[str, int]:
        return {name: self.server.count(name) for name in DATA_COLLECTIONS}

    def run_import(self, db_path: str, password: str | None = None) -> subprocess.CompletedProcess:
        return run_script(
            "import_sqlite",
            ["--db", db_path, "--url", self.server.url, "--email", IMPORT_EMAIL],
            self.password if password is None else password,
        )

    def records(self, collection: str) -> list[dict]:
        response = self.server.request(
            "GET",
            f"/api/collections/{collection}/records",
            token=self.server.superuser_token,
            params={"perPage": 500},
        )
        return response.json()["items"]


class ConversionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_script("import_sqlite")

    def test_convert_source_maps_old_labels(self) -> None:
        self.assertEqual(self.module.convert_source("LINKEDIN_POST"), "linkedin_posts")
        self.assertEqual(self.module.convert_source("LINKEDIN PUBLICAÇÕES"), "linkedin_posts")
        self.assertEqual(self.module.convert_source("GUPY"), "gupy")

    def test_convert_source_rejects_unknown(self) -> None:
        for raw in ("XYZ", "", None, "gupy"):
            with self.assertRaises(self.module.ImportFailure):
                self.module.convert_source(raw)

    def test_source_map_matches_store_enum(self) -> None:
        from store import Source

        valid = {member.value for member in Source}
        for value in self.module.SOURCE_BY_OLD_LABEL.values():
            self.assertIn(value, valid)
            self.assertNotEqual(value, Source.UNKNOWN.value)

    def test_naive_date_is_utc(self) -> None:
        self.assertEqual(self.module.to_pb_date("2026-09-29T16:14:36"), "2026-09-29 16:14:36.000Z")

    def test_offset_date_is_converted_to_utc(self) -> None:
        self.assertEqual(self.module.to_pb_date("2026-10-23T20:00:00-03:00"), "2026-10-23 23:00:00.000Z")

    def test_empty_date_is_blank(self) -> None:
        self.assertEqual(self.module.to_pb_date(None), "")
        self.assertEqual(self.module.to_pb_date(""), "")

    def test_invalid_date_raises(self) -> None:
        with self.assertRaises(self.module.ImportFailure):
            self.module.to_pb_date("ontem")

    def test_missing_db_file_exits_with_2(self) -> None:
        missing = os.path.join(tempfile.gettempdir(), f"missing_{secrets.token_hex(4)}.db")
        result = run_script(
            "import_sqlite",
            ["--db", missing, "--url", unused_url(), "--email", IMPORT_EMAIL],
            "secret",
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)

    def test_unreachable_server_exits_with_2(self) -> None:
        tmp = tempfile.mkdtemp(prefix="import_scripts_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db_path = os.path.join(tmp, "fixture.db")
        make_fixture_db(db_path)
        url = unused_url()
        result = run_script("import_sqlite", ["--db", db_path, "--url", url, "--email", IMPORT_EMAIL], "secret")
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"o PocketBase não respondeu em {url}", result.stderr)
        result = run_script(
            "create_bot_account",
            ["--url", url, "--email", IMPORT_EMAIL, "--bot-email", BOT_EMAIL, "--out", os.path.join(tmp, "pw")],
            "secret",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"o PocketBase não respondeu em {url}", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(tmp, "pw")))


@unittest.skipUnless(pocketbase_bin(), "POCKETBASE_BIN not set")
class ImportFixtureTest(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db_path = os.path.join(self.tmp, "fixture.db")
        make_fixture_db(self.db_path)

    def test_imports_every_table_with_english_fields(self) -> None:
        result = self.run_import(self.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.counts(), FIXTURE_COUNTS)
        self.assertEqual(result.stdout.count("SQLite"), 8)
        queued = self.records("queued_jobs")[0]
        self.assertEqual((queued["source"], queued["company"], queued["title"]), ("linkedin_posts", "Acme", "Dev Q"))
        tenants = {r["slug"]: r["found_at"] for r in self.records("inhire_tenants")}
        self.assertEqual(tenants["acme"], "2026-09-29 16:14:36.000Z")
        brave = {r["label"]: r["searched_at"] for r in self.records("brave_searches")}
        self.assertEqual(brave["web"], "2026-10-23 23:00:00.000Z")
        self.assertEqual(self.records("linkedin_sessions")[0]["reminded_at"], "")
        self.assertEqual(self.records("inhire_discoveries")[0]["found"], 0)

    def test_second_run_creates_nothing(self) -> None:
        self.assertEqual(self.run_import(self.db_path).returncode, 0)
        second = self.run_import(self.db_path)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.counts(), FIXTURE_COUNTS)
        self.assertIn("(criados 0, já existiam 2)", second.stdout)
        self.assertIn("alerts: SQLite 3 × PocketBase 3 (criados 0, já existiam 3)", second.stdout)

    def test_duplicate_alerts_are_both_imported(self) -> None:
        self.assertEqual(self.run_import(self.db_path).returncode, 0)
        texts = [r["text"] for r in self.records("alerts")]
        self.assertEqual(texts.count("aviso repetido"), 2)

    def test_unknown_label_stops_before_writing(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO avisos_pendentes VALUES ('XYZ', 'texto')")
        conn.commit()
        conn.close()
        result = self.run_import(self.db_path)
        self.assertEqual(result.returncode, 2)
        self.assertIn('rótulo de fonte desconhecido em avisos_pendentes.fonte: "XYZ"', result.stderr)
        self.assertEqual(sum(self.counts().values()), 0)

    def test_required_field_empty_stops_before_writing(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO avisos_pendentes VALUES ('GUPY', NULL)")
        conn.commit()
        conn.close()
        result = self.run_import(self.db_path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("avisos_pendentes.texto", result.stderr)
        self.assertEqual(sum(self.counts().values()), 0)

    def test_missing_table_is_skipped(self) -> None:
        make_path = os.path.join(self.tmp, "partial.db")
        make_fixture_db(make_path, skip_tables=("linkedin_session",))
        result = self.run_import(make_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tabela ausente no SQLite, pulada", result.stdout)
        self.assertEqual(self.server.count("linkedin_sessions"), 0)
        self.assertEqual(self.server.count("sent_jobs"), 2)

    def test_unexpected_columns_stop_before_writing(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE vagas_enviadas ADD COLUMN extra TEXT")
        conn.commit()
        conn.close()
        result = self.run_import(self.db_path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("vagas_enviadas", result.stderr)
        self.assertIn("extra", result.stderr)
        self.assertEqual(sum(self.counts().values()), 0)

    def test_count_mismatch_exits_with_1(self) -> None:
        self.server.create_record("sent_jobs", {"link": "https://other/1"})
        result = self.run_import(self.db_path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("sent_jobs", result.stderr)

    def test_wrong_password_exits_with_2(self) -> None:
        result = self.run_import(self.db_path, password="wrong-password")
        self.assertEqual(result.returncode, 2)
        self.assertIn("o login do superusuário foi recusado", result.stderr)
        self.assertEqual(sum(self.counts().values()), 0)

    def test_output_never_contains_password(self) -> None:
        for password in (self.password, "wrong-password"):
            result = self.run_import(self.db_path, password=password)
            self.assertNotIn(password, result.stdout + result.stderr)


@unittest.skipUnless(pocketbase_bin(), "POCKETBASE_BIN not set")
@unittest.skipUnless(os.path.exists(REAL_DB_PATH), "vagas_gupy.db not found")
class ImportRealDatabaseTest(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db_path = os.path.join(self.tmp, "vagas_gupy.db")
        shutil.copy(REAL_DB_PATH, self.db_path)

    def sqlite_counts(self) -> dict[str, int]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            return {
                collection: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for collection, table in SQLITE_TABLE_BY_COLLECTION.items()
            }
        finally:
            conn.close()

    def test_real_database_counts_match(self) -> None:
        result = self.run_import(self.db_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        print("\n" + result.stdout)
        self.assertEqual(self.counts(), self.sqlite_counts())

    def test_second_run_creates_nothing(self) -> None:
        self.assertEqual(self.run_import(self.db_path).returncode, 0)
        second = self.run_import(self.db_path)
        print("\n" + second.stdout)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.counts(), self.sqlite_counts())
        self.assertEqual(second.stdout.count("(criados 0,"), 8)


@unittest.skipUnless(pocketbase_bin(), "POCKETBASE_BIN not set")
class CreateBotAccountTest(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.out = os.path.join(self.tmp, "bot-password")

    def run_account(self, out: str | None = None, password: str | None = None) -> subprocess.CompletedProcess:
        return run_script(
            "create_bot_account",
            [
                "--url",
                self.server.url,
                "--email",
                IMPORT_EMAIL,
                "--bot-email",
                BOT_EMAIL,
                "--out",
                out or self.out,
            ],
            self.password if password is None else password,
        )

    def read_out(self) -> str:
        with open(self.out, encoding="utf-8") as handle:
            return handle.read()

    def bot_login_status(self, password: str) -> int:
        return self.server.request(
            "POST",
            "/api/collections/bot_accounts/auth-with-password",
            json={"identity": BOT_EMAIL, "password": password},
        ).status_code

    def test_creates_account_and_secret_file(self) -> None:
        result = self.run_account()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("conta do bot criada", result.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o600)
        secret = self.read_out()
        self.assertTrue(secret)
        self.assertFalse(secret.endswith("\n"))
        self.assertEqual(self.bot_login_status(secret), 200)
        self.assertFalse(os.path.exists(f"{self.out}.tmp"))

    def test_second_run_rotates_password_and_keeps_one_account(self) -> None:
        self.assertEqual(self.run_account().returncode, 0)
        old = self.read_out()
        result = self.run_account()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("conta do bot atualizada", result.stdout)
        new = self.read_out()
        self.assertNotEqual(old, new)
        self.assertEqual(self.bot_login_status(old), 400)
        self.assertEqual(self.bot_login_status(new), 200)
        self.assertEqual(self.server.count("bot_accounts"), 1)

    def test_existing_file_gets_mode_600(self) -> None:
        with open(self.out, "w", encoding="utf-8") as handle:
            handle.write("old")
        os.chmod(self.out, 0o644)
        self.assertEqual(self.run_account().returncode, 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o600)

    def test_wrong_password_keeps_previous_file(self) -> None:
        with open(self.out, "w", encoding="utf-8") as handle:
            handle.write("previous")
        result = self.run_account(password="wrong-password")
        self.assertEqual(result.returncode, 2)
        self.assertIn("o login do superusuário foi recusado", result.stderr)
        self.assertEqual(self.read_out(), "previous")
        self.assertFalse(os.path.exists(f"{self.out}.tmp"))
        self.assertEqual(self.server.count("bot_accounts"), 0)

    def test_missing_out_dir_exits_with_2(self) -> None:
        result = self.run_account(out=os.path.join(self.tmp, "no-such-dir", "pw"))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.server.count("bot_accounts"), 0)

    def test_output_never_contains_password(self) -> None:
        result = self.run_account()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout + result.stderr
        self.assertNotIn(self.password, output)
        self.assertNotIn(self.read_out(), output)
        failed = self.run_account(password="wrong-password")
        self.assertNotIn("wrong-password", failed.stdout + failed.stderr)


if __name__ == "__main__":
    unittest.main()
