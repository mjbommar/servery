"""Package + CLI parsing smoke tests."""

import contextlib
import io
import unittest

import servery
from servery import cli


class PackageTest(unittest.TestCase):
    def test_version_is_pep440_ish(self):
        self.assertIsInstance(servery.__version__, str)
        self.assertRegex(servery.__version__, r"^\d+\.\d+\.\d+")

    def test_public_api_exported(self):
        for name in ("Config", "serve", "make_server", "ServeryHandler"):
            self.assertIn(name, servery.__all__)
            self.assertTrue(hasattr(servery, name))


class CliParserTest(unittest.TestCase):
    def test_defaults(self):
        args = cli.build_parser().parse_args([])
        self.assertEqual(args.directory, ".")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8000)
        self.assertFalse(args.show_hidden)

    def test_config_from_args(self):
        args = cli.build_parser().parse_args(
            ["/tmp", "-p", "9001", "-b", "0.0.0.0", "--show-hidden"]
        )
        config = cli.config_from_args(args)
        self.assertEqual(config.port, 9001)
        self.assertEqual(config.host, "0.0.0.0")
        self.assertTrue(config.show_hidden)
        self.assertFalse(config.is_loopback_bind)
        self.assertTrue(config.directory.is_absolute())

    def test_version_flag_exits_zero(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(out):
            cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("servery", out.getvalue())


if __name__ == "__main__":
    unittest.main()
