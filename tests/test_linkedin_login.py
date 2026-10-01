"""Unit tests for the logged-in LinkedIn posts search (linkedin_login.py) and its
session state / Brave fallback in main.py.

Run: python -m unittest discover -s tests -v
"""
import importlib
import os
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import linkedin_login  # noqa: E402
import main  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
COOKIE = "AQEDAR-cookie"
POST_TEXT = "Vaga Flutter remota: estamos contratando dev mobile com Riverpod"


KEY = "35s5zzsw9zNZ_xI_pV1J2ytvY_PeY9QGIfI_G246hJM"
LINK = "https://www.linkedin.com/posts/maria-souza_vaga-flutter-remota-share-7511157047048974336-IP5b"


def post_html(key=KEY, author="Maria Souza", text=POST_TEXT, author_href="https://www.linkedin.com/in/maria-souza/"):
    """A result card as LinkedIn renders it: hashed classes, only componentkey and data-testid are stable."""
    return f"""
    <div class="auymuo auytj" role="listitem" componentkey="update-card-focus{key}FeedType_FLAGSHIP_SEARCH">
      <a class="auyx1" href="{author_href}"><p class="auyp9">{author}</p><p class="auyp9">• 2º</p></a>
      <p class="auymff"><span class="auymp4" data-testid="expandable-text-box">{text}<button
        data-testid="expandable-text-button">… mais</button></span></p>
    </div>"""


def payload(key=KEY, link=LINK, escaped=True):
    """A slice of the page's React payload: the card's key, then its data with the share link."""
    body = f'{{"key":"update-card-focus{key}"}},{{"actorName":"Maria","postSlugUrl":"{link}"}}'
    # Inside the page HTML the payload is a JSON string; "load more" responses are plain JSON
    return body.replace('"', '\\"') if escaped else body


def logged_post(link=LINK, text=POST_TEXT):
    return {"link": link, "author": "Maria Souza", "text": text, "date": "30/09/2026"}


class ParsePostsTest(unittest.TestCase):

    def parse(self, page, payloads=None, now=NOW):
        return linkedin_login.parse_posts(page, [payload()] if payloads is None else payloads, now=now)

    def test_reads_link_author_text_and_date(self):
        self.assertEqual(self.parse(post_html()), [{
            "link": LINK,
            "author": "Maria Souza",
            "text": POST_TEXT,
            "date": "30/09/2026",
        }])

    def test_reads_link_from_plain_json_payload(self):
        posts = self.parse(post_html(), [payload(escaped=False)])
        self.assertEqual(posts[0]["link"], LINK)

    def test_link_belongs_to_the_card_whose_key_comes_right_before_it(self):
        # Same author twice: only the payload position tells the two posts apart
        other_link = LINK.replace("7511157047048974336", "7511156450174349312")
        page = post_html(key="B" * 43, text="segundo post") + post_html(text="primeiro post")
        posts = self.parse(page, [payload() + payload(key="B" * 43, link=other_link)])
        self.assertEqual([(p["text"], p["link"]) for p in posts],
                         [("segundo post", other_link), ("primeiro post", LINK)])

    def test_links_from_later_payloads_count(self):
        # Cards loaded by scrolling get their links in the "load more" response
        page = post_html() + post_html(key="B" * 43)
        other_link = LINK.replace("7511157047048974336", "1")
        posts = self.parse(page, [payload(), payload(key="B" * 43, link=other_link, escaped=False)])
        self.assertEqual([p["link"] for p in posts], [LINK, other_link])

    def test_same_post_twice_is_kept_once(self):
        self.assertEqual(len(self.parse(post_html() + post_html())), 1)

    def test_card_without_link_is_skipped(self):
        page = post_html() + post_html(key="B" * 43)
        self.assertEqual([p["link"] for p in self.parse(page)], [LINK])

    def test_posts_without_any_link_raise_results_not_found(self):
        # The payload format changed: that must not pass as "no posts today"
        with self.assertRaises(linkedin_login.ResultsNotFound):
            self.parse(post_html(), payloads=["<html>no links here</html>"])

    def test_page_without_cards_has_no_posts(self):
        self.assertEqual(self.parse("<h2>Nenhum resultado encontrado</h2>", payloads=[]), [])

    def test_skips_post_without_text(self):
        self.assertEqual(self.parse(post_html(text="")), [])

    def test_more_button_is_not_part_of_the_text(self):
        self.assertNotIn("mais", self.parse(post_html(text="vaga flutter"))[0]["text"])

    def test_company_author(self):
        page = post_html(author="Acme Tech", author_href="https://www.linkedin.com/company/acme/")
        self.assertEqual(self.parse(page)[0]["author"], "Acme Tech")

    def test_author_fallback(self):
        page = post_html(author_href="https://www.linkedin.com/search/results/all/")
        self.assertEqual(self.parse(page)[0]["author"], "Autor não informado")

    def test_text_whitespace_is_collapsed(self):
        posts = self.parse(post_html(text="vaga\n\n   flutter<br>remota"))
        self.assertEqual(posts[0]["text"], "vaga flutter remota")

    def test_date_is_brasilia_day(self):
        # 01h UTC on Oct 1st is still Sep 30th in Brasília
        posts = self.parse(post_html(), now=datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(posts[0]["date"], "30/09/2026")

    def test_card_key_drops_prefix_and_feed_type(self):
        self.assertEqual(linkedin_login.card_key(f"update-card-focus{KEY}FeedType_FLAGSHIP_SEARCH"), KEY)

    def test_module_imports_without_beautifulsoup(self):
        # main.py imports it at startup; without bs4 the bot must still run the other sources
        try:
            with mock.patch.dict(sys.modules, {"bs4": None}):
                importlib.reload(linkedin_login)
        finally:
            importlib.reload(linkedin_login)


class CookieDroppedTest(unittest.TestCase):

    def test_valid_cookie_is_kept(self):
        self.assertFalse(linkedin_login.cookie_dropped([{"name": "li_at", "value": COOKIE}]))

    def test_missing_cookie_means_dropped(self):
        self.assertTrue(linkedin_login.cookie_dropped([{"name": "JSESSIONID", "value": "x"}]))

    def test_delete_me_value_means_dropped(self):
        self.assertTrue(linkedin_login.cookie_dropped([{"name": "li_at", "value": '"delete me"'}]))
        self.assertTrue(linkedin_login.cookie_dropped([{"name": "li_at", "value": ""}]))


class FakeError(Exception):
    """Stands in for playwright.sync_api.Error."""


class FakeTimeout(FakeError):
    """Like Playwright, its TimeoutError is a subclass of its Error."""


def fake_response(body, url="https://www.linkedin.com/search/results/content/", kind="document"):
    response = mock.Mock(url=url)
    response.request.resource_type = kind
    if isinstance(body, Exception):
        response.text.side_effect = body
    else:
        response.text.return_value = body
    return response


class FakePage:
    def __init__(self, url, status, html, render_ok, content_error, responses, cards, goto_error=None):
        self.url, self._status, self._html = url, status, html
        self._goto_error = goto_error
        self._render_ok, self._content_error = render_ok, content_error
        self._responses, self._handlers = responses, []
        self.cards = mock.Mock()
        self.cards.count.return_value = cards
        self.goto_url = self.locator_selector = None

    def on(self, event, handler):
        if event == "response":
            self._handlers.append(handler)

    def goto(self, url, **_kwargs):
        self.goto_url = url
        if self._goto_error:
            raise self._goto_error
        if self.url is None:
            self.url = url
        for response in self._responses:
            for handler in self._handlers:
                handler(response)
        return mock.Mock(status=self._status, ok=200 <= self._status < 300)

    def locator(self, selector):
        self.locator_selector = selector
        return self.cards

    def wait_for_selector(self, _selector, **_kwargs):
        if not self._render_ok:
            raise FakeTimeout()

    def wait_for_timeout(self, _ms):
        pass

    def content(self):
        if self._content_error:
            raise FakeError("Execution context was destroyed, most likely because of a navigation")
        return self._html


def fake_playwright(url=None, status=200, html="", render_ok=True, cookies_after=None, content_error=False,
                    responses=None, cards=1, goto_error=None):
    """sys.modules entries that stand in for playwright.sync_api."""
    cookies = [{"name": "li_at", "value": COOKIE}] if cookies_after is None else cookies_after
    responses = [fake_response(payload())] if responses is None else responses
    page = FakePage(url, status, html, render_ok, content_error, responses, cards, goto_error)
    context = mock.Mock()
    context.new_page.return_value = page
    context.cookies.return_value = cookies
    browser = mock.Mock(version="140.0.7339.16")
    browser.new_context.return_value = context
    runner = mock.MagicMock()
    runner.__enter__.return_value.chromium.launch.return_value = browser
    api = types.ModuleType("playwright.sync_api")
    api.sync_playwright = lambda: runner
    api.TimeoutError = FakeTimeout
    api.Error = FakeError
    package = types.ModuleType("playwright")
    package.sync_api = api
    modules = {"playwright": package, "playwright.sync_api": api}
    return modules, page, context, browser


class SearchPostsTest(unittest.TestCase):

    def search(self, **fake):
        modules, page, context, browser = fake_playwright(**fake)
        self.page, self.context, self.browser = page, context, browser
        with mock.patch.dict(sys.modules, modules):
            return linkedin_login.search_posts(COOKIE, "flutter vaga", scrolls=2, period="past-week")

    def test_returns_parsed_posts_with_the_cookie_set(self):
        posts = self.search(html=post_html())
        self.assertEqual([p["link"] for p in posts], [LINK])
        cookie = self.context.add_cookies.call_args.args[0][0]
        self.assertEqual((cookie["name"], cookie["value"], cookie["domain"]), ("li_at", COOKIE, ".linkedin.com"))
        self.assertIn("past-week", self.page.goto_url)
        self.browser.close.assert_called_once()

    def test_each_scroll_brings_the_last_card_into_view(self):
        self.search(html=post_html())
        self.assertIn("update-card-focus", self.page.locator_selector)
        self.assertEqual(self.page.cards.last.scroll_into_view_if_needed.call_count, 2)

    def test_no_scroll_without_cards(self):
        self.search(html="<h2>Nenhum resultado encontrado</h2>", cards=0)
        self.page.cards.last.scroll_into_view_if_needed.assert_not_called()

    def test_links_come_from_linkedin_page_and_fetch_responses_only(self):
        other_link = LINK.replace("7511157047048974336", "1")
        responses = [
            fake_response(payload()),
            fake_response(payload(key="B" * 43, link=other_link), kind="fetch"),
            fake_response(payload(key="C" * 43, link=LINK + "-img"), kind="image"),
            fake_response(payload(key="D" * 43, link=LINK + "-ads"), url="https://ads.example.com/x"),
            # A redirect has no body: reading it fails
            fake_response(FakeError("Response body is unavailable for redirect responses")),
        ]
        page = post_html() + "".join(post_html(key=k * 43) for k in "BCD")
        posts = self.search(html=page, responses=responses)
        self.assertEqual([p["link"] for p in posts], [LINK, other_link])

    def test_redirect_to_login_raises_login_expired(self):
        with self.assertRaises(linkedin_login.LoginExpired):
            self.search(url="https://www.linkedin.com/uas/login?session_redirect=x", status=200)
        self.browser.close.assert_called_once()

    def test_dropped_cookie_raises_login_expired(self):
        with self.assertRaises(linkedin_login.LoginExpired):
            self.search(cookies_after=[{"name": "li_at", "value": '"delete me"'}])

    def test_error_status_raises_search_blocked(self):
        with self.assertRaises(linkedin_login.SearchBlocked) as ctx:
            self.search(status=999)
        self.assertEqual(ctx.exception.status, 999)

    def test_page_without_posts_or_empty_state_raises_results_not_found(self):
        with self.assertRaises(linkedin_login.ResultsNotFound):
            self.search(render_ok=False)

    def test_late_login_redirect_wins_over_results_not_found(self):
        with self.assertRaises(linkedin_login.LoginExpired):
            self.search(render_ok=False, cookies_after=[])

    def test_empty_results_page_returns_no_posts(self):
        self.assertEqual(self.search(html="<h2>Nenhum resultado encontrado</h2>", cards=0), [])

    def test_navigation_error_with_dropped_cookie_raises_login_expired(self):
        # The login redirect happened while the page was being read
        with self.assertRaises(linkedin_login.LoginExpired):
            self.search(content_error=True, cookies_after=[])

    def test_navigation_error_with_valid_cookie_is_raised_as_is(self):
        with self.assertRaises(FakeError):
            self.search(content_error=True)

    def test_redirect_loop_raises_login_expired(self):
        # LinkedIn may bounce a rejected cookie between redirects, keeping li_at in place
        error = FakeError("Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://www.linkedin.com/search/")
        with self.assertRaises(linkedin_login.LoginExpired):
            # A failed first navigation leaves the page on about:blank
            self.search(url="about:blank", goto_error=error)
        self.browser.close.assert_called_once()

    def test_other_goto_error_with_valid_cookie_is_raised_as_is(self):
        with self.assertRaises(FakeError):
            # A failed first navigation leaves the page on about:blank
            self.search(url="about:blank", goto_error=FakeError("Page.goto: net::ERR_CONNECTION_RESET"))

    def test_user_agent_matches_the_launched_browser(self):
        self.search()
        user_agent = self.browser.new_context.call_args.kwargs["user_agent"]
        self.assertIn("Chrome/140.0.7339.16 ", user_agent)
        self.assertNotIn("Headless", user_agent)


class BrowserUserAgentTest(unittest.TestCase):

    def test_platform_matches_the_machine(self):
        self.assertIn("X11; Linux x86_64", linkedin_login.browser_user_agent("140.0", platform="linux"))
        self.assertIn("Macintosh", linkedin_login.browser_user_agent("140.0", platform="darwin"))
        self.assertIn("Windows NT 10.0", linkedin_login.browser_user_agent("140.0", platform="win32"))


class LoginWallTest(unittest.TestCase):

    def test_login_pages_are_detected(self):
        for url in ["https://www.linkedin.com/login?session_redirect=x",
                    "https://www.linkedin.com/uas/login",
                    "https://www.linkedin.com/checkpoint/challenge/abc",
                    "https://www.linkedin.com/authwall?trk=x",
                    "https://www.linkedin.com/signup"]:
            self.assertTrue(linkedin_login.is_login_wall(url), url)

    def test_search_page_is_not_a_login_wall(self):
        url = linkedin_login.build_search_url("login flutter")
        self.assertFalse(linkedin_login.is_login_wall(url))
        self.assertFalse(linkedin_login.is_login_wall("https://www.linkedin.com/loginhelp"))

    def test_search_url_matches_the_sites_posts_filter(self):
        url = linkedin_login.build_search_url("flutter vaga", "past-week")
        self.assertEqual(url, "https://www.linkedin.com/search/results/content/?keywords=flutter%20vaga"
                              "&origin=FACETED_SEARCH&sortBy=%5B%22date_posted%22%5D"
                              "&datePosted=%5B%22past-week%22%5D")

    def test_search_url_defaults_to_past_day(self):
        params = parse_qs(urlparse(linkedin_login.build_search_url("flutter")).query)
        self.assertEqual(params["datePosted"], ['["past-24h"]'])


class EstadoLoginTest(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.addCleanup(self.conn.close)
        self.erros = []

    def estado(self, now=NOW, cookie=COOKIE):
        return main.estado_login_linkedin(self.conn, self.cursor, cookie, agora=now)

    def expire(self, now=NOW, cookie=COOKIE):
        main.marcar_login_expirado(self.conn, self.cursor, cookie, self.erros, agora=now)

    def test_first_search_is_ok(self):
        self.assertEqual(self.estado(), "ok")

    def test_waits_inside_interval(self):
        self.estado()
        self.assertEqual(self.estado(now=NOW + timedelta(minutes=30)), "espera")

    def test_ok_after_interval_with_slack(self):
        self.estado()
        self.assertEqual(self.estado(now=NOW + timedelta(minutes=55)), "ok")

    def test_expiry_alerts_once_then_pauses(self):
        self.estado()
        self.expire()
        self.assertEqual(self.erros, [main.AVISO_COOKIE_LINKEDIN])
        self.assertEqual(self.estado(now=NOW + timedelta(hours=2)), "expirado")
        self.assertEqual(self.erros, [main.AVISO_COOKIE_LINKEDIN])

    def test_retries_once_after_a_day(self):
        self.estado()
        self.expire()
        self.assertEqual(self.estado(now=NOW + timedelta(hours=24)), "ok")
        # The retry restarts the 24h count, even if it fails for another reason
        self.assertEqual(self.estado(now=NOW + timedelta(hours=25)), "expirado")
        self.assertEqual(self.estado(now=NOW + timedelta(hours=48)), "ok")

    def test_valid_cookie_clears_the_pause(self):
        self.estado()
        self.expire()
        main.marcar_login_valido(self.conn, self.cursor, COOKIE)
        self.assertEqual(self.estado(now=NOW + timedelta(hours=2)), "ok")

    def test_new_cookie_resumes_search(self):
        self.estado()
        self.expire()
        self.assertEqual(self.estado(now=NOW + timedelta(minutes=5), cookie="new-cookie"), "ok")
        # The old cookie's state is gone, so the new one is not paused
        self.assertEqual(self.cursor.execute("SELECT COUNT(*) FROM linkedin_session").fetchone()[0], 1)

    def test_db_keeps_only_a_hash_of_the_cookie(self):
        self.estado()
        self.expire()
        rows = self.cursor.execute("SELECT * FROM linkedin_session").fetchall()
        self.assertNotIn(COOKIE, str(rows))


class BuscarPostsComLoginTest(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.addCleanup(self.conn.close)
        self.cursor.execute("CREATE TABLE vagas_enviadas (link TEXT PRIMARY KEY, data_publicacao TEXT, titulo TEXT)")
        main._enviados_sessao.clear()
        main._avisos.clear()
        self.addCleanup(main._avisos.clear)
        patches = [
            mock.patch.object(main, "LINKEDIN_LI_AT", COOKIE),
            mock.patch.object(main, "BRAVE_API_KEY", "key"),
            mock.patch.object(main, "enviar_telegram", return_value=True),
            mock.patch.object(main.time, "sleep"),
            mock.patch.object(main.linkedin_posts, "search_posts", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, **search):
        with mock.patch.object(main.linkedin_login, "search_posts", **search) as logged:
            main.buscar_posts_linkedin(self.conn, self.cursor)
        return logged

    def test_sends_logged_post_without_using_brave(self):
        self.run_with(return_value=[logged_post()])
        main.enviar_telegram.assert_called_once()
        mensagem = main.enviar_telegram.call_args.args[0]
        self.assertIn("Maria Souza", mensagem)
        self.assertIn(LINK, mensagem)
        main.linkedin_posts.search_posts.assert_not_called()

    def test_logged_post_goes_through_relevance_filter(self):
        self.run_with(return_value=[logged_post(text="Vaga Flutter presencial em SP")])
        main.enviar_telegram.assert_not_called()

    def test_expired_cookie_alerts_and_falls_back_to_brave(self):
        self.run_with(side_effect=linkedin_login.LoginExpired("https://www.linkedin.com/login"))
        self.assertEqual(main._avisos, [("LINKEDIN PUBLICAÇÕES", main.AVISO_COOKIE_LINKEDIN)])
        main.linkedin_posts.search_posts.assert_called_once()

    def test_expired_cookie_is_not_used_on_the_next_run(self):
        self.run_with(side_effect=linkedin_login.LoginExpired("https://www.linkedin.com/login"))
        main._avisos.clear()
        logged = self.run_with(return_value=[])
        logged.assert_not_called()
        self.assertEqual(main._avisos, [])

    def pause_a_day_ago(self):
        """Cookie expired on a previous run, whose alert went out over a day ago."""
        self.run_with(side_effect=linkedin_login.LoginExpired("/login"))
        dia_atras = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
        self.cursor.execute("UPDATE linkedin_session SET searched_at = ?, expired_at = ?, reminded_at = ?",
                            (dia_atras, dia_atras, dia_atras))
        main._avisos.clear()
        main.linkedin_posts.search_posts.reset_mock()

    def test_same_cookie_valid_again_resumes_without_alert(self):
        self.pause_a_day_ago()
        logged = self.run_with(return_value=[logged_post()])
        logged.assert_called_once()
        main.enviar_telegram.assert_called_once()
        self.assertEqual(main._avisos, [])
        expired = self.cursor.execute("SELECT expired_at FROM linkedin_session").fetchone()[0]
        self.assertIsNone(expired)

    def test_failed_retry_alerts_again(self):
        self.pause_a_day_ago()
        self.run_with(side_effect=linkedin_login.LoginExpired("/login"))
        self.assertEqual(main._avisos, [("LINKEDIN PUBLICAÇÕES", main.AVISO_COOKIE_LINKEDIN)])

    def test_retry_that_fails_for_another_reason_keeps_the_pause(self):
        self.pause_a_day_ago()
        self.run_with(side_effect=linkedin_login.SearchBlocked(999))
        logged = self.run_with(return_value=[])
        logged.assert_not_called()

    def test_search_does_not_force_the_bots_fixed_user_agent(self):
        logged = self.run_with(return_value=[])
        self.assertNotIn("user_agent", logged.call_args.kwargs)

    def test_second_run_inside_interval_skips_everything(self):
        self.run_with(return_value=[])
        logged = self.run_with(return_value=[])
        logged.assert_not_called()
        main.linkedin_posts.search_posts.assert_not_called()

    def test_missing_playwright_falls_back_to_brave(self):
        self.run_with(side_effect=ImportError("playwright"))
        main.linkedin_posts.search_posts.assert_called_once()
        self.assertEqual(main._avisos, [])

    def test_blocked_page_is_alerted_with_status(self):
        self.run_with(side_effect=linkedin_login.SearchBlocked(999))
        self.assertEqual(main._avisos, [("LINKEDIN PUBLICAÇÕES",
                                         "FLUTTER · VAGA · REMOTO: o LinkedIn bloqueou a busca de publicações (código 999).")])
        main.linkedin_posts.search_posts.assert_not_called()

    def test_unreadable_page_is_alerted(self):
        self.run_with(side_effect=linkedin_login.ResultsNotFound())
        self.assertEqual(len(main._avisos), 1)
        self.assertIn("não carregou ou mudou de formato", main._avisos[0][1])

    def test_page_error_is_alerted_without_brave(self):
        self.run_with(side_effect=TimeoutError("page timeout"))
        self.assertEqual(len(main._avisos), 1)
        self.assertIn("FLUTTER", main._avisos[0][1])
        main.linkedin_posts.search_posts.assert_not_called()

    def test_runs_with_cookie_and_no_brave_key(self):
        with mock.patch.object(main, "BRAVE_API_KEY", None):
            self.run_with(return_value=[logged_post()])
        main.enviar_telegram.assert_called_once()


if __name__ == "__main__":
    unittest.main()
