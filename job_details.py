"""Reads the page of a job already sent: full text, seniority and employment type, per source.

Pure parsers (no network) plus `fetch_details`, which builds the right request for the site of the link,
does a single GET through the proxy and translates the answer into `JobDetails` or `DetailsError`.
Deciding what to do about a failure (retry, give up, pause) is not done here.
"""
import html as html_lib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

import gupy
from http_proxy import scraper_proxies
from store import Source

MAX_TEXT_CHARS = 50_000
LINKEDIN_POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
INHIRE_JOB_URL = "https://api.inhire.app/job-posts/public/pages/{job_id}"
REMOTAR_JOB_URL = "https://api.remotar.com.br/jobs/{job_id}"
# (key in the Gupy JSON, title shown in the text), in the order they appear
GUPY_SECTIONS: tuple[tuple[str, str], ...] = (
    ("description", "Descrição da vaga"),
    ("responsibilities", "Responsabilidades e atribuições"),
    ("prerequisites", "Requisitos e qualificações"),
    ("relevantExperiences", "Informações adicionais"),
)
NOT_APPLICABLE = "Not Applicable"
LINKEDIN_SENIORITY_LABEL = "Seniority level"
LINKEDIN_EMPLOYMENT_LABEL = "Employment type"
HIDDEN_TAGS = ("script", "style", "nav", "header", "footer", "noscript")
JSON_LD_TYPE = "JobPosting"
SOLIDES_SENIORITY_RE = re.compile(r'\\?"seniority\\?"\s*:\s*\[\s*\{\s*\\?"name\\?"\s*:\s*\\?"([^"\\]+)')
HTTP_CLOSED = {404, 410}
HTTP_BLOCKED = {403, 429, 999}


@dataclass(frozen=True)
class JobDetails:
    text: str
    seniority: str = ""
    employment_type: str = ""


class DetailsFailure(StrEnum):
    CLOSED = "closed"
    BLOCKED = "blocked"
    REFUSED = "refused"
    NETWORK = "network"
    UNREADABLE = "unreadable"


class DetailsError(Exception):
    def __init__(self, kind: DetailsFailure, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _unreadable(detail: str) -> DetailsError:
    return DetailsError(DetailsFailure.UNREADABLE, detail)


def source_from_link(link: str) -> Source:
    parts = urlsplit((link or "").strip().lower())
    host, path = parts.hostname or "", parts.path
    if host.endswith("gupy.io"):
        return Source.GUPY
    if "linkedin.com" in host:
        if "/jobs/view" in path:
            return Source.LINKEDIN
        if path.startswith("/posts") or path.startswith("/feed/"):
            return Source.LINKEDIN_POSTS
        return Source.WEB
    if host.endswith("inhire.app"):
        return Source.INHIRE
    if host.endswith("solides.com.br"):
        return Source.SOLIDES
    if host.endswith("remotar.com.br"):
        return Source.REMOTAR
    return Source.WEB


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = html_lib.unescape(BeautifulSoup(raw, "html.parser").get_text("\n"))
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:MAX_TEXT_CHARS]


def _join(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if v and str(v).strip())
    return str(value).strip() if value else ""


def parse_gupy_page(html: str) -> JobDetails:
    script = BeautifulSoup(html or "", "html.parser").find("script", id="__NEXT_DATA__")
    if script is None or not script.string:
        raise _unreadable("página da Gupy sem dados da vaga")
    try:
        job = json.loads(script.string)["props"]["pageProps"]["job"]
    except (ValueError, KeyError, TypeError):
        raise _unreadable("dados da vaga da Gupy ilegíveis") from None
    if not isinstance(job, dict):
        raise _unreadable("dados da vaga da Gupy ilegíveis")
    sections = []
    for key, title in GUPY_SECTIONS:
        body = html_to_text(job.get(key))
        if body:
            sections.append(f"{title}\n{body}")
    text = "\n\n".join(sections)
    if not text:
        raise _unreadable("vaga da Gupy sem texto")
    job_type = job.get("jobType") or ""
    return JobDetails(text=text, employment_type=gupy.JOB_TYPES.get(job_type, job_type))


def parse_linkedin_posting(html: str) -> JobDetails:
    soup = BeautifulSoup(html or "", "html.parser")
    markup = soup.select_one(".show-more-less-html__markup")
    text = html_to_text(str(markup)) if markup else ""
    if not text:
        raise _unreadable("vaga do LinkedIn sem texto")
    values = {LINKEDIN_SENIORITY_LABEL: "", LINKEDIN_EMPLOYMENT_LABEL: ""}
    for item in soup.select(".description__job-criteria-item"):
        header = item.select_one(".description__job-criteria-subheader")
        value = item.select_one(".description__job-criteria-text")
        label = header.get_text(" ", strip=True) if header else ""
        if label in values and value:
            cleaned = re.sub(r"\s+", " ", value.get_text(" ")).strip()
            values[label] = "" if cleaned == NOT_APPLICABLE else cleaned
    return JobDetails(text=text, seniority=values[LINKEDIN_SENIORITY_LABEL],
                      employment_type=values[LINKEDIN_EMPLOYMENT_LABEL])


def parse_inhire_job(job: dict) -> JobDetails:
    text = html_to_text(job.get("description"))
    if not text:
        raise _unreadable("vaga da InHire sem descrição")
    return JobDetails(text=text, employment_type=_join(job.get("contractType")))


def _find_job_posting(node) -> dict | None:
    if isinstance(node, list):
        for item in node:
            found = _find_job_posting(item)
            if found:
                return found
    elif isinstance(node, dict):
        node_type = node.get("@type")
        if node_type == JSON_LD_TYPE or (isinstance(node_type, list) and JSON_LD_TYPE in node_type):
            return node
        return _find_job_posting(node.get("@graph"))
    return None


def _json_ld_posting(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except ValueError:
            continue
        found = _find_job_posting(data)
        if found:
            return found
    return None


def parse_solides_page(html: str) -> JobDetails:
    posting = _json_ld_posting(BeautifulSoup(html or "", "html.parser"))
    if posting is None:
        raise _unreadable("página da Solides sem dados da vaga")
    text = html_to_text(posting.get("description"))
    if not text:
        raise _unreadable("vaga da Solides sem texto")
    match = SOLIDES_SENIORITY_RE.search(html)
    return JobDetails(text=text, seniority=match.group(1).strip() if match else "",
                      employment_type=_join(posting.get("employmentType")))


def _remotar_part(value) -> str:
    if isinstance(value, list):
        names = [item.get("name") if isinstance(item, dict) else item for item in value]
        return "\n".join(str(n).strip() for n in names if n and str(n).strip())
    return html_to_text(value) if isinstance(value, str) else ""


def parse_remotar_job(job: dict) -> JobDetails:
    data = job.get("data")
    if isinstance(data, dict):
        job = data
    parts = [html_to_text(job.get("description"))]
    parts += [_remotar_part(job.get("moreInfos")), _remotar_part(job.get("jobRequirements"))]
    text = "\n\n".join(p for p in parts if p)
    if not text:
        raise _unreadable("vaga da Remotar sem texto")
    return JobDetails(text=text)


def parse_web_page(html: str) -> JobDetails:
    soup = BeautifulSoup(html or "", "html.parser")
    posting = _json_ld_posting(soup)
    if posting:
        text = html_to_text(posting.get("description"))
        if text:
            return JobDetails(text=text, employment_type=_join(posting.get("employmentType")))
    for tag in soup.find_all(HIDDEN_TAGS):
        tag.decompose()
    text = html_to_text(str(soup.body or soup))
    if not text:
        raise _unreadable("página sem texto visível")
    return JobDetails(text=text)


def _request_for(link: str, source: Source) -> tuple[str, dict[str, str]]:
    """URL and extra headers of the request; DetailsError when the id cannot be taken from the link."""
    parts = urlsplit(link.strip())
    path = parts.path
    if source == Source.LINKEDIN:
        match = re.search(r"-(\d+)/?$", path) or re.fullmatch(r"/jobs/view/(\d+)/?", path)
        if match:
            return LINKEDIN_POSTING_URL.format(job_id=match.group(1)), {}
    elif source == Source.INHIRE:
        host = (parts.hostname or "").lower()
        labels = host.split(".")
        match = re.search(r"/vagas/([^/?#]+)", path)
        if match and len(labels) > 2 and labels[0] != "www":
            return INHIRE_JOB_URL.format(job_id=match.group(1)), {"X-Tenant": labels[0], "Accept": "application/json"}
    elif source == Source.REMOTAR:
        match = re.search(r"/job/([^/?#]+)", path)
        if match:
            return REMOTAR_JOB_URL.format(job_id=match.group(1)), {"Accept": "application/json"}
    else:
        return link.strip(), {}
    raise _unreadable("link sem o código da vaga")


def fetch_details(link: str, *, user_agent: str, proxy: str | None, timeout: float = 20) -> JobDetails:
    source = source_from_link(link)
    if source == Source.LINKEDIN_POSTS:
        raise ValueError(f"publicação do LinkedIn não tem página de vaga: {link}")
    url, extra_headers = _request_for(link, source)
    headers = {"User-Agent": user_agent, **extra_headers}
    try:
        response = requests.get(url, headers=headers, timeout=timeout, proxies=scraper_proxies(proxy),
                                allow_redirects=True)
        status = response.status_code
        if status in HTTP_CLOSED:
            raise DetailsError(DetailsFailure.CLOSED, f"o site respondeu {status}")
        if status in HTTP_BLOCKED:
            raise DetailsError(DetailsFailure.BLOCKED, f"o site respondeu {status}")
        if status != 200:
            raise DetailsError(DetailsFailure.REFUSED, f"o site respondeu {status}")
        match source:
            case Source.GUPY:
                return parse_gupy_page(response.text)
            case Source.LINKEDIN:
                return parse_linkedin_posting(response.text)
            case Source.INHIRE:
                return parse_inhire_job(response.json())
            case Source.SOLIDES:
                return parse_solides_page(response.text)
            case Source.REMOTAR:
                return parse_remotar_job(response.json())
            case _:
                return parse_web_page(response.text)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _unreadable(f"resposta ilegível ({type(exc).__name__})") from exc
    except requests.RequestException as exc:
        raise DetailsError(DetailsFailure.NETWORK, f"falha de rede ({type(exc).__name__})") from exc
