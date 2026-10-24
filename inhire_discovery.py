"""Discover companies (tenants) that publish jobs on InHire.

InHire has no public list of companies: its job API needs each company's
subdomain (empresa.inhire.app). So we search the web for pages on *.inhire.app
and keep every subdomain found in the bot's database. Each discovery run adds new ones,
so the list keeps growing over time. Sources: Yahoo (free) and Brave (optional).
"""
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import requests

from linkedin_posts import BRAVE_URL, MAX_OFFSET, REQUEST_INTERVAL, RESULTS_PER_PAGE
from store import Store

YAHOO_URL = "https://search.yahoo.com/search"
YAHOO_PAGES = 5         # result pages read per query
YAHOO_PAGE_STEP = 7     # Yahoo's `b` param: index of the first result on the page
YAHOO_INTERVAL = 2      # seconds between requests, to avoid being blocked
RETRY_AFTER = timedelta(days=1)  # wait after a run that found nothing (sources blocked)
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


def search_yahoo(query: str) -> tuple[set[str], bool]:
    """Tenants in the first Yahoo result pages (free, no API key), and whether Yahoo answered.

    Yahoo sometimes fails a single page and the next one works, so the query only
    counts as failed when every page failed.
    """
    tenants: set[str] = set()
    failed = 0
    for page in range(YAHOO_PAGES):
        params = {"p": query, "b": 1 + page * YAHOO_PAGE_STEP}
        try:
            resp = requests.get(YAHOO_URL, params=params, headers=HEADERS, timeout=15)
        except requests.RequestException as e:
            print(f"   🛑 Yahoo: {e}")
            failed += 1
            continue
        finally:
            time.sleep(YAHOO_INTERVAL)
        if resp.status_code == 200:
            tenants |= extract_tenants(resp.text)
        else:
            print(f"   🛑 Yahoo HTTP {resp.status_code}")
            failed += 1
    return tenants, failed < YAHOO_PAGES


def search_brave(api_key: str, query: str) -> tuple[set[str], str | None]:
    """Tenants in every Brave result page (no date limit: companies don't expire).

    The second value explains why Brave stopped answering, or is None.
    """
    headers = {"Accept": "application/json", "X-Subscription-Token": api_key}
    tenants: set[str] = set()
    for offset in range(MAX_OFFSET + 1):
        params = {"q": query, "count": RESULTS_PER_PAGE, "offset": offset}
        try:
            resp = requests.get(BRAVE_URL, params=params, headers=headers, timeout=15)
        except requests.RequestException as e:
            print(f"   🛑 Brave: {e}")
            return tenants, "sem resposta"
        finally:
            time.sleep(REQUEST_INTERVAL)
        if resp.status_code != 200:
            print(f"   🛑 Brave HTTP {resp.status_code}: {resp.text[:120]}")
            # 429 = monthly quota used up or too many requests
            return tenants, f"código {resp.status_code}"
        data = resp.json()
        for result in data.get("web", {}).get("results", []):
            tenants |= extract_tenants(result.get("url", ""))
        if not data.get("query", {}).get("more_results_available"):
            break
    return tenants, None


def load_tenants(db: Store) -> list[str]:
    return db.inhire_tenants()


def remove_tenant(db: Store, slug: str) -> None:
    """Forget a subdomain InHire no longer recognizes."""
    db.remove_inhire_tenant(slug)


def discovery_due(db: Store, every: timedelta, now: datetime | None = None) -> bool:
    """True when `every` has passed, or RETRY_AFTER when the last run found nothing."""
    last = db.inhire_discovery()
    if last is None:
        return True
    last_run, found = last
    wait = RETRY_AFTER if found == 0 else every
    return (now or datetime.now(timezone.utc)) - last_run >= wait


def discover(db: Store, *, queries: list[str], brave_api_key: str | None, every: timedelta,
             errors: list[str] | None = None) -> list[str]:
    """Search for new tenants when the last run is older than `every`; return all known ones.

    Problems worth telling the user about are appended to `errors`.
    """
    errors = [] if errors is None else errors
    if not discovery_due(db, every):
        return load_tenants(db)

    print("\n🔎 INHIRE — procurando novas empresas...")
    found: set[str] = set()
    yahoo_failed = 0
    brave_errors: list[str] = []
    for query in queries:
        tenants, answered = search_yahoo(query)
        found |= tenants
        yahoo_failed += not answered
        if brave_api_key:
            tenants, error = search_brave(brave_api_key, query)
            found |= tenants
            if error:
                brave_errors.append(error)
    # One line per source, not per query, so the alert stays short
    if yahoo_failed:
        errors.append(f"O Yahoo não respondeu a {yahoo_failed} de {len(queries)} buscas.")
    if brave_errors:
        reasons = ", ".join(sorted(set(brave_errors)))
        errors.append(f"A Brave falhou em {len(brave_errors)} de {len(queries)} buscas ({reasons}).")

    now = datetime.now(timezone.utc)
    before = len(load_tenants(db))
    db.add_inhire_tenants(sorted(found), now)
    # Saved even when nothing was found, so a blocked source is retried after
    # RETRY_AFTER instead of on every run
    db.set_inhire_discovery(now, len(found))
    if not found:
        errors.append("A busca não achou nenhuma empresa (o site de busca pode ter bloqueado o bot). "
                      "Nova tentativa amanhã.")

    tenants = load_tenants(db)
    print(f"   ✅ {len(tenants) - before} empresas novas ({len(tenants)} no total)")
    return tenants
