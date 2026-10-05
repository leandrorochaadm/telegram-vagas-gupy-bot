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


if __name__ == "__main__":
    unittest.main()
