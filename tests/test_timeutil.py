import unittest
from datetime import datetime, timezone
from rip_swarm.timeutil import format_z, parse_z, add_seconds, parse_duration


class TestTimeutil(unittest.TestCase):
    def test_roundtrip_z(self):
        dt = datetime(2026, 9, 17, 9, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(format_z(dt), "2026-09-17T09:01:00Z")
        self.assertEqual(parse_z("2026-09-17T09:01:00Z"), dt)

    def test_reject_offset(self):
        with self.assertRaises(ValueError):
            parse_z("2026-09-17T11:01:00+02:00")

    def test_add_seconds(self):
        dt = parse_z("2026-09-17T09:01:00Z")
        self.assertEqual(format_z(add_seconds(dt, 900)), "2026-09-17T09:16:00Z")

    def test_parse_duration(self):
        self.assertEqual(parse_duration(900), 900)
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(parse_duration("1h"), 3600)
        self.assertEqual(parse_duration("45s"), 45)

    def test_parse_z_rejects_trailing_newline(self):
        with self.assertRaises(ValueError):
            parse_z("2026-09-17T09:01:00Z\n")

    def test_parse_duration_rejects_trailing_newline(self):
        with self.assertRaises(ValueError):
            parse_duration("30m\n")

    def test_parse_duration_rejects_bool(self):
        with self.assertRaises(ValueError):
            parse_duration(True)
        with self.assertRaises(ValueError):
            parse_duration(False)

    def test_parse_duration_rejects_zero(self):
        with self.assertRaises(ValueError):
            parse_duration(0)
        with self.assertRaises(ValueError):
            parse_duration("0")
        with self.assertRaises(ValueError):
            parse_duration("0s")

    def test_parse_duration_rejects_negative(self):
        with self.assertRaises(ValueError):
            parse_duration(-1)


if __name__ == "__main__":
    unittest.main()
