"""Unit tests for run_alert (Telegram alert when a systemd round fails or recovers).

Run: python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_alert  # noqa: E402
from run_alert import FailureReason  # noqa: E402


def fake_runner(result):
    def run(cmd):
        assert cmd[0] == "systemctl"
        return f"{result}\n"
    return run


class RunAlertTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.flag = Path(tmp.name) / "state" / run_alert.FAILED_FLAG_NAME
        self.sent = []

    def test_failure_alerts_once_per_streak(self):
        run = fake_runner("timeout")
        run_alert.handle_failed(self.flag, self.sent.append, run)
        run_alert.handle_failed(self.flag, self.sent.append, run)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("passou do tempo limite de 45 minutos", self.sent[0])
        self.assertIn("journalctl -u vagas-bot.service -n 50", self.sent[0])
        self.assertTrue(self.flag.exists())

    def test_ok_alerts_only_after_a_failure(self):
        run_alert.handle_ok(self.flag, self.sent.append)
        self.assertEqual(self.sent, [])
        run_alert.handle_failed(self.flag, self.sent.append, fake_runner("exit-code"))
        run_alert.handle_ok(self.flag, self.sent.append)
        run_alert.handle_ok(self.flag, self.sent.append)
        self.assertEqual(self.sent[1:], [run_alert.RECOVERED_TEXT])
        self.assertFalse(self.flag.exists())

    def test_one_phrase_per_reason(self):
        raw_to_phrase = {
            "oom-kill": "ficou sem memória (passou do limite de 2 GB)",
            "timeout": "passou do tempo limite de 45 minutos",
            "exit-code": "terminou com erro",
            "signal": "foi interrompido no meio",
            "core-dump": "foi interrompido no meio",
        }
        for raw, phrase in raw_to_phrase.items():
            self.assertEqual(run_alert.failure_reason(fake_runner(raw)).phrase, phrase)

    def test_unknown_reason(self):
        for raw in ("start-limit-hit", ""):
            reason = run_alert.failure_reason(fake_runner(raw))
            self.assertIs(reason, FailureReason.UNKNOWN)
            self.assertEqual(reason.phrase, "parou por um motivo desconhecido")

    def test_main_rejects_bad_usage(self):
        for argv in ([], ["oops"], ["ok", "extra"]):
            self.assertEqual(run_alert.main(argv), 2)


if __name__ == "__main__":
    unittest.main()
