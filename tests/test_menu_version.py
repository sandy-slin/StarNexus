import unittest

from bridge.botmenu import version_for_time


class MenuVersionTests(unittest.TestCase):
    def test_first_minutes_have_numeric_semver_component(self):
        # 06:29:00 was the production failure shape: 062900 is not SemVer.
        self.assertEqual(version_for_time(1794781740), "1.20261116.62900")

    def test_later_times_remain_monotonic_shape(self):
        self.assertEqual(version_for_time(1794785340), "1.20261116.72900")

