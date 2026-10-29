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
DB_COPIES_REMOVED = "db copies and the db commit were removed: every job writes to the central database"


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

# Logs one line per call ("launch" for `python -c`); the browser launch fails
# FAKE_LAUNCH_FAILS times before it works
FAKE_PLAYWRIGHT_PYTHON = """#!/usr/bin/env bash
if [ "$1" = "-c" ]; then
  echo launch >> "$FAKE_STATE/calls"
  fails=$(cat "$FAKE_STATE/fails" 2>/dev/null || echo "${FAKE_LAUNCH_FAILS:-0}")
  [ "$fails" -gt 0 ] && { echo $((fails - 1)) > "$FAKE_STATE/fails"; exit 1; }
  exit 0
fi
echo "$@" >> "$FAKE_STATE/calls"
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

    def run_step(self, elapsed: int, exit_code: int = 0, defer: bool = False) -> subprocess.CompletedProcess:
        env = dict(os.environ,
                   PATH=f"{self.bin}:{os.environ['PATH']}",
                   FAKE_STATE=self.dir.name,
                   FAKE_EXIT=str(exit_code),
                   GITHUB_OUTPUT=os.path.join(self.dir.name, "output"),
                   JOB_START=str(int(time.time()) - elapsed))
        env.pop("DEFER_UNREACHABLE", None)
        if defer:
            env["DEFER_UNREACHABLE"] = "true"
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
        self.assertIsNone(self.state("output"))

    def test_first_round_defers_an_unreachable_database_to_another_runner(self):
        result = self.run_step(elapsed=60, exit_code=main.EXIT_STORE_UNREACHABLE, defer=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state("python"), "main.py --source gupy --defer-unreachable")
        self.assertEqual(self.state("output"), "unreachable=true")

    def test_other_failures_still_fail_the_step_in_the_first_round(self):
        result = self.run_step(elapsed=60, exit_code=1, defer=True)
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(self.state("output"))


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
        self.assertIn("needs: [buscar-vagas, repetir-vagas]", last)
        self.assertIn("if: always()", last)
        self.assertNotIn("--source", last)

    @unittest.skip(DB_COPIES_REMOVED)
    def test_last_job_merges_then_sends_jobs_then_alerts_then_commits(self):
        names = step_names("finalizar")
        order = ["Juntar cópias do banco", "Enviar vagas", "Enviar avisos", "Salvar banco de dados atualizado"]
        self.assertEqual([n for n in names if n in order], order)

    @unittest.skip(DB_COPIES_REMOVED)
    def test_jobs_are_sent_even_when_a_search_job_failed(self):
        block = job_block("finalizar")
        step = block[block.index("- name: Enviar vagas"):block.index("- name: Enviar avisos")]
        self.assertIn("if: always() && steps.merge.outcome == 'success'", step)
        self.assertIn("run: python main.py --send-jobs", step)

    @unittest.skip(DB_COPIES_REMOVED)
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


class RetryOnAnotherRunnerTest(unittest.TestCase):
    MARK = "Banco inacessível"

    def test_retry_job_runs_the_same_steps_without_deferring(self):
        self.assertIn("steps: &search-steps", job_block("buscar-vagas"))
        self.assertIn("DEFER_UNREACHABLE: 'true'", job_block("buscar-vagas"))
        retry = job_block("repetir-vagas")
        self.assertIn("steps: *search-steps", retry)
        self.assertNotIn("DEFER_UNREACHABLE", retry)
        self.assertIn("source: ${{ fromJSON(needs.planejar-repeticao.outputs.sources) }}", retry)
        self.assertIn("needs.planejar-repeticao.outputs.sources != '[]'", retry)

    def test_unreachable_mark_comes_right_after_the_search(self):
        names = step_names("buscar-vagas")
        self.assertEqual(names[names.index("Executar bot") + 1], self.MARK)
        self.assertIn("if: steps.run.outputs.unreachable == 'true'", step_block("buscar-vagas", self.MARK))
        # The interrupted note would try the unreachable database too and alert directly
        self.assertIn("steps.run.outcome != 'success'", step_block("buscar-vagas", "Anotar busca interrompida"))

    def test_plan_reads_the_mark_by_name_with_read_only_actions_access(self):
        plan = job_block("planejar-repeticao")
        for text in ("needs: buscar-vagas", "if: ${{ !cancelled() }}", "actions: read",
                     f'.name == "{self.MARK}"', 'startswith("buscar-vagas (")',
                     "sources: ${{ steps.plan.outputs.sources }}"):
            self.assertIn(text, plan)
        self.assertNotIn("write", plan)
        self.assertNotIn("PB_", plan)

    def test_workflow_parses_with_the_anchor_resolved(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        jobs = yaml.safe_load(TEXT)["jobs"]
        self.assertEqual(jobs["repetir-vagas"]["steps"], jobs["buscar-vagas"]["steps"])


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
        for text in ("id: playwright-cache", "uses: actions/cache/restore@v6", "continue-on-error: true",
                     "path: ~/.cache/ms-playwright", "hashFiles('requirements.txt')", self.PLAYWRIGHT_IF):
            self.assertIn(text, step)
        self.assertIn(self.PLAYWRIGHT_IF, step_block("buscar-vagas", self.INSTALL))
        names = step_names("buscar-vagas")
        self.assertEqual(names.index(self.RESTORE) + 1, names.index(self.INSTALL))

    def run_install(self, cache_hit: str, launch_fails: int = 0) -> tuple[subprocess.CompletedProcess, list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            path = os.path.join(bin_dir, "python")
            with open(path, "w") as f:
                f.write(FAKE_PLAYWRIGHT_PYTHON)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_STATE=tmp,
                       PLAYWRIGHT_CACHE_HIT=cache_hit, FAKE_LAUNCH_FAILS=str(launch_fails))
            result = subprocess.run(["bash", "-e", "-c", run_script(self.INSTALL)],
                                    env=env, capture_output=True, text=True)
            with open(os.path.join(tmp, "calls")) as f:
                return result, f.read().splitlines()

    def test_playwright_install_skips_download_on_cache_hit(self):
        step = step_block("buscar-vagas", self.INSTALL)
        for text in ("id: playwright-install", "continue-on-error: true", "timeout-minutes: 5",
                     "PLAYWRIGHT_CACHE_HIT: ${{ steps.playwright-cache.outputs.cache-hit }}"):
            self.assertIn(text, step)
        for cache_hit, expected in (("true", ["launch"]),
                                    ("false", ["-m playwright install chromium", "launch"]),
                                    ("", ["-m playwright install chromium", "launch"])):
            result, calls = self.run_install(cache_hit)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls, expected)

    def test_playwright_system_deps_only_when_the_browser_does_not_launch(self):
        # The apt mirror can take minutes to serve fonts: install-deps is the fallback, not the default
        result, calls = self.run_install("true", launch_fails=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["launch", "-m playwright install-deps chromium", "launch"])
        result, calls = self.run_install("true", launch_fails=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, ["launch", "-m playwright install-deps chromium", "launch"])

    def test_playwright_cache_is_saved_only_after_successful_install(self):
        names = step_names("buscar-vagas")
        self.assertEqual(names.index(self.INSTALL) + 1, names.index(self.SAVE))
        step = step_block("buscar-vagas", self.SAVE)
        for text in ("uses: actions/cache/save@v6", "continue-on-error: true",
                     "steps.playwright-install.outcome == 'success'",
                     "steps.playwright-cache.outputs.cache-hit != 'true'", "matrix.source == 'linkedin_posts'"):
            self.assertIn(text, step)
        restore = step_block("buscar-vagas", self.RESTORE)
        for prefix in ("path:", "key:"):
            self.assertEqual(self.lines_with(step, prefix), self.lines_with(restore, prefix))
        for name in step_names("buscar-vagas"):
            block = step_block("buscar-vagas", name)
            if "uses: actions/cache@v6" in block:
                self.assertNotIn("ms-playwright", block)


class CentralDatabaseWorkflowTest(unittest.TestCase):
    PB_ENV = ("PB_URL: ${{ vars.PB_URL }}", "PB_BOT_EMAIL: ${{ secrets.PB_BOT_EMAIL }}",
              "PB_BOT_PASSWORD: ${{ secrets.PB_BOT_PASSWORD }}")

    def test_workflow_only_reads_the_repository(self):
        self.assertIn("permissions:\n  contents: read\n", TEXT)
        self.assertNotIn("contents: write", TEXT)

    def test_no_db_copy_artifact_or_commit_is_left(self):
        for text in ("vagas_gupy", "upload-artifact", "download-artifact", "--merge", "steps.merge",
                     "steps.upload", "git commit", "git push"):
            with self.subTest(text=text):
                self.assertNotIn(text, TEXT)
        for text in ("copy", "copies", "cópia", "merge"):
            with self.subTest(text=text):
                self.assertNotIn(text, TEXT.lower())

    def test_pocketbase_env_only_on_steps_that_run_the_bot(self):
        bot_steps = {"buscar-vagas": ["Executar bot", "Anotar busca interrompida"],
                     "finalizar": ["Enviar vagas", "Enviar avisos"]}
        for job, expected in bot_steps.items():
            with_bot = []
            for name in step_names(job):
                block = step_block(job, name)
                with self.subTest(job=job, step=name):
                    if "python main.py" in block:
                        with_bot.append(name)
                        for line in self.PB_ENV:
                            self.assertIn(line, block)
                    else:
                        self.assertNotIn("PB_", block)
            self.assertEqual(with_bot, expected)
            self.assertNotIn("PB_", job_block(job).split("    steps:")[0])
        self.assertNotIn("PB_", TEXT.split("jobs:")[0])

    def test_interrupted_search_is_noted_even_when_it_never_started(self):
        block = step_block("buscar-vagas", "Anotar busca interrompida")
        conditions = [line.strip() for line in block.splitlines() if line.strip().startswith("if:")]
        self.assertEqual(conditions, ["if: always() && steps.run.outcome != 'success'"])
        self.assertEqual(step_names("buscar-vagas")[-1], "Anotar busca interrompida")

    def test_jobs_and_alerts_are_sent_whatever_happened_before(self):
        self.assertEqual(step_names("finalizar"), ["Checkout repositório", "Configurar Python",
                                                   "Instalar dependências", "Enviar vagas", "Enviar avisos"])
        for name, command in (("Enviar vagas", "--send-jobs"), ("Enviar avisos", "--send-alerts")):
            block = step_block("finalizar", name)
            conditions = [line.strip() for line in block.splitlines() if line.strip().startswith("if:")]
            self.assertEqual(conditions, ["if: always()"])
            self.assertIn(f"run: python main.py {command}", block)


if __name__ == "__main__":
    unittest.main()
