"""Parsers and request mapping of job_details, with raw payloads in the shape of each source.

Run: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import job_details  # noqa: E402
from job_details import DetailsError, DetailsFailure, JobDetails  # noqa: E402
from store import Source  # noqa: E402


def gupy_page(job: dict) -> str:
    data = {"props": {"pageProps": {"job": job}}}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'


def linkedin_posting(markup: str, criteria: list[tuple[str, str]]) -> str:
    items = "".join(
        f'<li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader">{label}</h3>'
        f'<span class="description__job-criteria-text description__job-criteria-text--criteria"> {value} </span></li>'
        for label, value in criteria)
    return (f'<section><div class="show-more-less-html__markup">{markup}</div></section>'
            f'<ul class="description__job-criteria-list">{items}</ul>')


def solides_page(json_ld: dict, seniority: str | None) -> str:
    extra = f'<script>{{"seniority":[{{"name":"{seniority}","id":1}}]}}</script>' if seniority else ""
    return f'<html><head><script type="application/ld+json">{json.dumps(json_ld)}</script></head>{extra}</html>'


def response(status: int = 200, text: str = "", payload=None) -> mock.Mock:
    return mock.Mock(status_code=status, text=text, json=mock.Mock(return_value=payload))


POSTING = {"@type": "JobPosting", "description": "<p>Vaga &amp; cia</p>", "employmentType": "FULL_TIME"}


class SourceFromLinkTest(unittest.TestCase):

    def test_source_from_link_gupy(self):
        self.assertEqual(job_details.source_from_link("https://acme.gupy.io/job/abc?jobBoardSource=gupy_portal"),
                         Source.GUPY)

    def test_source_from_link_linkedin_jobs(self):
        self.assertEqual(job_details.source_from_link("https://br.linkedin.com/jobs/view/dev-123"), Source.LINKEDIN)

    def test_source_from_link_linkedin_posts(self):
        self.assertEqual(job_details.source_from_link("https://www.linkedin.com/posts/x"), Source.LINKEDIN_POSTS)

    def test_source_from_link_linkedin_feed(self):
        self.assertEqual(job_details.source_from_link("https://www.linkedin.com/feed/update/1"),
                         Source.LINKEDIN_POSTS)

    def test_source_from_link_linkedin_other_path_is_web(self):
        self.assertEqual(job_details.source_from_link("https://www.linkedin.com/company/x"), Source.WEB)

    def test_source_from_link_inhire(self):
        self.assertEqual(job_details.source_from_link("https://acme.inhire.app/vagas/1/dev"), Source.INHIRE)

    def test_source_from_link_solides(self):
        self.assertEqual(job_details.source_from_link("https://acme.vagas.solides.com.br/vaga/1"), Source.SOLIDES)

    def test_source_from_link_remotar(self):
        self.assertEqual(job_details.source_from_link("https://remotar.com.br/job/1"), Source.REMOTAR)

    def test_source_from_link_other(self):
        self.assertEqual(job_details.source_from_link("https://example.com/vaga"), Source.WEB)

    def test_source_from_link_empty(self):
        self.assertEqual(job_details.source_from_link(""), Source.WEB)


class HtmlToTextTest(unittest.TestCase):

    def test_html_to_text_decodes_entities(self):
        self.assertEqual(job_details.html_to_text("<p>A &amp; B&nbsp;C &amp;amp; D</p>"), "A & B C & D")

    def test_html_to_text_collapses_blank_lines(self):
        raw = "<p>a   \t b</p>\n\n\n\n<p>c</p>"
        self.assertEqual(job_details.html_to_text(raw), "a b\n\nc")

    def test_html_to_text_cuts_at_max_chars(self):
        text = job_details.html_to_text("<p>" + "x" * (job_details.MAX_TEXT_CHARS + 10) + "</p>")
        self.assertEqual(len(text), job_details.MAX_TEXT_CHARS)

    def test_html_to_text_empty(self):
        self.assertEqual(job_details.html_to_text(None), "")
        self.assertEqual(job_details.html_to_text(""), "")


class ParseTest(unittest.TestCase):

    def assertUnreadable(self, call, *args):
        with self.assertRaises(DetailsError) as ctx:
            call(*args)
        self.assertEqual(ctx.exception.kind, DetailsFailure.UNREADABLE)

    def test_parse_gupy_has_sections_in_order_and_type(self):
        job = {"description": "<p>D</p>", "responsibilities": "<p>R</p>", "prerequisites": "<p>P</p>",
               "relevantExperiences": "<p>I</p>", "jobType": "vacancy_type_internship"}
        result = job_details.parse_gupy_page(gupy_page(job))
        self.assertEqual(result.text, "Descrição da vaga\nD\n\nResponsabilidades e atribuições\nR\n\n"
                                      "Requisitos e qualificações\nP\n\nInformações adicionais\nI")
        self.assertEqual(result.employment_type, "Estágio")
        self.assertEqual(result.seniority, "")

    def test_parse_gupy_skips_empty_sections(self):
        job = {"description": "<p>D</p>", "responsibilities": "", "relevantExperiences": None}
        result = job_details.parse_gupy_page(gupy_page(job))
        self.assertEqual(result.text, "Descrição da vaga\nD")

    def test_parse_gupy_unknown_job_type_keeps_raw_value(self):
        job = {"description": "<p>D</p>", "jobType": "vacancy_type_new"}
        self.assertEqual(job_details.parse_gupy_page(gupy_page(job)).employment_type, "vacancy_type_new")
        self.assertEqual(job_details.parse_gupy_page(gupy_page({"description": "D"})).employment_type, "")

    def test_parse_gupy_without_next_data_is_unreadable(self):
        self.assertUnreadable(job_details.parse_gupy_page, "<html><body>oi</body></html>")

    def test_parse_linkedin_reads_text_and_criteria(self):
        html = linkedin_posting("<p>Texto</p>", [("Seniority level", "Mid-Senior level"),
                                                 ("Employment type", "Full-time")])
        self.assertEqual(job_details.parse_linkedin_posting(html),
                         JobDetails("Texto", "Mid-Senior level", "Full-time"))

    def test_parse_linkedin_not_applicable_is_empty(self):
        html = linkedin_posting("<p>Texto</p>", [("Seniority level", "Not Applicable")])
        self.assertEqual(job_details.parse_linkedin_posting(html), JobDetails("Texto"))
        self.assertEqual(job_details.parse_linkedin_posting(linkedin_posting("<p>T</p>", [])), JobDetails("T"))

    def test_parse_inhire_joins_contract_types(self):
        result = job_details.parse_inhire_job({"description": "<p>X</p>", "contractType": ["CLT", "PJ"]})
        self.assertEqual(result, JobDetails("X", "", "CLT, PJ"))

    def test_parse_inhire_without_description_is_unreadable(self):
        self.assertUnreadable(job_details.parse_inhire_job, {"contractType": ["CLT"]})

    def test_parse_solides_reads_json_ld_and_seniority(self):
        result = job_details.parse_solides_page(solides_page(POSTING, "Pleno"))
        self.assertEqual(result, JobDetails("Vaga & cia", "Pleno", "FULL_TIME"))

    def test_parse_solides_without_seniority(self):
        self.assertEqual(job_details.parse_solides_page(solides_page(POSTING, None)).seniority, "")

    def test_parse_remotar_joins_extra_fields(self):
        job = {"description": "<p>D</p>", "moreInfos": "<p>M</p>", "jobRequirements": [{"name": "A"}, "B"]}
        self.assertEqual(job_details.parse_remotar_job(job).text, "D\n\nM\n\nA\nB")

    def test_parse_remotar_has_no_seniority(self):
        result = job_details.parse_remotar_job({"data": {"description": "<p>D</p>", "moreInfos": None}})
        self.assertEqual(result, JobDetails("D"))

    def test_parse_web_with_json_ld(self):
        html = solides_page(POSTING, None)
        self.assertEqual(job_details.parse_web_page(html), JobDetails("Vaga & cia", "", "FULL_TIME"))

    def test_parse_web_json_ld_in_graph(self):
        html = solides_page({"@graph": [{"@type": "WebSite"}, POSTING]}, None)
        self.assertEqual(job_details.parse_web_page(html).text, "Vaga & cia")

    def test_parse_web_without_json_ld_uses_visible_text(self):
        html = ("<html><head><style>.a{}</style></head><body><nav>Menu</nav><header>Topo</header>"
                "<main><p>Vaga aberta</p></main><script>x()</script><footer>Rodapé</footer></body></html>")
        self.assertEqual(job_details.parse_web_page(html), JobDetails("Vaga aberta"))

    def test_parse_web_empty_body_is_unreadable(self):
        self.assertUnreadable(job_details.parse_web_page, "<html><body></body></html>")


class FetchDetailsTest(unittest.TestCase):

    def fetch(self, link, resp=None, **kwargs):
        with mock.patch.object(job_details.requests, "get", return_value=resp or response(), **kwargs) as get:
            try:
                return job_details.fetch_details(link, user_agent="UA", proxy=None), get
            except DetailsError as exc:
                return exc, get

    def test_parser_chosen_by_link_not_origin(self):
        link = "https://acme.gupy.io/job/abc"
        result, get = self.fetch(link, response(text=gupy_page({"description": "D"})))
        self.assertEqual(result.text, "Descrição da vaga\nD")
        self.assertEqual(get.call_args.args[0], link)

    def test_fetch_linkedin_uses_guest_posting_url(self):
        _, get = self.fetch("https://br.linkedin.com/jobs/view/dev-flutter-4012345/",
                            response(text=linkedin_posting("<p>T</p>", [])))
        self.assertEqual(get.call_args.args[0], "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4012345")

    def test_fetch_inhire_sends_tenant_header(self):
        _, get = self.fetch("https://acme.inhire.app/vagas/abc-1/dev", response(payload={"description": "<p>x</p>"}))
        self.assertEqual(get.call_args.args[0], "https://api.inhire.app/job-posts/public/pages/abc-1")
        self.assertEqual(get.call_args.kwargs["headers"]["X-Tenant"], "acme")

    def test_fetch_remotar_uses_api_url(self):
        _, get = self.fetch("https://remotar.com.br/job/77", response(payload={"description": "<p>x</p>"}))
        self.assertEqual(get.call_args.args[0], "https://api.remotar.com.br/jobs/77")

    def test_fetch_404_and_410_are_closed(self):
        for status in (404, 410):
            with self.subTest(status=status):
                error, _ = self.fetch("https://example.com/v", response(status))
                self.assertEqual(error.kind, DetailsFailure.CLOSED)

    def test_fetch_403_429_999_are_blocked(self):
        for status in (403, 429, 999):
            with self.subTest(status=status):
                error, _ = self.fetch("https://example.com/v", response(status))
                self.assertEqual(error.kind, DetailsFailure.BLOCKED)

    def test_fetch_server_error_is_refused(self):
        for status in (500, 302):
            with self.subTest(status=status):
                error, _ = self.fetch("https://example.com/v", response(status))
                self.assertEqual(error.kind, DetailsFailure.REFUSED)

    def test_fetch_timeout_is_network(self):
        with mock.patch.object(job_details.requests, "get", side_effect=job_details.requests.Timeout):
            with self.assertRaises(DetailsError) as ctx:
                job_details.fetch_details("https://example.com/v", user_agent="UA", proxy=None)
        self.assertEqual(ctx.exception.kind, DetailsFailure.NETWORK)

    def test_fetch_invalid_json_is_unreadable(self):
        resp = response()
        resp.json.side_effect = ValueError("bad json")
        error, _ = self.fetch("https://remotar.com.br/job/1", resp)
        self.assertEqual(error.kind, DetailsFailure.UNREADABLE)

    def test_fetch_link_without_id_is_unreadable_without_request(self):
        for link in ("https://www.linkedin.com/jobs/view/", "https://www.inhire.app/vagas/1/x",
                     "https://remotar.com.br/jobs"):
            with self.subTest(link=link):
                error, get = self.fetch(link)
                self.assertEqual(error.kind, DetailsFailure.UNREADABLE)
                get.assert_not_called()

    def test_fetch_linkedin_post_raises_value_error(self):
        with mock.patch.object(job_details.requests, "get") as get:
            with self.assertRaises(ValueError):
                job_details.fetch_details("https://www.linkedin.com/posts/x", user_agent="UA", proxy=None)
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
