"""Every request to a job site carries the proxy; Telegram and Brave do not.

Run: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gupy  # noqa: E402
import inhire_discovery  # noqa: E402
import job_details  # noqa: E402
import linkedin_login  # noqa: E402
import main  # noqa: E402
import remotar  # noqa: E402
from fake_store import FakeStore  # noqa: E402
from test_linkedin_login import COOKIE, fake_playwright  # noqa: E402

PROXY = "socks5h://127.0.0.1:40000"
PROXIES = {"http": PROXY, "https": PROXY}


def ok(text="", status=200, payload=None):
    return mock.Mock(status_code=status, text=text, headers={},
                     json=mock.Mock(return_value=payload if payload is not None else {"data": []}))


class SourceModulesTest(unittest.TestCase):

    def test_gupy(self):
        with mock.patch.object(gupy.requests, "get", return_value=ok()) as get:
            gupy.search_jobs(FakeStore(), filters=[{"nome": "F", "params": {}}], max_age_days=5, user_agent="UA",
                             send=mock.Mock(), check=lambda t, c: (False, ""), proxy=PROXY)
        self.assertEqual(get.call_args.kwargs["proxies"], PROXIES)

    def test_gupy_without_proxy(self):
        with mock.patch.object(gupy.requests, "get", return_value=ok()) as get:
            gupy.search_jobs(FakeStore(), filters=[{"nome": "F", "params": {}}], max_age_days=5, user_agent="UA",
                             send=mock.Mock(), check=lambda t, c: (False, ""))
        self.assertIsNone(get.call_args.kwargs["proxies"])

    def test_remotar(self):
        with mock.patch.object(remotar.requests, "get", return_value=ok()) as get, \
                mock.patch.object(remotar.time, "sleep"):
            remotar.search_jobs(FakeStore(), filters=[{"nome": "F", "termo": "flutter"}], max_age_days=5,
                                ignore_foreign=True, user_agent="UA", send=mock.Mock(),
                                check=lambda t, c: (False, ""), proxy=PROXY)
        self.assertEqual(get.call_args.kwargs["proxies"], PROXIES)

    def test_yahoo_uses_proxy_and_brave_does_not(self):
        with mock.patch.object(inhire_discovery.requests, "get", return_value=ok("")) as get, \
                mock.patch.object(inhire_discovery.time, "sleep"):
            inhire_discovery.search_yahoo("q", proxy=PROXY)
            yahoo_calls = list(get.call_args_list)
            get.reset_mock()
            inhire_discovery.search_brave("key", "q")
        self.assertTrue(all(c.kwargs["proxies"] == PROXIES for c in yahoo_calls))
        self.assertTrue(all("proxies" not in c.kwargs for c in get.call_args_list))

    def test_discover_passes_the_proxy_to_yahoo(self):
        with mock.patch.object(inhire_discovery, "search_yahoo", return_value=({"acme"}, True)) as yahoo:
            inhire_discovery.discover(FakeStore(), queries=["q"], brave_api_key=None,
                                      every=inhire_discovery.RETRY_AFTER, proxy=PROXY)
        yahoo.assert_called_once_with("q", proxy=PROXY)

    def test_playwright_browser_is_launched_with_the_proxy(self):
        modules, _page, _context, browser = fake_playwright(html="")
        launch = None
        with mock.patch.dict(sys.modules, modules):
            try:
                linkedin_login.search_posts(COOKIE, "q", proxy=PROXY)
            except Exception:
                pass
            launch = sys.modules["playwright.sync_api"].sync_playwright().__enter__.return_value.chromium.launch
        self.assertEqual(launch.call_args.kwargs["proxy"], {"server": "socks5://127.0.0.1:40000"})


class MainRequestsTest(unittest.TestCase):

    def setUp(self):
        for patch in (mock.patch.object(main, "SCRAPER_PROXY", PROXY), mock.patch.object(main.time, "sleep")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_linkedin_get(self):
        with mock.patch.object(main.requests, "get", return_value=ok()) as get:
            main._linkedin_get("u", {}, {}, 0)
        self.assertEqual(get.call_args.kwargs["proxies"], PROXIES)

    def test_inhire_get(self):
        with mock.patch.object(main.requests, "get", return_value=ok()) as get:
            main._get_inhire("u", {})
        self.assertEqual(get.call_args.kwargs["proxies"], PROXIES)

    def test_solides_page(self):
        with mock.patch.object(main.requests, "get", return_value=ok(status=500)) as get:
            with self.assertRaises(main.FalhaFonte):
                main._solides_pagina("remoto/flutter", 1, {})
        self.assertEqual(get.call_args.kwargs["proxies"], PROXIES)

    def test_gupy_remotar_posts_and_discovery_receive_the_proxy(self):
        with mock.patch.object(main.gupy, "search_jobs") as g:
            main._varrer_gupy(None, [])
        with mock.patch.object(main.remotar, "search_jobs") as r:
            main._varrer_remotar(None, [])
        with mock.patch.object(main.inhire_discovery, "discover", side_effect=RuntimeError("stop")) as d:
            with self.assertRaises(RuntimeError):
                main._varrer_inhire(None, [])
        with mock.patch.object(main, "BS4_DISPONIVEL", True), \
                mock.patch.object(main, "LINKEDIN_LI_AT", COOKIE), \
                mock.patch.object(main.linkedin_login, "search_posts", return_value=[]) as p, \
                mock.patch.object(main, "marcar_login_valido"):
            main._posts_com_login(None, [])
        for fake in (g, r, d):
            self.assertEqual(fake.call_args.kwargs["proxy"], PROXY)
        self.assertEqual(p.call_args.kwargs["proxy"], PROXY)


class JobDetailsRequestsTest(unittest.TestCase):

    def test_fetch_details_uses_proxy_and_user_agent(self):
        with mock.patch.object(job_details.requests, "get", return_value=ok(payload={"description": "<p>x</p>"})) as get:
            job_details.fetch_details("https://remotar.com.br/job/1", user_agent="UA", proxy=PROXY)
        self.assertEqual(get.call_args.kwargs["proxies"], PROXIES)
        self.assertEqual(get.call_args.kwargs["headers"]["User-Agent"], "UA")

    def test_fetch_details_without_proxy(self):
        with mock.patch.object(job_details.requests, "get", return_value=ok(payload={"description": "<p>x</p>"})) as get:
            job_details.fetch_details("https://remotar.com.br/job/1", user_agent="UA", proxy=None)
        self.assertIsNone(get.call_args.kwargs["proxies"])


if __name__ == "__main__":
    unittest.main()
