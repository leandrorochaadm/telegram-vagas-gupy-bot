"""Unit tests for the InHire job scan in main.py (tenant removal + Telegram error alert).

Run: python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inhire_discovery  # noqa: E402
import main  # noqa: E402


def response(status=200, text="", json_data=None):
    resp = mock.Mock(status_code=status, text=text)
    resp.json.return_value = json_data or {}
    return resp


def jobs_page(*jobs, tenant_name="Acme"):
    return response(json_data={"tenantName": tenant_name, "jobsPage": list(jobs)})


def job(title="Desenvolvedor Flutter Pleno", workplace="remote", job_id="1"):
    return {"status": "published", "displayName": title, "workplaceType": workplace,
            "jobId": job_id, "location": "Brasil"}


class BuscarVagasInhireTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE vagas_enviadas (link TEXT PRIMARY KEY, data_publicacao TEXT, titulo TEXT)")
        inhire_discovery.init_tables(self.cursor)
        self.cursor.executemany("INSERT INTO inhire_tenants VALUES (?, '2026-09-29')", [("acme",), ("beta",)])
        patches = [
            # Discovery already covered in test_inhire_discovery: return what is in the db
            mock.patch.object(main.inhire_discovery, "discover",
                              side_effect=lambda conn, cursor, **kw: inhire_discovery.load_tenants(cursor)),
            mock.patch.object(main, "enviar_telegram"),
            mock.patch.object(main, "registrar_e_enviar"),
            mock.patch.object(main, "filtros_basicos", return_value=(False, "")),
            mock.patch.object(main.time, "sleep"),
        ]
        self.discover, self.telegram, self.send, _, _ = [p.start() for p in patches]
        main._avisos.clear()
        self.addCleanup(main._avisos.clear)
        for p in patches:
            self.addCleanup(p.stop)

    def tearDown(self):
        self.conn.close()

    def scan(self, *responses):
        with mock.patch.object(main.requests, "get", side_effect=list(responses)):
            main.buscar_vagas_inhire(self.conn, self.cursor)
        main.enviar_avisos(self.conn, self.cursor)

    def test_sends_matching_job_without_alert(self):
        self.scan(jobs_page(job()), jobs_page(job(title="Designer")))
        self.send.assert_called_once()
        self.assertIn("acme.inhire.app/vagas/1/", self.send.call_args.args[2])
        self.telegram.assert_not_called()

    def test_removes_tenant_only_when_inhire_says_it_does_not_exist(self):
        self.scan(response(404, '{"message":"Tenant not found"}'), jobs_page())
        self.assertEqual(inhire_discovery.load_tenants(self.cursor), ["beta"])
        self.telegram.assert_not_called()

    def test_generic_404_keeps_tenants_and_alerts(self):
        self.scan(*[response(404, "Not Found")] * 4)
        self.assertEqual(inhire_discovery.load_tenants(self.cursor), ["acme", "beta"])
        self.telegram.assert_called_once()
        alert = self.telegram.call_args.args[0]
        self.assertIn("2 de 2 empresas não responderam", alert)
        self.assertIn("acme (404)", alert)

    def test_network_error_is_alerted(self):
        self.scan(main.requests.ConnectionError("down"), main.requests.ConnectionError("down"), jobs_page())
        self.assertIn("acme (sem resposta)", self.telegram.call_args.args[0])

    def test_unreadable_answer_is_alerted_as_read_error(self):
        broken = response()
        broken.json.side_effect = ValueError("not json")
        self.scan(broken, jobs_page())
        self.assertIn("acme (erro ao ler)", self.telegram.call_args.args[0])

    def test_many_missing_tenants_are_kept_and_alerted(self):
        not_found = response(404, '{"message":"Tenant not found"}')
        with mock.patch.object(main, "MAX_REMOCOES_INHIRE", 1):
            self.scan(not_found, not_found)
        self.assertEqual(inhire_discovery.load_tenants(self.cursor), ["acme", "beta"])
        alert = self.telegram.call_args.args[0]
        self.assertIn("2 de 2 empresas não existem", alert)
        self.assertIn("nenhuma foi apagada", alert)

    def test_missing_tenants_up_to_the_limit_are_removed(self):
        not_found = response(404, '{"message":"Tenant not found"}')
        with mock.patch.object(main, "MAX_REMOCOES_INHIRE", 2):
            self.scan(not_found, not_found)
        self.assertEqual(inhire_discovery.load_tenants(self.cursor), [])
        self.telegram.assert_not_called()

    def test_alert_lists_at_most_the_limit(self):
        with mock.patch.object(main, "LIMITE_ITENS_NO_AVISO", 1):
            self.scan(*[response(500)] * 4)
        alert = self.telegram.call_args.args[0]
        self.assertIn("acme (500) e mais 1", alert)
        self.assertNotIn("beta", alert)

    def test_discovery_errors_are_alerted_in_one_message(self):
        def discover(conn, cursor, **kw):
            kw["errors"].append('O Yahoo não respondeu à busca "<q>".')
            return ["acme"]
        self.discover.side_effect = discover
        self.scan(response(500), response(500))
        self.telegram.assert_called_once()
        alert = self.telegram.call_args.args[0]
        self.assertIn("&lt;q&gt;", alert)  # escaped for Telegram's HTML mode
        self.assertIn("1 de 1 empresas não responderam", alert)

    def test_retries_once_before_counting_a_failure(self):
        self.scan(response(400), jobs_page(job()), main.requests.Timeout("slow"), jobs_page())
        self.send.assert_called_once()
        self.telegram.assert_not_called()

    def test_tenant_not_found_is_not_retried(self):
        with mock.patch.object(main.requests, "get",
                               side_effect=[response(404, '{"message":"Tenant not found"}'), jobs_page()]) as get:
            main.buscar_vagas_inhire(self.conn, self.cursor)
        self.assertEqual(get.call_count, 2)

    def test_unexpected_error_is_alerted_and_does_not_raise(self):
        self.discover.side_effect = sqlite3.OperationalError("database is locked")
        main.buscar_vagas_inhire(self.conn, self.cursor)
        main.enviar_avisos(self.conn, self.cursor)
        self.assertIn("database is locked", self.telegram.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
