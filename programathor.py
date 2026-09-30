"""ProgramaThor job search (HTML scraping of the public job list).

ProgramaThor sits behind Cloudflare, which blocks GitHub's datacenter IPs (403);
in CI the requests go through a WARP SOCKS proxy (see .github/workflows/vagas.yml).
"""
import html
import time
from collections.abc import Callable
from datetime import datetime

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

BASE_URL = "https://programathor.com.br"
MAX_PAGES = 5
PAGE_INTERVAL = 1  # seconds between pages
FIELD_ICONS = {
    "fa-briefcase": "company",
    "fa-map-marker-alt": "location",
    "fa-money-bill-alt": "salary",
    "fa-chart-bar": "level",
    "fa-file-alt": "type",
}


def build_url(term: str, location: str = "") -> str:
    """/jobs-{term}/{location}, or /jobs-{term} without a location filter."""
    path = f"/jobs-{term.lower()}"
    return f"{BASE_URL}{path}/{location}" if location else f"{BASE_URL}{path}"


def _icon_fields(card) -> dict[str, str]:
    """Card details keyed by their icon: salary is optional, so position is not reliable."""
    fields: dict[str, str] = {}
    for span in card.select(".cell-list-content-icon span"):
        icon = span.find("i")
        classes = icon.get("class", []) if icon else []
        field = next((FIELD_ICONS[c] for c in classes if c in FIELD_ICONS), None)
        if field:
            fields[field] = span.get_text(strip=True)
    return fields


def parse_cards(page_html: str) -> list[dict]:
    """Jobs listed on one result page; expired ones are left out."""
    jobs: list[dict] = []
    for card in BeautifulSoup(page_html, "html.parser").find_all("div", class_="cell-list"):
        link_el = card.find("a", href=lambda h: h and "/jobs/" in h)
        if not link_el:
            continue

        title_el = card.find("h3")
        raw_title = title_el.get_text(strip=True) if title_el else ""
        if "vencida" in raw_title.lower():
            continue

        fields = _icon_fields(card)
        jobs.append({
            "link": BASE_URL + link_el["href"],
            "title": raw_title.replace("NOVA", "").strip() or "Título Indisponível",
            "company": fields.get("company") or "Empresa não informada",
            "location": fields.get("location", ""),
            "salary": fields.get("salary", ""),
            "level": fields.get("level", ""),
            "type": fields.get("type", ""),
            "tags": [t.get_text(strip=True) for t in card.select("span.tag-list")],
        })
    return jobs


def build_message(filter_name: str, job: dict) -> str:
    esc = lambda v: html.escape(str(v))  # noqa: E731
    tags = ", ".join(job["tags"][:6])
    return (
        f"🟤 <b>PROGRAMATHOR — {filter_name}</b>\n\n"
        f"💼 <b>Vaga:</b> {esc(job['title'])}\n"
        f"🏢 <b>Empresa:</b> {esc(job['company'])}\n"
        f"📍 <b>Local:</b> {esc(job['location'])}\n"
        f"📄 <b>Nível:</b> {esc(job['level'])}"
        + (f" · {esc(job['type'])}" if job["type"] else "") + "\n"
        + (f"💰 <b>Salário:</b> {esc(job['salary'])}\n" if job["salary"] else "")
        + (f"🛠️  <b>Stack:</b> <i>{esc(tags)}</i>\n" if tags else "")
        + "\n"
        f"🔗 <a href='{esc(job['link'])}'>Aplicar no ProgramaThor</a>"
    )


def _already_sent(cursor, link: str) -> bool:
    cursor.execute("SELECT 1 FROM vagas_enviadas WHERE link = ?", (link,))
    return cursor.fetchone() is not None


def search_jobs(conn, cursor, *, filters: list[dict], user_agent: str, send: Callable,
                check: Callable[[str, str], tuple[bool, str]], proxy: str | None = None,
                errors: list[str] | None = None) -> None:
    """Run every filter and send the unseen jobs, up to MAX_PAGES pages each.

    filters: {"nome", "termo", "local_filtro"}; local_filtro is the URL slug ("remoto", "sao-paulo", "").
    check: main.filtros_basicos(title, company) -> (blocked, reason).
    send: main.registrar_e_enviar(conn, cursor, link, title, company, date, message, source).
    proxy: SOCKS/HTTP proxy URL for the requests (WARP in CI).
    errors: failed searches are appended here, as sentences ready for the Telegram alert.
    """
    errors = [] if errors is None else errors
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None

    for job_filter in filters:
        name = job_filter["nome"]
        print(f"\n   🔎 {name}...")
        url = build_url(job_filter["termo"], job_filter.get("local_filtro", ""))

        for page in range(1, MAX_PAGES + 1):
            params = {} if page == 1 else {"page": page}
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=15, proxies=proxies)
                if resp.status_code != 200:
                    print(f"   🛑 HTTP {resp.status_code}")
                    errors.append(f"{name}: o ProgramaThor recusou a busca ({resp.status_code}).")
                    break

                jobs = parse_cards(resp.text)
                if not jobs:
                    print("   🔚 Sem mais vagas.")
                    break

                new_on_page = 0
                for job in jobs:
                    blocked, reason = check(job["title"], job["company"])
                    if blocked:
                        print(f"   {reason}")
                        continue
                    if _already_sent(cursor, job["link"]):
                        continue
                    new_on_page += 1
                    send(conn, cursor, job["link"], job["title"], job["company"],
                         datetime.now().strftime("%d/%m/%Y"), build_message(name, job), "PROGRAMATHOR")

                print(f"   📄 Página {page}: {len(jobs)} vagas, {new_on_page} novas.")
                if new_on_page == 0:
                    break
                time.sleep(PAGE_INTERVAL)

            except requests.RequestException as e:
                print(f"   ⚠️  Erro: {e}")
                errors.append(f"{name}: o site não respondeu.")
                break
            except Exception as e:
                print(f"   ⚠️  Erro: {e}")
                errors.append(f"{name}: erro ao ler as vagas ({e}).")
                break
