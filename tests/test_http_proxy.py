"""Unit tests for http_proxy (SCRAPER_PROXY translation and the WARP check).

Run: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_proxy  # noqa: E402

PROXY = "socks5h://127.0.0.1:40000"


def trace(text="fl=1\nwarp=on\n", status=200):
    return mock.Mock(status_code=status, text=text)


class FormatTest(unittest.TestCase):

    def test_empty_proxy_means_no_proxy(self):
        for empty in (None, ""):
            self.assertIsNone(http_proxy.scraper_proxies(empty))
            self.assertIsNone(http_proxy.playwright_proxy(empty))

    def test_requests_keeps_socks5h(self):
        self.assertEqual(http_proxy.scraper_proxies(PROXY), {"http": PROXY, "https": PROXY})

    def test_playwright_swaps_socks5h_for_socks5(self):
        self.assertEqual(http_proxy.playwright_proxy(PROXY), {"server": "socks5://127.0.0.1:40000"})

    def test_playwright_keeps_other_schemes(self):
        self.assertEqual(http_proxy.playwright_proxy("http://10.0.0.1:3128"), {"server": "http://10.0.0.1:3128"})


class WarpIsOnTest(unittest.TestCase):

    def check(self, **kwargs):
        with mock.patch.object(http_proxy.requests, "get", **kwargs) as get:
            return http_proxy.warp_is_on(PROXY), get

    def test_on(self):
        on, get = self.check(return_value=trace())
        self.assertTrue(on)
        self.assertEqual(get.call_args.kwargs["proxies"], {"http": PROXY, "https": PROXY})
        self.assertEqual(get.call_args.args[0], http_proxy.WARP_TRACE_URL)

    def test_off(self):
        self.assertFalse(self.check(return_value=trace("warp=off\n"))[0])

    def test_line_must_match_exactly(self):
        self.assertFalse(self.check(return_value=trace("xwarp=on\nwarp=onx\n"))[0])

    def test_http_error(self):
        self.assertFalse(self.check(return_value=trace(status=503))[0])

    def test_network_error(self):
        self.assertFalse(self.check(side_effect=requests.ConnectionError("down"))[0])


if __name__ == "__main__":
    unittest.main()
