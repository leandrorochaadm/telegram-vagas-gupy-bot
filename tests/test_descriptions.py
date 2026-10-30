"""Tests for the description queue on send and the backfill of old jobs (main.py).

Run: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main  # noqa: E402
from fake_store import FakeStore  # noqa: E402
from store import Collection, DescriptionStatus, Source, StoreError, StoreFailure  # noqa: E402

GUPY_LINK = "https://acme.gupy.io/job/abc?jobBoardSource=gupy_portal"
LINKEDIN_LINK = "https://br.linkedin.com/jobs/view/dev-flutter-123"
POST_LINK = "https://www.linkedin.com/posts/fulano_flutter-activity-456"


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


if __name__ == "__main__":
    unittest.main()
