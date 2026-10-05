"""Unit tests for the parallel run in main.py: one job per source, then merge and alert.

Run: python -m unittest discover -s tests -v
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

SCHEMA = [
    "CREATE TABLE vagas_enviadas (link TEXT PRIMARY KEY, data_publicacao TEXT, titulo TEXT)",
    "CREATE TABLE avisos_pendentes (fonte TEXT, texto TEXT)",
    "CREATE TABLE avisos_enviados (enviado_em TEXT)",
    "CREATE TABLE brave_searches (source TEXT PRIMARY KEY, searched_at TEXT)",
    "CREATE TABLE inhire_tenants (slug TEXT PRIMARY KEY, found_at TEXT)",
    "CREATE TABLE inhire_discovery (last_run TEXT, found INTEGER)",
]


class ParallelTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = os.path.join(self.dir.name, "vagas_gupy.db")
        self.write(self.base, SCHEMA + [
            "INSERT INTO vagas_enviadas VALUES ('https://a', '01/10', 'A')",
            "INSERT INTO avisos_pendentes VALUES ('GUPY', 'antigo')",
            "INSERT INTO avisos_enviados VALUES ('2026-10-01T10:00:00+00:00')",
            "INSERT INTO brave_searches VALUES ('WEB', '2026-10-01T10:00:00+00:00')",
            "INSERT INTO inhire_tenants VALUES ('acme', '2026-09-01')",
            "INSERT INTO inhire_tenants VALUES ('gone', '2026-09-01')",
            "INSERT INTO inhire_discovery VALUES ('2026-09-01T00:00:00', 2)",
        ])
        for state in (main._enviados_sessao, main._falhas_envio, main._avisos):
            state.clear()
            self.addCleanup(state.clear)
        # main(["--source", ...]) turns queueing on for the module
        self.addCleanup(setattr, main, "_queue_jobs", False)

    def write(self, path, statements):
        conn = sqlite3.connect(path)
        for sql in statements:
            conn.execute(sql)
        conn.commit()
        conn.close()

    def copy_of_base(self, source, statements):
        """Copy of the base database as job `source` leaves it, in the downloaded artifacts layout."""
        folder = os.path.join(self.dir.name, "copias", f"db-{source}")
        os.makedirs(folder)
        path = os.path.join(folder, "vagas_gupy.db")
        src = sqlite3.connect(self.base)
        dst = sqlite3.connect(path)
        src.backup(dst)
        src.close()
        dst.close()
        self.write(path, statements)
        return path

    def rows(self, sql):
        conn = sqlite3.connect(self.base)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def merge(self, copies):
        conn = sqlite3.connect(self.base)
        main.merge_databases(conn, conn.cursor(), copies)
        conn.close()


class MergeDatabasesTest(ParallelTestCase):
    def test_sent_jobs_from_every_copy_are_kept_once(self):
        gupy = self.copy_of_base("gupy", ["INSERT INTO vagas_enviadas VALUES ('https://b', '02/10', 'B')"])
        remotar = self.copy_of_base("remotar", ["INSERT INTO vagas_enviadas VALUES ('https://c', '02/10', 'C')"])
        self.merge({"gupy": gupy, "remotar": remotar})
        self.assertEqual(self.rows("SELECT link FROM vagas_enviadas ORDER BY link"),
                         [("https://a",), ("https://b",), ("https://c",)])

    def test_only_new_pending_alerts_are_added(self):
        gupy = self.copy_of_base("gupy", ["INSERT INTO avisos_pendentes VALUES ('GUPY', 'novo')"])
        inhire = self.copy_of_base("inhire", ["INSERT INTO avisos_pendentes VALUES ('INHIRE', 'outro')"])
        self.merge({"gupy": gupy, "inhire": inhire})
        self.assertEqual(self.rows("SELECT fonte, texto FROM avisos_pendentes ORDER BY rowid"),
                         [("GUPY", "antigo"), ("GUPY", "novo"), ("INHIRE", "outro")])

    def test_owner_copy_replaces_its_tables_so_deleted_rows_stay_deleted(self):
        inhire = self.copy_of_base("inhire", [
            "DELETE FROM inhire_tenants WHERE slug = 'gone'",
            "INSERT INTO inhire_tenants VALUES ('nova', '2026-10-05')",
            "DELETE FROM inhire_discovery",
            "INSERT INTO inhire_discovery VALUES ('2026-10-05T00:00:00', 1)",
        ])
        # Another job's copy still has 'gone' from the base: it must not bring it back
        gupy = self.copy_of_base("gupy", [])
        self.merge({"inhire": inhire, "gupy": gupy})
        self.assertEqual(self.rows("SELECT slug FROM inhire_tenants ORDER BY slug"), [("acme",), ("nova",)])
        self.assertEqual(self.rows("SELECT last_run, found FROM inhire_discovery"),
                         [("2026-10-05T00:00:00", 1)])

    def test_brave_search_time_takes_the_copy_value(self):
        web = self.copy_of_base("linkedin_posts", [
            "INSERT INTO brave_searches VALUES ('LINKEDIN PUBLICAÇÕES', '2026-10-05T12:00:00+00:00')",
        ])
        self.merge({"linkedin_posts": web})
        self.assertEqual(self.rows("SELECT source FROM brave_searches ORDER BY source"),
                         [("LINKEDIN PUBLICAÇÕES",), ("WEB",)])

    def test_older_brave_time_from_a_later_copy_does_not_undo_a_newer_one(self):
        self.write(self.base, ["INSERT INTO brave_searches VALUES ('LINKEDIN PUBLICAÇÕES', '2026-10-01T10:00:00+00:00')"])
        posts = self.copy_of_base("linkedin_posts", [
            "UPDATE brave_searches SET searched_at = '2026-10-05T12:00:00+00:00' "
            "WHERE source = 'LINKEDIN PUBLICAÇÕES'",
        ])
        # Merged after linkedin_posts and still holding the base time
        remotar = self.copy_of_base("remotar", [])
        self.merge({"linkedin_posts": posts, "remotar": remotar})
        self.assertEqual(self.rows("SELECT searched_at FROM brave_searches WHERE source = 'LINKEDIN PUBLICAÇÕES'"),
                         [("2026-10-05T12:00:00+00:00",)])

    def test_broken_copy_is_alerted_and_the_others_are_still_merged(self):
        broken = self.copy_of_base("gupy", [])
        with open(broken, "wb") as f:
            f.write(b"not a database")
        remotar = self.copy_of_base("remotar", ["INSERT INTO vagas_enviadas VALUES ('https://c', '02/10', 'C')"])
        self.merge({"gupy": broken, "remotar": remotar})
        self.assertIn(("https://c",), self.rows("SELECT link FROM vagas_enviadas"))
        self.assertEqual(main._avisos, [("GUPY", main.AVISO_COPIA_ESTRAGADA)])

    def test_copy_of_a_job_killed_mid_write_is_rolled_back_by_its_journal(self):
        path = self.copy_of_base("gupy", [
            f"INSERT INTO vagas_enviadas VALUES ('https://x/{i}', '02/10', 'salva')" for i in range(300)
        ])
        # Uncommitted update spilled into the file's existing pages (tiny cache), journal
        # beside it: what the artifact holds when the job is killed mid-write
        job = sqlite3.connect(path, isolation_level=None)
        job.execute("PRAGMA cache_size = 1")
        job.execute("BEGIN")
        job.execute("UPDATE vagas_enviadas SET titulo = 'pela metade' || hex(randomblob(150))")
        killed = path + ".killed"
        shutil.copy(path, killed)
        shutil.copy(path + "-journal", killed + "-journal")
        job.rollback()
        job.close()
        os.replace(killed, path)
        os.replace(killed + "-journal", path + "-journal")

        self.merge({"gupy": path})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM vagas_enviadas WHERE titulo LIKE 'pela metade%'"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM vagas_enviadas"), [(301,)])
        self.assertEqual(main._avisos, [])

    def test_table_missing_in_the_main_database_is_created(self):
        posts = self.copy_of_base("linkedin_posts", [
            "CREATE TABLE linkedin_session (cookie_hash TEXT PRIMARY KEY, searched_at TEXT, "
            "expired_at TEXT, reminded_at TEXT)",
            "INSERT INTO linkedin_session VALUES ('h', '2026-10-05', NULL, NULL)",
        ])
        self.merge({"linkedin_posts": posts})
        self.assertEqual(self.rows("SELECT cookie_hash FROM linkedin_session"), [("h",)])

    def test_alert_send_time_is_not_taken_from_copies(self):
        gupy = self.copy_of_base("gupy", ["DELETE FROM avisos_enviados"])
        self.merge({"gupy": gupy})
        self.assertEqual(self.rows("SELECT enviado_em FROM avisos_enviados"), [("2026-10-01T10:00:00+00:00",)])


class MainModesTest(ParallelTestCase):
    def run_main(self, *argv, side_effects=None):
        """Run main.main(argv) with every source faked; (source fakes, enviar_avisos, enviar_telegram)."""
        conn = sqlite3.connect(self.base)
        names = [name for name, _ in main.SOURCES.values()]
        with mock.patch.multiple(main, TOKEN="t", CHAT_ID="c", **{name: mock.DEFAULT for name in names}) as fakes, \
             mock.patch.object(main, "iniciar_banco", return_value=(conn, conn.cursor())), \
             mock.patch.object(main, "enviar_avisos") as send, \
             mock.patch.object(main, "enviar_telegram", return_value=True) as telegram:
            for name, effect in (side_effects or {}).items():
                fakes[name].side_effect = effect
            main.main(list(argv))
        return fakes, send, telegram

    def test_source_runs_only_that_source_and_saves_alerts_without_sending(self):
        def gupy(conn, cursor):
            main.anotar_avisos("GUPY", ["falhou"])
        fakes, send, telegram = self.run_main("--source", "gupy", side_effects={"buscar_vagas_gupy": gupy})
        fakes["buscar_vagas_gupy"].assert_called_once()
        self.assertEqual([name for name, fake in fakes.items() if fake.called], ["buscar_vagas_gupy"])
        send.assert_not_called()
        telegram.assert_not_called()
        self.assertEqual(self.rows("SELECT fonte, texto FROM avisos_pendentes ORDER BY rowid"),
                         [("GUPY", "antigo"), ("GUPY", "falhou")])

    def test_failed_saves_an_interrupted_alert(self):
        fakes, send, _ = self.run_main("--failed", "linkedin_posts")
        self.assertFalse(any(fake.called for fake in fakes.values()))
        send.assert_not_called()
        self.assertIn(("LINKEDIN PUBLICAÇÕES", main.AVISO_FONTE_INTERROMPIDA),
                      self.rows("SELECT fonte, texto FROM avisos_pendentes"))

    def test_merge_alerts_sources_without_a_copy(self):
        self.copy_of_base("gupy", ["INSERT INTO vagas_enviadas VALUES ('https://b', '02/10', 'B')"])
        _, send, _ = self.run_main("--merge", os.path.join(self.dir.name, "copias"))
        send.assert_not_called()
        self.assertIn(("https://b",), self.rows("SELECT link FROM vagas_enviadas"))
        missing = [fonte for fonte, texto in self.rows("SELECT fonte, texto FROM avisos_pendentes")
                   if texto == main.AVISO_FONTE_SEM_BANCO]
        self.assertEqual(sorted(missing),
                         sorted(label for source, (_, label) in main.SOURCES.items()
                                if source != "gupy"))

    def test_send_alerts_only_sends(self):
        fakes, send, _ = self.run_main("--send-alerts")
        self.assertFalse(any(fake.called for fake in fakes.values()))
        send.assert_called_once()

    def test_no_arguments_runs_every_source_in_order_and_sends(self):
        calls = []
        names = [name for name, _ in main.SOURCES.values()]
        effects = {name: (lambda conn, cursor, name=name: calls.append(name)) for name in names}
        _, send, _ = self.run_main(side_effects=effects)
        self.assertEqual(calls, names)
        self.assertEqual(calls[-1], "buscar_vagas_web")
        send.assert_called_once()


QUEUE_TABLE = ("CREATE TABLE vagas_pendentes (link TEXT PRIMARY KEY, titulo TEXT, "
               "empresa TEXT, data_publicacao TEXT, mensagem TEXT, fonte TEXT)")


def queued(link, title, company="Acme", source="GUPY"):
    return (f"INSERT INTO vagas_pendentes VALUES ('{link}', '{title}', '{company}', "
            f"'02/10', 'msg {link}', '{source}')")


class JobQueueTest(MainModesTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(main.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_source_queues_jobs_without_sending(self):
        def gupy(conn, cursor):
            main.registrar_e_enviar(conn, cursor, "https://b", "Dev Flutter", "Acme", "02/10", "msg", "GUPY")
        _, _, telegram = self.run_main("--source", "gupy", side_effects={"buscar_vagas_gupy": gupy})
        telegram.assert_not_called()
        self.assertEqual(self.rows("SELECT link, mensagem FROM vagas_pendentes"), [("https://b", "msg")])
        self.assertNotIn(("https://b",), self.rows("SELECT link FROM vagas_enviadas"))

    def test_merge_brings_the_queued_jobs_of_every_copy_dedicated_sites_first(self):
        self.write(self.base, [QUEUE_TABLE, queued("https://old", "Antiga")])
        self.copy_of_base("web", [queued("https://c", "C web", source="WEB"),
                                  queued("https://w", "W", source="WEB")])
        self.copy_of_base("gupy", [queued("https://b", "B")])
        self.copy_of_base("remotar", [queued("https://c", "C", source="REMOTAR")])
        self.run_main("--merge", os.path.join(self.dir.name, "copias"))
        self.assertEqual(self.rows("SELECT link, fonte FROM vagas_pendentes ORDER BY rowid"),
                         [("https://old", "GUPY"), ("https://b", "GUPY"), ("https://c", "REMOTAR"),
                          ("https://w", "WEB")])

    def test_send_jobs_sends_each_job_once_in_queue_order(self):
        self.write(self.base, [
            QUEUE_TABLE,
            queued("https://gupy/1", "Dev Flutter"),
            queued("https://linkedin/1", "Dev Flutter", source="LINKEDIN"),  # same job, other site
            queued("https://remotar/2", "Dev Mobile", source="REMOTAR"),
        ])
        _, send, telegram = self.run_main("--send-jobs")
        self.assertEqual([c.args[0] for c in telegram.call_args_list],
                         ["msg https://gupy/1", "msg https://remotar/2"])
        self.assertEqual(self.rows("SELECT link FROM vagas_pendentes"), [])
        self.assertLessEqual({("https://gupy/1",), ("https://linkedin/1",), ("https://remotar/2",)},
                             set(self.rows("SELECT link FROM vagas_enviadas")))
        send.assert_not_called()

    def test_send_jobs_skips_a_job_already_sent(self):
        self.write(self.base, [QUEUE_TABLE, queued("https://a", "A")])
        _, _, telegram = self.run_main("--send-jobs")
        telegram.assert_not_called()
        self.assertEqual(self.rows("SELECT link FROM vagas_pendentes"), [])

    def test_send_jobs_without_a_queue_does_nothing(self):
        _, _, telegram = self.run_main("--send-jobs")
        telegram.assert_not_called()

    def test_refused_job_stays_queued_and_is_alerted(self):
        self.write(self.base, [QUEUE_TABLE, queued("https://b", "Dev Flutter")])
        conn = sqlite3.connect(self.base)
        with mock.patch.multiple(main, TOKEN="t", CHAT_ID="c"), \
             mock.patch.object(main, "iniciar_banco", return_value=(conn, conn.cursor())), \
             mock.patch.object(main, "enviar_telegram", return_value=False):
            main.main(["--send-jobs"])
        self.assertEqual(self.rows("SELECT link FROM vagas_pendentes"), [("https://b",)])
        self.assertNotIn(("https://b",), self.rows("SELECT link FROM vagas_enviadas"))
        self.assertIn("TELEGRAM", [fonte for fonte, _ in self.rows("SELECT fonte, texto FROM avisos_pendentes")])

    def test_same_link_found_twice_in_a_job_is_queued_once(self):
        def gupy(conn, cursor):
            for _ in range(2):
                main.registrar_e_enviar(conn, cursor, "https://b", "Dev Flutter", "Acme", "02/10", "msg", "GUPY")
        self.run_main("--source", "gupy", side_effects={"buscar_vagas_gupy": gupy})
        self.assertEqual(self.rows("SELECT link FROM vagas_pendentes"), [("https://b",)])

    def test_refused_job_does_not_stop_the_next_ones(self):
        self.write(self.base, [QUEUE_TABLE, queued("https://b", "B"), queued("https://c", "C")])
        conn = sqlite3.connect(self.base)
        with mock.patch.multiple(main, TOKEN="t", CHAT_ID="c"), \
             mock.patch.object(main, "iniciar_banco", return_value=(conn, conn.cursor())), \
             mock.patch.object(main, "enviar_telegram", side_effect=[False, True]) as telegram:
            main.main(["--send-jobs"])
        self.assertEqual(telegram.call_count, 2)
        self.assertEqual(self.rows("SELECT link FROM vagas_pendentes"), [("https://b",)])
        self.assertIn(("https://c",), self.rows("SELECT link FROM vagas_enviadas"))

    def test_run_after_a_source_run_sends_directly_again(self):
        self.run_main("--source", "gupy")
        def gupy(conn, cursor):
            main.registrar_e_enviar(conn, cursor, "https://b", "Dev Flutter", "Acme", "02/10", "msg", "GUPY")
        _, _, telegram = self.run_main(side_effects={"buscar_vagas_gupy": gupy})
        telegram.assert_called_once_with("msg")
        self.assertIn(("https://b",), self.rows("SELECT link FROM vagas_enviadas"))

    def test_full_run_also_sends_what_was_left_queued(self):
        self.write(self.base, [QUEUE_TABLE, queued("https://b", "B")])
        _, _, telegram = self.run_main()
        telegram.assert_called_once_with("msg https://b")
        self.assertEqual(self.rows("SELECT link FROM vagas_pendentes"), [])


if __name__ == "__main__":
    unittest.main()
