"""schedule.py: daily windows, overnight windows and time zones."""
from __future__ import annotations

import unittest
from datetime import datetime, time, timezone

from loralarm.schedule import Window, local_time, parse_window, zone


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class ParseTest(unittest.TestCase):
    def test_parses_and_prints_normalised(self):
        w = parse_window(" 9:00 - 21:30 ")
        self.assertEqual((w.start, w.end), (time(9, 0), time(21, 30)))
        self.assertEqual(str(w), "09:00-21:30")

    def test_rejects_bad_text(self):
        for bad in ["", "9-21", "21:00", "25:00-09:00", "21:00-09:60", "24:00-09:00",
                    "21:00–09:00", "21:00-21:00", "abc", None]:
            with self.subTest(text=bad), self.assertRaises(ValueError):
                parse_window(bad)

    def test_equal_start_and_end_message_points_to_empty(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            parse_window("08:00-08:00")


class ContainsTest(unittest.TestCase):
    def test_daytime_window_start_inclusive_end_exclusive(self):
        w = parse_window("09:00-21:00")
        self.assertEqual([w.contains(time(h, m, s)) for h, m, s in
                          [(9, 0, 0), (12, 0, 0), (20, 59, 59), (21, 0, 0), (8, 59, 59), (0, 0, 0)]],
                         [True, True, True, False, False, False])

    def test_overnight_window(self):
        w = parse_window("21:00-09:00")
        self.assertEqual([w.contains(time(h, m, s)) for h, m, s in
                          [(21, 0, 0), (23, 59, 59), (0, 0, 0), (8, 59, 59), (9, 0, 0), (12, 0, 0), (20, 59, 59)]],
                         [True, True, True, True, False, False, False])

    def test_window_is_hashable_and_comparable(self):
        self.assertEqual(parse_window("21:00-09:00"), Window(time(21), time(9)))
        self.assertEqual(len({parse_window("21:00-09:00"), parse_window("21:00-09:00")}), 1)


class LocalTimeTest(unittest.TestCase):
    def test_winter_and_summer_time(self):
        self.assertEqual(local_time(utc(2026, 11, 9, 15, 33), "Europe/Stockholm"), time(16, 33))   # CET, +1
        self.assertEqual(local_time(utc(2026, 7, 1, 12, 0), "Europe/Stockholm"), time(14, 0))      # CEST, +2

    def test_spring_forward_2026_03_29(self):
        self.assertEqual(local_time(utc(2026, 3, 29, 0, 30), "Europe/Stockholm"), time(1, 30))     # +1
        self.assertEqual(local_time(utc(2026, 3, 29, 1, 30), "Europe/Stockholm"), time(3, 30))     # +2

    def test_fall_back_2026_10_25_repeats_the_hour(self):
        self.assertEqual(local_time(utc(2026, 10, 25, 0, 30), "Europe/Stockholm"), time(2, 30))    # +2
        self.assertEqual(local_time(utc(2026, 10, 25, 1, 30), "Europe/Stockholm"), time(2, 30))    # +1

    def test_naive_time_is_taken_as_utc(self):
        self.assertEqual(local_time(datetime(2026, 11, 9, 15, 33), "Europe/Stockholm"), time(16, 33))

    def test_other_zone_and_unknown_zone(self):
        self.assertEqual(local_time(utc(2026, 11, 9, 15, 33), "UTC"), time(15, 33))
        with self.assertRaises(ValueError):
            zone("Mars/Olympus_Mons")
        with self.assertRaises(ValueError):
            local_time(utc(2026, 11, 9, 15, 33), "")


if __name__ == "__main__":
    unittest.main()
