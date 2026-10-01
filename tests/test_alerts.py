"""Unit tests for the Telegram delivery and the single error alert in main.py.

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

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def response(status=200, json_data=None):
    resp = mock.Mock(status_code=status, text="")
    resp.json.return_value = json_data or {}
    return resp


class AlertTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("CREATE TABLE vagas_enviadas (link TEXT PRIMARY KEY, data_publicacao TEXT, titulo TEXT)")
        for state in (main._enviados_sessao, main._falhas_envio, main._avisos):
            state.clear()
            self.addCleanup(state.clear)
        patches = [
            mock.patch.object(main, "enviar_telegram", return_value=True),
            mock.patch.object(main.time, "sleep"),
        ]
        self.telegram, self.sleep = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def tearDown(self):
        self.conn.close()

    def flush(self, now=None):
        self.telegram.reset_mock()
        main.enviar_avisos(self.conn, self.cursor, agora=now)

    def pending(self):
        return self.cursor.execute("SELECT fonte, texto FROM avisos_pendentes").fetchall()


class EnviarTelegramTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(main.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, *responses):
        with mock.patch.object(main.requests, "post", side_effect=list(responses)) as post:
            result = main.enviar_telegram("msg")
        return result, post

    def test_waits_what_telegram_asks_and_retries_once(self):
        limited = response(429, {"parameters": {"retry_after": 7}})
        result, post = self.post(limited, response(200))
        self.assertTrue(result)
        self.assertEqual(post.call_count, 2)
        self.sleep.assert_called_once_with(7)

    def test_gives_up_when_the_wait_is_too_long(self):
        result, post = self.post(response(429, {"parameters": {"retry_after": main.MAX_ESPERA_TELEGRAM + 1}}))
        self.assertFalse(result)
        self.assertEqual(post.call_count, 1)

    def test_other_refusals_are_not_retried(self):
        result, post = self.post(response(400))
        self.assertFalse(result)
        self.assertEqual(post.call_count, 1)

    def test_network_error_returns_false(self):
        result, _ = self.post(main.requests.ConnectionError("down"))
        self.assertFalse(result)


class RegistrarEEnviarTest(AlertTestCase):
    def send(self, link="https://x/1", title="Dev Flutter"):
        main.registrar_e_enviar(self.conn, self.cursor, link, title, "Acme", "29/09/2026", "msg", "GUPY")

    def saved_links(self):
        return [row[0] for row in self.cursor.execute("SELECT link FROM vagas_enviadas")]

    def test_delivered_job_is_saved(self):
        self.send()
        self.assertEqual(self.saved_links(), ["https://x/1"])
        self.sleep.assert_called_once_with(main.PAUSA_ENTRE_VAGAS)

    def test_same_job_from_another_source_is_not_sent_on_the_next_run(self):
        self.send("https://gupy/1")
        self.send("https://linkedin/1")  # same title and company, other source
        self.assertEqual(self.telegram.call_count, 1)
        self.assertCountEqual(self.saved_links(), ["https://gupy/1", "https://linkedin/1"])

        main._enviados_sessao.clear()  # next run
        for link in ("https://gupy/1", "https://linkedin/1"):
            if not main.ja_enviada(self.cursor, link):
                self.send(link)
        self.assertEqual(self.telegram.call_count, 1)

    def test_refused_job_is_not_saved_so_next_run_retries(self):
        self.telegram.return_value = False
        self.send()
        self.assertEqual(self.saved_links(), [])
        self.assertEqual(main._falhas_envio, ["Dev Flutter (GUPY)"])

    def test_refused_jobs_go_into_the_alert(self):
        self.telegram.return_value = False
        self.send("https://x/1", "Dev Flutter <Pleno>")
        self.send("https://x/2", "Dev Flutter Sênior")
        main.anotar_falhas_envio()
        self.telegram.return_value = True
        self.flush()
        alert = self.telegram.call_args.args[0]
        self.assertIn("2 vagas não chegaram ao grupo", alert)
        self.assertIn("Dev Flutter &lt;Pleno&gt; (GUPY)", alert)


class EnviarAvisosTest(AlertTestCase):
    def test_every_source_goes_in_one_message(self):
        main.anotar_avisos("GUPY", ["FLUTTER: a Gupy recusou a busca (403)."])
        main.anotar_avisos("WEB", ["FLUTTER: a Brave não respondeu."])
        self.flush()
        self.telegram.assert_called_once()
        alert = self.telegram.call_args.args[0]
        self.assertIn("<b>GUPY</b>\n• FLUTTER: a Gupy recusou a busca (403).", alert)
        self.assertIn("<b>WEB</b>\n• FLUTTER: a Brave não respondeu.", alert)
        self.assertEqual(self.pending(), [])

    def test_nothing_is_sent_without_problems(self):
        self.flush()
        self.telegram.assert_not_called()

    def test_waits_the_interval_and_sends_everything_together(self):
        main.anotar_avisos("GUPY", ["erro 1"])
        self.flush(NOW)
        main.anotar_avisos("LINKEDIN", ["erro 2"])
        self.flush(NOW + timedelta(minutes=main.INTERVALO_AVISOS_MIN - 1))
        self.telegram.assert_not_called()
        self.assertEqual(self.pending(), [("LINKEDIN", "erro 2")])

        main.anotar_avisos("WEB", ["erro 3"])
        self.flush(NOW + timedelta(minutes=main.INTERVALO_AVISOS_MIN))
        alert = self.telegram.call_args.args[0]
        self.assertIn("erro 2", alert)
        self.assertIn("erro 3", alert)
        self.assertEqual(self.pending(), [])

    def test_failed_alert_is_kept_for_the_next_one(self):
        self.telegram.return_value = False
        main.anotar_avisos("GUPY", ["erro 1"])
        self.flush(NOW)
        self.assertEqual(self.pending(), [("GUPY", "erro 1")])
        self.telegram.return_value = True
        self.flush(NOW + timedelta(minutes=1))  # nothing was sent, so no interval to wait
        self.assertIn("erro 1", self.telegram.call_args.args[0])

    def test_interval_is_measured_in_utc(self):
        main.anotar_avisos("GUPY", ["erro 1"])
        self.flush()  # real clock, as in a run
        sent_at = self.cursor.execute("SELECT enviado_em FROM avisos_enviados").fetchone()[0]
        self.assertEqual(datetime.fromisoformat(sent_at).utcoffset(), timedelta(0))

    def test_problems_stay_in_memory_when_the_database_fails(self):
        main.anotar_avisos("GUPY", ["erro 1"])
        conn = mock.Mock(commit=mock.Mock(side_effect=sqlite3.OperationalError("database is locked")))
        with self.assertRaises(sqlite3.OperationalError):
            main.enviar_avisos(conn, self.cursor, agora=NOW)
        self.assertEqual(main._avisos, [("GUPY", "erro 1")])

    def test_repeated_problem_is_counted_not_repeated(self):
        main.anotar_avisos("LINKEDIN", ["recusou (429)."] * 3)
        self.flush()
        alert = self.telegram.call_args.args[0]
        self.assertEqual(alert.count("recusou (429)."), 1)
        self.assertIn("recusou (429). (3x)", alert)

    def test_long_alert_is_cut_before_telegram_limit(self):
        main.anotar_avisos("INHIRE", [f"empresa {i} " + "x" * 100 for i in range(100)])
        self.flush()
        alert = self.telegram.call_args.args[0]
        self.assertLessEqual(len(alert), main.TAMANHO_MAX_AVISO + 50)
        self.assertRegex(alert, r"… e mais \d+ problemas\.$")


class SourceErrorsTest(AlertTestCase):
    def test_gupy_http_error_is_noted(self):
        with mock.patch.object(main, "FILTROS_GUPY", [{"nome": "FLUTTER", "params": {}}]), \
             mock.patch.object(main.requests, "get", return_value=response(403)):
            main.buscar_vagas_gupy(self.conn, self.cursor)
        self.assertEqual(main._avisos, [("GUPY", "FLUTTER: a Gupy recusou a busca (403).")])

    def test_network_error_is_noted_as_no_answer(self):
        with mock.patch.object(main, "FILTROS_GUPY", [{"nome": "FLUTTER", "params": {}}]), \
             mock.patch.object(main.requests, "get", side_effect=main.requests.Timeout("slow")):
            main.buscar_vagas_gupy(self.conn, self.cursor)
        self.assertEqual(main._avisos, [("GUPY", "FLUTTER: o site não respondeu.")])

    def test_gupy_title_is_escaped(self):
        vaga = {"jobUrl": "https://acme.gupy.io/jobs/1?a=1&b=2", "name": "Dev Flutter <Pleno> & Mobile",
                "careerPageName": "Acme", "publishedDate": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
        with mock.patch.object(main, "FILTROS_GUPY", [{"nome": "FLUTTER", "params": {}}]), \
             mock.patch.object(main.requests, "get",
                               side_effect=[response(json_data={"data": [vaga]}), response(json_data={"data": []})]):
            main.buscar_vagas_gupy(self.conn, self.cursor)
        message = self.telegram.call_args.args[0]
        self.assertIn("Dev Flutter &lt;Pleno&gt; &amp; Mobile", message)
        self.assertIn("href='https://acme.gupy.io/jobs/1?a=1&amp;b=2'", message)

    def test_gupy_pages_advance_by_the_configured_limit(self):
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        page = lambda n: response(json_data={"data": [
            {"jobUrl": f"https://acme.gupy.io/jobs/{n}", "name": f"Dev Flutter {n}", "careerPageName": "Acme",
             "publishedDate": now}]})
        with mock.patch.object(main, "FILTROS_GUPY", [{"nome": "FLUTTER", "params": {"limit": 15}}]), \
             mock.patch.object(main.requests, "get",
                               side_effect=[page(1), page(2), response(json_data={"data": []})]) as get:
            main.buscar_vagas_gupy(self.conn, self.cursor)
        offsets = [call.kwargs["params"]["offset"] for call in get.call_args_list]
        self.assertEqual(offsets, [0, 15, 30])

    def test_linkedin_waits_and_retries_once_when_limited(self):
        with mock.patch.object(main, "FILTROS_LINKEDIN", [{"nome": "FLUTTER", "params": {}}]), \
             mock.patch.object(main, "PAGINAS_LINKEDIN", 1), \
             mock.patch.object(main.requests, "get", side_effect=[response(429), response(200)]) as get:
            main.buscar_vagas_linkedin(self.conn, self.cursor)
        self.assertEqual(get.call_count, 2)
        self.sleep.assert_any_call(main.PAUSA_NOVA_TENTATIVA_LINKEDIN)
        self.assertEqual(main._avisos, [])

    def test_linkedin_still_limited_after_retry_is_noted(self):
        with mock.patch.object(main, "FILTROS_LINKEDIN", [{"nome": "FLUTTER", "params": {}}]), \
             mock.patch.object(main.requests, "get", return_value=response(429)):
            main.buscar_vagas_linkedin(self.conn, self.cursor)
        self.assertEqual(main._avisos, [("LINKEDIN", "FLUTTER: o LinkedIn recusou a busca (429).")])

    def test_linkedin_posts_brave_refusal_is_noted(self):
        def refuse(api_key, query, pages, errors):
            errors.append("código 429")
            return []
        with mock.patch.object(main, "BRAVE_API_KEY", "key"), \
             mock.patch.object(main, "LINKEDIN_LI_AT", None), \
             mock.patch.object(main, "FILTROS_POSTS_LINKEDIN", [{"nome": "POSTS", "termo": "flutter"}]), \
             mock.patch.object(main.linkedin_posts, "search_posts", side_effect=refuse):
            main.buscar_posts_linkedin(self.conn, self.cursor)
        self.assertEqual(main._avisos, [("LINKEDIN PUBLICAÇÕES", "POSTS: a Brave recusou a busca (código 429).")])

    def test_web_errors_are_noted(self):
        def fail(conn, cursor, **kw):
            kw["errors"].append("FLUTTER: a Brave não respondeu.")
        with mock.patch.object(main.web_search, "search_jobs", side_effect=fail):
            main.buscar_vagas_web(self.conn, self.cursor)
        self.assertEqual(main._avisos, [("WEB", "FLUTTER: a Brave não respondeu.")])

    def test_unexpected_error_is_noted_and_does_not_raise(self):
        with mock.patch.object(main, "FILTROS_GUPY", None):
            main.buscar_vagas_gupy(self.conn, self.cursor)
        self.assertIn("A varredura parou no meio por um erro inesperado", main._avisos[0][1])


class MainTest(AlertTestCase):
    def run_main(self, **patches):
        sources = ["buscar_vagas_gupy", "buscar_vagas_programathor", "buscar_vagas_linkedin",
                   "buscar_posts_linkedin", "buscar_vagas_inhire", "buscar_vagas_solides", "buscar_vagas_web"]
        with mock.patch.multiple(main, TOKEN="t", CHAT_ID="c", **{name: mock.DEFAULT for name in sources}), \
             mock.patch.multiple(main, **patches):
            main.main()

    def test_database_error_is_alerted_directly_and_stops_the_run(self):
        with mock.patch.object(main, "buscar_vagas_gupy") as gupy:
            self.run_main(iniciar_banco=mock.Mock(side_effect=sqlite3.OperationalError("disk I/O error")))
        gupy.assert_not_called()
        self.assertIn("disk I/O error", self.telegram.call_args.args[0])

    def test_alert_storage_error_sends_this_run_problems_directly(self):
        main.anotar_avisos("GUPY", ["erro 1"])
        self.run_main(iniciar_banco=mock.Mock(return_value=(self.conn, self.cursor)),
                      enviar_avisos=mock.Mock(side_effect=sqlite3.OperationalError("database is locked")))
        alert = self.telegram.call_args.args[0]
        self.assertIn("erro 1", alert)
        self.assertIn("database is locked", alert)

    def test_missing_beautifulsoup_is_alerted_once(self):
        self.run_main(BS4_DISPONIVEL=False, iniciar_banco=mock.Mock(return_value=(self.conn, self.cursor)),
                      enviar_avisos=mock.DEFAULT)
        self.assertEqual(main._avisos, [("BOT", main.AVISO_SEM_BS4)])


if __name__ == "__main__":
    unittest.main()
