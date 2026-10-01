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

import main  # noqa: E402

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


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


if __name__ == "__main__":
    unittest.main()
