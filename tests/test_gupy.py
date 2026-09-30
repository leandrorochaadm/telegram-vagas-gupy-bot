"""Unit tests for the Gupy job search (API request, parsing and sending pipeline).

Run: python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gupy  # noqa: E402
import main  # noqa: E402


def utc_iso(days_ago=1):
    """publishedDate as the API sends it (UTC, milliseconds, trailing Z)."""
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def api_job(job_id=1, name="Desenvolvedor Flutter Pleno", company="Acme", workplace="remote",
            published=None, job_type="vacancy_type_effective", disabilities=False, **extra):
    """One job as returned by portal.gupy.io/api/job-search/jobs."""
    job = {
        "id": job_id, "name": name, "careerPageName": company, "workplaceType": workplace,
        "jobUrl": f"https://acme.gupy.io/jobs/{job_id}", "publishedDate": published or utc_iso(),
        "type": job_type, "disabilities": disabilities, "city": "Curitiba", "state": "Paraná",
    }
    job.update(extra)
    return job


def response(jobs=(), status=200):
    return mock.Mock(status_code=status, json=mock.Mock(return_value={"data": list(jobs)}))


def invalid_json_response():
    error = requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
    return mock.Mock(status_code=200, json=mock.Mock(side_effect=error))


class PublishedTest(unittest.TestCase):

    def test_converts_utc_to_brasilia(self):
        published = gupy._published({"publishedDate": "2026-09-04T13:30:00.000Z"})
        self.assertEqual(published, datetime(2026, 9, 4, 10, 30))

    def test_missing_date_is_none(self):
        self.assertIsNone(gupy._published({}))

    def test_null_date_is_none(self):
        self.assertIsNone(gupy._published({"publishedDate": None}))

    def test_unreadable_date_is_none(self):
        self.assertIsNone(gupy._published({"publishedDate": "ontem"}))


class BuildMessageTest(unittest.TestCase):

    def test_remote_filter_shows_anywhere_and_translates_fields(self):
        job = api_job(disabilities=True)
        message = gupy.build_message("FLUTTER · REMOTO", job, datetime(2026, 9, 4, 10, 30))
        self.assertIn("🟣 <b>GUPY — FLUTTER · REMOTO</b>", message)
        self.assertIn("📍 <b>Local:</b> Qualquer lugar (Remoto)\n", message)
        self.assertIn("💻 <b>Modelo:</b> Remoto\n", message)
        self.assertIn("📄 <b>Tipo:</b> Efetivo\n", message)
        self.assertIn("♿ <b>PCD:</b> Sim\n", message)
        self.assertIn("📅 <b>Data:</b> 04/09/2026 às 10:30", message)
        self.assertIn("href='https://acme.gupy.io/jobs/1'", message)

    def test_other_filter_shows_city_and_state(self):
        message = gupy.build_message("FLUTTER · CURITIBA", api_job(workplace="on-site"), None)
        self.assertIn("📍 <b>Local:</b> Curitiba - Paraná\n", message)
        self.assertIn("💻 <b>Modelo:</b> Presencial\n", message)

    def test_unknown_values_and_missing_date(self):
        job = api_job(workplace="", job_type="vacancy_type_other")
        message = gupy.build_message("FLUTTER · REMOTO", job, None)
        self.assertIn("💻 <b>Modelo:</b> Não informado\n", message)
        self.assertIn("📄 <b>Tipo:</b> Outros\n", message)
        self.assertIn("♿ <b>PCD:</b> Não informado\n", message)
        self.assertIn("📅 <b>Data:</b> Sem data às --:--", message)

    def test_escapes_html(self):
        job = api_job(name="Dev Flutter <Pleno> & Mobile", jobUrl="https://acme.gupy.io/jobs/1?a=1&b=2")
        message = gupy.build_message("FLUTTER · REMOTO", job, None)
        self.assertIn("Dev Flutter &lt;Pleno&gt; &amp; Mobile", message)
        self.assertIn("href='https://acme.gupy.io/jobs/1?a=1&amp;b=2'", message)


class SearchJobsTest(unittest.TestCase):

    FILTERS = [{"nome": "FLUTTER · REMOTO",
                "params": {"workplaceType": "remote", "jobName": "flutter", "limit": 15}}]

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE vagas_enviadas (link TEXT PRIMARY KEY, data_publicacao TEXT, titulo TEXT)")
        self.send = mock.Mock()

    def run_with(self, responses, errors=None, filters=None):
        with mock.patch.object(gupy.requests, "get", side_effect=responses) as get:
            gupy.search_jobs(self.conn, self.cursor, filters=filters or self.FILTERS, max_age_days=10,
                             user_agent="UA", check=main.filtros_basicos, send=self.send, errors=errors)
        return get

    def sent_links(self):
        return [c.args[2] for c in self.send.call_args_list]

    def test_calls_the_job_search_endpoint_with_the_filter_params(self):
        get = self.run_with([response([])])
        self.assertEqual(get.call_args.args[0], "https://portal.gupy.io/api/job-search/jobs")
        self.assertEqual(get.call_args.kwargs["params"],
                         {"workplaceType": "remote", "jobName": "flutter", "limit": 15, "offset": 0})
        self.assertEqual(get.call_args.kwargs["headers"]["User-Agent"], "UA")

    def test_does_not_change_the_configured_params(self):
        self.run_with([response([])])
        self.assertNotIn("offset", self.FILTERS[0]["params"])

    def test_sends_new_job(self):
        job = api_job(published=utc_iso(days_ago=1))
        self.run_with([response([job]), response([])])
        link, title, company, date, message, source = self.send.call_args.args[2:]
        self.assertEqual((link, title, company, source),
                         ("https://acme.gupy.io/jobs/1", "Desenvolvedor Flutter Pleno", "Acme", "GUPY"))
        self.assertEqual(date, gupy._published(job).strftime("%d/%m/%Y"))
        self.assertIn("Desenvolvedor Flutter Pleno", message)

    def test_job_without_date_is_sent_as_undated(self):
        job = api_job()
        del job["publishedDate"]
        self.run_with([response([job]), response([])])
        self.assertEqual(self.send.call_args.args[5], "Sem data")

    def test_pages_advance_by_the_filter_limit(self):
        get = self.run_with([response([api_job(1)]), response([api_job(2)]), response([])])
        self.assertEqual([c.kwargs["params"]["offset"] for c in get.call_args_list], [0, 15, 30])
        self.assertEqual(self.sent_links(), ["https://acme.gupy.io/jobs/1", "https://acme.gupy.io/jobs/2"])

    def test_default_limit_is_ten(self):
        filters = [{"nome": "FLUTTER", "params": {"jobName": "flutter"}}]
        get = self.run_with([response([api_job(1)]), response([])], filters=filters)
        self.assertEqual([c.kwargs["params"]["offset"] for c in get.call_args_list], [0, 10])

    def test_stops_after_the_last_page(self):
        get = self.run_with([response([api_job(n)]) for n in range(1, gupy.MAX_PAGES + 2)])
        self.assertEqual(get.call_count, gupy.MAX_PAGES)

    def test_skips_job_without_link(self):
        self.run_with([response([api_job(jobUrl="")]), response([])])
        self.send.assert_not_called()

    def test_skips_title_without_required_term(self):
        self.run_with([response([api_job(name="UX Writer")]), response([])])
        self.send.assert_not_called()

    def test_skips_jobs_from_other_countries(self):
        jobs = [api_job(1, country="Portugal"), api_job(2, country="Brasil"), api_job(3)]
        self.run_with([response(jobs), response([])])
        self.assertEqual(self.sent_links(), ["https://acme.gupy.io/jobs/2", "https://acme.gupy.io/jobs/3"])

    def test_skips_already_sent(self):
        self.cursor.execute("INSERT INTO vagas_enviadas VALUES ('https://acme.gupy.io/jobs/1', '', '')")
        self.run_with([response([api_job(1), api_job(2)]), response([])])
        self.assertEqual(self.sent_links(), ["https://acme.gupy.io/jobs/2"])

    def test_old_job_stops_the_search(self):
        jobs = [api_job(1), api_job(2, published=utc_iso(days_ago=30)), api_job(3)]
        get = self.run_with([response(jobs), response([api_job(4)])])
        self.assertEqual(self.sent_links(), ["https://acme.gupy.io/jobs/1"])
        self.assertEqual(get.call_count, 1)

    def test_many_already_sent_in_a_row_stops_the_search(self):
        jobs = [api_job(n) for n in range(1, gupy.MAX_SEEN_IN_A_ROW + 2)]
        for job in jobs:
            self.cursor.execute("INSERT INTO vagas_enviadas VALUES (?, '', '')", (job["jobUrl"],))
        get = self.run_with([response(jobs), response([api_job(99)])])
        self.send.assert_not_called()
        self.assertEqual(get.call_count, 1)

    def test_a_new_job_resets_the_already_sent_count(self):
        seen = [api_job(n) for n in range(1, gupy.MAX_SEEN_IN_A_ROW)]
        for job in seen:
            self.cursor.execute("INSERT INTO vagas_enviadas VALUES (?, '', '')", (job["jobUrl"],))
        get = self.run_with([response(seen + [api_job(50)]), response(seen), response([])])
        self.assertEqual(self.sent_links(), ["https://acme.gupy.io/jobs/50"])
        self.assertEqual(get.call_count, 3)

    def test_http_error_is_noted(self):
        errors = []
        self.run_with([response(status=404)], errors=errors)
        self.assertEqual(errors, ["FLUTTER · REMOTO: a Gupy recusou a busca (404)."])

    def test_invalid_json_is_noted_as_unreadable(self):
        errors = []
        self.run_with([invalid_json_response()], errors=errors)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("FLUTTER · REMOTO: erro ao ler as vagas ("))

    def test_network_error_is_noted_as_no_answer(self):
        errors = []
        self.run_with([requests.Timeout("slow")], errors=errors)
        self.assertEqual(errors, ["FLUTTER · REMOTO: o site não respondeu."])

    def test_a_failed_filter_does_not_stop_the_next(self):
        filters = [{"nome": "A", "params": {"jobName": "flutter"}},
                   {"nome": "B", "params": {"jobName": "flutter"}}]
        errors = []
        self.run_with([response(status=500), response([api_job()]), response([])],
                      errors=errors, filters=filters)
        self.assertEqual(errors, ["A: a Gupy recusou a busca (500)."])
        self.assertEqual(self.sent_links(), ["https://acme.gupy.io/jobs/1"])


if __name__ == "__main__":
    unittest.main()
