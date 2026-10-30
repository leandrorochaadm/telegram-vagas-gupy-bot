"""Unit tests for run_all (one round on the VPS: WARP check, parallel sources, final alerts).

Run: python -m unittest discover -s tests -v
"""
import io
import itertools
import os
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_all  # noqa: E402
from run_all import SourceOutcome  # noqa: E402
from store import Source  # noqa: E402

SOURCES = [s.api_value for s in Source.search_sources()]


class FakeProc:
    """Popen stand-in: finishes with `code` after `polls` polls (None = never)."""

    def __init__(self, code, polls=0, output=""):
        self.code, self.polls, self.pid = code, polls, 4242
        self.stdout = io.StringIO(output)
        self.killed = False

    def poll(self):
        if self.polls > 0:
            self.polls -= 1
            return None
        return None if self.code is None else self.code

    def wait(self, timeout=None):
        return self.code


class RunAllTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cg = Path(tmp.name)
        self.spawned = []
        self.steps = []
        self.codes = {}
        self.now = 0.0
        self.killpg = mock.patch.object(run_all.os, "killpg").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(run_all, "cgroup_dir", return_value=self.cg).start()

    def spawn_factory(self, procs):
        def spawn(cmd, **kwargs):
            self.spawned.append(cmd)
            return procs[cmd[cmd.index("--source") + 1]]
        return spawn

    def run_step(self, cmd, **kwargs):
        self.steps.append(cmd[3:] if len(cmd) > 3 else cmd)
        flag = next((a for a in cmd if a.startswith("--") and a != "-u"), "")
        return self.codes.get(flag, 0)

    def clock(self):
        self.now += 1
        return self.now

    def go(self, procs=None, clock=None):
        procs = procs or {name: FakeProc(0) for name in SOURCES}
        real_run_sources = run_all.run_sources

        def run_sources(spawn, cgroup):
            return real_run_sources(spawn, cgroup, clock=clock or self.clock, pause=lambda _: None)

        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(run_all, "run_sources", run_sources):
            code = run_all.main(spawn=self.spawn_factory(procs), run=self.run_step)
        return code, out.getvalue()

    def flags(self):
        return [next(a for a in s if a.startswith("--")) for s in self.steps]

    def test_all_sources_start_together_with_send_now(self):
        code, out = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.spawned), len(SOURCES))
        for cmd in self.spawned:
            self.assertEqual(cmd[0], sys.executable)
            self.assertIn("-u", cmd)
            self.assertIn("--send-now", cmd)
        self.assertEqual(self.flags(), ["--check-proxy", "--send-jobs", "--send-alerts"])
        self.assertIn("[gupy] ✅ terminou em", out)

    def test_warp_down_starts_no_source_but_alerts_go_out(self):
        self.codes["--check-proxy"] = 78
        code, _ = self.go()
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.flags(), ["--check-proxy", "--send-jobs", "--send-alerts"])
        self.assertEqual(code, 0)

    def test_other_check_proxy_error_is_treated_as_warp_down(self):
        self.codes["--check-proxy"] = 1
        self.go()
        self.assertEqual(self.spawned, [])

    def test_failed_source_gets_failed_step_only_for_itself(self):
        procs = {name: FakeProc(0) for name in SOURCES}
        procs["remotar"] = FakeProc(1, output="Traceback boom\n")
        _, out = self.go(procs)
        self.assertEqual(self.flags(), ["--check-proxy", "--failed", "--send-jobs", "--send-alerts"])
        self.assertEqual(self.steps[1][-1], "remotar")
        self.assertIn("[remotar] ❌ terminou com erro (código 1) em", out)
        self.assertIn("[remotar] Traceback boom", out)

    def test_timeout_kills_only_that_group(self):
        procs = {name: FakeProc(0) for name in SOURCES}
        procs["linkedin_posts"] = FakeProc(None)
        ticks = itertools.count(0, 400)
        code, out = self.go(procs, clock=lambda: next(ticks))
        self.assertEqual(code, 0)
        self.killpg.assert_called_once_with(procs["linkedin_posts"].pid, signal.SIGTERM)
        self.assertIn("[linkedin_posts] ⏱️ passou de 12 min e foi encerrada", out)
        self.assertIn("--failed", self.flags())

    def test_sigkill_with_higher_oom_counter_is_out_of_memory(self):
        events = self.cg / "memory.events"
        events.write_text("low 0\noom_kill 0\n")
        proc = FakeProc(-signal.SIGKILL)

        class Killer(FakeProc):
            def poll(inner):
                events.write_text("low 0\noom_kill 1\n")
                return -signal.SIGKILL

        procs = {name: FakeProc(0) for name in SOURCES}
        procs["linkedin_posts"] = Killer(-signal.SIGKILL)
        _, out = self.go(procs)
        self.assertIn("[linkedin_posts] 💥 sem memória: o bot passou do limite de 2 GB e esta fonte foi encerrada", out)
        del proc

    def test_sigkill_with_same_counter_is_plain_error(self):
        (self.cg / "memory.events").write_text("oom_kill 0\n")
        procs = {name: FakeProc(0) for name in SOURCES}
        procs["gupy"] = FakeProc(-signal.SIGKILL)
        _, out = self.go(procs)
        self.assertIn("[gupy] ❌ terminou com erro (código -9)", out)

    def test_without_cgroup_files_there_is_no_memory_line(self):
        _, out = self.go()
        self.assertNotIn("Pico de memória", out)

    def test_peak_line(self):
        (self.cg / "memory.peak").write_text(str(int(1.3 * run_all.BYTES_PER_GB)))
        _, out = self.go()
        self.assertIn("Pico de memória da rodada: 1,3 GB de 2 GB", out)

    def test_exit_code_is_1_when_send_alerts_fails(self):
        self.codes["--send-alerts"] = 1
        code, _ = self.go()
        self.assertEqual(code, 1)

    def test_every_outcome_has_a_log_line(self):
        for outcome in SourceOutcome:
            self.assertTrue(run_all.outcome_line("x", outcome, 72, 1).startswith("[x] "))
        self.assertEqual(run_all.outcome_line("gupy", SourceOutcome.OK, 72, 0), "[gupy] ✅ terminou em 1m12s")


class _RoundHarness(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cg = Path(tmp.name)
        self.spawned = []
        self.steps = []
        self.kwargs = []
        self.codes = {}
        self.now = 0.0
        self.killpg = mock.patch.object(run_all.os, "killpg").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(run_all, "cgroup_dir", return_value=self.cg).start()

    def spawn_factory(self, procs):
        def spawn(cmd, **kwargs):
            self.spawned.append(cmd)
            return procs[cmd[cmd.index("--source") + 1]]
        return spawn

    def run_step(self, cmd, **kwargs):
        self.steps.append(cmd[3:] if len(cmd) > 3 else cmd)
        flag = next((a for a in cmd if a.startswith("--") and a != "-u"), "")
        self.kwargs.append((flag, kwargs))
        return self.codes.get(flag, 0)

    def clock(self):
        self.now += 1
        return self.now

    def go(self, procs=None, clock=None):
        procs = procs or {name: FakeProc(0) for name in SOURCES}
        real_run_sources = run_all.run_sources

        def run_sources(spawn, cgroup):
            return real_run_sources(spawn, cgroup, clock=clock or self.clock, pause=lambda _: None)

        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(run_all, "run_sources", run_sources):
            code = run_all.main(spawn=self.spawn_factory(procs), run=self.run_step, fetch_descriptions=True)
        return code, out.getvalue()

    def flags(self):
        return [next(a for a in s if a.startswith("--")) for s in self.steps]


class DescriptionsStepTest(_RoundHarness):

    def test_descriptions_step_runs_between_send_jobs_and_alerts(self):
        code, _ = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(self.flags(), ["--check-proxy", "--send-jobs", "--fetch-descriptions", "--send-alerts"])

    def test_descriptions_step_runs_after_failed_retries(self):
        procs = {name: FakeProc(0) for name in SOURCES}
        procs["gupy"] = FakeProc(1)
        self.go(procs)
        self.assertEqual(self.flags(), ["--check-proxy", "--failed", "--send-jobs", "--fetch-descriptions",
                                        "--send-alerts"])

    def test_descriptions_step_skipped_when_warp_down(self):
        self.codes["--check-proxy"] = 78
        self.go()
        self.assertEqual(self.flags(), ["--check-proxy", "--send-jobs", "--send-alerts"])

    def test_descriptions_step_skipped_on_other_proxy_error(self):
        self.codes["--check-proxy"] = 1
        self.go()
        self.assertNotIn("--fetch-descriptions", self.flags())

    def test_descriptions_step_failure_does_not_change_exit_code(self):
        self.codes["--fetch-descriptions"] = 1
        code, _ = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(self.flags()[-1], "--send-alerts")

    def test_descriptions_step_proxy_exit_does_not_change_exit_code(self):
        self.codes["--fetch-descriptions"] = 78
        code, _ = self.go()
        self.assertEqual(code, 0)

    def test_descriptions_step_uses_its_own_timeout(self):
        self.go()
        timeouts = {flag: kwargs.get("timeout") for flag, kwargs in self.kwargs}
        self.assertEqual(timeouts["--fetch-descriptions"], run_all.DESCRIPTIONS_TIMEOUT_S)
        self.assertEqual(run_all.DESCRIPTIONS_TIMEOUT_S, 600)
        for flag in ("--check-proxy", "--send-jobs", "--send-alerts"):
            self.assertIsNone(timeouts[flag])

    def test_exit_code_still_follows_send_alerts(self):
        self.codes["--send-alerts"] = 1
        code, _ = self.go()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
