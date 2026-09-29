"""Unit tests for InHire company (tenant) discovery.

Run: python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inhire_discovery  # noqa: E402

WEEK = timedelta(days=7)


def response(status=200, text="", json_data=None):
    resp = mock.Mock(status_code=status, text=text)
    resp.json.return_value = json_data or {}
    return resp


class ExtractTenantsTest(unittest.TestCase):
    def test_finds_subdomains_in_plain_and_encoded_links(self):
        text = (
            '<a href="https://acme.inhire.app/vagas/1/dev">acme.inhire.app</a>'
            '<a href="/r?u=https%3A%2F%2Fbeta-co.inhire.app%2Fvagas">x</a>'
            " Gamma.InHire.app/vagas"
        )
        self.assertEqual(inhire_discovery.extract_tenants(text), {"acme", "beta-co", "gamma"})

    def test_ignores_inhire_own_subdomains(self):
        text = "https://www.inhire.app https://api.inhire.app https://status.inhire.app"
        self.assertEqual(inhire_discovery.extract_tenants(text), set())

    def test_ignores_nested_subdomains(self):
        self.assertEqual(inhire_discovery.extract_tenants("https://senior.plugin.inhire.app"), set())

    def test_ignores_other_domains(self):
        self.assertEqual(inhire_discovery.extract_tenants("https://acme.inhire.com.br https://notinhire.app"), set())


@mock.patch.object(inhire_discovery.time, "sleep")
class SearchYahooTest(unittest.TestCase):
    def test_reads_every_page_and_skips_failed_ones(self, _sleep):
        pages = [
            response(text="https://acme.inhire.app"),
            response(status=500),
            response(text="https://beta.inhire.app https://acme.inhire.app"),
        ]
        with mock.patch.object(inhire_discovery, "YAHOO_PAGES", 3), \
                mock.patch.object(inhire_discovery.requests, "get", side_effect=pages) as get:
            tenants = inhire_discovery.search_yahoo("site:inhire.app vagas")
        self.assertEqual(tenants, {"acme", "beta"})
        self.assertEqual([c.kwargs["params"]["b"] for c in get.call_args_list], [1, 8, 15])


@mock.patch.object(inhire_discovery.time, "sleep")
class SearchBraveTest(unittest.TestCase):
    def test_collects_tenants_until_no_more_results(self, _sleep):
        pages = [
            response(json_data={"web": {"results": [{"url": "https://acme.inhire.app/vagas/1"}]},
                                "query": {"more_results_available": True}}),
            response(json_data={"web": {"results": [{"url": "https://beta.inhire.app/"}]},
                                "query": {"more_results_available": False}}),
        ]
        with mock.patch.object(inhire_discovery.requests, "get", side_effect=pages) as get:
            tenants = inhire_discovery.search_brave("key", "site:inhire.app vagas")
        self.assertEqual(tenants, {"acme", "beta"})
        self.assertEqual(get.call_count, 2)
        self.assertNotIn("freshness", get.call_args.kwargs["params"])

    def test_stops_on_http_error(self, _sleep):
        with mock.patch.object(inhire_discovery.requests, "get", return_value=response(status=429)) as get:
            self.assertEqual(inhire_discovery.search_brave("key", "q"), set())
        self.assertEqual(get.call_count, 1)


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()

    def tearDown(self):
        self.conn.close()

    def discover(self, brave_api_key=None, queries=("q1", "q2")):
        return inhire_discovery.discover(
            self.conn, self.cursor, queries=list(queries), brave_api_key=brave_api_key, every=WEEK,
        )

    def test_saves_tenants_from_every_query(self):
        with mock.patch.object(inhire_discovery, "search_yahoo", side_effect=[{"acme"}, {"beta", "acme"}]):
            self.assertEqual(self.discover(), ["acme", "beta"])

    def test_uses_brave_only_with_api_key(self):
        with mock.patch.object(inhire_discovery, "search_yahoo", return_value=set()), \
                mock.patch.object(inhire_discovery, "search_brave", return_value={"gamma"}) as brave:
            self.discover(queries=["q1"])
            brave.assert_not_called()
            self.assertEqual(self.discover(brave_api_key="key", queries=["q1"]), ["gamma"])
            brave.assert_called_once_with("key", "q1")

    def test_skips_search_until_interval_passes(self):
        with mock.patch.object(inhire_discovery, "search_yahoo", return_value={"acme"}) as yahoo:
            self.discover()
            yahoo.reset_mock()
            self.assertEqual(self.discover(), ["acme"])
            yahoo.assert_not_called()

    def test_keeps_old_tenants_on_new_run(self):
        with mock.patch.object(inhire_discovery, "search_yahoo", return_value={"acme"}):
            self.discover()
        self.cursor.execute("UPDATE inhire_discovery SET last_run = ?",
                            ((datetime.now() - WEEK).isoformat(),))
        with mock.patch.object(inhire_discovery, "search_yahoo", return_value={"beta"}):
            self.assertEqual(self.discover(), ["acme", "beta"])

    def test_retries_next_run_when_nothing_found(self):
        with mock.patch.object(inhire_discovery, "search_yahoo", return_value=set()):
            self.discover()
        self.assertTrue(inhire_discovery.discovery_due(self.cursor, WEEK))

    def test_search_error_does_not_stop_other_queries(self):
        error = inhire_discovery.requests.RequestException("timeout")
        with mock.patch.object(inhire_discovery, "search_yahoo", side_effect=[error, {"beta"}]):
            self.assertEqual(self.discover(), ["beta"])

    def test_remove_tenant(self):
        with mock.patch.object(inhire_discovery, "search_yahoo", return_value={"acme", "beta"}):
            self.discover(queries=["q1"])
        inhire_discovery.remove_tenant(self.conn, self.cursor, "acme")
        self.assertEqual(inhire_discovery.load_tenants(self.cursor), ["beta"])


if __name__ == "__main__":
    unittest.main()
