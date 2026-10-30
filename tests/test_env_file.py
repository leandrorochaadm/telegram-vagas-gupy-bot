"""Unit tests for env_file.load_env_file.

Run: python -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from env_file import load_env_file  # noqa: E402


class LoadEnvFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "bot.env")

    def write(self, content: str) -> None:
        with open(self.path, "w") as f:
            f.write(content)

    def test_load_env_file_rules(self):
        self.write(
            "\n# COMMENTED=1\nNO_EQUALS\n  SPACED  =  value  \nURL=http://x?a=b\nEMPTY=\n"
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            load_env_file(self.path)
            self.assertEqual(os.environ["SPACED"], "value")
            self.assertEqual(os.environ["URL"], "http://x?a=b")
            self.assertEqual(os.environ["EMPTY"], "")
            self.assertNotIn("COMMENTED", os.environ)
            self.assertNotIn("NO_EQUALS", os.environ)

    def test_existing_variable_is_not_overwritten(self):
        self.write("KEY=new\n")
        with mock.patch.dict(os.environ, {"KEY": "old"}, clear=True):
            load_env_file(self.path)
            self.assertEqual(os.environ["KEY"], "old")

    def test_missing_file_does_nothing(self):
        with mock.patch.dict(os.environ, {"A": "1"}, clear=True):
            load_env_file(os.path.join(self.tmp, "missing.env"))
            self.assertEqual(dict(os.environ), {"A": "1"})


if __name__ == "__main__":
    unittest.main()
