"""Smoke tests for the package scaffold.

These exist so the CI gates (lint, type, security, test, coverage, build) run
against real code from the start. Feature tests arrive with each milestone.
"""

import contextlib
import io
import unittest

import servery
from servery import cli


class PackageTest(unittest.TestCase):
    def test_version_is_pep440_ish(self):
        self.assertIsInstance(servery.__version__, str)
        self.assertRegex(servery.__version__, r"^\d+\.\d+\.\d+")

    def test_version_exported(self):
        self.assertIn("__version__", servery.__all__)


class CliTest(unittest.TestCase):
    def test_main_returns_zero(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([]), 0)

    def test_version_flag_exits_zero(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(out):
            cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("servery", out.getvalue())


if __name__ == "__main__":
    unittest.main()
