"""Unit tests for the open-web job search (Brave Search API parsing + sending pipeline).

Run: python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_search  # noqa: E402


def hours_ago(n):
    """Brave's page_age format: ISO 8601 in UTC without offset."""
    return (datetime.now(timezone.utc) - timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M:%S")


def brave_result(**overrides):
    result = {
        "title": "Vaga Desenvolvedor <strong>Flutter</strong> Pleno",
        "url": "https://www.acme.com.br/carreiras/flutter-pleno#aplicar",
        "description": "Trabalho remoto, CLT.",
        "page_age": hours_ago(2),
    }
    result.update(overrides)
    return result


def brave_response(results, more=False, status=200):
    resp = mock.Mock(status_code=status, text="")
    resp.json.return_value = {"web": {"results": results}, "query": {"more_results_available": more}}
    return resp


class ParseWebResultTest(unittest.TestCase):

    def test_uses_site_domain_as_author_and_drops_fragment(self):
        page = web_search.parse_web_result(brave_result())
        self.assertEqual(page["author"], "acme.com.br")
        self.assertEqual(page["link"], "https://www.acme.com.br/carreiras/flutter-pleno")
        self.assertIn("Flutter Pleno Trabalho remoto", page["text"])

    def test_keeps_query_string(self):
        page = web_search.parse_web_result(brave_result(url="https://jobs.example.com/view?id=42"))
        self.assertEqual(page["link"], "https://jobs.example.com/view?id=42")

    def test_drops_excluded_site_and_its_subdomains(self):
        for url in ["https://linkedin.com/jobs/view/1", "https://br.linkedin.com/posts/x-1"]:
            self.assertIsNone(web_search.parse_web_result(brave_result(url=url), excluded_sites=["linkedin.com"]))

    def test_keeps_site_that_only_ends_like_excluded_one(self):
        page = web_search.parse_web_result(brave_result(url="https://notlinkedin.com/vaga"),
                                           excluded_sites=["linkedin.com"])
        self.assertEqual(page["author"], "notlinkedin.com")

    def test_drops_pages_older_than_24h(self):
        self.assertIsNone(web_search.parse_web_result(brave_result(page_age=hours_ago(25))))

    def test_keeps_page_without_page_age(self):
        page = web_search.parse_web_result(brave_result(page_age=None))
        self.assertEqual(page["date"], datetime.now(timezone.utc).strftime("%d/%m/%Y"))

    def test_drops_result_without_url(self):
        self.assertIsNone(web_search.parse_web_result(brave_result(url="")))


class SearchWebTest(unittest.TestCase):

    @mock.patch("web_search.time.sleep")
    @mock.patch("web_search.requests.get")
    def test_sends_past_day_query_without_site_restriction(self, get, sleep):
        get.return_value = brave_response([brave_result()])
        pages = web_search.search_web("key", "flutter vaga", pages=3)

        self.assertEqual([p["author"] for p in pages], ["acme.com.br"])
        self.assertEqual(get.call_count, 1)  # no more results → stops paging
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["q"], "flutter vaga")
        self.assertEqual(params["freshness"], "pd")
        self.assertEqual(get.call_args.kwargs["headers"]["X-Subscription-Token"], "key")
        sleep.assert_called_once_with(web_search.REQUEST_INTERVAL)

    @mock.patch("web_search.time.sleep")
    @mock.patch("web_search.requests.get")
    def test_excludes_sites_in_query_and_results(self, get, _sleep):
        get.return_value = brave_response([brave_result(url="https://br.linkedin.com/posts/x-1"), brave_result()])
        pages = web_search.search_web("key", "flutter vaga remoto OR remota", excluded_sites=["linkedin.com"])

        self.assertEqual(get.call_args.kwargs["params"]["q"],
                         "-site:linkedin.com flutter vaga remoto OR remota")
        self.assertEqual([p["author"] for p in pages], ["acme.com.br"])

    @mock.patch("web_search.time.sleep")
    @mock.patch("web_search.requests.get")
    def test_pages_while_more_results(self, get, _sleep):
        get.side_effect = [brave_response([brave_result()], more=True),
                           brave_response([brave_result(url="https://other.com/vaga")])]
        pages = web_search.search_web("key", "flutter vaga", pages=2)
        self.assertEqual(pages[-1]["link"], "https://other.com/vaga")
        self.assertEqual(get.call_args.kwargs["params"]["offset"], 1)

    @mock.patch("web_search.time.sleep")
    @mock.patch("web_search.requests.get")
    def test_http_error_returns_empty(self, get, _sleep):
        get.return_value = brave_response([], status=429)
        self.assertEqual(web_search.search_web("key", "flutter vaga"), [])


class SearchJobsTest(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE vagas_enviadas (link TEXT PRIMARY KEY, data_publicacao TEXT, titulo TEXT)")
        self.send = mock.Mock()

    def run_with(self, results, api_key="key", ignored_companies=()):
        pages = [web_search.parse_web_result(r) for r in results]
        with mock.patch.object(web_search, "search_web", return_value=pages) as search:
            web_search.search_jobs(
                self.conn, self.cursor,
                api_key=api_key,
                filters=[{"nome": "FLUTTER", "termo": "flutter vaga"}],
                pages=1,
                excluded_sites=["linkedin.com"],
                required_terms=[["flutter"], ["vaga"], ["remoto", "remota"]],
                ignored_companies=list(ignored_companies),
                send=self.send,
            )
        return search

    def test_sends_page_with_site_and_link(self):
        self.run_with([brave_result()])
        self.send.assert_called_once()
        args = self.send.call_args.args
        link, site, message, source = args[2], args[4], args[6], args[7]
        self.assertEqual(link, "https://www.acme.com.br/carreiras/flutter-pleno")
        self.assertEqual(site, "acme.com.br")
        self.assertEqual(source, "WEB")
        self.assertIn("Ver vaga", message)

    def test_skips_page_without_required_terms(self):
        self.run_with([brave_result(description="Presencial em SP.")])
        self.send.assert_not_called()

    def test_requires_whole_words(self):
        self.run_with([brave_result(title="Vagabundo Flutter", description="remoto")])
        self.send.assert_not_called()

    def test_skips_ignored_company_domain(self):
        self.run_with([brave_result()], ignored_companies=["ACME"])
        self.send.assert_not_called()

    def test_skips_already_sent_link(self):
        self.cursor.execute("INSERT INTO vagas_enviadas VALUES (?, '', '')",
                            ("https://www.acme.com.br/carreiras/flutter-pleno",))
        self.run_with([brave_result()])
        self.send.assert_not_called()

    def test_escapes_html(self):
        self.run_with([brave_result(description="Vaga remota Flutter & Dart <3")])
        self.assertIn("Flutter &amp; Dart &lt;3", self.send.call_args.args[6])

    def test_disabled_without_api_key(self):
        search = self.run_with([brave_result()], api_key=None)
        search.assert_not_called()
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
