"""Translates SCRAPER_PROXY to the format of requests and Playwright, and checks that WARP is on."""
import requests

WARP_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
WARP_ON_LINE = "warp=on"
SOCKS5H = "socks5h://"
SOCKS5 = "socks5://"


def scraper_proxies(proxy: str | None) -> dict[str, str] | None:
    """The `proxies` argument of requests; None (no proxy) when `proxy` is empty."""
    return {"http": proxy, "https": proxy} if proxy else None


def playwright_proxy(proxy: str | None) -> dict[str, str] | None:
    """The `proxy` argument of chromium.launch. Chromium has no socks5h: it already resolves DNS through the proxy."""
    if not proxy:
        return None
    if proxy.startswith(SOCKS5H):
        proxy = SOCKS5 + proxy[len(SOCKS5H):]
    return {"server": proxy}


def warp_is_on(proxy: str, *, timeout: float = 10) -> bool:
    """True only when the Cloudflare trace through the proxy answers 200 with the exact line `warp=on`."""
    try:
        resp = requests.get(WARP_TRACE_URL, proxies=scraper_proxies(proxy), timeout=timeout)
    except Exception:
        return False
    return resp.status_code == 200 and WARP_ON_LINE in resp.text.splitlines()
