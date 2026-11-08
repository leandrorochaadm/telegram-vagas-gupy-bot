"""Unit tests for the weekly job analysis units and the config files the deploy uses.

Reads files only (no import of main, store or anthropic).

Run: python -m unittest discover -s tests -v
"""
import configparser
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")


def read_unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=True, interpolation=None)
    parser.optionxform = str
    with open(os.path.join(DEPLOY, name), encoding="utf-8") as f:
        parser.read_file(f)
    return parser


def read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


class AnalysisUnitsTest(unittest.TestCase):
    def test_analysis_units(self):
        timer = read_unit("vagas-analysis.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "Mon *-*-* 08:00:00 America/Sao_Paulo")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(timer["Timer"]["RandomizedDelaySec"], "60")
        self.assertEqual(timer["Timer"]["AccuracySec"], "1s")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")
        service = read_unit("vagas-analysis.service")
        self.assertEqual(service["Service"]["User"], "deploy")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        for section in service.sections():
            self.assertNotIn("OnFailure", service[section])

    def test_service_runs_the_script_like_the_other_units(self):
        service = read_unit("vagas-analysis.service")
        failed = read_unit("vagas-bot-failed.service")
        self.assertEqual(
            service["Service"]["ExecStart"], "/home/deploy/.local/bin/uv run --script analyze_descriptions.py"
        )
        for key in ("WorkingDirectory", "EnvironmentFile", "Environment", "Group"):
            self.assertEqual(service["Service"][key], failed["Service"][key], key)
        self.assertEqual(service["Service"]["TimeoutStartSec"], "30min")
        self.assertEqual(service["Service"]["MemoryMax"], "1G")
        self.assertIn("pocketbase-job-search.service", service["Unit"]["After"])
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "analyze_descriptions.py")))

    def test_mobile_units_mirror_the_flutter_ones(self):
        timer = read_unit("vagas-analysis-mobile.timer")
        flutter_timer = read_unit("vagas-analysis.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "Mon *-*-* 08:30:00 America/Sao_Paulo")
        for key in ("Persistent", "RandomizedDelaySec", "AccuracySec"):
            self.assertEqual(timer["Timer"][key], flutter_timer["Timer"][key], key)
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")
        service = read_unit("vagas-analysis-mobile.service")
        flutter = read_unit("vagas-analysis.service")
        self.assertEqual(
            service["Service"]["ExecStart"],
            "/home/deploy/.local/bin/uv run --script analyze_descriptions.py --track mobile",
        )
        for key in ("Type", "User", "Group", "WorkingDirectory", "EnvironmentFile", "Environment",
                    "TimeoutStartSec", "MemoryMax", "NoNewPrivileges", "PrivateTmp"):
            self.assertEqual(service["Service"][key], flutter["Service"][key], key)
        self.assertIn("vagas-analysis.service", service["Unit"]["After"].split())
        for section in service.sections():
            self.assertNotIn("OnFailure", service[section])

    def test_script_log_hint_names_the_unit(self):
        with open(os.path.join(ROOT, "analyze_descriptions.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("vagas-analysis.service", text)
        self.assertIn("vagas-analysis-mobile.service", text)

    def test_env_example_and_gitignore(self):
        lines = read_lines(os.path.join(DEPLOY, "bot.env.example"))
        for key in ("ANTHROPIC_API_KEY=", "RESUME_PATH="):
            self.assertIn(key, lines)
            self.assertTrue(lines[lines.index(key) - 1].startswith("#"), key)
        self.assertIn("reports/", read_lines(os.path.join(ROOT, ".gitignore")))


if __name__ == "__main__":
    unittest.main()
