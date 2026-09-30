"""Unit tests for the ProgramaThor job search (HTML parsing + sending pipeline).

Run: python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import programathor  # noqa: E402


def card(href="/jobs/1-flutter-senior", title="Desenvolvedor(a) Flutter - Sênior", salary=None,
         company="Acme", level="Sênior", job_type="PJ", tags=("Dart", "Flutter")):
    """One result card, shaped like programathor.com.br (salary is optional there)."""
    icons = [("fa fa-briefcase", company), ("fas fa-map-marker-alt", "Remoto"),
             ("fa fa-building", "Startup")]
    if salary:
        icons.append(("far fa-money-bill-alt", salary))
    icons += [("far fa-chart-bar", level), ("far fa-file-alt", job_type)]
    spans = "".join(f'<span><i class="{icon}"></i> {text}</span>' for icon, text in icons)
    tag_spans = "".join(f'<span class="tag-list background-gray">{t}</span>' for t in tags)
    return (f'<div class="cell-list"><a href="{href}"><div class="cell-list-content">'
            f'<h3>{title}</h3><div class="cell-list-content-icon">{spans}</div>'
            f'<div>{tag_spans}</div></div></a></div>')


def page(*cards):
    return f"<html><body>{''.join(cards)}</body></html>"


def response(text="", status=200):
    return mock.Mock(status_code=status, text=text)


class BuildUrlTest(unittest.TestCase):

    def test_with_location(self):
        self.assertEqual(programathor.build_url("Flutter", "remoto"), "https://programathor.com.br/jobs-flutter/remoto")

    def test_without_location(self):
        self.assertEqual(programathor.build_url("flutter"), "https://programathor.com.br/jobs-flutter")


class ParseCardsTest(unittest.TestCase):

    def test_reads_fields_without_salary(self):
        job, = programathor.parse_cards(page(card()))
        self.assertEqual(job["link"], "https://programathor.com.br/jobs/1-flutter-senior")
        self.assertEqual(job["company"], "Acme")
        self.assertEqual(job["location"], "Remoto")
        self.assertEqual(job["salary"], "")
        self.assertEqual(job["level"], "Sênior")
        self.assertEqual(job["type"], "PJ")
        self.assertEqual(job["tags"], ["Dart", "Flutter"])

    def test_reads_fields_with_salary(self):
        job, = programathor.parse_cards(page(card(salary="Até R$15.000")))
        self.assertEqual(job["salary"], "Até R$15.000")
        self.assertEqual(job["level"], "Sênior")
        self.assertEqual(job["type"], "PJ")

    def test_skips_expired_and_cards_without_link(self):
        html = page(card(title="VencidaDESENVOLVEDOR(A) FLUTTER"),
                    '<div class="cell-list"><h3>Anúncio</h3></div>')
        self.assertEqual(programathor.parse_cards(html), [])

    def test_strips_new_badge_from_title(self):
        job, = programathor.parse_cards(page(card(title="NOVADev Flutter")))
        self.assertEqual(job["title"], "Dev Flutter")


class BuildMessageTest(unittest.TestCase):

    def test_escapes_html_and_omits_missing_salary(self):
        job, = programathor.parse_cards(page(card(title="Dev Flutter &lt;Sênior&gt;")))
        message = programathor.build_message("FLUTTER · REMOTO", job)
        self.assertIn("Dev Flutter &lt;Sênior&gt;", message)
        self.assertNotIn("Salário", message)
        self.assertIn("📄 <b>Nível:</b> Sênior · PJ", message)


class SearchJobsTest(unittest.TestCase):

    FILTERS = [{"nome": "FLUTTER · REMOTO", "termo": "flutter", "local_filtro": "remoto"}]

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE vagas_enviadas (link TEXT PRIMARY KEY, data_publicacao TEXT, titulo TEXT)")
        self.send = mock.Mock()

    def run_with(self, responses, proxy=None, errors=None):
        with mock.patch.object(programathor.requests, "get", side_effect=responses) as get, \
             mock.patch.object(programathor.time, "sleep"):
            programathor.search_jobs(self.conn, self.cursor, filters=self.FILTERS, user_agent="UA",
                                     proxy=proxy, check=main.filtros_basicos, send=self.send, errors=errors)
        return get

    def test_sends_new_jobs_and_stops_on_empty_page(self):
        get = self.run_with([response(page(card())), response(page())])
        self.assertEqual(self.send.call_count, 1)
        link, title, company = self.send.call_args.args[2:5]
        self.assertEqual((link, company), ("https://programathor.com.br/jobs/1-flutter-senior", "Acme"))
        self.assertEqual(self.send.call_args.args[-1], "PROGRAMATHOR")
        self.assertEqual(get.call_args_list[1].kwargs["params"], {"page": 2})

    def test_stops_when_page_has_nothing_new(self):
        self.cursor.execute("INSERT INTO vagas_enviadas VALUES (?, '', '')",
                            ("https://programathor.com.br/jobs/1-flutter-senior",))
        get = self.run_with([response(page(card()))])
        self.send.assert_not_called()
        self.assertEqual(get.call_count, 1)

    def test_sends_every_level(self):
        cards = [card(href=f"/jobs/{i}", title=f"Dev Flutter {level}", level=level)
                 for i, level in enumerate(["Júnior", "Pleno", "Sênior"])]
        self.run_with([response(page(*cards)), response(page())])
        self.assertEqual([c.args[3] for c in self.send.call_args_list],
                         ["Dev Flutter Júnior", "Dev Flutter Pleno", "Dev Flutter Sênior"])

    def test_skips_title_without_required_term(self):
        self.run_with([response(page(card(title="Desenvolvedor Java")))])
        self.send.assert_not_called()

    def test_routes_requests_through_proxy(self):
        get = self.run_with([response(page())], proxy="socks5h://127.0.0.1:40000")
        self.assertEqual(get.call_args.kwargs["proxies"],
                         {"http": "socks5h://127.0.0.1:40000", "https": "socks5h://127.0.0.1:40000"})

    def test_reports_refused_search(self):
        errors = []
        self.run_with([response(status=403)], errors=errors)
        self.assertEqual(errors, ["FLUTTER · REMOTO: o ProgramaThor recusou a busca (403)."])

    def test_reports_site_down(self):
        errors = []
        self.run_with(requests.ConnectionError("down"), errors=errors)
        self.assertEqual(errors, ["FLUTTER · REMOTO: o site não respondeu."])


if __name__ == "__main__":
    unittest.main()
