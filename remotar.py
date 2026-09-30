"""Remotar job search (public JSON API behind remotar.com.br).

The search also matches the job description, so the required title term is
checked by main.filtros_basicos like in every other source.
"""
import html
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import requests

API_URL = "https://api.remotar.com.br/jobs"
SITE_URL = "https://remotar.com.br"
MAX_PAGES = 5  # the API returns 50 jobs per page, newest first
PAGE_INTERVAL = 1  # seconds between pages
WORKPLACE_NAMES = {"remote": "Remoto", "hybrid": "Híbrido", "on-site": "Presencial"}
# Remote jobs from companies abroad: salary in a foreign currency or this tag
FOREIGN_CURRENCIES = {"USD", "EUR", "GBP"}
FOREIGN_TAG = "vaga internacional"
# Tags that repeat the workplace already shown in "Local"
WORKPLACE_TAGS = {"100% remoto", "vaga híbrida"}


def _tag_names(job: dict) -> list[str]:
    """Tag names without their leading emoji ("🧓🏽 Sênior" -> "Sênior")."""
    names = []
    for job_tag in job.get("jobTags") or []:
        name = (job_tag.get("tag") or {}).get("name", "")
        name = re.sub(r"^[^\w]+", "", name).strip()
        if name:
            names.append(name)
    return names


def _salary(job: dict) -> str:
    """"R$ 5.000 a R$ 8.000", "Até R$ 8.000", or "" when not informed."""
    salary = job.get("jobSalary") or {}
    try:
        low, high = float(salary.get("from") or 0), float(salary.get("to") or 0)
    except (TypeError, ValueError):
        return ""  # an odd salary must not cost the whole job
    if not (low or high):
        return ""
    prefix = "R$" if salary.get("currency") == "BRL" else salary.get("currency") or ""
    fmt = lambda v: f"{prefix} {v:,.0f}".replace(",", ".").strip()  # noqa: E731
    if low and high:
        return f"{fmt(low)} a {fmt(high)}"
    return f"A partir de {fmt(low)}" if low else f"Até {fmt(high)}"


def is_foreign(job: dict) -> bool:
    currency = (job.get("jobSalary") or {}).get("currency")
    return currency in FOREIGN_CURRENCIES or any(t.lower() == FOREIGN_TAG for t in _tag_names(job))


def parse_job(job: dict) -> dict:
    location = ", ".join(p for p in (job.get("city"), job.get("state")) if p)
    return {
        "link": f"{SITE_URL}/job/{job['id']}",
        "title": (job.get("title") or "").strip() or "Título Indisponível",
        "company": (job.get("company") or {}).get("name") or job.get("companyDisplayName")
                   or "Empresa não informada",
        "workplace": job.get("type") or "",
        "location": location,
        "salary": _salary(job),
        "tags": [t for t in _tag_names(job) if t.lower() not in WORKPLACE_TAGS],
        "created_at": datetime.fromisoformat(job["createdAt"]),
        "foreign": is_foreign(job),
    }


def build_message(filter_name: str, job: dict) -> str:
    esc = lambda v: html.escape(str(v))  # noqa: E731
    workplace = WORKPLACE_NAMES.get(job["workplace"], job["workplace"])
    place = " · ".join(p for p in (workplace, job["location"]) if p)
    tags = " · ".join(job["tags"][:6])
    return (
        f"🟠 <b>REMOTAR — {filter_name}</b>\n\n"
        f"💼 <b>Vaga:</b> {esc(job['title'])}\n"
        f"🏢 <b>Empresa:</b> {esc(job['company'])}\n"
        + (f"📍 <b>Local:</b> {esc(place)}\n" if place else "")
        + (f"🏷️ <b>Detalhes:</b> {esc(tags)}\n" if tags else "")
        + (f"💰 <b>Salário:</b> {esc(job['salary'])}\n" if job["salary"] else "")
        + f"📅 <b>Publicada em:</b> {job['created_at'].strftime('%d/%m/%Y')}\n\n"
        f"🔗 <a href='{esc(job['link'])}'>Ver vaga no Remotar</a>"
    )


def _already_sent(cursor, link: str) -> bool:
    cursor.execute("SELECT 1 FROM vagas_enviadas WHERE link = ?", (link,))
    return cursor.fetchone() is not None


def _skip_reason(job: dict, workplaces: list[str], ignore_foreign: bool,
                 check: Callable[[str, str], tuple[bool, str]]) -> str:
    """Why the job is not sent, or "" when it passes every filter."""
    if workplaces and job["workplace"] not in workplaces:
        return f"🚫 Modalidade {WORKPLACE_NAMES.get(job['workplace'], job['workplace'])}: {job['title'][:50]}"
    if ignore_foreign and job["foreign"]:
        return f"🚫 Vaga internacional: {job['title'][:50]}"
    blocked, reason = check(job["title"], job["company"])
    return reason if blocked else ""


def search_jobs(conn, cursor, *, filters: list[dict], max_age_days: int, ignore_foreign: bool,
                user_agent: str, send: Callable, check: Callable[[str, str], tuple[bool, str]],
                errors: list[str] | None = None, now: datetime | None = None) -> None:
    """Run every filter and send the unseen jobs published in the last max_age_days.

    filters: {"nome", "termo", "modalidades"}; modalidades lists "remote" / "hybrid" / "on-site"
        (empty or missing = any).
    ignore_foreign: skip remote jobs from companies abroad (foreign currency salary or tag).
    check: main.filtros_basicos(title, company) -> (blocked, reason).
    send: main.registrar_e_enviar(conn, cursor, link, title, company, date, message, source).
    errors: failed searches are appended here, as sentences ready for the Telegram alert.
    """
    errors = [] if errors is None else errors
    headers = {"User-Agent": user_agent, "Accept": "application/json"}

    for job_filter in filters:
        name = job_filter["nome"]
        workplaces = job_filter.get("modalidades") or []
        print(f"\n   🔎 {name}...")

        for page in range(1, MAX_PAGES + 1):
            params = {"search": job_filter["termo"], "active": "true", "page": page}
            try:
                resp = requests.get(API_URL, params=params, headers=headers, timeout=15)
                if resp.status_code != 200:
                    print(f"   🛑 HTTP {resp.status_code}")
                    errors.append(f"{name}: o Remotar recusou a busca ({resp.status_code}).")
                    break

                body = resp.json()
                raw_jobs = body.get("data") or []
                if not raw_jobs:
                    print("   🔚 Sem mais vagas.")
                    break

                jobs = []
                for raw in raw_jobs:
                    try:
                        jobs.append(parse_job(raw))
                    except (ValueError, KeyError, TypeError) as e:
                        # One malformed job must not hide the others on every run. Only logged:
                        # an alert would repeat every run until the job leaves the search window
                        print(f"   ⚠️  Vaga ilegível ({e}): {str(raw)[:80]}")
                if not jobs:
                    # No job could be read: the API most likely changed its format
                    errors.append(f"{name}: o Remotar mudou o formato das vagas e o bot não conseguiu ler nenhuma.")
                    break

                current = now or datetime.now(jobs[0]["created_at"].tzinfo)
                cutoff = current - timedelta(days=max_age_days)
                recent = [j for j in jobs if j["created_at"] >= cutoff]
                sent = 0
                for job in recent:
                    reason = _skip_reason(job, workplaces, ignore_foreign, check)
                    if reason:
                        print(f"   {reason}")
                        continue
                    if _already_sent(cursor, job["link"]):
                        continue
                    sent += 1
                    send(conn, cursor, job["link"], job["title"], job["company"],
                         job["created_at"].strftime("%d/%m/%Y"), build_message(name, job), "REMOTAR")

                print(f"   📄 Página {page}: {len(jobs)} vagas, {sent} novas.")
                # Newest first: when the page ends in an old job, the next pages are older still.
                # The last job is checked, not any job, so one out-of-order job doesn't stop the search
                last_page = (body.get("meta") or {}).get("last_page", page)
                if jobs[-1]["created_at"] < cutoff or page >= last_page:
                    break
                time.sleep(PAGE_INTERVAL)

            # Before RequestException: an invalid JSON body is both, and the site did answer
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                print(f"   ⚠️  Erro: {e}")
                errors.append(f"{name}: erro ao ler as vagas ({e}).")
                break
            except requests.RequestException as e:
                print(f"   ⚠️  Erro: {e}")
                errors.append(f"{name}: o site não respondeu.")
                break
