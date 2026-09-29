"""Job search on the open web through the Brave Search API.

Same kind of query as linkedin_posts.py, but without the `site:` restriction,
so openings published on any site (blogs, job boards, company pages...) from
the last 24h are found. Sites already covered by other sources (LinkedIn) are
left out with `-site:`. All settings come from main.py as parameters.
"""
import html
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from linkedin_posts import BRAVE_URL, MAX_AGE, MAX_OFFSET, REQUEST_INTERVAL, RESULTS_PER_PAGE, parse_page_age

SUMMARY_MAX_CHARS = 300


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _on_site(host: str, sites: list[str]) -> bool:
    # "linkedin.com" also covers br.linkedin.com, www.linkedin.com...
    return any(host == site or host.endswith("." + site) for site in sites)


def build_query(query: str, excluded_sites: list[str]) -> str:
    # Exclusions first, so an OR inside the query can't split them off one side
    return " ".join([*(f"-site:{site}" for site in excluded_sites), query])


def parse_web_result(result: dict, now: datetime | None = None, excluded_sites: list[str] = ()) -> dict | None:
    """Turn any Brave web result into a page dict; the site's domain goes in `author`."""
    url = result.get("url") or ""
    host = urlsplit(url).hostname
    # -site: in the query should already drop these; this is just a safety net
    if not host or _on_site(host, excluded_sites):
        return None
    published = parse_page_age(result.get("page_age"))
    now = now or datetime.now(timezone.utc)
    # freshness=pd filters by Brave's crawl date, so an old page may still slip in
    if published and now - published > MAX_AGE:
        return None
    title = _strip_tags(result.get("title", ""))
    return {
        "link": url.split("#", 1)[0],
        "author": host.removeprefix("www."),
        "text": f"{title} {_strip_tags(result.get('description', ''))}".strip(),
        "date": (published or now).strftime("%d/%m/%Y"),
    }


def search_web(api_key: str, query: str, pages: int = 1, excluded_sites: list[str] = ()) -> list[dict]:
    """Search pages from the past 24h matching `query`, on any site but `excluded_sites`."""
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    items: list[dict] = []
    for offset in range(min(pages, MAX_OFFSET + 1)):
        params = {"q": build_query(query, excluded_sites), "count": RESULTS_PER_PAGE, "offset": offset, "freshness": "pd"}
        resp = requests.get(BRAVE_URL, params=params, headers=headers, timeout=15)
        # Always wait: the next request may come from the next filter, not the next page
        time.sleep(REQUEST_INTERVAL)
        if resp.status_code != 200:
            print(f"   🛑 Brave HTTP {resp.status_code}: {resp.text[:120]}")
            break
        data = resp.json()
        results = data.get("web", {}).get("results", [])
        items += [item for r in results if (item := parse_web_result(r, excluded_sites=excluded_sites))]
        if not data.get("query", {}).get("more_results_available"):
            break
    return items


def _word_pattern(terms: list[str]) -> re.Pattern:
    # Whole word only, same rule as main.py ("ios" must not match "negócios")
    alternatives = "|".join(re.escape(t.lower()) for t in sorted(set(terms), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


def _already_sent(cursor, link: str) -> bool:
    cursor.execute("SELECT 1 FROM vagas_enviadas WHERE link = ?", (link,))
    return cursor.fetchone() is not None


def search_jobs(conn, cursor, *, api_key, filters, pages, excluded_sites, required_terms, ignored_companies, send) -> None:
    """Run every filter and send the relevant, unseen pages.

    excluded_sites: domains left out of the search (e.g. "linkedin.com", already covered elsewhere).
    required_terms: groups of words; the text needs at least one word of each group.
    send: main.registrar_e_enviar(conn, cursor, link, title, company, date, message, source).
    """
    if not api_key:
        print("\n⚠️  Busca na web desativada: defina BRAVE_API_KEY no .env")
        return

    print("\n🌐 WEB — iniciando varredura...")
    patterns = [_word_pattern(group) for group in required_terms]
    ignored = [company.lower() for company in ignored_companies]

    for web_filter in filters:
        print(f"\n   🔎 {web_filter['nome']}...")
        try:
            found = search_web(api_key, web_filter["termo"], pages, excluded_sites)
        except Exception as e:
            print(f"   ⚠️  Erro: {e}")
            continue

        for item in found:
            text, site, link = item["text"], item["author"], item["link"]

            if not all(p.search(text.lower()) for p in patterns):
                print(f"   🚫 Sem os termos obrigatórios: {text[:55]}")
                continue
            if any(company in site.lower() for company in ignored):
                print(f"   🚫 Site ignorado: {site[:50]}")
                continue
            if _already_sent(cursor, link):
                continue

            summary = text if len(text) <= SUMMARY_MAX_CHARS else text[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"
            message = (
                f"🌐 <b>WEB — {web_filter['nome']}</b>\n\n"
                f"👤 <b>Site:</b> {html.escape(site)}\n"
                f"📅 <b>Data:</b> {item['date']}\n\n"
                f"💬 {html.escape(summary)}\n\n"
                f"🔗 <a href='{html.escape(link)}'>Ver vaga</a>"
            )
            send(conn, cursor, link, text, site, item["date"], message, "WEB")
