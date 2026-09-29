"""Unit tests for the Solides scraper (Next.js page parsing + filtering pipeline).

Run: python -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

FILTER = {"nome": "FLUTTER · REMOTO", "caminho": "remoto/flutter"}


def text_row(row_id, text):
    """RSC text row: '<id>:T<hex byte length>,<text>' (no trailing newline)."""
    return f"{row_id}:T{len(text.encode('utf-8')):x},{text}"


def next_html(flight, parts=2):
    """Wrap a flight payload in self.__next_f.push scripts, split across `parts` chunks."""
    size = -(-len(flight) // parts)
    chunks = [flight[i:i + size] for i in range(0, len(flight), size)]
    scripts = ['<script>self.__next_f.push([0])</script>']
    scripts += [f'<script>self.__next_f.push({json.dumps([1, c])})</script>' for c in chunks]
    return "<html><body>" + "".join(scripts) + "</body></html>"


def initial_data_row(vagas, total_pages=1, current_page=1):
    data = {"totalPages": total_pages, "currentPage": current_page, "count": len(vagas), "data": vagas}
    return '5:["$","$L1d",null,{"initialData":' + json.dumps(data, ensure_ascii=False) + '}]\n'


def days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def make_vaga(**overrides):
    vaga = {
        "id": 914710,
        "slug": "cedrotech",
        "title": "Desenvolvedor Flutter Pleno",
        "companyName": "Cedro",
        "description": "<p>Flutter, Dart e Riverpod</p>",
        "city": {"name": "Uberlândia"},
        "state": {"code": "MG"},
        "jobType": "remoto",
        "createdAt": days_ago(1),
    }
    vaga.update(overrides)
    return vaga


class FlightParsingTest(unittest.TestCase):

    def test_flight_joins_push_chunks_and_skips_bootstrap(self):
        flight = '0:{"a":1}\n1:"b"\n'
        self.assertEqual(main._solides_flight(next_html(flight, parts=3)), flight)

    def test_flight_ignores_invalid_json_chunk(self):
        page = '<script>self.__next_f.push([1,"ok"])</script><script>self.__next_f.push([1,oops])</script>'
        self.assertEqual(main._solides_flight(page), "ok")

    def test_texts_uses_byte_length_for_multibyte_content(self):
        text = "<p>Programação ção ção</p>"
        flight = '0:{"x":1}\n' + text_row("1e", text) + '2:"after"\n'
        self.assertEqual(main._solides_textos(flight), {"1e": text})

    def test_texts_handles_row_glued_to_text_ending_in_hex_char(self):
        # Text rows have no newline, so the next id follows right after "...a"
        flight = text_row("1f", "texto termina em a") + text_row("20", "segundo") + '3:"x"\n'
        self.assertEqual(main._solides_textos(flight), {"1f": "texto termina em a", "20": "segundo"})

    def test_texts_skips_rows_with_empty_id_and_non_row_lines(self):
        flight = ':HL["/font.woff2","font"]\n' + "garbage line\n" + text_row("21", "desc") + '4:"x"\n'
        self.assertEqual(main._solides_textos(flight), {"21": "desc"})


class SolidesPageTest(unittest.TestCase):

    def _response(self, status, text=""):
        return mock.Mock(status_code=status, text=text)

    def test_page_returns_jobs_total_pages_and_texts(self):
        flight = text_row("1e", "<p>Dart</p>") + initial_data_row([make_vaga(description="$1e")], total_pages=3)
        with mock.patch.object(main.requests, "get", return_value=self._response(200, next_html(flight))) as get:
            vagas, total, textos = main._solides_pagina("remoto/flutter", 2, {})

        self.assertEqual(get.call_args.args[0], "https://vagas.solides.com.br/vagas/remoto/flutter")
        self.assertEqual(get.call_args.kwargs["params"], {"page": 2})
        self.assertEqual(total, 3)
        self.assertEqual(vagas[0]["id"], 914710)
        self.assertEqual(textos["1e"], "<p>Dart</p>")

    def test_page_raises_on_http_error(self):
        with mock.patch.object(main.requests, "get", return_value=self._response(403)):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                main._solides_pagina("remoto/flutter", 1, {})

    def test_page_raises_when_layout_has_no_initial_data(self):
        with mock.patch.object(main.requests, "get", return_value=self._response(200, next_html('0:{"x":1}\n'))):
            with self.assertRaisesRegex(RuntimeError, "initialData"):
                main._solides_pagina("remoto/flutter", 1, {})


class BuscarVagasSolidesTest(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE vagas_enviadas (link TEXT PRIMARY KEY, data_publicacao TEXT, titulo TEXT)")
        main._enviados_sessao.clear()
        self.sent = []
        for target, value in [
            ("FILTROS_SOLIDES", [FILTER]),
            ("DIAS_BUSCA_SOLIDES", 20),
            ("enviar_telegram", self.sent.append),
        ]:
            patcher = mock.patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(main.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.conn.close()

    def run_with_pages(self, *pages):
        """Each page is (vagas, total_pages, textos) or an Exception to raise."""
        with mock.patch.object(main, "_solides_pagina", side_effect=list(pages)) as pagina:
            main.buscar_vagas_solides(self.conn, self.cursor)
        return pagina

    def saved_links(self):
        return [row[0] for row in self.cursor.execute("SELECT link FROM vagas_enviadas")]

    def test_sends_recent_flutter_job_with_company_link(self):
        self.run_with_pages(([make_vaga()], 1, {}))

        self.assertEqual(self.saved_links(), ["https://cedrotech.vagas.solides.com.br/vaga/914710"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("📍 <b>Local:</b> Uberlândia - MG", self.sent[0])
        self.assertIn("💻 <b>Modelo:</b> Remoto", self.sent[0])

    def test_escapes_html_in_title_and_company(self):
        self.run_with_pages(([make_vaga(title="Dev Flutter & React <Pleno>", companyName="A&B")], 1, {}))

        self.assertIn("Dev Flutter &amp; React &lt;Pleno&gt;", self.sent[0])
        self.assertIn("A&amp;B", self.sent[0])

    def test_skips_title_without_flutter(self):
        self.run_with_pages(([make_vaga(title="Desenvolvedor React Native")], 1, {}))
        self.assertEqual(self.sent, [])

    def test_skips_ignored_company(self):
        self.run_with_pages(([make_vaga(companyName="Jobgether")], 1, {}))
        self.assertEqual(self.sent, [])

    def test_skips_job_older_than_limit(self):
        self.run_with_pages(([make_vaga(createdAt=days_ago(21))], 1, {}))
        self.assertEqual(self.sent, [])

    def test_skips_job_already_in_database(self):
        self.cursor.execute("INSERT INTO vagas_enviadas VALUES (?, ?, ?)",
                            ("https://cedrotech.vagas.solides.com.br/vaga/914710", "", ""))
        self.run_with_pages(([make_vaga()], 1, {}))
        self.assertEqual(self.sent, [])

    def test_skips_job_without_slug_or_id(self):
        self.run_with_pages(([make_vaga(slug=None), make_vaga(id=None)], 1, {}))
        self.assertEqual(self.sent, [])

    def test_filter_name_no_longer_drops_non_remote_jobs(self):
        # Modality comes from the URL path; the "REMOTO" label must not filter by itself
        self.run_with_pages(([make_vaga(jobType="hibrido")], 1, {}))
        self.assertEqual(len(self.sent), 1)

    def test_null_fields_do_not_abort_the_remaining_jobs(self):
        null_vaga = make_vaga(id=1, companyName=None, city={"name": None}, state=None,
                              jobType=None, createdAt=None, description=None)
        self.run_with_pages(([null_vaga, make_vaga(id=2, title="Dev Flutter Sênior")], 1, {}))

        self.assertEqual(len(self.sent), 2)
        self.assertIn("Empresa não informada", self.sent[0])
        self.assertIn("📍 <b>Local:</b> Brasil", self.sent[0])
        self.assertNotIn("None", self.sent[0])

    def test_paginates_until_total_pages(self):
        pagina = self.run_with_pages(
            ([make_vaga(id=1)], 2, {}),
            ([make_vaga(id=2, title="Flutter Dev II")], 2, {}),
        )
        self.assertEqual([c.args[1] for c in pagina.call_args_list], [1, 2])
        self.assertEqual(len(self.sent), 2)

    def test_stops_on_empty_page(self):
        pagina = self.run_with_pages(([], 5, {}))
        self.assertEqual(pagina.call_count, 1)

    def test_page_error_stops_search_without_raising(self):
        pagina = self.run_with_pages(RuntimeError("HTTP 403"))
        self.assertEqual(pagina.call_count, 1)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
