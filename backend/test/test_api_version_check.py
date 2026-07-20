"""Tests for the Qumulo Core minimum-version gate (api.check_min_version).

This was ported from qsnap but never wired into snapman-v2's request flow
until a live test against an older cluster hit a raw KeyError deep in
FileAttrs parsing (older releases don't return logical_datablocks from
/v1/files/{ref}/info/attributes). check_min_version fails fast instead, with
a clear reason, before any such parsing is attempted.
"""

import unittest

from app.qumulo.api import MIN_CORE_VERSION, UnsupportedVersionError, check_min_version, parse_core_version
from test.client import TestClient


class ParseCoreVersionTest(unittest.TestCase):
    def test_parses_standard_revision_string(self) -> None:
        self.assertEqual(parse_core_version("Qumulo Core 7.9.0"), (7, 9, 0))

    def test_parses_trailing_build_suffix(self) -> None:
        self.assertEqual(parse_core_version("Qumulo Core 7.9.1.2-abcdef"), (7, 9, 1))

    def test_unparseable_string_returns_none(self) -> None:
        self.assertIsNone(parse_core_version("not a version string"))


class CheckMinVersionTest(unittest.TestCase):
    def test_at_minimum_passes(self) -> None:
        c = TestClient()
        c.set_revision_id("Qumulo Core " + ".".join(str(p) for p in MIN_CORE_VERSION))
        check_min_version(c)  # should not raise

    def test_above_minimum_passes(self) -> None:
        c = TestClient()
        c.set_revision_id("Qumulo Core 8.1.0")
        check_min_version(c)  # should not raise

    def test_below_minimum_raises(self) -> None:
        c = TestClient()
        c.set_revision_id("Qumulo Core 6.6.0")
        with self.assertRaises(UnsupportedVersionError) as ctx:
            check_min_version(c)
        self.assertIn("6.6.0", str(ctx.exception))
        self.assertIn("7.9.0", str(ctx.exception))

    def test_unparseable_version_passes_the_gate(self) -> None:
        """Better to let an unusual build proceed and hit the API's own
        error than to wrongly lock it out on a string we can't parse."""
        c = TestClient()
        c.set_revision_id("some unusual internal build string")
        check_min_version(c)  # should not raise


if __name__ == "__main__":
    unittest.main()
