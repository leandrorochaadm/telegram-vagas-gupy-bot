"""Unit tests for the LinkedIn posts search (Brave Search API parsing + sending pipeline).

Run: python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import linkedin_posts  # noqa: E402
import main  # noqa: E402


def hours_ago(n):
    """Brave's page_age format: ISO 8601 in UTC without offset."""
    return (datetime.now(timezone.utc) - timedelta(hours=n)).strftime("%Y-%m-%dT%H:%M:%S")


def brave_result(**overrides):
    result = {
        "title": "Maria Souza on LinkedIn: Estamos contratando dev <strong>Flutter</strong>",
        "url": "https://www.linkedin.com/posts/maria-souza_flutter-vaga-activity-123?utm_source=share",
        "description": "Vaga remota para <strong>Flutter</strong> com Riverpod e Firebase.",
        "page_age": hours_ago(2),
    }
    result.update(overrides)
    return result


def brave_response(results, more=False, status=200):
    resp = mock.Mock(status_code=status, text="")
    resp.json.return_value = {"web": {"results": results}, "query": {"more_results_available": more}}
    return resp


class ParseTest(unittest.TestCase):

    def test_author_from_on_linkedin_prefix(self):
        self.assertEqual(linkedin_posts.parse_author("Maria Souza on LinkedIn: vaga"), "Maria Souza")
        self.assertEqual(linkedin_posts.parse_author("João Lima no LinkedIn: vaga"), "João Lima")

    def test_author_from_pipe_suffix(self):
        self.assertEqual(linkedin_posts.parse_author("#flutter #vaga | Ana Reis"), "Ana Reis")

    def test_author_fallback(self):
        self.assertEqual(linkedin_posts.parse_author("Vaga Flutter"), "Autor não informado")

    def test_parse_result_strips_tags_and_query_string(self):
        post = linkedin_posts.parse_result(brave_result())
        self.assertEqual(post["link"], "https://www.linkedin.com/posts/maria-souza_flutter-vaga-activity-123")
        self.assertEqual(post["author"], "Maria Souza")
        self.assertNotIn("<strong>", post["text"])
        self.assertIn("Riverpod", post["text"])
        published = datetime.now(timezone.utc) - timedelta(hours=2)
        self.assertEqual(post["date"], published.strftime("%d/%m/%Y"))

    def test_parse_result_drops_posts_older_than_24h(self):
        self.assertIsNone(linkedin_posts.parse_result(brave_result(page_age=hours_ago(25))))

    def test_parse_result_keeps_post_without_page_age(self):
        post = linkedin_posts.parse_result(brave_result(page_age=None))
        self.assertEqual(post["date"], datetime.now(timezone.utc).strftime("%d/%m/%Y"))

    def test_parse_page_age_with_offset(self):
        parsed = linkedin_posts.parse_page_age("2026-09-28T14:30:00Z")
        self.assertEqual(parsed, datetime(2026, 9, 28, 14, 30, tzinfo=timezone.utc))

    def test_parse_result_normalizes_country_subdomain(self):
        post = linkedin_posts.parse_result(brave_result(
            url="https://br.linkedin.com/posts/maria-souza_flutter-vaga-activity-123/?trk=public"))
        self.assertEqual(post["link"], "https://www.linkedin.com/posts/maria-souza_flutter-vaga-activity-123")

    def test_parse_result_ignores_non_post_urls(self):
        self.assertIsNone(linkedin_posts.parse_result(brave_result(url="https://www.linkedin.com/jobs/view/1")))
        self.assertIsNone(linkedin_posts.parse_result(brave_result(url="https://evil.com/linkedin.com/posts/x")))


class SearchPostsTest(unittest.TestCase):

    @mock.patch("linkedin_posts.time.sleep")
    @mock.patch("linkedin_posts.requests.get")
    def test_sends_past_day_site_query(self, get, sleep):
        get.return_value = brave_response([brave_result()])
        posts = linkedin_posts.search_posts("key", "flutter vaga", pages=3)

        self.assertEqual(len(posts), 1)
        self.assertEqual(get.call_count, 1)  # no more results → stops paging
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["q"], "site:linkedin.com/posts flutter vaga")
        self.assertEqual(params["freshness"], "pd")
        self.assertEqual(get.call_args.kwargs["headers"]["X-Subscription-Token"], "key")
        # Waits even after the last page, so the next filter's query respects the rate limit
        sleep.assert_called_once_with(linkedin_posts.REQUEST_INTERVAL)

    @mock.patch("linkedin_posts.time.sleep")
    @mock.patch("linkedin_posts.requests.get")
    def test_pages_while_more_results(self, get, _sleep):
        get.side_effect = [brave_response([brave_result()], more=True),
                           brave_response([brave_result(url="https://www.linkedin.com/posts/x-2")])]
        posts = linkedin_posts.search_posts("key", "flutter vaga", pages=2)
        self.assertEqual([p["link"] for p in posts][-1], "https://www.linkedin.com/posts/x-2")
        self.assertEqual(get.call_args.kwargs["params"]["offset"], 1)

    @mock.patch("linkedin_posts.time.sleep")
    @mock.patch("linkedin_posts.requests.get")
    def test_http_error_returns_empty(self, get, _sleep):
        get.return_value = brave_response([], status=429)
        self.assertEqual(linkedin_posts.search_posts("key", "flutter vaga"), [])

    @mock.patch("linkedin_posts.time.sleep")
    @mock.patch("linkedin_posts.requests.get")
    def test_http_error_is_reported(self, get, _sleep):
        get.return_value = brave_response([], status=429)
        errors = []
        linkedin_posts.search_posts("key", "flutter vaga", errors=errors)
        self.assertEqual(errors, ["código 429"])


class BuscarPostsLinkedinTest(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE vagas_enviadas (link TEXT PRIMARY KEY, data_publicacao TEXT, titulo TEXT)")
        main._enviados_sessao.clear()
        patches = [
            mock.patch.object(main, "BRAVE_API_KEY", "key"),
            mock.patch.object(main, "enviar_telegram"),
            mock.patch.object(main.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, posts):
        with mock.patch.object(main.linkedin_posts, "search_posts", return_value=posts):
            main.buscar_posts_linkedin(self.conn, self.cursor)

    def test_sends_post_with_author_and_link(self):
        self.run_with([linkedin_posts.parse_result(brave_result())])
        main.enviar_telegram.assert_called_once()
        mensagem = main.enviar_telegram.call_args.args[0]
        self.assertIn("Maria Souza", mensagem)
        self.assertIn("Ver publicação", mensagem)

    def test_skips_post_without_flutter(self):
        self.run_with([linkedin_posts.parse_result(brave_result(
            title="Maria on LinkedIn: vaga React", description="React e Node"))])
        main.enviar_telegram.assert_not_called()

    def test_skips_post_without_vaga(self):
        self.run_with([linkedin_posts.parse_result(brave_result(
            title="Maria on LinkedIn: dica de Flutter", description="Trabalho remoto com Flutter"))])
        main.enviar_telegram.assert_not_called()

    def test_skips_post_without_remote(self):
        self.run_with([linkedin_posts.parse_result(brave_result(
            title="Maria on LinkedIn: vaga Flutter", description="Vaga presencial em SP"))])
        main.enviar_telegram.assert_not_called()

    def test_accepts_remoto_as_well_as_remota(self):
        self.run_with([linkedin_posts.parse_result(brave_result(
            title="Maria on LinkedIn: vaga Flutter", description="Trabalho remoto"))])
        main.enviar_telegram.assert_called_once()

    def test_accepts_vagas_plural(self):
        self.run_with([linkedin_posts.parse_result(brave_result(
            title="Maria on LinkedIn: vagas Flutter", description="Trabalho remoto"))])
        main.enviar_telegram.assert_called_once()

    def test_skips_already_sent_link(self):
        post = linkedin_posts.parse_result(brave_result())
        self.cursor.execute("INSERT INTO vagas_enviadas VALUES (?, '', '')", (post["link"],))
        self.run_with([post])
        main.enviar_telegram.assert_not_called()

    def test_escapes_html_in_post_text(self):
        self.run_with([linkedin_posts.parse_result(brave_result(description="Vaga remota Flutter & Dart <3"))])
        mensagem = main.enviar_telegram.call_args.args[0]
        self.assertIn("Flutter &amp; Dart &lt;3", mensagem)

    def test_disabled_without_api_key(self):
        with mock.patch.object(main, "BRAVE_API_KEY", None), \
             mock.patch.object(main.linkedin_posts, "search_posts") as search:
            main.buscar_posts_linkedin(self.conn, self.cursor)
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
