"""Unit tests for the Remotar job search (API parsing + sending pipeline).

Run: python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import remotar  # noqa: E402

NOW = datetime.fromisoformat("2026-09-30T12:00:00-03:00")


def api_job(job_id=1, title="Desenvolvedor Flutter Sênior", company="Acme", workplace="remote",
            created="2026-09-25T10:00:00.000-03:00", currency=None, low=None, high=None,
            tags=("🧓🏽 Sênior", "💼 CLT"), city="", state=""):
    """One job as returned by api.remotar.com.br/jobs."""
    return {
        "id": job_id, "title": title, "type": workplace, "createdAt": created,
        "city": city, "state": state, "companyDisplayName": None,
        "company": {"name": company},
        "jobSalary": {"from": low, "to": high, "currency": currency},
        "jobTags": [{"tag": {"name": t}} for t in tags],
    }


def response(jobs=(), status=200, last_page=1):
    body = {"meta": {"last_page": last_page}, "data": list(jobs)}
    return mock.Mock(status_code=status, json=mock.Mock(return_value=body))


def invalid_json_response():
    error = requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
    return mock.Mock(status_code=200, json=mock.Mock(side_effect=error))


class ParseJobTest(unittest.TestCase):

    def test_reads_fields(self):
        job = remotar.parse_job(api_job(job_id=42, city="Blumenau", state="SC"))
        self.assertEqual(job["link"], "https://remotar.com.br/job/42")
        self.assertEqual(job["company"], "Acme")
        self.assertEqual(job["location"], "Blumenau, SC")
        self.assertEqual(job["tags"], ["Sênior", "CLT"])
        self.assertEqual(job["salary"], "")
        self.assertFalse(job["foreign"])

    def test_drops_tags_that_repeat_workplace(self):
        job = remotar.parse_job(api_job(tags=("🌍 100% Remoto", "🧓🏽 Sênior")))
        self.assertEqual(job["tags"], ["Sênior"])

    def test_formats_salary_range(self):
        job = remotar.parse_job(api_job(currency="BRL", low=5000, high=8000))
        self.assertEqual(job["salary"], "R$ 5.000 a R$ 8.000")

    def test_formats_salary_ceiling_only(self):
        job = remotar.parse_job(api_job(currency="BRL", low=0, high=8000))
        self.assertEqual(job["salary"], "Até R$ 8.000")

    def test_ignores_unreadable_salary(self):
        job = remotar.parse_job(api_job(currency="BRL", low="a combinar", high=None))
        self.assertEqual(job["salary"], "")

    def test_dollar_salary_is_foreign(self):
        self.assertTrue(remotar.parse_job(api_job(currency="USD", low=0, high=0))["foreign"])

    def test_international_tag_is_foreign(self):
        self.assertTrue(remotar.parse_job(api_job(tags=("✈️ Vaga internacional",)))["foreign"])


class BuildMessageTest(unittest.TestCase):

    def test_escapes_html_and_omits_missing_salary(self):
        job = remotar.parse_job(api_job(title="Dev Flutter <Sênior>"))
        message = remotar.build_message("FLUTTER · REMOTO", job)
        self.assertIn("Dev Flutter &lt;Sênior&gt;", message)
        self.assertNotIn("Salário", message)
        self.assertIn("📍 <b>Local:</b> Remoto\n", message)
        self.assertIn("📅 <b>Publicada em:</b> 25/09/2026", message)


class SearchJobsTest(unittest.TestCase):

    FILTERS = [{"nome": "FLUTTER · REMOTO", "termo": "flutter", "modalidades": ["remote"]}]

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE vagas_enviadas (link TEXT PRIMARY KEY, data_publicacao TEXT, titulo TEXT)")
        self.send = mock.Mock()

    def run_with(self, responses, errors=None, ignore_foreign=True):
        with mock.patch.object(remotar.requests, "get", side_effect=responses) as get, \
             mock.patch.object(remotar.time, "sleep"):
            remotar.search_jobs(self.conn, self.cursor, filters=self.FILTERS, max_age_days=30,
                                ignore_foreign=ignore_foreign, user_agent="UA", check=main.filtros_basicos,
                                send=self.send, errors=errors, now=NOW)
        return get

    def sent_titles(self):
        return [c.args[3] for c in self.send.call_args_list]

    def test_sends_new_job(self):
        get = self.run_with([response([api_job()])])
        link, title, company, date = self.send.call_args.args[2:6]
        self.assertEqual((link, company, date), ("https://remotar.com.br/job/1", "Acme", "25/09/2026"))
        self.assertEqual(self.send.call_args.args[-1], "REMOTAR")
        self.assertEqual(get.call_args.kwargs["params"], {"search": "flutter", "active": "true", "page": 1})

    def test_skips_title_without_required_term(self):
        self.run_with([response([api_job(title="UX Writer")])])
        self.send.assert_not_called()

    def test_skips_other_workplaces(self):
        self.run_with([response([api_job(workplace="hybrid")])])
        self.send.assert_not_called()

    def test_skips_foreign_jobs(self):
        self.run_with([response([api_job(currency="USD")])])
        self.send.assert_not_called()

    def test_keeps_foreign_jobs_when_allowed(self):
        self.run_with([response([api_job(currency="USD")])], ignore_foreign=False)
        self.assertEqual(self.send.call_count, 1)

    def test_skips_already_sent(self):
        self.cursor.execute("INSERT INTO vagas_enviadas VALUES ('https://remotar.com.br/job/1', '', '')")
        self.run_with([response([api_job()])])
        self.send.assert_not_called()

    def test_skips_old_jobs_and_stops_paging(self):
        jobs = [api_job(job_id=1, title="Dev Flutter Novo"),
                api_job(job_id=2, title="Dev Flutter Antigo", created="2026-08-01T10:00:00.000-03:00")]
        get = self.run_with([response(jobs, last_page=3)])
        self.assertEqual(self.sent_titles(), ["Dev Flutter Novo"])
        self.assertEqual(get.call_count, 1)

    def test_reads_next_page_while_jobs_are_recent(self):
        get = self.run_with([response([api_job(job_id=1, title="Dev Flutter A")], last_page=2),
                             response([api_job(job_id=2, title="Dev Flutter B")], last_page=2)])
        self.assertEqual(self.sent_titles(), ["Dev Flutter A", "Dev Flutter B"])
        self.assertEqual(get.call_args.kwargs["params"]["page"], 2)

    def test_old_job_mid_page_does_not_stop_paging(self):
        jobs = [api_job(job_id=1, title="Dev Flutter A", created="2026-08-01T10:00:00.000-03:00"),
                api_job(job_id=2, title="Dev Flutter B")]
        get = self.run_with([response(jobs, last_page=2), response([api_job(job_id=3, title="Dev Flutter C")])])
        self.assertEqual(self.sent_titles(), ["Dev Flutter B", "Dev Flutter C"])
        self.assertEqual(get.call_count, 2)

    def test_skips_malformed_job_and_sends_the_rest(self):
        errors = []
        malformed = api_job(job_id=1, title="Dev Flutter Quebrada")
        del malformed["createdAt"]
        self.run_with([response([malformed, api_job(job_id=2, title="Dev Flutter Ok")])], errors=errors)
        self.assertEqual(self.sent_titles(), ["Dev Flutter Ok"])
        self.assertEqual(errors, [])

    def test_reports_when_no_job_is_readable(self):
        errors = []
        malformed = api_job()
        del malformed["createdAt"]
        self.run_with([response([malformed])], errors=errors)
        self.send.assert_not_called()
        self.assertEqual(errors, ["FLUTTER · REMOTO: o Remotar mudou o formato das vagas e o bot não conseguiu ler nenhuma."])

    def test_reports_invalid_json_as_read_error(self):
        errors = []
        self.run_with([invalid_json_response()], errors=errors)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("FLUTTER · REMOTO: erro ao ler as vagas"), errors[0])

    def test_reports_refused_search(self):
        errors = []
        self.run_with([response(status=403)], errors=errors)
        self.assertEqual(errors, ["FLUTTER · REMOTO: o Remotar recusou a busca (403)."])

    def test_reports_site_down(self):
        errors = []
        self.run_with(requests.ConnectionError("down"), errors=errors)
        self.assertEqual(errors, ["FLUTTER · REMOTO: o site não respondeu."])


if __name__ == "__main__":
    unittest.main()
