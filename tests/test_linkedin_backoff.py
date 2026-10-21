"""Unit tests for the LinkedIn 429 backoff in main.py.

Run: python -m unittest discover -s tests -v
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from test_alerts import AlertTestCase, response  # noqa: E402

PAUSES = main.PAUSAS_NOVA_TENTATIVA_LINKEDIN
FILTER = [{"nome": "FLUTTER", "params": {}}]
LIMIT_WARNING = ("LINKEDIN", "FLUTTER: o LinkedIn recusou a busca (429).")


def limited(retry_after=None):
    resp = response(429)
    resp.headers = {} if retry_after is None else {"Retry-After": retry_after}
    return resp


def linkedin_page(*titles):
    cards = "".join(
        '<div class="base-card"><h3 class="base-search-card__title">' + t + '</h3>'
        '<h4 class="base-search-card__subtitle">Acme</h4>'
        '<a href="https://br.linkedin.com/jobs/view/' + t.replace(" ", "-") + '">x</a>'
        '<time datetime="2026-10-20"></time></div>'
        for t in titles
    )
    resp = response(200)
    resp.text = cards
    return resp


class LinkedinBackoffTest(AlertTestCase):
    def run_linkedin(self, side_effect=None, return_value=None, pages=1, filters=FILTER):
        kwargs = {"side_effect": side_effect} if side_effect is not None else {"return_value": return_value}
        with mock.patch.object(main, "FILTROS_LINKEDIN", filters), \
             mock.patch.object(main, "PAGINAS_LINKEDIN", pages), \
             mock.patch.object(main.requests, "get", **kwargs) as get:
            main.buscar_vagas_linkedin(self.conn, self.cursor)
        return get

    def sleeps(self):
        return [call.args[0] for call in self.sleep.call_args_list]

    def test_linkedin_succeeds_on_last_retry(self):
        get = self.run_linkedin([response(429)] * 3 + [response(200)])
        self.assertEqual(get.call_count, 4)
        self.assertEqual(self.sleeps(), list(PAUSES))
        self.assertEqual(main._avisos, [])

    def test_linkedin_uses_retry_after_header(self):
        self.run_linkedin([limited("5"), response(200)])
        self.assertEqual(self.sleep.call_args_list, [mock.call(5)])
        self.assertEqual(main._avisos, [])

    def test_linkedin_caps_or_ignores_invalid_retry_after(self):
        cases = {"600": max(PAUSES), "Wed, 21 Oct 2026 07:28:00 GMT": PAUSES[0], "-3": PAUSES[0], "²": PAUSES[0]}
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.sleep.reset_mock()
                main._avisos.clear()
                self.run_linkedin([limited(header), response(200)])
                self.assertEqual(self.sleep.call_args_list, [mock.call(expected)])
                self.assertEqual(main._avisos, [])

    def test_linkedin_accepts_zero_retry_after(self):
        get = self.run_linkedin([limited("0"), response(200)])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(self.sleep.call_args_list, [mock.call(0)])

    def test_linkedin_other_error_is_not_retried(self):
        get = self.run_linkedin(return_value=response(500))
        self.assertEqual(get.call_count, 1)
        self.sleep.assert_not_called()
        self.assertEqual(main._avisos, [("LINKEDIN", "FLUTTER: o LinkedIn recusou a busca (500).")])

    def test_linkedin_pauses_between_pages(self):
        self.run_linkedin([linkedin_page("Dev Flutter " + str(n)) for n in (1, 2, 3)], pages=3)
        job, page = main.PAUSA_ENTRE_VAGAS, main.PAUSA_ENTRE_PAGINAS_LINKEDIN
        self.assertEqual(self.sleeps(), [job, page, job, page, job])
        self.assertEqual(self.telegram.call_count, 3)

    def test_linkedin_late_page_limit_keeps_earlier_jobs(self):
        pages = [linkedin_page("Dev Flutter 1"), linkedin_page("Dev Flutter 2")] + [response(429)] * 4
        get = self.run_linkedin(pages, pages=3)
        self.assertEqual(get.call_count, 6)
        self.assertEqual(self.cursor.execute("SELECT COUNT(*) FROM vagas_enviadas").fetchone()[0], 2)
        self.assertEqual(main._avisos, [LIMIT_WARNING])

    def test_linkedin_stops_retrying_when_total_pause_budget_is_spent(self):
        filters = FILTER + [{"nome": "MOBILE", "params": {}}]
        get = self.run_linkedin(return_value=limited(), filters=filters)
        self.assertEqual(get.call_count, 7)
        self.assertEqual(self.sleeps(), [*PAUSES, PAUSES[0], PAUSES[1]])
        self.assertLessEqual(sum(self.sleeps()), main.PAUSA_TOTAL_MAXIMA_LINKEDIN)
        self.assertEqual(len(main._avisos), 2)

    def test_linkedin_pause_equal_to_budget_still_waits(self):
        with mock.patch.object(main, "PAUSA_TOTAL_MAXIMA_LINKEDIN", PAUSES[0] + PAUSES[1]):
            get = self.run_linkedin([limited(), limited(), response(200)])
        self.assertEqual(self.sleeps(), [PAUSES[0], PAUSES[1]])
        self.assertEqual(get.call_count, 3)
        self.assertEqual(main._avisos, [])

    def test_linkedin_does_not_wait_when_budget_is_below_next_pause(self):
        with mock.patch.object(main, "PAUSA_TOTAL_MAXIMA_LINKEDIN", PAUSES[0] - 1):
            get = self.run_linkedin(return_value=limited())
        self.assertEqual(get.call_count, 1)
        self.sleep.assert_not_called()
        self.assertEqual(main._avisos, [LIMIT_WARNING])

    def test_linkedin_network_error_during_retry_is_noted_as_no_answer(self):
        self.run_linkedin([limited(), main.requests.Timeout("slow")])
        self.assertEqual(main._avisos, [("LINKEDIN", "FLUTTER: o site não respondeu.")])

    def test_linkedin_empty_page_stops_without_page_pause(self):
        get = self.run_linkedin(return_value=response(200))
        self.assertEqual(get.call_count, 1)
        self.sleep.assert_not_called()
