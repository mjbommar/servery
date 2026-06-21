"""Unit tests for Range header parsing (RFC 9110 §14)."""

import unittest

from servery import ranges
from servery.ranges import ByteRange


class ParseTest(unittest.TestCase):
    def test_absent_or_blank(self):
        self.assertIsNone(ranges.parse(None, 100))
        self.assertIsNone(ranges.parse("", 100))

    def test_non_bytes_unit(self):
        self.assertIsNone(ranges.parse("items=0-1", 100))

    def test_full_explicit(self):
        r = ranges.parse("bytes=0-99", 100)
        self.assertEqual(r, ByteRange(0, 100))
        assert isinstance(r, ByteRange)
        self.assertEqual(r.end, 99)

    def test_partial(self):
        self.assertEqual(ranges.parse("bytes=0-49", 100), ByteRange(0, 50))

    def test_open_ended(self):
        self.assertEqual(ranges.parse("bytes=50-", 100), ByteRange(50, 50))

    def test_suffix(self):
        self.assertEqual(ranges.parse("bytes=-20", 100), ByteRange(80, 20))

    def test_suffix_exceeding_size(self):
        self.assertEqual(ranges.parse("bytes=-200", 100), ByteRange(0, 100))

    def test_end_clamped_to_size(self):
        self.assertEqual(ranges.parse("bytes=90-999", 100), ByteRange(90, 10))

    def test_unsatisfiable_start_past_end(self):
        self.assertIs(ranges.parse("bytes=100-", 100), ranges.UNSATISFIABLE)
        self.assertIs(ranges.parse("bytes=150-160", 100), ranges.UNSATISFIABLE)

    def test_empty_file_is_unsatisfiable(self):
        self.assertIs(ranges.parse("bytes=0-", 0), ranges.UNSATISFIABLE)

    def test_multiple_ranges_served_full(self):
        self.assertIsNone(ranges.parse("bytes=0-1,5-6", 100))

    def test_malformed(self):
        self.assertIsNone(ranges.parse("bytes=abc", 100))
        self.assertIsNone(ranges.parse("bytes=5-2", 100))
        self.assertIsNone(ranges.parse("bytes=-0", 100))
        self.assertIsNone(ranges.parse("bytes=", 100))


if __name__ == "__main__":
    unittest.main()
