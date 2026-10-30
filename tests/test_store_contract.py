"""Contract tests: FakeStore and the real Store must behave the same.

The same cases run on both. The real one needs a throwaway PocketBase
(POCKETBASE_BIN) and is skipped without it.

Run: python -m unittest discover -s tests -v
"""
import os
import secrets
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_store import FakeStore  # noqa: E402
from pocketbase_server import PocketBaseServer, pocketbase_bin  # noqa: E402
from store import (  # noqa: E402
    Collection, DescriptionStatus, JobDescription, SentJob, Source, Store, StoreError,
    StoreFailure, _parse_date,
)

BOT_EMAIL = "bot@test.local"
BRASILIA = timezone(timedelta(hours=-3))
DATA_COLLECTIONS = [c for c in Collection if c is not Collection.BOT_ACCOUNTS]
MISSING_ID = "zzzzzzzzzzzzzzz"
PAGE = 200


def clear_data_collections(server: PocketBaseServer) -> None:
    for collection in DATA_COLLECTIONS:
        path = f"/api/collections/{collection}/records"
        while True:
            response = server.request("GET", path, token=server.superuser_token,
                                      params={"perPage": PAGE, "fields": "id"})
            assert response.status_code == 200, f"{collection}: {response.status_code}"
            items = response.json()["items"]
            if not items:
                break
            for item in items:
                deleted = server.request("DELETE", f"{path}/{item['id']}",
                                         token=server.superuser_token)
                assert deleted.status_code == 204, f"{collection}: {deleted.status_code}"


def utc_ms(at: datetime) -> datetime:
    """Same instant in UTC, truncated to milliseconds."""
    utc = at.astimezone(timezone.utc)
    return utc.replace(microsecond=utc.microsecond // 1000 * 1000)


class StoreContractCases:
    def make_store(self) -> "Store | FakeStore":
        raise NotImplementedError

    def reopen(self) -> "Store | FakeStore":
        raise NotImplementedError

    def row_count(self, collection: Collection) -> int:
        raise NotImplementedError

    def pause(self) -> None:
        pass

    def description_row(self, link: str) -> dict:
        raise NotImplementedError

    def setUp(self) -> None:
        self.db = self.make_store()

    def queue(self, link: str, source: Source, title: str = "t") -> None:
        self.db.queue_job(link, title, "company", "2026-01-01", f"msg {link}", source)

    def test_is_sent_before_and_after_mark_sent(self) -> None:
        self.assertFalse(self.db.is_sent("https://a"))
        self.db.mark_sent("https://a", "2026-01-01", "A")
        self.assertTrue(self.db.is_sent("https://a"))
        self.assertFalse(self.db.is_sent("https://b"))

    def test_mark_sent_visible_after_reopen(self) -> None:
        self.db.mark_sent("https://a", "2026-01-01", "A")
        self.assertTrue(self.reopen().is_sent("https://a"))

    def test_mark_sent_twice_keeps_one_row(self) -> None:
        self.db.mark_sent("https://a", "2026-01-01", "A")
        self.db.mark_sent("https://a", "2026-01-02", "A2")
        self.assertEqual(self.row_count(Collection.SENT_JOBS), 1)

    def test_queued_jobs_web_last_then_created(self) -> None:
        for link, source in (("A", Source.WEB), ("B", Source.GUPY),
                             ("C", Source.LINKEDIN), ("D", Source.WEB)):
            self.queue(link, source, title=f"title {link}")
            self.pause()
        jobs = self.db.queued_jobs()
        self.assertEqual([job.link for job in jobs], ["B", "C", "A", "D"])
        first = jobs[0]
        self.assertEqual(
            (first.title, first.company, first.published, first.message, first.source),
            ("title B", "company", "2026-01-01", "msg B", Source.GUPY))
        self.assertEqual([job.source for job in jobs],
                         [Source.GUPY, Source.LINKEDIN, Source.WEB, Source.WEB])

    def test_same_link_two_sources_and_duplicate(self) -> None:
        self.queue("L", Source.WEB)
        self.pause()
        self.queue("L", Source.GUPY)
        jobs = self.db.queued_jobs()
        self.assertEqual([job.source for job in jobs], [Source.GUPY, Source.WEB])
        self.queue("L", Source.GUPY)
        self.assertEqual(len(self.db.queued_jobs()), 2)

    def test_delete_queued_only_given_and_missing_ignored(self) -> None:
        self.queue("A", Source.GUPY)
        self.pause()
        self.queue("B", Source.GUPY)
        first, second = self.db.queued_jobs()
        self.db.delete_queued(first.id)
        self.assertEqual([job.id for job in self.db.queued_jobs()], [second.id])
        self.db.delete_queued(MISSING_ID)
        self.assertEqual(len(self.db.queued_jobs()), 1)

    def test_delete_alerts_only_read_ids(self) -> None:
        self.db.add_alerts([("GUPY", "one"), ("WEB", "two")])
        self.pause()
        read = self.db.pending_alerts()
        self.assertEqual([(a.label, a.text) for a in read], [("GUPY", "one"), ("WEB", "two")])
        self.db.add_alerts([("BOT", "three")])
        self.db.delete_alerts([alert.id for alert in read])
        left = self.db.pending_alerts()
        self.assertEqual([(a.label, a.text) for a in left], [("BOT", "three")])

    def test_empty_lists_are_noops(self) -> None:
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.db.add_alerts([])
        self.db.delete_alerts([])
        self.db.add_inhire_tenants([], when)
        self.assertEqual(self.db.pending_alerts(), [])
        self.assertEqual(self.db.inhire_tenants(), [])

    def test_last_alert_at_single_row(self) -> None:
        first = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        second = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
        self.assertIsNone(self.db.last_alert_at())
        self.db.set_last_alert_at(first)
        self.db.set_last_alert_at(second)
        self.assertEqual(self.db.last_alert_at(), second)
        self.assertEqual(self.row_count(Collection.ALERT_SENDS), 1)

    def test_brave_upsert_per_label(self) -> None:
        first = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        second = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
        self.assertIsNone(self.db.brave_searched_at("web"))
        self.db.set_brave_searched_at("web", first)
        self.db.set_brave_searched_at("web", second)
        self.db.set_brave_searched_at("linkedin_posts", first)
        self.assertEqual(self.db.brave_searched_at("web"), second)
        self.assertEqual(self.db.brave_searched_at("linkedin_posts"), first)
        self.assertEqual(self.row_count(Collection.BRAVE_SEARCHES), 2)

    def test_filter_value_with_quote_and_backslash(self) -> None:
        when = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        label = 'a"b\\c'
        self.db.set_brave_searched_at(label, when)
        self.assertEqual(self.db.brave_searched_at(label), when)
        self.assertIsNone(self.db.brave_searched_at('a"b'))

    def test_save_linkedin_session_partial(self) -> None:
        first = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        second = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
        self.assertIsNone(self.db.linkedin_session("h"))
        self.db.save_linkedin_session("h", searched_at=first)
        session = self.db.linkedin_session("h")
        self.assertEqual((session.searched_at, session.expired_at, session.reminded_at),
                         (first, None, None))
        self.db.save_linkedin_session("h", expired_at=second)
        session = self.db.linkedin_session("h")
        self.assertEqual((session.searched_at, session.expired_at, session.reminded_at),
                         (first, second, None))
        self.db.save_linkedin_session("h", expired_at=None)
        session = self.db.linkedin_session("h")
        self.assertEqual((session.searched_at, session.expired_at, session.reminded_at),
                         (first, None, None))
        self.assertEqual(self.row_count(Collection.LINKEDIN_SESSIONS), 1)

    def test_save_linkedin_session_rejects_unknown_field(self) -> None:
        with self.assertRaises(ValueError):
            self.db.save_linkedin_session("h", other_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(self.row_count(Collection.LINKEDIN_SESSIONS), 0)

    def test_delete_linkedin_sessions_except(self) -> None:
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for cookie_hash in ("h1", "h2", "h3"):
            self.db.save_linkedin_session(cookie_hash, searched_at=when)
        self.db.delete_linkedin_sessions_except("h2")
        self.assertIsNone(self.db.linkedin_session("h1"))
        self.assertIsNotNone(self.db.linkedin_session("h2"))
        self.assertIsNone(self.db.linkedin_session("h3"))
        self.assertEqual(self.row_count(Collection.LINKEDIN_SESSIONS), 1)

    def test_inhire_tenants_sorted_dedup_remove(self) -> None:
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.db.add_inhire_tenants(["zeta", "alfa"], when)
        self.db.add_inhire_tenants(["alfa"], when)
        self.assertEqual(self.db.inhire_tenants(), ["alfa", "zeta"])
        self.db.remove_inhire_tenant("zeta")
        self.assertEqual(self.db.inhire_tenants(), ["alfa"])
        self.db.remove_inhire_tenant("ausente")
        self.assertEqual(self.db.inhire_tenants(), ["alfa"])

    def test_inhire_discovery_single_row_and_zero(self) -> None:
        first = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        second = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
        self.assertIsNone(self.db.inhire_discovery())
        self.db.set_inhire_discovery(first, 5)
        self.db.set_inhire_discovery(second, 0)
        self.assertEqual(self.db.inhire_discovery(), (second, 0))
        self.assertEqual(self.row_count(Collection.INHIRE_DISCOVERIES), 1)

    def test_dates_round_trip_in_utc(self) -> None:
        local = datetime(2026, 3, 4, 12, 0, 0, 123456, tzinfo=BRASILIA)
        expected = datetime(2026, 3, 4, 15, 0, 0, 123000, tzinfo=timezone.utc)
        self.assertEqual(utc_ms(local), expected)
        self.db.set_brave_searched_at("web", local)
        self.db.save_linkedin_session("h", searched_at=local)
        brave = self.db.brave_searched_at("web")
        searched = self.db.linkedin_session("h").searched_at
        for value in (brave, searched):
            self.assertEqual(value, expected)
            self.assertEqual(value.tzinfo, timezone.utc)

    def test_naive_datetime_raises_value_error(self) -> None:
        naive = datetime(2026, 1, 1, 10)
        calls = {
            "set_last_alert_at": lambda: self.db.set_last_alert_at(naive),
            "set_brave_searched_at": lambda: self.db.set_brave_searched_at("web", naive),
            "add_inhire_tenants": lambda: self.db.add_inhire_tenants(["alfa"], naive),
            "set_inhire_discovery": lambda: self.db.set_inhire_discovery(naive, 1),
            "save_linkedin_session": lambda: self.db.save_linkedin_session("h", searched_at=naive),
        }
        for name, call in calls.items():
            with self.subTest(method=name):
                with self.assertRaises(ValueError):
                    call()
        for collection in (Collection.ALERT_SENDS, Collection.BRAVE_SEARCHES,
                           Collection.INHIRE_TENANTS, Collection.INHIRE_DISCOVERIES,
                           Collection.LINKEDIN_SESSIONS):
            self.assertEqual(self.row_count(collection), 0, collection)

    # -- job_descriptions ----------------------------------------------

    def add_description(self, link: str, description: str | None = None,
                        title: str = "t") -> None:
        self.db.add_job_description(link, title, "company", Source.GUPY, "2026-01-01",
                                    description)

    def description_id(self, link: str) -> str:
        for job in self.db.pending_descriptions():
            if job.link == link:
                return job.id
        raise AssertionError(f"{link} is not pending")

    def test_add_job_description_twice_keeps_one_row(self) -> None:
        self.add_description("A", title="first")
        self.add_description("A", description="text", title="second")
        self.assertEqual(self.row_count(Collection.JOB_DESCRIPTIONS), 1)
        self.assertEqual(self.description_row("A")["status"], DescriptionStatus.PENDING)
        self.assertEqual([job.title for job in self.db.pending_descriptions()], ["first"])

    def test_add_job_description_pending_fields(self) -> None:
        self.add_description("A")
        (job,) = self.db.pending_descriptions()
        self.assertIsInstance(job, JobDescription)
        self.assertEqual((job.link, job.source, job.title, job.company, job.published),
                         ("A", Source.GUPY, "t", "company", "2026-01-01"))
        self.assertEqual(job.attempts, 0)
        self.assertIsNone(job.first_try_at)
        self.assertEqual(job.created.utcoffset(), timedelta(0))

    def test_add_job_description_with_text_is_saved(self) -> None:
        self.add_description("A", description="texto")
        row = self.description_row("A")
        self.assertEqual((row["status"], row["description"]), (DescriptionStatus.SAVED, "texto"))
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(self.db.pending_descriptions(), [])

    def test_pending_descriptions_order(self) -> None:
        self.assertEqual(self.db.pending_descriptions(), [])
        for link in "ABCDEFG":
            self.add_description(link)
            self.pause()
        t1 = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
        self.db.mark_description_try(self.description_id("B"), 1, t2)
        self.db.mark_description_try(self.description_id("D"), 1, t1)
        self.db.save_description(self.description_id("C"), "x", "senior", "clt", t1)
        self.db.finish_description(self.description_id("E"), DescriptionStatus.CLOSED, t1)
        self.db.finish_description(self.description_id("F"), DescriptionStatus.GAVE_UP, t1)
        self.assertEqual([job.link for job in self.db.pending_descriptions()],
                         ["D", "B", "G", "A"])

    def test_save_description_sets_saved_and_finished_at(self) -> None:
        self.add_description("A")
        at = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
        self.db.save_description(self.description_id("A"), "texto", "pleno", "clt", at)
        row = self.description_row("A")
        self.assertEqual(
            (row["description"], row["seniority"], row["employment_type"], row["status"],
             row["finished_at"]),
            ("texto", "pleno", "clt", DescriptionStatus.SAVED, at))

    def test_mark_description_try_keeps_first_try_at(self) -> None:
        self.add_description("A")
        record_id = self.description_id("A")
        t1 = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
        self.db.mark_description_try(record_id, 1, t1)
        self.db.mark_description_try(record_id, 2, t2)
        row = self.description_row("A")
        self.assertEqual((row["attempts"], row["first_try_at"], row["last_try_at"], row["status"]),
                         (2, t1, t2, DescriptionStatus.PENDING))

    def test_finish_description_closed_and_gave_up(self) -> None:
        at = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
        for link, status in (("A", DescriptionStatus.CLOSED), ("B", DescriptionStatus.GAVE_UP)):
            self.add_description(link)
            self.db.finish_description(self.description_id(link), status, at)
            row = self.description_row(link)
            self.assertEqual((row["status"], row["finished_at"]), (status, at))

    def test_finish_description_rejects_other_status(self) -> None:
        self.add_description("A")
        record_id = self.description_id("A")
        at = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
        for status in (DescriptionStatus.PENDING, DescriptionStatus.SAVED,
                       DescriptionStatus.UNKNOWN):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    self.db.finish_description(record_id, status, at)
        row = self.description_row("A")
        self.assertEqual(row["status"], DescriptionStatus.PENDING)
        self.assertIsNone(row["finished_at"])

    def test_description_update_missing_id_is_rejected(self) -> None:
        at = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
        calls = {
            "save_description": lambda: self.db.save_description(MISSING_ID, "x", "s", "t", at),
            "mark_description_try": lambda: self.db.mark_description_try(MISSING_ID, 1, at),
            "finish_description": lambda: self.db.finish_description(
                MISSING_ID, DescriptionStatus.CLOSED, at),
        }
        for name, call in calls.items():
            with self.subTest(method=name):
                with self.assertRaises(StoreError) as caught:
                    call()
                self.assertIs(caught.exception.kind, StoreFailure.REJECTED)

    def test_description_links_includes_every_status(self) -> None:
        self.assertEqual(self.db.description_links(), set())
        self.add_description("A")
        self.add_description("B", description="texto")
        self.assertEqual(self.db.description_links(), {"A", "B"})

    def test_sent_jobs_lists_link_published_title(self) -> None:
        self.assertEqual(self.db.sent_jobs(), [])
        self.db.mark_sent("https://a", "2026-01-01", "A")
        self.db.mark_sent("https://b", "2026-01-02", "B")
        self.assertEqual(
            sorted(self.db.sent_jobs(), key=lambda job: job.link),
            [SentJob("https://a", "2026-01-01", "A"), SentJob("https://b", "2026-01-02", "B")])

    def test_description_dates_round_trip_in_utc(self) -> None:
        self.add_description("A")
        record_id = self.description_id("A")
        local = datetime(2026, 3, 4, 12, 0, 0, 123456, tzinfo=BRASILIA)
        self.db.mark_description_try(record_id, 1, local)
        self.db.save_description(record_id, "x", "s", "t", local)
        row = self.description_row("A")
        for value in (row["first_try_at"], row["last_try_at"], row["finished_at"]):
            self.assertEqual(value, utc_ms(local))
            self.assertEqual(value.tzinfo, timezone.utc)

    def test_naive_datetime_in_descriptions_raises_value_error(self) -> None:
        self.add_description("A")
        record_id = self.description_id("A")
        naive = datetime(2026, 1, 1, 10)
        calls = {
            "save_description": lambda: self.db.save_description(record_id, "x", "s", "t", naive),
            "mark_description_try": lambda: self.db.mark_description_try(record_id, 1, naive),
            "finish_description": lambda: self.db.finish_description(
                record_id, DescriptionStatus.CLOSED, naive),
        }
        for name, call in calls.items():
            with self.subTest(method=name):
                with self.assertRaises(ValueError):
                    call()
        row = self.description_row("A")
        self.assertEqual((row["status"], row["attempts"], row["description"]),
                         (DescriptionStatus.PENDING, 0, ""))
        self.assertIsNone(row["finished_at"])


class FakeStoreContractTest(StoreContractCases, unittest.TestCase):
    def make_store(self) -> FakeStore:
        return FakeStore()

    def reopen(self) -> FakeStore:
        return self.db

    def row_count(self, collection: Collection) -> int:
        return self.db.count(collection)

    def description_row(self, link: str) -> dict:
        row = self.db.descriptions[link]
        return {"status": row.status, "description": row.description,
                "seniority": row.seniority, "employment_type": row.employment_type,
                "attempts": row.attempts, "first_try_at": row.first_try_at,
                "last_try_at": row.last_try_at, "finished_at": row.finished_at}


@unittest.skipUnless(pocketbase_bin(), "POCKETBASE_BIN not set")
class RealStoreContractTest(StoreContractCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = PocketBaseServer(pocketbase_bin())
        cls.server.start()
        cls.addClassCleanup(cls.server.stop)
        cls.password = secrets.token_urlsafe(24)
        cls.server.create_auth_account(Collection.BOT_ACCOUNTS, BOT_EMAIL, cls.password)

    def make_store(self) -> Store:
        clear_data_collections(self.server)
        return self.reopen()

    def reopen(self) -> Store:
        store = Store(self.server.url, BOT_EMAIL, self.password)
        store.connect()
        return store

    def row_count(self, collection: Collection) -> int:
        return self.server.count(collection)

    def pause(self) -> None:
        time.sleep(0.01)

    def description_row(self, link: str) -> dict:
        response = self.server.request(
            "GET", "/api/collections/job_descriptions/records",
            token=self.server.superuser_token, params={"filter": f'link = "{link}"'})
        row = response.json()["items"][0]
        return {"status": DescriptionStatus.from_api(row["status"]),
                "description": row["description"], "seniority": row["seniority"],
                "employment_type": row["employment_type"], "attempts": int(row["attempts"] or 0),
                "first_try_at": _parse_date(row["first_try_at"]),
                "last_try_at": _parse_date(row["last_try_at"]),
                "finished_at": _parse_date(row["finished_at"])}


class FakeStoreOnlyTest(unittest.TestCase):
    def test_fail_with_raises_same_instance_and_keeps_state(self) -> None:
        error = StoreError("o banco de vagas não respondeu")
        fake = FakeStore(fail_with=error)
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        calls = {
            "connect": fake.connect,
            "is_sent": lambda: fake.is_sent("x"),
            "mark_sent": lambda: fake.mark_sent("x", "p", "t"),
            "queued_jobs": fake.queued_jobs,
            "pending_alerts": fake.pending_alerts,
            "set_last_alert_at": lambda: fake.set_last_alert_at(when),
        }
        for name, call in calls.items():
            with self.subTest(method=name):
                with self.assertRaises(StoreError) as caught:
                    call()
                self.assertIs(caught.exception, error)
        self.assertFalse(fake.connected)
        self.assertEqual(fake.sent, {})
        self.assertEqual(fake.count(Collection.ALERT_SENDS), 0)

        fake.fail_with = None
        fake.connect()
        fake.mark_sent("x", "p", "t")
        self.assertTrue(fake.connected)
        self.assertTrue(fake.is_sent("x"))

    def test_fail_with_covers_description_methods(self) -> None:
        error = StoreError("o banco de vagas não respondeu")
        fake = FakeStore(fail_with=error)
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        calls = {
            "add_job_description": lambda: fake.add_job_description(
                "x", "t", "c", Source.GUPY, "p"),
            "pending_descriptions": fake.pending_descriptions,
            "save_description": lambda: fake.save_description("i", "x", "s", "t", when),
            "mark_description_try": lambda: fake.mark_description_try("i", 1, when),
            "finish_description": lambda: fake.finish_description(
                "i", DescriptionStatus.CLOSED, when),
            "description_links": fake.description_links,
            "sent_jobs": fake.sent_jobs,
        }
        for name, call in calls.items():
            with self.subTest(method=name):
                with self.assertRaises(StoreError) as caught:
                    call()
                self.assertIs(caught.exception, error)
        self.assertEqual(fake.count(Collection.JOB_DESCRIPTIONS), 0)

    def test_count_bot_accounts_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            FakeStore().count(Collection.BOT_ACCOUNTS)

    def test_sent_shows_published_and_title(self) -> None:
        fake = FakeStore()
        fake.mark_sent("https://a", "2026-01-01", "A")
        fake.mark_sent("https://a", "2026-02-02", "other")
        self.assertEqual(fake.sent, {"https://a": ("2026-01-01", "A")})


if __name__ == "__main__":
    unittest.main()
