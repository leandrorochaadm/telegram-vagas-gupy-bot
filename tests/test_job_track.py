"""Unit tests for the job track (Flutter, mobile or back-end) decided by the title.

Run: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_track import Track, analysis_track, is_sent_to_telegram, track_of  # noqa: E402


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

    def test_backend_spellings(self):
        for title in ("Desenvolvedor Backend Sênior", "Java Back-End Developer", "Dev Back End .Net",
                      "BACK_END Pleno", "Dev Back\u2011end", "Dev Back\u2013end", "Pessoa Desenvolvedora BackEnd C#"):
            self.assertIs(track_of(title), Track.BACKEND, title)

    def test_backend_is_only_the_word_not_a_technology(self):
        for title in ("Desenvolvedor Java Sênior", "Dev Node.js", "Python Developer", "Backends Ops",
                      "Feedback Analyst", "Back office", "Backend_dev"):
            self.assertIsNone(track_of(title), title)

    def test_flutter_and_mobile_win_over_backend(self):
        self.assertIs(track_of("Fullstack Flutter + Back-end Node"), Track.FLUTTER)
        self.assertIs(track_of("Back-end Kotlin"), Track.MOBILE)
        self.assertIs(track_of("Dev Mobile e Backend"), Track.MOBILE)

    def test_analysis_track_of_backend(self):
        self.assertIs(analysis_track("Dev Backend"), Track.BACKEND)
        self.assertIs(analysis_track("Dev Backend Flutter"), Track.FLUTTER)

    def test_only_flutter_goes_to_telegram(self):
        self.assertTrue(is_sent_to_telegram(Track.FLUTTER))
        self.assertFalse(is_sent_to_telegram(Track.MOBILE))
        self.assertFalse(is_sent_to_telegram(Track.BACKEND))

    def test_from_api(self):
        self.assertIs(Track.from_api("backend"), Track.BACKEND)
        self.assertIs(Track.from_api("mobile"), Track.MOBILE)
        with self.assertRaises(ValueError):
            Track.from_api("Mobile")


if __name__ == "__main__":
    unittest.main()
