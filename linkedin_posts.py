"""LinkedIn posts search through the Brave Search API.

LinkedIn's own content search requires a logged-in session, so posts are found
by searching the web for `site:linkedin.com/posts` restricted to the last 24h.
Free plan: $5 credit per month (~1,000 queries): https://brave.com/search/api/
"""
import re
import html
import time
from datetime import datetime, timedelta, timezone

import requests

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
RESULTS_PER_PAGE = 20  # Brave maximum
MAX_OFFSET = 9         # Brave accepts page offsets 0..9
MAX_AGE = timedelta(hours=24)
REQUEST_INTERVAL = 1.1  # safety margin between queries (Search plan allows 50/s)

# Titles look like "Jane Doe on LinkedIn: text..." / "Jane Doe no LinkedIn: ..."
# or "text... | Jane Doe"
# Same post shows up as br.linkedin.com, pt.linkedin.com, linkedin.com...
_RE_POST_URL = re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com(/posts/[^?#]+?)/?(?:[?#].*)?$", re.IGNORECASE)
_RE_AUTHOR_PREFIX = re.compile(r"^(.+?)\s+(?:on|no|em)\s+LinkedIn\s*:", re.IGNORECASE)


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def parse_author(title: str) -> str:
    match = _RE_AUTHOR_PREFIX.match(title)
    if match:
        return match.group(1).strip()
    if "|" in title:
        return title.rsplit("|", 1)[1].strip() or "Autor não informado"
    return "Autor não informado"


def parse_page_age(page_age: str | None) -> datetime | None:
    """Brave's page_age is ISO 8601 in UTC, usually without offset."""
    try:
        parsed = datetime.fromisoformat(page_age.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_result(result: dict, now: datetime | None = None) -> dict | None:
    """Turn one Brave web result into a post dict, or None if it is not a recent post."""
    url_match = _RE_POST_URL.match(result.get("url") or "")
    if not url_match:
        return None
    # Canonical form keeps the db dedup working across subdomains
    link = "https://www.linkedin.com" + url_match.group(1)
    published = parse_page_age(result.get("page_age"))
    now = now or datetime.now(timezone.utc)
    # freshness=pd filters by Brave's crawl date, so an old post may still slip in
    if published and now - published > MAX_AGE:
        return None
    title = _strip_tags(result.get("title", ""))
    return {
        "link": link,
        "author": parse_author(title),
        "text": f"{title} {_strip_tags(result.get('description', ''))}".strip(),
        "date": (published or now).strftime("%d/%m/%Y"),
    }


def search_posts(api_key: str, query: str, pages: int = 1, errors: list[str] | None = None) -> list[dict]:
    """Search LinkedIn posts from the past 24h matching `query`.

    When Brave refuses a page, the reason ("código 429") is appended to `errors`.
    """
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    posts: list[dict] = []
    for offset in range(min(pages, MAX_OFFSET + 1)):
        params = {
            # site: first, so an OR inside the query can't split it off one side
            "q": f"site:linkedin.com/posts {query}",
            "count": RESULTS_PER_PAGE,
            "offset": offset,
            "freshness": "pd",  # past day
        }
        resp = requests.get(BRAVE_URL, params=params, headers=headers, timeout=15)
        # Always wait: the next request may come from the next filter, not the next page
        time.sleep(REQUEST_INTERVAL)
        if resp.status_code != 200:
            print(f"   🛑 Brave HTTP {resp.status_code}: {resp.text[:120]}")
            if errors is not None:
                errors.append(f"código {resp.status_code}")
            break
        data = resp.json()
        results = data.get("web", {}).get("results", [])
        posts += [p for p in map(parse_result, results) if p]
        if not data.get("query", {}).get("more_results_available"):
            break
    return posts
