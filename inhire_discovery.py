"""Discover companies (tenants) that publish jobs on InHire.

InHire has no public list of companies: its job API needs each company's
subdomain (empresa.inhire.app). So we search the web for pages on *.inhire.app
and keep every subdomain found in SQLite. Each discovery run adds new ones,
so the list keeps growing over time. Sources: Yahoo (free) and Brave (optional).
"""
import re
import time
from datetime import datetime, timedelta
from urllib.parse import unquote

import requests

from linkedin_posts import BRAVE_URL, MAX_OFFSET, REQUEST_INTERVAL, RESULTS_PER_PAGE

YAHOO_URL = "https://search.yahoo.com/search"
YAHOO_PAGES = 5         # result pages read per query
YAHOO_PAGE_STEP = 7     # Yahoo's `b` param: index of the first result on the page
YAHOO_INTERVAL = 2      # seconds between requests, to avoid being blocked
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

# Subdomains that belong to InHire itself, not to a hiring company
NON_TENANTS = {"www", "api", "app", "files", "status", "plugin", "senior", "blog", "help", "docs", "cdn", "static"}

# Not preceded by a letter, dot or dash: skips "senior.plugin.inhire.app"
_RE_TENANT = re.compile(r"(?<![\w.-])([a-z0-9][a-z0-9-]*)\.inhire\.app", re.I)


def extract_tenants(text: str) -> set[str]:
    """Company subdomains mentioned in `text` (URL-encoded links included)."""
    return {slug.lower() for slug in _RE_TENANT.findall(unquote(text))} - NON_TENANTS


def search_yahoo(query: str) -> set[str]:
    """Tenants in the first Yahoo result pages (free, no API key)."""
    tenants: set[str] = set()
    for page in range(YAHOO_PAGES):
        params = {"p": query, "b": 1 + page * YAHOO_PAGE_STEP}
        resp = requests.get(YAHOO_URL, params=params, headers=HEADERS, timeout=15)
        time.sleep(YAHOO_INTERVAL)
        # Yahoo sometimes answers 500 to a single page; the next one usually works
        if resp.status_code == 200:
            tenants |= extract_tenants(resp.text)
        else:
            print(f"   🛑 Yahoo HTTP {resp.status_code}")
    return tenants


def search_brave(api_key: str, query: str) -> set[str]:
    """Tenants in every Brave result page (no date limit: companies don't expire)."""
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    tenants: set[str] = set()
    for offset in range(MAX_OFFSET + 1):
        params = {"q": query, "count": RESULTS_PER_PAGE, "offset": offset}
        resp = requests.get(BRAVE_URL, params=params, headers=headers, timeout=15)
        time.sleep(REQUEST_INTERVAL)
        if resp.status_code != 200:
            print(f"   🛑 Brave HTTP {resp.status_code}: {resp.text[:120]}")
            break
        data = resp.json()
        for result in data.get("web", {}).get("results", []):
            tenants |= extract_tenants(result.get("url", ""))
        if not data.get("query", {}).get("more_results_available"):
            break
    return tenants


def init_tables(cursor) -> None:
    cursor.execute("CREATE TABLE IF NOT EXISTS inhire_tenants (slug TEXT PRIMARY KEY, found_at TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS inhire_discovery (last_run TEXT)")


def load_tenants(cursor) -> list[str]:
    cursor.execute("SELECT slug FROM inhire_tenants ORDER BY slug")
    return [row[0] for row in cursor.fetchall()]


def remove_tenant(conn, cursor, slug: str) -> None:
    """Forget a subdomain InHire no longer recognizes."""
    cursor.execute("DELETE FROM inhire_tenants WHERE slug = ?", (slug,))
    conn.commit()


def discovery_due(cursor, every: timedelta, now: datetime | None = None) -> bool:
    cursor.execute("SELECT last_run FROM inhire_discovery")
    row = cursor.fetchone()
    if not row:
        return True
    return (now or datetime.now()) - datetime.fromisoformat(row[0]) >= every


def discover(conn, cursor, *, queries: list[str], brave_api_key: str | None, every: timedelta) -> list[str]:
    """Search for new tenants when the last run is older than `every`; return all known ones."""
    init_tables(cursor)
    if not discovery_due(cursor, every):
        return load_tenants(cursor)

    print("\n🔎 INHIRE — procurando novas empresas...")
    found: set[str] = set()
    for query in queries:
        try:
            found |= search_yahoo(query)
            if brave_api_key:
                found |= search_brave(brave_api_key, query)
        except requests.RequestException as e:
            print(f"   ⚠️  Erro na busca '{query}': {e}")

    now = datetime.now().isoformat(timespec="seconds")
    before = len(load_tenants(cursor))
    cursor.executemany(
        "INSERT OR IGNORE INTO inhire_tenants (slug, found_at) VALUES (?, ?)",
        [(slug, now) for slug in sorted(found)],
    )
    # Blocked by every source: retry on the next run instead of waiting `every`
    if found:
        cursor.execute("DELETE FROM inhire_discovery")
        cursor.execute("INSERT INTO inhire_discovery (last_run) VALUES (?)", (now,))
    conn.commit()

    tenants = load_tenants(cursor)
    print(f"   ✅ {len(tenants) - before} empresas novas ({len(tenants)} no total)")
    return tenants
