"""Package + CLI parsing smoke tests."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

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

    def test_bad_auth_reports_error_not_traceback(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["--auth", "nocolon"])
        self.assertEqual(code, 2)
        self.assertIn("error", err.getvalue())

    def test_http3_without_extra_reports_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["--http3"])
        self.assertEqual(code, 2)
        self.assertIn("error", err.getvalue())

    def test_tls_help_prints_and_exits_zero(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--tls-help"])
        self.assertEqual(code, 0)
        self.assertIn("openssl", out.getvalue())

    def test_feature_flags(self):
        args = cli.build_parser().parse_args(
            ["--cors", "--spa", "--cache", "60", "--no-security-headers"]
        )
        config = cli.config_from_args(args)
        self.assertTrue(config.cors)
        self.assertTrue(config.spa)
        self.assertEqual(config.cache_max_age, 60)
        self.assertFalse(config.security_headers)
        self.assertEqual(config.cache_control, "max-age=60")
        self.assertEqual(servery.Config.create(".").cache_control, "no-cache")

    def test_log_configure_stderr_idempotent(self):
        from servery import _log

        before = list(_log.logger.handlers)
        _log.configure_stderr()
        count = len(_log.logger.handlers)
        _log.configure_stderr()  # idempotent: no second handler
        self.assertEqual(len(_log.logger.handlers), count)
        for handler in list(_log.logger.handlers):
            if handler not in before:
                _log.logger.removeHandler(handler)
        _log._stderr_handler = None

    def test_hardening_flags(self):
        args = cli.build_parser().parse_args(["--timeout", "10", "--max-workers", "4", "--http2"])
        config = cli.config_from_args(args)
        self.assertEqual(config.timeout, 10.0)
        self.assertEqual(config.max_workers, 4)
        self.assertTrue(config.http2)

    def test_startup_warnings(self):
        unsafe = servery.Config.create(".", host="0.0.0.0", auth="u:p")
        warnings = unsafe.startup_warnings()
        self.assertTrue(any("network" in w for w in warnings))
        self.assertTrue(any("cleartext" in w for w in warnings))
        self.assertEqual(servery.Config.create(".").startup_warnings(), [])

    def test_tls_config_and_password_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            pw = Path(tmp) / "pw.txt"
            pw.write_text("s3cret\n")
            args = cli.build_parser().parse_args(
                ["--tls-cert", "c.pem", "--tls-key", "k.pem", "--tls-password-file", str(pw)]
            )
            config = cli.config_from_args(args)
        self.assertTrue(config.uses_tls)
        self.assertEqual(config.tls_password, "s3cret")


class ConfigValidationTest(unittest.TestCase):
    def test_rejects_bad_numerics(self):
        from servery.config import Config

        with self.assertRaises(ValueError):
            Config.create(".", port=70000)
        with self.assertRaises(ValueError):
            Config.create(".", port=-1)
        with self.assertRaises(ValueError):
            Config.create(".", max_upload_size=0)
        with self.assertRaises(ValueError):
            Config.create(".", timeout=0)
        with self.assertRaises(ValueError):
            Config.create(".", cache_max_age=-1)

    def test_accepts_ephemeral_port_zero(self):
        from servery.config import Config

        self.assertEqual(Config.create(".", port=0).port, 0)  # ephemeral is valid


if __name__ == "__main__":
    unittest.main()
