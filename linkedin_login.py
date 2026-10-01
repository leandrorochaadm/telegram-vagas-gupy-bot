"""LinkedIn posts search with a logged-in session (li_at cookie) in a headless browser.

Opens LinkedIn's own content search, filtered by post date and sorted by date,
so it finds posts that search engines never index. The li_at cookie comes from a
browser where the account is logged in (DevTools → Application → Cookies).

When the cookie is no longer valid, LinkedIn redirects to a login page, drops
the cookie or loops through redirects, and search_posts raises LoginExpired.
A refused page raises SearchBlocked, and a page with neither posts nor the
"no results" message (or with posts but no post links) raises ResultsNotFound,
so a layout change never passes as "no posts today".
"""
import re
import sys
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlparse

SEARCH_URL = "https://www.linkedin.com/search/results/content/"
# Where LinkedIn sends a session it no longer accepts
LOGIN_PATHS = ("/login", "/uas/login", "/checkpoint", "/authwall", "/signup")
# A rejected li_at can also make LinkedIn bounce the page between redirects
# until Chromium gives up, instead of landing on a login page
REDIRECT_LOOP_ERROR = "ERR_TOO_MANY_REDIRECTS"
PAGE_TIMEOUT_MS = 45_000
RESULTS_TIMEOUT_MS = 20_000  # search results load after the page itself
SCROLL_WAIT_MS = 2_500       # each scroll loads the next batch of posts
# Brazil has no daylight saving time since 2019
BRT = timezone(timedelta(hours=-3))
# Value LinkedIn writes over li_at when it ends the session
DELETED_COOKIE_VALUES = ("", "delete me")
# User agent platform matching the machine, so it agrees with the headers Chromium sends
_UA_PLATFORMS = {
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "win32": "Windows NT 10.0; Win64; x64",
}

# Class names are generated hashes that change on every deploy: only these attributes are stable.
# Each result card is componentkey="update-card-focus<key>FeedType_FLAGSHIP_SEARCH".
_CARD_PREFIX = "update-card-focus"
_CARD_SUFFIX = "FeedType_"
_CARD_SELECTOR = f'[componentkey^="{_CARD_PREFIX}"]'
# The box holds the full text plus the "… mais" button, which only unclamps it on screen
_TEXT_SELECTOR = '[data-testid="expandable-text-box"]'
_MORE_BUTTON_SELECTOR = '[data-testid="expandable-text-button"]'
_AUTHOR_LINK_SELECTOR = 'a[href*="/in/"], a[href*="/company/"]'
# Either a post or the empty state: anything else means the page did not render as expected.
# The empty state has no stable attribute, only its text (the browser runs with locale pt-BR).
_RESULTS_OR_EMPTY = f'{_CARD_SELECTOR}, h2:has-text("Nenhum resultado")'
# The page has no post links: they only come in the React payloads the page is built
# from (the HTML itself and the "load more" responses), as the card's share link
_RE_POST_SLUG_URL = re.compile(r'postSlugUrl\\?"\s*:\s*\\?"(https://www\.linkedin\.com/posts/[^"\\]+)')


class LoginExpired(RuntimeError):
    """LinkedIn refused the li_at cookie and sent the browser to a login page."""


class SearchBlocked(RuntimeError):
    """LinkedIn answered the search page with an error status (999 = bot blocked)."""

    def __init__(self, status: int | None):
        super().__init__(f"status {status}")
        self.status = status


class ResultsNotFound(RuntimeError):
    """The page showed neither posts nor the "no results" message."""


def is_login_wall(url: str) -> bool:
    path = urlparse(url).path
    return any(path == p or path.startswith(p + "/") for p in LOGIN_PATHS)


def build_search_url(query: str, period: str = "past-24h") -> str:
    """Same URL the site builds for the "Posts" filter; `period`: past-24h, past-week or past-month."""
    params = {
        "keywords": query,
        "origin": "FACETED_SEARCH",
        # Filter values are JSON lists, as in the site's own URLs
        "sortBy": '["date_posted"]',
        "datePosted": f'["{period}"]',
    }
    return f"{SEARCH_URL}?{urlencode(params, quote_via=quote)}"


def _clean_text(element) -> str:
    return " ".join(element.get_text(" ", strip=True).split())


def card_key(componentkey: str) -> str:
    """"update-card-focus<key>FeedType_FLAGSHIP_SEARCH" → "<key>", the id the payloads use."""
    return componentkey.removeprefix(_CARD_PREFIX).split(_CARD_SUFFIX)[0]


def match_post_links(payloads: list[str], keys: list[str]) -> dict[str, str]:
    """Share link of each card key found in the page payloads.

    A card's data comes right after its key, so each link belongs to the card
    whose key shows up last before it. The first link found for a card wins.
    """
    links: dict[str, str] = {}
    for body in payloads:
        marks = sorted((m.start(), key) for key in keys for m in re.finditer(re.escape(key), body))
        positions = [pos for pos, _ in marks]
        for match in _RE_POST_SLUG_URL.finditer(body):
            i = bisect_left(positions, match.start()) - 1
            if i >= 0:
                links.setdefault(marks[i][1], match.group(1))
    return links


def _author(card) -> str:
    # The link holds the name and the connection degree ("• 2º") in separate paragraphs
    for link in card.select(_AUTHOR_LINK_SELECTOR):
        name = _clean_text(link.find("p") or link)
        if name:
            return name
    return "Autor não informado"


def cookie_dropped(cookies: list[dict]) -> bool:
    """True when LinkedIn removed or blanked li_at, i.e. it ended the session."""
    values = [c.get("value", "") for c in cookies if c.get("name") == "li_at"]
    return not values or all(v.strip('"') in DELETED_COOKIE_VALUES for v in values)


def parse_posts(page_html: str, payloads: list[str], now: datetime | None = None) -> list[dict]:
    """Posts on a rendered search results page, in page order, without duplicates.

    `payloads` are the page's raw responses, where the post links are. Raises
    ResultsNotFound when there are posts but none of them has a link: the
    payload format changed, which must not pass as "no posts today".
    """
    # Imported here so main.py still starts without beautifulsoup4 (it warns and skips LinkedIn)
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page_html, "html.parser")
    # The message shows Brasília's date, like the other sources
    now = (now or datetime.now(timezone.utc)).astimezone(BRT)
    cards = []
    for card in soup.select(_CARD_SELECTOR):
        text_box = card.select_one(_TEXT_SELECTOR)
        if text_box is None:
            continue
        for button in text_box.select(_MORE_BUTTON_SELECTOR):
            button.decompose()
        text = _clean_text(text_box)
        if text:
            cards.append((card_key(card["componentkey"]), card, text))
    links = match_post_links(payloads, [key for key, _, _ in cards])
    if cards and not links:
        raise ResultsNotFound("posts without links")
    posts: list[dict] = []
    seen: set[str] = set()
    for key, card, text in cards:
        link = links.get(key)
        if not link or link in seen:
            continue
        seen.add(link)
        posts.append({
            "link": link,
            "author": _author(card),
            "text": text,
            # The page only shows relative times ("2h"): the day the post was found
            "date": now.strftime("%d/%m/%Y"),
        })
    return posts


def browser_user_agent(version: str, platform: str = sys.platform) -> str:
    """Regular Chrome user agent for the launched Chromium version and this OS.

    Headless Chromium says "HeadlessChrome", and a fixed UA from another version or
    OS contradicts the client hints Chromium sends: both are classic bot signals.
    """
    os_part = _UA_PLATFORMS.get(platform, "X11; Linux x86_64")
    return f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"


def _check_session(page, context) -> None:
    if is_login_wall(page.url) or cookie_dropped(context.cookies("https://www.linkedin.com")):
        raise LoginExpired(urlparse(page.url).path)


def search_posts(li_at: str, query: str, scrolls: int = 2, user_agent: str | None = None,
                 period: str = "past-24h") -> list[dict]:
    """Search posts from `period` matching `query`, logged in with the `li_at` cookie.

    `user_agent` defaults to browser_user_agent(). Raises LoginExpired when LinkedIn
    no longer accepts the cookie, SearchBlocked or ResultsNotFound when the page
    cannot be read, and ImportError when Playwright is not installed.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(user_agent=user_agent or browser_user_agent(browser.version),
                                          locale="pt-BR")
            context.add_cookies([{
                "name": "li_at", "value": li_at, "domain": ".linkedin.com", "path": "/",
                "httpOnly": True, "secure": True,
            }])
            page = context.new_page()
            payloads: list[str] = []
            page.on("response", lambda response: _keep_payload(response, payloads))
            try:
                return _read_results(page, context, build_search_url(query, period), scrolls,
                                     PlaywrightTimeout, payloads)
            except PlaywrightError as error:
                if REDIRECT_LOOP_ERROR in str(error):
                    raise LoginExpired(REDIRECT_LOOP_ERROR) from None
                # A redirect to the login page while the page loads surfaces as a
                # navigation error ("execution context was destroyed"), not a timeout
                _check_session(page, context)
                raise
        finally:
            browser.close()


def _keep_payload(response, payloads: list[str]) -> None:
    """Keeps the page itself and the "load more" responses, where the post links are."""
    if response.request.resource_type not in ("document", "fetch"):
        return
    if urlparse(response.url).hostname != "www.linkedin.com":
        return
    try:
        payloads.append(response.text())
    except Exception:
        # Redirects and aborted requests have no body
        pass


def _read_results(page, context, url: str, scrolls: int, timeout_error: type[Exception],
                  payloads: list[str]) -> list[dict]:
    response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    # Login first: a redirect to the login page may also come with an odd status
    _check_session(page, context)
    if response is None or not response.ok:
        raise SearchBlocked(response.status if response else None)
    try:
        page.wait_for_selector(_RESULTS_OR_EMPTY, timeout=RESULTS_TIMEOUT_MS)
    except timeout_error:
        # A late redirect to the login page also ends up here
        _check_session(page, context)
        raise ResultsNotFound() from None
    # The results list scrolls inside its own column: a mouse wheel on the page loads
    # one batch at most, bringing the last card into view loads one batch each time
    cards = page.locator(_CARD_SELECTOR)
    for _ in range(scrolls):
        if not cards.count():
            break
        cards.last.scroll_into_view_if_needed()
        page.wait_for_timeout(SCROLL_WAIT_MS)
    _check_session(page, context)
    return parse_posts(page.content(), payloads)
