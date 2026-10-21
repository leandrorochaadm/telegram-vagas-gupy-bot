"""Unit tests for the job layout of .github/workflows/vagas.yml.

The "Executar bot" script is extracted from the workflow and run for real, with fake
timeout and python on the PATH: it must cut every search 14 min after the job start.

Run: python -m unittest discover -s tests -v
"""
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "vagas.yml")
SEARCH_WINDOW = 14 * 60

with open(WORKFLOW, encoding="utf-8") as f:
    TEXT = f.read()
LINES = TEXT.splitlines()


def job_block(name: str) -> str:
    """Text of the top-level job `name`, up to the next job."""
    start = LINES.index(f"  {name}:")
    end = next((i for i in range(start + 1, len(LINES))
                if LINES[i].startswith("  ") and not LINES[i].startswith("   ") and LINES[i].strip().endswith(":")),
               len(LINES))
    return "\n".join(LINES[start:end])


def step_names(job: str) -> list[str]:
    return [line.strip()[len("- name: "):] for line in job_block(job).splitlines()
            if line.strip().startswith("- name: ")]


def step_block(job: str, name: str) -> str:
    """Text of the step `name` in `job`, up to the next step or the end of the job."""
    lines = job_block(job).splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("- name:")), len(lines))
    return "\n".join(lines[start:end])


def run_script(step_name: str) -> str:
    start = next(i for i, line in enumerate(LINES) if line.strip() == f"- name: {step_name}")
    run_at = next(i for i in range(start, len(LINES)) if LINES[i].strip() == "run: |")
    indent = len(LINES[run_at]) - len(LINES[run_at].lstrip())
    body = []
    for line in LINES[run_at + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    # GitHub fills the expression in before bash runs
    return textwrap.dedent("\n".join(body)).replace("${{ matrix.source }}", "gupy") + "\n"


# Records the limit it got, then runs the command like the real one would
FAKE_TIMEOUT = """#!/usr/bin/env bash
echo "$1" > "$FAKE_STATE/limit"
shift
exec "$@"
"""

FAKE_PYTHON = """#!/usr/bin/env bash
echo "$@" > "$FAKE_STATE/python"
exit "${FAKE_EXIT:-0}"
"""


class SearchDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.bin = os.path.join(self.dir.name, "bin")
        os.makedirs(self.bin)
        for name, body in (("timeout", FAKE_TIMEOUT), ("python", FAKE_PYTHON)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def run_step(self, elapsed: int, exit_code: int = 0) -> subprocess.CompletedProcess:
        env = dict(os.environ,
                   PATH=f"{self.bin}:{os.environ['PATH']}",
                   FAKE_STATE=self.dir.name,
                   FAKE_EXIT=str(exit_code),
                   JOB_START=str(int(time.time()) - elapsed))
        # GitHub runs `run:` with bash -e
        return subprocess.run(["bash", "-e", "-c", run_script("Executar bot")],
                              env=env, capture_output=True, text=True)

    def state(self, name: str) -> str | None:
        path = os.path.join(self.dir.name, name)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read().strip()

    def test_search_gets_what_is_left_of_the_window_after_setup(self):
        result = self.run_step(elapsed=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        # A second may pass between JOB_START and the script's own clock
        self.assertIn(int(self.state("limit")), (SEARCH_WINDOW - 120, SEARCH_WINDOW - 121))
        self.assertEqual(self.state("python"), "main.py --source gupy")

    def test_setup_that_used_the_whole_window_fails_without_searching(self):
        result = self.run_step(elapsed=SEARCH_WINDOW)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(self.state("python"))

    def test_search_cut_by_the_timeout_fails_the_step(self):
        # 124: what timeout returns when it stops the command
        result = self.run_step(elapsed=60, exit_code=124)
        self.assertEqual(result.returncode, 124)


class WorkflowLayoutTest(unittest.TestCase):
    def test_every_source_runs_as_a_parallel_job(self):
        matrix = next(line for line in job_block("buscar-vagas").splitlines() if line.strip().startswith("source:"))
        sources = [s.strip() for s in matrix.split("[", 1)[1].rstrip("]").split(",")]
        self.assertEqual(sources, list(main.SOURCES))

    def test_job_start_is_marked_before_any_setup(self):
        self.assertEqual(step_names("buscar-vagas")[0], "Marcar início")

    def test_search_jobs_only_queue_and_the_last_job_sends(self):
        self.assertIn("python main.py --source ${{ matrix.source }}", job_block("buscar-vagas"))
        self.assertNotIn("--send-jobs", job_block("buscar-vagas"))
        last = job_block("finalizar")
        self.assertIn("needs: buscar-vagas", last)
        self.assertIn("if: always()", last)
        self.assertNotIn("--source", last)

    def test_last_job_merges_then_sends_jobs_then_alerts_then_commits(self):
        names = step_names("finalizar")
        order = ["Juntar cópias do banco", "Enviar vagas", "Enviar avisos", "Salvar banco de dados atualizado"]
        self.assertEqual([n for n in names if n in order], order)

    def test_jobs_are_sent_even_when_a_search_job_failed(self):
        block = job_block("finalizar")
        step = block[block.index("- name: Enviar vagas"):block.index("- name: Enviar avisos")]
        self.assertIn("if: always() && steps.merge.outcome == 'success'", step)
        self.assertIn("run: python main.py --send-jobs", step)

    def test_db_copy_upload_is_retried_with_overwrite_when_first_fails(self):
        first, wait, retry = ("Guardar cópia do banco", "Esperar antes de reenviar a cópia",
                              "Guardar cópia do banco (nova tentativa)")
        names = step_names("buscar-vagas")
        self.assertEqual(names[names.index(first):names.index(first) + 3], [first, wait, retry])
        base_if = "if: always() && steps.run.outcome != 'skipped'"
        retry_if = f"{base_if} && steps.upload.outcome == 'failure'"

        first_step = step_block("buscar-vagas", first)
        for text in ("id: upload", "continue-on-error: true", base_if):
            self.assertIn(text, first_step)

        wait_step = step_block("buscar-vagas", wait)
        self.assertIn("run: sleep 15", wait_step)
        self.assertIn(retry_if, wait_step)

        retry_step = step_block("buscar-vagas", retry)
        for text in (retry_if, "uses: actions/upload-artifact@v4", "name: db-${{ matrix.source }}",
                     "path: vagas_gupy.db*", "retention-days: 1", "if-no-files-found: ignore",
                     "overwrite: true"):
            self.assertIn(text, retry_step)
        self.assertNotIn("continue-on-error", retry_step)


class DependencyCacheTest(unittest.TestCase):
    PLAYWRIGHT_IF = "if: matrix.source == 'linkedin_posts' && env.HAS_LINKEDIN_LI_AT == 'true'"
    RESTORE = "Restaurar navegador do Playwright"
    INSTALL = "Instalar navegador do Playwright"
    SAVE = "Guardar navegador do Playwright"

    def lines_with(self, block: str, prefix: str) -> list[str]:
        return [line.strip() for line in block.splitlines() if line.strip().startswith(prefix)]

    def test_every_setup_python_caches_pip(self):
        self.assertEqual(TEXT.count("uses: actions/setup-python@v6"), 2)
        for job in ("buscar-vagas", "finalizar"):
            step = step_block(job, "Configurar Python")
            self.assertIn("cache: 'pip'", step)
            self.assertIn("cache-dependency-path: requirements.txt", step)

    def test_playwright_browser_is_cached_by_requirements_hash(self):
        step = step_block("buscar-vagas", self.RESTORE)
        for text in ("id: playwright-cache", "uses: actions/cache/restore@v4", "continue-on-error: true",
                     "path: ~/.cache/ms-playwright", "hashFiles('requirements.txt')", self.PLAYWRIGHT_IF):
            self.assertIn(text, step)
        self.assertIn(self.PLAYWRIGHT_IF, step_block("buscar-vagas", self.INSTALL))
        names = step_names("buscar-vagas")
        self.assertEqual(names.index(self.RESTORE) + 1, names.index(self.INSTALL))

    def run_install(self, cache_hit: str) -> tuple[subprocess.CompletedProcess, str]:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            path = os.path.join(bin_dir, "python")
            with open(path, "w") as f:
                f.write(FAKE_PYTHON)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_STATE=tmp,
                       PLAYWRIGHT_CACHE_HIT=cache_hit)
            result = subprocess.run(["bash", "-e", "-c", run_script(self.INSTALL)],
                                    env=env, capture_output=True, text=True)
            with open(os.path.join(tmp, "python")) as f:
                return result, f.read().strip()

    def test_playwright_install_skips_download_on_cache_hit(self):
        step = step_block("buscar-vagas", self.INSTALL)
        for text in ("id: playwright-install", "continue-on-error: true", "timeout-minutes: 5",
                     "PLAYWRIGHT_CACHE_HIT: ${{ steps.playwright-cache.outputs.cache-hit }}"):
            self.assertIn(text, step)
        for cache_hit, expected in (("true", "-m playwright install-deps chromium"),
                                    ("false", "-m playwright install --with-deps chromium"),
                                    ("", "-m playwright install --with-deps chromium")):
            result, called = self.run_install(cache_hit)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(called, expected)

    def test_playwright_cache_is_saved_only_after_successful_install(self):
        names = step_names("buscar-vagas")
        self.assertEqual(names.index(self.INSTALL) + 1, names.index(self.SAVE))
        step = step_block("buscar-vagas", self.SAVE)
        for text in ("uses: actions/cache/save@v4", "continue-on-error: true",
                     "steps.playwright-install.outcome == 'success'",
                     "steps.playwright-cache.outputs.cache-hit != 'true'", "matrix.source == 'linkedin_posts'"):
            self.assertIn(text, step)
        restore = step_block("buscar-vagas", self.RESTORE)
        for prefix in ("path:", "key:"):
            self.assertEqual(self.lines_with(step, prefix), self.lines_with(restore, prefix))
        for name in step_names("buscar-vagas"):
            block = step_block("buscar-vagas", name)
            if "uses: actions/cache@v4" in block:
                self.assertNotIn("ms-playwright", block)


if __name__ == "__main__":
    unittest.main()
