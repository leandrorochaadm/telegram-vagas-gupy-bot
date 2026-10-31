"""Tests for the description queue on send and the backfill of old jobs (main.py).

Run: python -m unittest discover -s tests -v
"""
import itertools
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main  # noqa: E402
from job_details import DetailsError, DetailsFailure, JobDetails  # noqa: E402
from fake_store import FakeStore  # noqa: E402
from store import Collection, DescriptionStatus, Source, StoreError, StoreFailure  # noqa: E402

GUPY_LINK = "https://acme.gupy.io/job/abc?jobBoardSource=gupy_portal"
LINKEDIN_LINK = "https://br.linkedin.com/jobs/view/dev-flutter-123"
POST_LINK = "https://www.linkedin.com/posts/fulano_flutter-activity-456"
REMOTAR_LINK = "https://remotar.com.br/job/1"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


class DescriptionTestCase(unittest.TestCase):
    def setUp(self):
        self.db = FakeStore()
        for state in (main._enviados_sessao, main._falhas_envio, main._avisos):
            state.clear()
            self.addCleanup(state.clear)
        self.addCleanup(setattr, main, "_queue_jobs", False)
        patches = [
            mock.patch.object(main, "enviar_telegram", return_value=True),
            mock.patch.object(main.time, "sleep"),
        ]
        self.telegram, self.sleep = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def send(self, link, title="Dev Flutter", company="Acme", source=Source.GUPY, published="29/10/2026"):
        main.registrar_e_enviar(self.db, link, title, company, published, "msg", source)

    def run_main(self, *argv):
        """Run main.main(argv) with every source faked; (source fakes, store_alerts, enviar_avisos)."""
        names = [name for name, _ in main.SOURCES.values()]
        with mock.patch.multiple(main, TOKEN="t", CHAT_ID="c", **{name: mock.DEFAULT for name in names}) as fakes, \
             mock.patch.object(main, "from_env", return_value=self.db), \
             mock.patch.object(main, "store_alerts") as store, \
             mock.patch.object(main, "enviar_avisos") as send:
            main.main(list(argv))
        return fakes, store, send


class QueueOnSendTest(DescriptionTestCase):
    def test_sent_job_creates_pending_description(self):
        self.send(GUPY_LINK)
        row = self.db.descriptions[GUPY_LINK]
        self.assertEqual(row.status, DescriptionStatus.PENDING)
        self.assertEqual(row.attempts, 0)
        self.assertEqual(row.source, Source.GUPY)
        self.assertEqual(row.company, "Acme")
        self.assertEqual(row.published, "29/10/2026")

    def test_send_jobs_creates_pending_description(self):
        self.db.queue_job(GUPY_LINK, "Dev Flutter", "Acme", "29/10/2026", "msg", Source.GUPY)
        self.run_main("--send-jobs")
        self.assertEqual(self.db.descriptions[GUPY_LINK].status, DescriptionStatus.PENDING)

    def test_linkedin_post_is_saved_without_request(self):
        with mock.patch.object(main.job_details, "fetch_details") as fetch:
            self.send(POST_LINK, title="Vaga Flutter", source=Source.LINKEDIN_POSTS)
        row = self.db.descriptions[POST_LINK]
        self.assertEqual(row.status, DescriptionStatus.SAVED)
        self.assertEqual(row.description, "Vaga Flutter")
        self.assertEqual(self.db.pending_descriptions(), [])
        fetch.assert_not_called()

    def test_post_link_from_other_source_is_saved(self):
        self.send(POST_LINK, title="Vaga Flutter", source=Source.WEB)
        row = self.db.descriptions[POST_LINK]
        self.assertEqual(row.status, DescriptionStatus.SAVED)
        self.assertEqual(row.description, "Vaga Flutter")
        self.assertEqual(row.source, Source.WEB)

    def test_store_failure_on_queue_alerts_and_keeps_job(self):
        with mock.patch.object(self.db, "add_job_description",
                               side_effect=StoreError("down", StoreFailure.UNAVAILABLE)):
            self.send(GUPY_LINK)
        self.assertTrue(self.db.is_sent(GUPY_LINK))
        self.assertEqual(main._avisos, [
            (Source.BOT, main.ALERT_DESCRIPTION_NOT_QUEUED.format(title="Dev Flutter"))])
        self.sleep.assert_called_once_with(main.PAUSA_ENTRE_VAGAS)

    def test_mark_sent_failure_creates_no_description(self):
        with mock.patch.object(self.db, "mark_sent", side_effect=StoreError("down", StoreFailure.UNAVAILABLE)):
            self.send(GUPY_LINK)
        self.assertEqual(self.db.count(Collection.JOB_DESCRIPTIONS), 0)
        self.assertEqual(len(main._avisos), 1)
        self.assertIn("não ficou registrada", main._avisos[0][1])

    def test_refused_job_creates_no_description(self):
        self.telegram.return_value = False
        self.send(GUPY_LINK)
        self.assertEqual(self.db.count(Collection.JOB_DESCRIPTIONS), 0)

    def test_queued_job_creates_no_description(self):
        main._queue_jobs = True
        self.send(GUPY_LINK)
        self.assertEqual(self.db.count(Collection.JOB_DESCRIPTIONS), 0)

    def test_session_duplicate_creates_no_description(self):
        self.send(GUPY_LINK)
        self.send(LINKEDIN_LINK, source=Source.LINKEDIN)
        self.assertEqual(list(self.db.descriptions), [GUPY_LINK])


class BackfillTest(DescriptionTestCase):
    def test_backfill_creates_missing_only_and_is_idempotent(self):
        self.db.mark_sent(GUPY_LINK, "29/10/2026", "Dev Flutter")
        self.db.mark_sent(POST_LINK, "28/10/2026", "Vaga no post")
        self.db.mark_sent(LINKEDIN_LINK, "27/10/2026", "Dev Dart")
        self.db.add_job_description(LINKEDIN_LINK, "Dev Dart", "Beta", Source.LINKEDIN, "27/10/2026")
        with mock.patch.object(main.job_details, "fetch_details") as fetch:
            self.assertEqual(main.recuperar_descricoes_antigas(self.db), 2)
            self.assertEqual(main.recuperar_descricoes_antigas(self.db), 0)
        fetch.assert_not_called()
        gupy = self.db.descriptions[GUPY_LINK]
        self.assertEqual((gupy.status, gupy.company, gupy.source), (DescriptionStatus.PENDING, "", Source.GUPY))
        post = self.db.descriptions[POST_LINK]
        self.assertEqual((post.status, post.description), (DescriptionStatus.SAVED, "Vaga no post"))
        self.assertEqual(self.db.descriptions[LINKEDIN_LINK].company, "Beta")
        self.assertEqual(self.db.count(Collection.JOB_DESCRIPTIONS), 3)

    def test_backfill_skips_same_title_and_date(self):
        self.db.mark_sent(GUPY_LINK, "29/10/2026", "Dev Flutter")
        self.db.mark_sent(LINKEDIN_LINK, "29/10/2026", "dev  flutter!")
        self.assertEqual(main.recuperar_descricoes_antigas(self.db), 1)
        self.assertEqual(self.db.count(Collection.JOB_DESCRIPTIONS), 1)
        self.assertEqual(main.recuperar_descricoes_antigas(self.db), 0)
        self.assertEqual(self.db.count(Collection.JOB_DESCRIPTIONS), 1)

    def test_backfill_keeps_same_title_other_date(self):
        self.db.mark_sent(GUPY_LINK, "29/10/2026", "Dev Flutter")
        self.db.mark_sent(LINKEDIN_LINK, "28/10/2026", "Dev Flutter")
        self.assertEqual(main.recuperar_descricoes_antigas(self.db), 2)

    def test_backfill_with_nothing_sent_creates_nothing(self):
        self.assertEqual(main.recuperar_descricoes_antigas(self.db), 0)
        self.assertEqual(self.db.count(Collection.JOB_DESCRIPTIONS), 0)

    def test_backfill_mode_only_stores_alerts(self):
        self.db.mark_sent(GUPY_LINK, "29/10/2026", "Dev Flutter")
        self.db.queue_job(LINKEDIN_LINK, "Outra", "Acme", "29/10/2026", "msg", Source.LINKEDIN)
        with mock.patch.object(main.job_details, "fetch_details") as fetch:
            fakes, store, send = self.run_main("--backfill-descriptions")
        fetch.assert_not_called()
        self.assertFalse(any(fake.called for fake in fakes.values()))
        store.assert_called_once_with(self.db)
        send.assert_not_called()
        self.assertIn(GUPY_LINK, self.db.descriptions)
        self.assertFalse(self.db.is_sent(LINKEDIN_LINK))

    def test_backfill_store_failure_is_alerted(self):
        with mock.patch.object(self.db, "description_links",
                               side_effect=StoreError("down", StoreFailure.UNAVAILABLE)):
            self.run_main("--backfill-descriptions")
        self.assertEqual(len(main._avisos), 1)
        self.assertEqual(main._avisos[0][0], Source.BOT)
        self.assertIn("Não foi possível recuperar as descrições", main._avisos[0][1])

    def test_backfill_is_exclusive_with_other_modes(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            main.parse_args(["--backfill-descriptions", "--send-jobs"])


class FetchDescriptionsTest(DescriptionTestCase):
    def pending(self, link, source=Source.GUPY, title="Dev Flutter", attempts=0, first_try_at=None):
        self.db.add_job_description(link, title, "Acme", source, "29/10/2026")
        row_id = self.db.descriptions[link].id
        if attempts > 0:
            self.db.mark_description_try(row_id, attempts, first_try_at or NOW)
        return row_id

    def fetch(self, details_by_link, agora=NOW, relogio=None):
        def side_effect(link, **kwargs):
            result = details_by_link[link]
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(main.job_details, "fetch_details", side_effect=side_effect) as fetch:
            self.pausa = mock.Mock()
            main.buscar_descricoes(self.db, agora, pausa=self.pausa, relogio=relogio or (lambda: 0.0))
        return fetch

    def test_sent_gupy_job_is_saved_with_text_and_type(self):
        self.send(GUPY_LINK)
        self.fetch({GUPY_LINK: JobDetails("Texto", "Pleno", "Efetivo")})
        row = self.db.descriptions[GUPY_LINK]
        self.assertEqual(row.status, DescriptionStatus.SAVED)
        self.assertEqual((row.description, row.seniority, row.employment_type), ("Texto", "Pleno", "Efetivo"))
        self.assertEqual(row.finished_at, NOW)

    def test_unreadable_page_counts_a_try(self):
        self.pending(GUPY_LINK)
        self.fetch({GUPY_LINK: DetailsError(DetailsFailure.UNREADABLE)})
        row = self.db.descriptions[GUPY_LINK]
        self.assertEqual((row.status, row.attempts), (DescriptionStatus.PENDING, 1))
        self.assertEqual((row.first_try_at, row.last_try_at), (NOW, NOW))

    def test_network_error_counts_a_try(self):
        self.pending(GUPY_LINK)
        self.fetch({GUPY_LINK: DetailsError(DetailsFailure.NETWORK)})
        self.assertEqual(self.db.descriptions[GUPY_LINK].attempts, 1)

    def test_server_error_counts_a_try(self):
        self.pending(GUPY_LINK)
        self.fetch({GUPY_LINK: DetailsError(DetailsFailure.REFUSED)})
        row = self.db.descriptions[GUPY_LINK]
        self.assertEqual((row.status, row.attempts), (DescriptionStatus.PENDING, 1))

    def test_second_failure_keeps_first_try_date(self):
        first = NOW - timedelta(days=1)
        self.pending(GUPY_LINK, attempts=1, first_try_at=first)
        self.fetch({GUPY_LINK: DetailsError(DetailsFailure.NETWORK)})
        row = self.db.descriptions[GUPY_LINK]
        self.assertEqual((row.attempts, row.first_try_at, row.last_try_at), (2, first, NOW))

    def test_closed_job_is_finished(self):
        self.pending(GUPY_LINK)
        self.fetch({GUPY_LINK: DetailsError(DetailsFailure.CLOSED)})
        row = self.db.descriptions[GUPY_LINK]
        self.assertEqual((row.status, row.finished_at), (DescriptionStatus.CLOSED, NOW))
        self.assertEqual(self.db.pending_descriptions(), [])

    def test_linkedin_block_skips_rest_of_linkedin(self):
        other = "https://br.linkedin.com/jobs/view/dev-dart-456"
        self.pending(LINKEDIN_LINK, source=Source.LINKEDIN)
        self.pending(other, source=Source.LINKEDIN)
        self.pending(GUPY_LINK)
        fetch = self.fetch({LINKEDIN_LINK: DetailsError(DetailsFailure.BLOCKED),
                            other: DetailsError(DetailsFailure.BLOCKED), GUPY_LINK: JobDetails("Texto")})
        called = [c.args[0] for c in fetch.call_args_list]
        self.assertEqual(len([link for link in called if "linkedin" in link]), 1)
        self.assertIn(GUPY_LINK, called)
        attempts = sorted(self.db.descriptions[link].attempts for link in (LINKEDIN_LINK, other))
        self.assertEqual(attempts, [0, 1])
        self.assertEqual(self.db.descriptions[GUPY_LINK].status, DescriptionStatus.SAVED)

    def test_pause_only_between_linkedin_requests(self):
        other = "https://br.linkedin.com/jobs/view/dev-dart-456"
        self.pending(LINKEDIN_LINK, source=Source.LINKEDIN)
        self.pending(other, source=Source.LINKEDIN)
        self.pending(GUPY_LINK)
        self.pending(REMOTAR_LINK, source=Source.REMOTAR)
        self.fetch({link: JobDetails("x") for link in (LINKEDIN_LINK, other, GUPY_LINK, REMOTAR_LINK)})
        self.pausa.assert_called_once_with(main.PAUSA_ENTRE_DESCRICOES_LINKEDIN)

    def test_gives_up_at_exactly_three_days_after_first_try(self):
        self.pending(GUPY_LINK, attempts=1, first_try_at=NOW - timedelta(days=3))
        fetch = self.fetch({})
        fetch.assert_not_called()
        self.assertEqual(self.db.descriptions[GUPY_LINK].status, DescriptionStatus.GAVE_UP)
        self.assertEqual(len(main._avisos), 1)
        self.assertEqual(main._avisos[0][0], Source.BOT)
        self.assertEqual(main._avisos[0][1],
                         "Fiquei sem a descrição destas vagas depois de 3 dias tentando: Dev Flutter.")

    def test_retries_just_before_three_days(self):
        self.pending(GUPY_LINK, attempts=1, first_try_at=NOW - timedelta(days=3) + timedelta(minutes=1))
        fetch = self.fetch({GUPY_LINK: JobDetails("Texto")})
        fetch.assert_called_once()
        self.assertEqual(self.db.descriptions[GUPY_LINK].status, DescriptionStatus.SAVED)

    def test_gave_up_titles_in_one_alert(self):
        old = NOW - timedelta(days=4)
        titles = [f"Vaga {i}" for i in range(main.LIMITE_ITENS_NO_AVISO + 2)]
        for i, title in enumerate(titles):
            self.pending(f"https://acme.gupy.io/job/{i}", title=title, attempts=1, first_try_at=old)
        self.fetch({})
        self.assertEqual(len(main._avisos), 1)
        self.assertIn(main.resumir_lista(titles), main._avisos[0][1])
        self.assertIn("e mais", main._avisos[0][1])

    def test_caps_requests_per_run(self):
        links = [f"https://acme.gupy.io/job/{i}" for i in range(45)]
        for link in links:
            self.pending(link)
        fetch = self.fetch({link: JobDetails("x") for link in links})
        self.assertEqual(fetch.call_count, main.MAX_DESCRICOES_POR_RODADA)
        self.assertEqual(len(self.db.pending_descriptions()), 5)
        self.assertTrue(all(r.attempts == 0 for r in self.db.pending_descriptions()))

    def test_failing_jobs_do_not_starve_fresh_ones(self):
        failing = [f"https://acme.com/vaga/{i}" for i in range(main.MAX_DESCRICOES_POR_RODADA)]
        for link in failing:
            self.pending(link, source=Source.WEB, attempts=5)
        fresh = "https://acme.gupy.io/job/new"
        self.pending(fresh)
        error = DetailsError("falhou", DetailsFailure.NETWORK)
        self.fetch({fresh: JobDetails("x"), **{link: error for link in failing}})
        self.assertEqual(self.db.descriptions[fresh].status, DescriptionStatus.SAVED)

    def test_give_up_does_not_count_in_cap(self):
        links = [f"https://acme.gupy.io/job/{i}" for i in range(main.MAX_DESCRICOES_POR_RODADA)]
        for link in links:
            self.pending(link)
        old = "https://acme.gupy.io/job/old"
        self.pending(old, title="Velha", attempts=1, first_try_at=NOW - timedelta(days=5))
        fetch = self.fetch({link: JobDetails("x") for link in links})
        self.assertEqual(fetch.call_count, main.MAX_DESCRICOES_POR_RODADA)
        self.assertEqual(self.db.descriptions[old].status, DescriptionStatus.GAVE_UP)
        self.assertEqual(len(main._avisos), 1)

    def test_never_tried_is_not_given_up(self):
        self.pending(GUPY_LINK)
        self.db.descriptions[GUPY_LINK].created = NOW - timedelta(days=10)
        fetch = self.fetch({GUPY_LINK: JobDetails("Texto")})
        fetch.assert_called_once()
        self.assertEqual(self.db.descriptions[GUPY_LINK].status, DescriptionStatus.SAVED)
        self.assertEqual(main._avisos, [])

    def test_time_budget_stops_without_counting_tries(self):
        links = [f"https://acme.gupy.io/job/{i}" for i in range(5)]
        for link in links:
            self.pending(link)
        old = "https://acme.gupy.io/job/old"
        self.pending(old, title="Velha", attempts=1, first_try_at=NOW - timedelta(days=5))
        relogio = mock.Mock(side_effect=itertools.chain([0, 10, 20], itertools.repeat(main.ORCAMENTO_DESCRICOES_S)))
        fetch = self.fetch({link: JobDetails("x") for link in links}, relogio=relogio)
        self.assertEqual(fetch.call_count, 2)
        left = self.db.pending_descriptions()
        self.assertEqual(len(left), 3)
        self.assertTrue(all(r.attempts == 0 for r in left))
        self.assertEqual(self.db.descriptions[old].status, DescriptionStatus.GAVE_UP)

    def test_no_pending_makes_no_request(self):
        fetch = self.fetch({})
        fetch.assert_not_called()
        self.assertEqual(main._avisos, [])

    def test_pending_post_link_is_saved_without_request(self):
        self.pending(POST_LINK, source=Source.LINKEDIN_POSTS, title="Vaga Flutter")
        fetch = self.fetch({})
        fetch.assert_not_called()
        row = self.db.descriptions[POST_LINK]
        self.assertEqual((row.status, row.description), (DescriptionStatus.SAVED, "Vaga Flutter"))

    def test_store_failure_on_fetch_is_alerted(self):
        with mock.patch.object(self.db, "pending_descriptions",
                               side_effect=StoreError("down", StoreFailure.UNAVAILABLE)):
            self.run_main("--fetch-descriptions")
        self.assertEqual(len(main._avisos), 1)
        self.assertEqual(main._avisos[0][0], Source.BOT)
        self.assertIn("Não foi possível buscar as descrições das vagas", main._avisos[0][1])


class FetchDescriptionsModeTest(DescriptionTestCase):
    def test_fetch_descriptions_requires_proxy(self):
        with mock.patch.dict(os.environ, {main.ENV_REQUIRE_SCRAPER_PROXY: "true"}), \
             mock.patch.object(main, "SCRAPER_PROXY", ""), \
             mock.patch.object(main, "buscar_descricoes") as fetch_step, \
             mock.patch.object(main.job_details, "fetch_details") as fetch, \
             self.assertRaises(SystemExit) as caught:
            self.run_main("--fetch-descriptions")
        self.assertEqual(caught.exception.code, main.EXIT_PROXY_DOWN)
        fetch_step.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(main._avisos, [])

    def test_fetch_mode_only_stores_alerts(self):
        self.db.queue_job(LINKEDIN_LINK, "Outra", "Acme", "29/10/2026", "msg", Source.LINKEDIN)
        with mock.patch.object(main, "buscar_descricoes") as fetch_step:
            fakes, store, send = self.run_main("--fetch-descriptions")
        fetch_step.assert_called_once_with(self.db)
        self.assertFalse(any(fake.called for fake in fakes.values()))
        store.assert_called_once_with(self.db)
        send.assert_not_called()
        self.assertFalse(self.db.is_sent(LINKEDIN_LINK))

    def test_fetch_is_exclusive_with_other_modes(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            main.parse_args(["--fetch-descriptions", "--send-jobs"])


if __name__ == "__main__":
    unittest.main()
