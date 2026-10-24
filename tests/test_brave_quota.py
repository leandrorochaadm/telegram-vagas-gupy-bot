"""Unit tests for the Brave quota throttle (brave_due) in main.py.

Run: python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main  # noqa: E402
from fake_store import FakeStore  # noqa: E402
from store import Source, StoreError  # noqa: E402

SQLITE_REMOVED = "SQLite was replaced by the PocketBase store: the *StoreTest classes cover this"

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


@unittest.skip(SQLITE_REMOVED)
class BraveDueTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()

    def tearDown(self):
        self.conn.close()

    def due(self, source="WEB", interval=60, now=NOW):
        return main.brave_due(self.conn, self.cursor, source, interval, now=now)

    def test_first_search_is_due(self):
        self.assertTrue(self.due())

    def test_skips_inside_interval(self):
        self.due()
        self.assertFalse(self.due(now=NOW + timedelta(minutes=30)))

    def test_due_after_interval(self):
        self.due()
        self.assertTrue(self.due(now=NOW + timedelta(minutes=60)))

    def test_slack_covers_trigger_arriving_early(self):
        self.due()
        self.assertTrue(self.due(now=NOW + timedelta(minutes=59, seconds=50)))

    def test_skip_keeps_last_search_time(self):
        self.due()
        self.due(now=NOW + timedelta(minutes=30))
        # Had the skip saved 12h30, 13h00 would still be inside the interval
        self.assertTrue(self.due(now=NOW + timedelta(minutes=60)))

    def test_sources_are_independent(self):
        self.due(source="WEB", interval=180)
        self.assertTrue(self.due(source="LINKEDIN PUBLICAÇÕES", now=NOW + timedelta(minutes=5)))
        self.assertFalse(self.due(source="WEB", interval=180, now=NOW + timedelta(minutes=60)))


@unittest.skip(SQLITE_REMOVED)
class BuscarVagasWebTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        patcher = mock.patch.object(main.web_search, "search_jobs")
        self.search_jobs = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.conn.close)

    def test_second_run_inside_interval_skips_search(self):
        with mock.patch.object(main, "BRAVE_API_KEY", "key"):
            main.buscar_vagas_web(self.conn, self.cursor)
            main.buscar_vagas_web(self.conn, self.cursor)
        self.assertEqual(self.search_jobs.call_count, 1)

    def test_without_key_does_not_spend_the_slot(self):
        with mock.patch.object(main, "BRAVE_API_KEY", None):
            main.buscar_vagas_web(self.conn, self.cursor)
        self.assertTrue(main.brave_due(self.conn, self.cursor, "WEB", main.INTERVALO_WEB_MIN))

    def test_db_error_becomes_alert_instead_of_crashing(self):
        self.conn.close()
        with mock.patch.object(main, "BRAVE_API_KEY", "key"), \
             mock.patch.object(main, "anotar_avisos") as anotar:
            main.buscar_vagas_web(self.conn, self.cursor)
        self.search_jobs.assert_not_called()
        source, errors = anotar.call_args.args
        self.assertEqual(source, "WEB")
        self.assertEqual(len(errors), 1)


@unittest.skip(SQLITE_REMOVED)
class BuscarPostsLinkedinTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        patcher = mock.patch.object(main.linkedin_posts, "search_posts", return_value=[])
        self.search_posts = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.conn.close)

    def test_second_run_inside_interval_skips_search(self):
        with mock.patch.object(main, "BRAVE_API_KEY", "key"), mock.patch.object(main, "LINKEDIN_LI_AT", None):
            main.buscar_posts_linkedin(self.conn, self.cursor)
            main.buscar_posts_linkedin(self.conn, self.cursor)
        self.assertEqual(self.search_posts.call_count, len(main.FILTROS_POSTS_LINKEDIN))


class BraveDueStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeStore()

    def due(self, source=Source.WEB, interval=60, now=NOW):
        return main.brave_due(self.db, source, interval, now=now)

    def test_first_search_is_due(self):
        self.assertTrue(self.due())

    def test_skips_inside_interval(self):
        self.due()
        self.assertFalse(self.due(now=NOW + timedelta(minutes=30)))

    def test_due_after_interval(self):
        self.due()
        self.assertTrue(self.due(now=NOW + timedelta(minutes=60)))

    def test_slack_covers_trigger_arriving_early(self):
        self.due()
        self.assertTrue(self.due(now=NOW + timedelta(minutes=59, seconds=50)))

    def test_skip_keeps_last_search_time(self):
        self.due()
        self.due(now=NOW + timedelta(minutes=30))
        # Had the skip saved 12h30, 13h00 would still be inside the interval
        self.assertTrue(self.due(now=NOW + timedelta(minutes=60)))

    def test_sources_are_independent(self):
        self.due(source=Source.WEB, interval=180)
        self.assertTrue(self.due(source=Source.LINKEDIN_POSTS, now=NOW + timedelta(minutes=5)))
        self.assertFalse(self.due(source=Source.WEB, interval=180, now=NOW + timedelta(minutes=60)))

    def test_saves_under_the_source_key(self):
        self.due()
        self.assertEqual(self.db.brave_searched_at("web"), NOW)
        self.assertIsNone(self.db.brave_searched_at("linkedin_posts"))

    def test_naive_time_is_refused(self):
        with self.assertRaises(ValueError):
            self.due(now=NOW.replace(tzinfo=None))


class BuscarVagasWebStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeStore()
        patcher = mock.patch.object(main.web_search, "search_jobs")
        self.search_jobs = patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_run_inside_interval_skips_search(self):
        with mock.patch.object(main, "BRAVE_API_KEY", "key"):
            main.buscar_vagas_web(self.db)
            main.buscar_vagas_web(self.db)
        self.assertEqual(self.search_jobs.call_count, 1)

    def test_without_key_does_not_spend_the_slot(self):
        with mock.patch.object(main, "BRAVE_API_KEY", None):
            main.buscar_vagas_web(self.db)
        self.assertTrue(main.brave_due(self.db, Source.WEB, main.INTERVALO_WEB_MIN))

    def test_db_error_becomes_alert_instead_of_crashing(self):
        self.db = FakeStore(fail_with=StoreError())
        with mock.patch.object(main, "BRAVE_API_KEY", "key"), \
             mock.patch.object(main, "anotar_avisos") as anotar:
            main.buscar_vagas_web(self.db)
        self.search_jobs.assert_not_called()
        source, errors = anotar.call_args.args
        self.assertEqual(source, Source.WEB)
        self.assertEqual(len(errors), 1)


class BuscarPostsLinkedinStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeStore()
        patcher = mock.patch.object(main.linkedin_posts, "search_posts", return_value=[])
        self.search_posts = patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_run_inside_interval_skips_search(self):
        with mock.patch.object(main, "BRAVE_API_KEY", "key"), mock.patch.object(main, "LINKEDIN_LI_AT", None):
            main.buscar_posts_linkedin(self.db)
            main.buscar_posts_linkedin(self.db)
        self.assertEqual(self.search_posts.call_count, len(main.FILTROS_POSTS_LINKEDIN))


if __name__ == "__main__":
    unittest.main()
