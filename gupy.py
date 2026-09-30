"""Gupy job search (JSON API behind portal.gupy.io).

The search matches the job title (jobName) and the workplace (workplaceType).
"""
import html
from collections.abc import Callable
from datetime import datetime, timedelta

import requests

API_URL = "https://portal.gupy.io/api/job-search/jobs"
SITE_URL = "https://portal.gupy.io"
MAX_PAGES = 35
# Consecutive already-sent jobs that end the search: the rest was seen in earlier runs
MAX_SEEN_IN_A_ROW = 20
WORKPLACE_NAMES = {"on-site": "Presencial", "hybrid": "Híbrido", "remote": "Remoto"}
JOB_TYPES = {
    "vacancy_type_effective":  "Efetivo",
    "vacancy_type_apprentice": "Jovem Aprendiz",
    "vacancy_type_internship": "Estágio",
    "vacancy_type_temporary":  "Temporário",
    "vacancy_type_freelancer": "Freelancer",
}
BRAZIL_NAMES = {"brasil", "brazil", "br"}


def _already_sent(cursor, link: str) -> bool:
    cursor.execute("SELECT 1 FROM vagas_enviadas WHERE link = ?", (link,))
    return cursor.fetchone() is not None


def _published(job: dict) -> datetime | None:
    """Publication time in BRT (naive), or None when missing or unreadable."""
    try:
        utc = datetime.strptime(job.get("publishedDate", "").split(".")[0], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, AttributeError):
        return None
    return utc - timedelta(hours=3)


def build_message(filter_name: str, job: dict, published: datetime | None) -> str:
    esc = lambda v: html.escape(str(v))  # noqa: E731
    if "REMOTO" in filter_name:
        place = "Qualquer lugar (Remoto)"
    else:
        place = f"{job.get('city', 'Não informado')} - {job.get('state', 'Não informado')}"
    workplace = WORKPLACE_NAMES.get(job.get("workplaceType", ""), "Não informado")
    job_type = JOB_TYPES.get(job.get("type", ""), "Outros")
    pwd = "Sim" if job.get("disabilities") else "Não informado"
    when = published.strftime("%d/%m/%Y às %H:%M") if published else "Sem data às --:--"
    return (
        f"🟣 <b>GUPY — {filter_name}</b>\n\n"
        f"💼 <b>Vaga:</b> {esc(job.get('name', 'Título Indisponível'))}\n"
        f"🏢 <b>Empresa:</b> {esc(job.get('careerPageName', 'Empresa não informada'))}\n"
        f"📍 <b>Local:</b> {esc(place)}\n"
        f"💻 <b>Modelo:</b> {workplace}\n"
        f"📄 <b>Tipo:</b> {job_type}\n"
        f"♿ <b>PCD:</b> {pwd}\n"
        f"📅 <b>Data:</b> {when}\n\n"
        f"🔗 <a href='{esc(job['jobUrl'])}'>Aplicar na Gupy</a>"
    )


def search_jobs(conn, cursor, *, filters: list[dict], max_age_days: int, user_agent: str,
                send: Callable, check: Callable[[str, str], tuple[bool, str]],
                errors: list[str] | None = None) -> None:
    """Run every filter and send the unseen jobs published in the last max_age_days.

    filters: {"nome", "params"}; params go straight to the API (jobName, workplaceType, limit).
    check: main.filtros_basicos(title, company) -> (blocked, reason).
    send: main.registrar_e_enviar(conn, cursor, link, title, company, date, message, source).
    errors: failed searches are appended here, as sentences ready for the Telegram alert.
    """
    errors = [] if errors is None else errors
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Origin": SITE_URL,
    }

    for job_filter in filters:
        name = job_filter["nome"]
        print(f"\n   🔎 {name}...")
        seen_in_a_row = 0

        for page in range(1, MAX_PAGES + 1):
            params = job_filter["params"].copy()
            params["offset"] = (page - 1) * params.get("limit", 10)

            try:
                resp = requests.get(API_URL, headers=headers, params=params, timeout=15)
                if resp.status_code != 200:
                    print(f"   🛑 HTTP {resp.status_code}")
                    errors.append(f"{name}: a Gupy recusou a busca ({resp.status_code}).")
                    break

                jobs = resp.json().get("data", [])
                if not jobs:
                    print("   🔚 Sem mais vagas.")
                    break

                for job in jobs:
                    link = job.get("jobUrl", "")
                    if not link:
                        continue

                    country = job.get("country", "")
                    if country and country.lower() not in BRAZIL_NAMES:
                        continue

                    published = _published(job)
                    if published and datetime.now() - published > timedelta(days=max_age_days):
                        print(f"   📅 Vaga antiga ({published:%d/%m/%Y}). Encerrando busca.")
                        seen_in_a_row = MAX_SEEN_IN_A_ROW
                        break

                    title = job.get("name", "Título Indisponível")
                    company = job.get("careerPageName", "Empresa não informada")
                    blocked, reason = check(title, company)
                    if blocked:
                        print(f"   {reason}")
                        continue

                    if _already_sent(cursor, link):
                        seen_in_a_row += 1
                        if seen_in_a_row >= MAX_SEEN_IN_A_ROW:
                            break
                        continue

                    seen_in_a_row = 0
                    date = published.strftime("%d/%m/%Y") if published else "Sem data"
                    send(conn, cursor, link, title, company, date,
                         build_message(name, job, published), "GUPY")

                if seen_in_a_row >= MAX_SEEN_IN_A_ROW:
                    print("   🛑 Encerrando paginação.")
                    break

            # Before RequestException: an invalid JSON body is both, and the site did answer
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                print(f"   ⚠️  Erro: {e}")
                errors.append(f"{name}: erro ao ler as vagas ({e}).")
                break
            except requests.RequestException as e:
                print(f"   ⚠️  Erro: {e}")
                errors.append(f"{name}: o site não respondeu.")
                break
