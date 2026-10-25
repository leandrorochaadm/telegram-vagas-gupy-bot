"""Unit tests for the PocketBase client (no network: fake requests session).

Run: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store  # noqa: E402

BRASILIA = timezone(timedelta(hours=-3))


def response(status, body=None, *, json_error=False):
    resp = mock.Mock()
    resp.status_code = status
    if json_error:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = body if body is not None else {}
    return resp


def page(items, page=1, total_pages=1):
    return {"page": page, "perPage": 500, "totalItems": len(items),
            "totalPages": total_pages, "items": items}


def not_unique(field):
    return {"data": {field: {"code": "validation_not_unique", "message": "taken"}}}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock(spec=requests.Session)
        patcher = mock.patch("store.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        self.db = store.Store("http://pb.test/", "bot@test.local", "secret", session=self.session)

    def reply(self, *items):
        self.session.request.side_effect = list(items)

    def call(self, index=-1):
        return self.session.request.call_args_list[index]

    @property
    def calls(self):
        return self.session.request.call_args_list


class ConnectTest(StoreTestCase):
    def test_connect_sends_credentials_and_uses_token(self):
        self.reply(response(200, {"token": "tok", "record": {}}), response(200, page([])))
        self.db.connect()
        first = self.call(0)
        self.assertEqual(first.args, ("POST", "http://pb.test/api/collections/bot_accounts/auth-with-password"))
        self.assertEqual(first.kwargs["json"], {"identity": "bot@test.local", "password": "secret"})
        self.assertNotIn("Authorization", first.kwargs["headers"])
        self.db.is_sent("x")
        self.assertEqual(self.call(1).kwargs["headers"]["Authorization"], "tok")
        self.assertEqual(self.call(1).kwargs["timeout"], 15)

    def test_connect_400_raises_login_refused(self):
        self.reply(response(400, {"message": "Failed to authenticate."}))
        with self.assertRaises(store.StoreError) as ctx:
            self.db.connect()
        self.assertEqual(ctx.exception.kind, store.StoreFailure.LOGIN_REFUSED)
        self.assertEqual(str(ctx.exception), store.MSG_LOGIN_REFUSED)
        self.assertEqual(len(self.calls), 1)


class TransportTest(StoreTestCase):
    def test_401_and_403_are_not_retried(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.session.request.reset_mock()
                self.reply(response(status))
                with self.assertRaises(store.StoreError) as ctx:
                    self.db.is_sent("x")
                self.assertEqual(ctx.exception.kind, store.StoreFailure.LOGIN_REFUSED)
                self.assertEqual(len(self.calls), 1)

    def test_502_then_200_retries_once(self):
        self.reply(response(502), response(200, page([])))
        self.assertFalse(self.db.is_sent("x"))
        self.assertEqual(len(self.calls), 2)
        self.sleep.assert_called_once_with(2)

    def test_502_twice_raises_unavailable(self):
        self.reply(response(502), response(502))
        with self.assertRaises(store.StoreError) as ctx:
            self.db.is_sent("x")
        self.assertEqual(ctx.exception.kind, store.StoreFailure.UNAVAILABLE)
        self.assertEqual(str(ctx.exception), store.MSG_UNAVAILABLE)
        self.assertEqual(len(self.calls), 2)

    def test_timeout_then_ok_retries(self):
        self.reply(requests.Timeout(), response(200, page([])))
        self.assertFalse(self.db.is_sent("x"))
        self.assertEqual(len(self.calls), 2)

    def test_timeout_twice_raises_unavailable(self):
        self.reply(requests.Timeout(), requests.Timeout())
        with self.assertRaises(store.StoreError) as ctx:
            self.db.is_sent("x")
        self.assertEqual(ctx.exception.kind, store.StoreFailure.UNAVAILABLE)

    def test_connection_error_twice_raises_unavailable(self):
        self.reply(requests.ConnectionError(), requests.ConnectionError())
        with self.assertRaises(store.StoreError) as ctx:
            self.db.is_sent("x")
        self.assertEqual(ctx.exception.kind, store.StoreFailure.UNAVAILABLE)

    def test_other_4xx_raises_rejected(self):
        self.reply(response(400, {"data": {"link": {"code": "validation_required"}}}))
        with self.assertRaises(store.StoreError) as ctx:
            self.db.mark_sent("l", "p", "t")
        self.assertEqual(ctx.exception.kind, store.StoreFailure.REJECTED)
        self.assertEqual(str(ctx.exception), store.MSG_REJECTED)
        self.assertEqual(len(self.calls), 1)

    def test_not_unique_on_patch_raises_rejected(self):
        self.reply(response(400, not_unique("label")))
        with self.assertRaises(store.StoreError) as ctx:
            self.db._request("PATCH", "/api/collections/brave_searches/records/b1", json={})
        self.assertEqual(ctx.exception.kind, store.StoreFailure.REJECTED)

    def test_invalid_json_raises_unavailable(self):
        self.reply(response(200, json_error=True))
        with self.assertRaises(store.StoreError) as ctx:
            self.db.is_sent("x")
        self.assertEqual(ctx.exception.kind, store.StoreFailure.UNAVAILABLE)
        self.assertEqual(len(self.calls), 1)

    def test_error_log_does_not_leak_secrets(self):
        self.reply(response(502), response(502))
        with mock.patch("builtins.print") as printed:
            with self.assertRaises(store.StoreError):
                self.db.is_sent("x")
        output = " ".join(str(arg) for c in printed.call_args_list for arg in c.args)
        self.assertNotIn("secret", output)


class PaginationAndQuoteTest(StoreTestCase):
    def test_list_reads_every_page(self):
        self.reply(response(200, page([{"link": "a"}], 1, 3)),
                   response(200, page([{"link": "b"}], 2, 3)),
                   response(200, page([{"link": "c"}], 3, 3)))
        rows = self.db._list_all(store.Collection.SENT_JOBS)
        self.assertEqual([r["link"] for r in rows], ["a", "b", "c"])
        self.assertEqual([c.kwargs["params"]["page"] for c in self.calls], [1, 2, 3])
        self.assertTrue(all(c.kwargs["params"]["perPage"] == 500 for c in self.calls))

    def test_empty_collection_makes_one_request(self):
        self.reply(response(200, page([], 1, 0)))
        self.assertEqual(self.db._list_all(store.Collection.SENT_JOBS), [])
        self.assertEqual(len(self.calls), 1)

    def test_quote_escapes_quote_and_backslash(self):
        self.assertEqual(store._quote('a"b\\c'), '"a\\"b\\\\c"')

    def test_filter_uses_quoted_value(self):
        self.reply(response(200, page([])))
        self.db.brave_searched_at('we"b')
        self.assertEqual(self.call().kwargs["params"]["filter"], 'label = "we\\"b"')


class SentJobsTest(StoreTestCase):
    def test_is_sent_loads_links_once(self):
        self.reply(response(200, page([{"link": "a"}])))
        self.assertTrue(self.db.is_sent("a"))
        self.assertFalse(self.db.is_sent("b"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.call().kwargs["params"]["fields"], "link")

    def test_mark_sent_updates_cache(self):
        self.reply(response(200, page([])), response(200, {"id": "1"}))
        self.assertFalse(self.db.is_sent("a"))
        self.db.mark_sent("a", "2026-10-24", "T")
        self.assertTrue(self.db.is_sent("a"))
        self.assertEqual(self.call().kwargs["json"], {"link": "a", "published": "2026-10-24", "title": "T"})
        self.assertEqual(len(self.calls), 2)

    def test_mark_sent_duplicate_is_ignored(self):
        self.reply(response(200, page([])), response(400, not_unique("link")))
        self.assertFalse(self.db.is_sent("a"))
        self.db.mark_sent("a", "p", "t")
        self.assertTrue(self.db.is_sent("a"))


class QueueTest(StoreTestCase):
    def test_queue_job_sends_source_api_value(self):
        self.reply(response(200, {"id": "1"}))
        self.db.queue_job("l", "t", "c", "p", "m", store.Source.GUPY)
        self.assertEqual(self.call().kwargs["json"]["source"], "gupy")

    def test_queue_job_duplicate_is_ignored(self):
        for field in ("link", "source"):
            with self.subTest(field=field):
                self.reply(response(400, not_unique(field)))
                self.db.queue_job("l", "t", "c", "p", "m", store.Source.WEB)

    def row(self, record_id, link, source, created):
        return {"id": record_id, "link": link, "title": "t", "company": "c", "published": "p",
                "message": "m", "source": source, "created": created}

    def test_queued_jobs_puts_web_last_then_created(self):
        self.reply(response(200, page([
            self.row("1", "a", "web", "2026-10-24 10:00:00.000Z"),
            self.row("2", "b", "gupy", "2026-10-24 11:00:00.000Z"),
            self.row("3", "c", "web", "2026-10-24 09:00:00.000Z"),
            self.row("4", "d", "remotar", "2026-10-24 10:30:00.000Z"),
        ])))
        jobs = self.db.queued_jobs()
        self.assertEqual([j.id for j in jobs], ["4", "2", "3", "1"])
        self.assertEqual(jobs[1].source, store.Source.GUPY)

    def test_unknown_source_does_not_break_queue(self):
        self.reply(response(200, page([
            self.row("1", "a", "web", "2026-10-24 10:00:00.000Z"),
            self.row("2", "b", "GUPY", "2026-10-24 11:00:00.000Z"),
        ])))
        jobs = self.db.queued_jobs()
        self.assertEqual([j.id for j in jobs], ["2", "1"])
        self.assertEqual(jobs[0].source, store.Source.UNKNOWN)

    def test_delete_queued_404_is_ignored(self):
        self.reply(response(404))
        self.db.delete_queued("gone")
        self.assertEqual(self.call().args[0], "DELETE")


class AlertsTest(StoreTestCase):
    def test_empty_lists_make_no_request(self):
        self.db.add_alerts([])
        self.db.delete_alerts([])
        self.assertEqual(len(self.calls), 0)

    def test_add_alerts_posts_in_order(self):
        self.reply(response(200, {}), response(200, {}))
        self.db.add_alerts([("GUPY", "one"), ("WEB", "two")])
        self.assertEqual([c.kwargs["json"] for c in self.calls],
                         [{"label": "GUPY", "text": "one"}, {"label": "WEB", "text": "two"}])

    def test_pending_alerts_in_created_order(self):
        self.reply(response(200, page([
            {"id": "2", "label": "B", "text": "b", "created": "2026-10-24 11:00:00.000Z"},
            {"id": "1", "label": "A", "text": "a", "created": "2026-10-24 10:00:00.000Z"},
        ])))
        self.assertEqual([a.id for a in self.db.pending_alerts()], ["1", "2"])

    def test_delete_alerts_only_given_ids(self):
        self.reply(response(204), response(404))
        self.db.delete_alerts(["x", "y"])
        self.assertEqual([c.args for c in self.calls], [
            ("DELETE", "http://pb.test/api/collections/alerts/records/x"),
            ("DELETE", "http://pb.test/api/collections/alerts/records/y")])

    def test_last_alert_at_none_when_empty(self):
        self.reply(response(200, page([])))
        self.assertIsNone(self.db.last_alert_at())
        self.assertEqual(self.call().kwargs["params"]["sort"], "-sent_at")

    def test_set_last_alert_at_keeps_one_row(self):
        at = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        self.reply(response(200, page([])), response(200, {"id": "r1"}),
                   response(200, page([{"id": "r1"}])), response(200, {"id": "r1"}))
        self.db.set_last_alert_at(at)
        self.db.set_last_alert_at(at)
        self.assertEqual([c.args[0] for c in self.calls], ["GET", "POST", "GET", "PATCH"])
        self.assertTrue(self.call().args[1].endswith("/alert_sends/records/r1"))

    def test_set_last_alert_at_rejects_naive(self):
        with self.assertRaises(ValueError):
            self.db.set_last_alert_at(datetime(2026, 10, 24, 12, 0))
        self.assertEqual(len(self.calls), 0)


class UpsertTest(StoreTestCase):
    AT = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

    def test_brave_upsert_patches_existing(self):
        self.reply(response(200, page([{"id": "b1"}])), response(200, {}))
        self.db.set_brave_searched_at("WEB", self.AT)
        self.assertEqual(self.call().args[0], "PATCH")
        self.assertEqual(self.call().kwargs["json"], {"searched_at": "2026-10-24 12:00:00.000Z"})

    def test_brave_upsert_creates_missing(self):
        self.reply(response(200, page([])), response(200, {}))
        self.db.set_brave_searched_at("WEB", self.AT)
        self.assertEqual(self.call().args[0], "POST")
        self.assertEqual(self.call().kwargs["json"],
                         {"label": "WEB", "searched_at": "2026-10-24 12:00:00.000Z"})

    def test_upsert_not_unique_refetches_and_patches(self):
        self.reply(response(200, page([])), response(400, not_unique("label")),
                   response(200, page([{"id": "b1"}])), response(200, {}))
        self.db.set_brave_searched_at("WEB", self.AT)
        self.assertEqual([c.args[0] for c in self.calls], ["GET", "POST", "GET", "PATCH"])

    def test_upsert_not_unique_without_row_raises_rejected(self):
        self.reply(response(200, page([])), response(400, not_unique("label")), response(200, page([])))
        with self.assertRaises(store.StoreError) as ctx:
            self.db.set_brave_searched_at("WEB", self.AT)
        self.assertEqual(ctx.exception.kind, store.StoreFailure.REJECTED)

    def test_save_linkedin_session_sends_only_given_fields(self):
        self.reply(response(200, page([{"id": "s1"}])), response(200, {}))
        self.db.save_linkedin_session("h", searched_at=self.AT, expired_at=None)
        self.assertEqual(self.call().kwargs["json"],
                         {"searched_at": "2026-10-24 12:00:00.000Z", "expired_at": ""})

    def test_save_linkedin_session_rejects_unknown_field(self):
        with self.assertRaises(ValueError):
            self.db.save_linkedin_session("h", bogus=self.AT)
        self.assertEqual(len(self.calls), 0)

    def test_save_linkedin_session_without_fields_creates_row(self):
        self.reply(response(200, page([])), response(200, {}))
        self.db.save_linkedin_session("h")
        self.assertEqual(self.call().kwargs["json"], {"cookie_hash": "h"})

    def test_delete_sessions_except_current(self):
        self.reply(response(200, page([{"id": "o1"}, {"id": "o2"}])), response(204), response(204))
        self.db.delete_linkedin_sessions_except("cur")
        self.assertEqual(self.call(0).kwargs["params"]["filter"], 'cookie_hash != "cur"')
        self.assertEqual([c.args[0] for c in self.calls], ["GET", "DELETE", "DELETE"])

    def test_linkedin_session_none_when_missing(self):
        self.reply(response(200, page([])))
        self.assertIsNone(self.db.linkedin_session("h"))

    def test_linkedin_session_parses_dates(self):
        self.reply(response(200, page([{"id": "s", "searched_at": "2026-10-24 12:00:00.000Z",
                                        "expired_at": "", "reminded_at": ""}])))
        session = self.db.linkedin_session("h")
        self.assertEqual(session.searched_at, self.AT)
        self.assertIsNone(session.expired_at)


class InhireTest(StoreTestCase):
    AT = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

    def test_tenants_sorted(self):
        self.reply(response(200, page([{"slug": "zeta"}, {"slug": "alfa"}])))
        self.assertEqual(self.db.inhire_tenants(), ["alfa", "zeta"])

    def test_add_tenants_ignores_duplicates(self):
        self.reply(response(400, not_unique("slug")), response(200, {}))
        self.db.add_inhire_tenants(["a", "b"], self.AT)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.call().kwargs["json"], {"slug": "b", "found_at": "2026-10-24 12:00:00.000Z"})

    def test_add_tenants_empty_makes_no_request(self):
        self.db.add_inhire_tenants([], self.AT)
        self.assertEqual(len(self.calls), 0)

    def test_remove_missing_tenant_does_nothing(self):
        self.reply(response(200, page([])))
        self.db.remove_inhire_tenant("x")
        self.assertEqual([c.args[0] for c in self.calls], ["GET"])

    def test_remove_tenant_deletes(self):
        self.reply(response(200, page([{"id": "t1"}])), response(204))
        self.db.remove_inhire_tenant("x")
        self.assertEqual(self.call().args[0], "DELETE")

    def test_discovery_none_when_empty(self):
        self.reply(response(200, page([])))
        self.assertIsNone(self.db.inhire_discovery())

    def test_discovery_found_zero(self):
        self.reply(response(200, page([{"id": "d", "last_run": "2026-10-24 12:00:00.000Z", "found": 0}])))
        self.assertEqual(self.db.inhire_discovery(), (self.AT, 0))

    def test_discovery_found_missing_is_none(self):
        self.reply(response(200, page([{"id": "d", "last_run": "2026-10-24 12:00:00.000Z", "found": None}])))
        self.assertEqual(self.db.inhire_discovery(), (self.AT, None))

    def test_set_discovery_keeps_one_row(self):
        self.reply(response(200, page([])), response(200, {"id": "d"}),
                   response(200, page([{"id": "d"}])), response(200, {}))
        self.db.set_inhire_discovery(self.AT, 3)
        self.db.set_inhire_discovery(self.AT, 4)
        self.assertEqual([c.args[0] for c in self.calls], ["GET", "POST", "GET", "PATCH"])
        self.assertEqual(self.call().kwargs["json"]["found"], 4)


class DatesTest(unittest.TestCase):
    def test_aware_datetime_round_trip_in_utc(self):
        at = datetime(2026, 10, 24, 12, 0, tzinfo=BRASILIA)
        raw = store._format_date(at)
        self.assertEqual(raw, "2026-10-24 15:00:00.000Z")
        self.assertEqual(store._parse_date(raw), at)
        self.assertEqual(store._parse_date(raw).utcoffset(), timedelta(0))

    def test_microseconds_truncated_to_millis(self):
        at = datetime(2026, 10, 24, 12, 0, 0, 123999, tzinfo=timezone.utc)
        self.assertEqual(store._format_date(at), "2026-10-24 12:00:00.123Z")

    def test_empty_date_is_none(self):
        self.assertIsNone(store._parse_date(""))
        self.assertIsNone(store._parse_date(None))

    def test_naive_datetime_raises_value_error(self):
        with self.assertRaises(ValueError):
            store._format_date(datetime(2026, 10, 24, 12, 0))


class SourceTest(unittest.TestCase):
    def test_from_api(self):
        self.assertIs(store.Source.from_api("web"), store.Source.WEB)
        for raw in ("nope", None, "", "GUPY", "LINKEDIN_POST"):
            with self.subTest(raw=raw):
                self.assertIs(store.Source.from_api(raw), store.Source.UNKNOWN)

    def test_search_sources_order_and_labels(self):
        sources = store.Source.search_sources()
        self.assertEqual([s.api_value for s in sources],
                         ["gupy", "linkedin", "linkedin_posts", "inhire", "solides", "remotar", "web"])
        self.assertEqual([s.label for s in sources],
                         ["GUPY", "LINKEDIN", "LINKEDIN PUBLICAÇÕES", "INHIRE", "SOLIDES", "REMOTAR", "WEB"])
        for source in (store.Source.PROGRAMATHOR, store.Source.BOT,
                       store.Source.TELEGRAM, store.Source.UNKNOWN):
            self.assertNotIn(source, sources)


class FromEnvTest(unittest.TestCase):
    ENV = {store.ENV_URL: "http://pb.test", store.ENV_BOT_EMAIL: "bot@test.local",
           store.ENV_BOT_PASSWORD: "secret"}

    def test_missing_variable_raises_store_error(self):
        for name in self.ENV:
            for value in (None, ""):
                with self.subTest(name=name, value=value):
                    env = dict(self.ENV)
                    if value is None:
                        del env[name]
                    else:
                        env[name] = value
                    with mock.patch.dict(os.environ, env, clear=False):
                        if value is None:
                            os.environ.pop(name, None)
                        with self.assertRaises(store.StoreError) as ctx:
                            store.from_env()
                    self.assertEqual(ctx.exception.kind, store.StoreFailure.MISSING_CONFIG)

    def test_builds_store_without_connecting(self):
        with mock.patch.dict(os.environ, self.ENV, clear=False), \
                mock.patch("store.requests.Session") as session_cls:
            db = store.from_env()
        self.assertIsInstance(db, store.Store)
        session_cls.return_value.request.assert_not_called()
        self.assertNotIn("secret", repr(db))


if __name__ == "__main__":
    unittest.main()
