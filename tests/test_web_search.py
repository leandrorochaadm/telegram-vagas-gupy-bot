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

import main  # noqa: E402
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
        self.assertEqual(page["date"], "Sem data")

    def test_max_age_is_configurable(self):
        result = brave_result(page_age=hours_ago(24 * 5))
        self.assertIsNone(web_search.parse_web_result(result))
        self.assertIsNotNone(web_search.parse_web_result(result, max_age=timedelta(days=7)))

    def test_drops_result_without_url(self):
        self.assertIsNone(web_search.parse_web_result(brave_result(url="")))


class FreshnessTest(unittest.TestCase):

    def test_picks_smallest_brave_window(self):
        self.assertEqual([web_search.freshness(d) for d in (1, 2, 7, 8, 31, 90, 400)],
                         ["pd", "pw", "pw", "pm", "pm", "py", "py"])


class SearchWebTest(unittest.TestCase):

    @mock.patch("web_search.time.sleep")
    @mock.patch("web_search.requests.get")
    def test_week_search_keeps_five_day_old_page(self, get, _sleep):
        get.return_value = brave_response([brave_result(page_age=hours_ago(24 * 5))])
        pages = web_search.search_web("key", "flutter remoto", max_age_days=7)
        self.assertEqual(get.call_args.kwargs["params"]["freshness"], "pw")
        self.assertEqual(len(pages), 1)

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

    @mock.patch("web_search.time.sleep")
    @mock.patch("web_search.requests.get")
    def test_http_error_is_reported(self, get, _sleep):
        get.return_value = brave_response([], status=429)
        errors = []
        web_search.search_web("key", "flutter vaga", errors=errors)
        self.assertEqual(errors, ["código 429"])


class SearchJobsTest(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE vagas_enviadas (link TEXT PRIMARY KEY, data_publicacao TEXT, titulo TEXT)")
        self.send = mock.Mock()

    def run_with(self, results, api_key="key", ignored_companies=(), errors=None, search_error=None,
                 blocked_terms=()):
        pages = [web_search.parse_web_result(r) for r in results]
        with mock.patch.object(web_search, "search_web", return_value=pages, side_effect=search_error) as search:
            web_search.search_jobs(
                self.conn, self.cursor,
                api_key=api_key,
                filters=[{"nome": "FLUTTER", "termo": "flutter vaga"}],
                pages=1,
                excluded_sites=["linkedin.com"],
                required_terms=[["flutter"], ["vaga"], ["remoto", "remota"]],
                ignored_companies=list(ignored_companies),
                blocked_terms=list(blocked_terms),
                send=self.send,
                errors=errors,
            )
        return search

    def test_brave_refusal_becomes_alert_sentence(self):
        def refuse(*args, errors, **_options):
            errors.append("código 429")
            return []
        errors = []
        self.run_with([], errors=errors, search_error=refuse)
        self.assertEqual(errors, ["FLUTTER: a Brave recusou a busca (código 429)."])

    def test_network_error_becomes_alert_sentence(self):
        errors = []
        self.run_with([], errors=errors, search_error=web_search.requests.ConnectionError("down"))
        self.assertEqual(errors, ["FLUTTER: a Brave não respondeu."])
        self.send.assert_not_called()

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

    def test_skips_page_with_blocked_term(self):
        self.run_with([brave_result(title="[Híbrido] Desenvolvedor Flutter", description="Vaga remota")],
                      blocked_terms=["híbrido"])
        self.send.assert_not_called()

    def test_blocked_term_needs_whole_word(self):
        self.run_with([brave_result(description="Vaga remota, sem presencialidade")], blocked_terms=["presencial"])
        self.send.assert_called_once()

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


class MainWiringTest(unittest.TestCase):
    """main.py only passes settings; check it passes the web ones."""

    def test_passes_web_settings(self):
        with mock.patch.object(main.web_search, "search_jobs") as search_jobs, \
             mock.patch.object(main, "anotar_avisos"):
            main.buscar_vagas_web(mock.Mock(), mock.Mock())
        kwargs = search_jobs.call_args.kwargs
        self.assertIs(kwargs["required_terms"], main.TERMOS_OBRIGATORIOS_WEB)
        self.assertIs(kwargs["filters"], main.FILTROS_WEB)
        self.assertIs(kwargs["excluded_sites"], main.SITES_EXCLUIDOS_WEB)
        self.assertIs(kwargs["blocked_terms"], main.TERMOS_BLOQUEADOS_WEB)
        self.assertEqual(kwargs["max_age_days"], main.DIAS_WEB)

    def test_web_rule_accepts_job_without_vaga_word(self):
        patterns = [web_search._word_pattern(g) for g in main.TERMOS_OBRIGATORIOS_WEB]
        for text in ["desenvolvedor flutter · remoto", "dev flutter home-office", "flutter homeoffice"]:
            self.assertTrue(all(p.search(text) for p in patterns), text)


if __name__ == "__main__":
    unittest.main()
