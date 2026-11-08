"""Unit tests for the job track (Flutter or mobile) decided by the title.

Run: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_track import Track, analysis_track, track_of  # noqa: E402


class TrackOfTest(unittest.TestCase):
    def test_flutter_wins_over_mobile(self):
        self.assertIs(track_of("Desenvolvedor Mobile Flutter Sênior"), Track.FLUTTER)
        self.assertIs(track_of("FLUTTER developer"), Track.FLUTTER)

    def test_mobile_terms(self):
        for title in ("Dev Mobile Pleno", "Android Engineer", "Dev Kotlin", "Engenheiro iOS",
                      "Swift Developer", "Desenvolvedor React Native", "Dev React-Native"):
            self.assertIs(track_of(title), Track.MOBILE, title)

    def test_whole_word_only(self):
        for title in ("Analista de Negócios", "Flutterwave Analyst", "Swiftly Ops", "Desenvolvedor React"):
            self.assertIsNone(track_of(title), title)

    def test_analysis_track_defaults_to_flutter(self):
        self.assertIs(analysis_track("Dev 3"), Track.FLUTTER)
        self.assertIs(analysis_track("Dev Flutter iOS"), Track.FLUTTER)
        self.assertIs(analysis_track("Dev iOS"), Track.MOBILE)

    def test_from_api(self):
        self.assertIs(Track.from_api("mobile"), Track.MOBILE)
        with self.assertRaises(ValueError):
            Track.from_api("Mobile")


if __name__ == "__main__":
    unittest.main()
