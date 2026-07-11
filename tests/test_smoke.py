"""Package + CLI parsing smoke tests."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

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
        self.assertEqual(args.listing_page_size, 1000)
        self.assertIsNone(args.keepalive_timeout)
        self.assertIsNone(args.request_head_timeout)
        self.assertIsNone(args.request_body_timeout)
        self.assertIsNone(args.write_timeout)
        self.assertEqual(args.drain_timeout, 30.0)
        self.assertEqual(args.workers, "1")
        self.assertEqual(args.worker_start_timeout, 30.0)
        self.assertEqual(args.force_timeout, 1.0)
        self.assertEqual(args.max_requests_per_connection, 0)
        self.assertEqual(args.lifespan, "auto")
        self.assertEqual(args.lifespan_timeout, 5.0)

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

    def test_supervisor_startup_failure_reports_error_not_traceback(self):
        err = io.StringIO()
        with (
            mock.patch.object(
                cli, "serve", side_effect=cli.SupervisorError("worker 2 failed startup")
            ),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(["--workers", "2"])
        self.assertEqual(code, 2)
        self.assertIn("worker 2 failed startup", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

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

    def test_asgi_lifespan_flags(self):
        args = cli.build_parser().parse_args(
            ["--asgi", "m:a", "--lifespan", "on", "--lifespan-timeout", "2.5"]
        )
        config = cli.config_from_args(args)
        self.assertEqual(config.lifespan, "on")
        self.assertEqual(config.lifespan_timeout, 2.5)

    def test_explicit_asgi_lifespan_failure_is_a_clean_cli_error(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            code = cli.main(
                [
                    "--asgi",
                    "tests._asgiapp:lifespan_startup_failed",
                    "--port",
                    "0",
                    "--quiet",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("database unavailable", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())

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
        args = cli.build_parser().parse_args(
            [
                "--timeout",
                "10",
                "--keepalive-timeout",
                "2.5",
                "--request-head-timeout",
                "3.5",
                "--request-body-timeout",
                "4.5",
                "--write-timeout",
                "1.5",
                "--drain-timeout",
                "12.5",
                "--workers",
                "2",
                "--worker-start-timeout",
                "7.5",
                "--force-timeout",
                "0.5",
                "--max-workers",
                "4",
                "--max-archive-streams",
                "2",
                "--max-connections",
                "8",
                "--max-requests-per-connection",
                "9",
                "--http2",
                "--max-h2-streams",
                "12",
                "--max-request-body",
                "1000",
                "--keepalive-drain-limit",
                "100",
                "--write-lock-timeout",
                "0.5",
                "--partial-upload-ttl",
                "60",
                "--max-partial-uploads",
                "7",
                "--max-compress-size",
                "2000",
                "--compression-cache-size",
                "3000",
                "--max-buffered-response",
                "4000",
                "--small-file-buffer-size",
                "16000",
                "--max-listing-entries",
                "50",
                "--listing-page-size",
                "10",
                "--listing-details-threshold",
                "20",
                "--max-propfind-entries",
                "30",
                "--max-tftp-transfers",
                "2",
                "--access-log-queue",
                "9",
                "--access-log-queue-bytes",
                "4096",
                "--access-log-overflow",
                "drop",
                "--access-log-batch-size",
                "3",
                "--access-log-batch-wait",
                "0.01",
                "--access-log-drain-timeout",
                "0.5",
            ]
        )
        config = cli.config_from_args(args)
        self.assertEqual(config.timeout, 10.0)
        self.assertEqual(config.keepalive_timeout, 2.5)
        self.assertEqual(config.request_head_timeout, 3.5)
        self.assertEqual(config.request_body_timeout, 4.5)
        self.assertEqual(config.write_timeout, 1.5)
        self.assertEqual(config.drain_timeout, 12.5)
        self.assertEqual(config.workers, 2)
        self.assertEqual(config.worker_start_timeout, 7.5)
        self.assertEqual(config.force_timeout, 0.5)
        self.assertEqual(config.max_workers, 4)
        self.assertEqual(config.max_archive_streams, 2)
        self.assertEqual(config.max_connections, 8)
        self.assertEqual(config.max_requests_per_connection, 9)
        self.assertTrue(config.http2)
        self.assertEqual(config.max_h2_streams, 12)
        self.assertEqual(config.max_request_body, 1000)
        self.assertEqual(config.keepalive_drain_limit, 100)
        self.assertEqual(config.write_lock_timeout, 0.5)
        self.assertEqual(config.partial_upload_ttl, 60)
        self.assertEqual(config.max_partial_uploads, 7)
        self.assertEqual(config.max_compress_size, 2000)
        self.assertEqual(config.compression_cache_size, 3000)
        self.assertEqual(config.max_buffered_response, 4000)
        self.assertEqual(config.small_file_buffer_size, 16000)
        self.assertEqual(config.max_listing_entries, 50)
        self.assertEqual(config.listing_page_size, 10)
        self.assertEqual(config.listing_details_threshold, 20)
        self.assertEqual(config.max_propfind_entries, 30)
        self.assertEqual(config.max_tftp_transfers, 2)
        self.assertEqual(config.access_log_queue, 9)
        self.assertEqual(config.access_log_queue_bytes, 4096)
        self.assertEqual(config.access_log_overflow, "drop")
        self.assertEqual(config.access_log_batch_size, 3)
        self.assertEqual(config.access_log_batch_wait, 0.01)
        self.assertEqual(config.access_log_drain_timeout, 0.5)

    def test_worker_auto_and_unsupported_multiworker_modes(self):
        from servery.config import Config

        self.assertGreaterEqual(Config.create(".", workers="auto").workers, 1)
        cases: tuple[tuple[str, dict[str, Any]], ...] = (
            ("--upload", {"workers": 2, "upload": True}),
            ("--dav", {"workers": 2, "dav": True}),
            ("--cgi", {"workers": 2, "cgi_dir": "cgi-bin"}),
            ("--proxy", {"workers": 2, "proxy": ["/api=http://127.0.0.1:9000"]}),
            ("--http3", {"workers": 2, "http3": True, "tls_self_signed": True}),
            ("--tftp", {"workers": 2, "tftp": True}),
            ("--discoverable", {"workers": 2, "discoverable": True}),
            ("--qr", {"workers": 2, "qr": True}),
            ("--acme", {"workers": 2, "acme": ("example.test",)}),
            ("--access-log", {"workers": 2, "access_log": "access.log"}),
        )
        for flag, values in cases:
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(ValueError, flag),
            ):
                Config.create(".", **values)

    def test_multiworker_resource_budgets_are_explicitly_per_worker(self):
        from servery.config import Config

        config = Config.create(".", workers=4, max_connections=128, compression_cache_size=1024)
        self.assertEqual(config.aggregate_connection_limit, 512)
        self.assertEqual(config.aggregate_compression_cache_size, 4096)
        self.assertIsNone(
            Config.create(".", workers=4, max_connections=None).aggregate_connection_limit
        )

    def test_http3_transport_flags(self):
        config = cli.config_from_args(
            cli.build_parser().parse_args(
                [
                    "--http3",
                    "--http3-only",
                    "--http3-port",
                    "9443",
                    "--tls-self-signed",
                ]
            )
        )
        self.assertTrue(config.http3)
        self.assertTrue(config.http3_only)
        self.assertEqual(config.http3_port, 9443)

    def test_startup_warnings(self):
        unsafe = servery.Config.create(".", host="0.0.0.0", auth="u:p")
        warnings = unsafe.startup_warnings()
        self.assertTrue(any("network" in w for w in warnings))
        self.assertTrue(any("cleartext" in w for w in warnings))
        self.assertEqual(servery.Config.create(".").startup_warnings(), [])

    def test_tftp_flags_and_warnings(self):
        args = cli.build_parser().parse_args(["--tftp", "--tftp-port", "6900", "--tftp-write"])
        config = cli.config_from_args(args)
        self.assertTrue(config.tftp)
        self.assertEqual(config.tftp_port, 6900)
        self.assertTrue(config.tftp_write)
        warnings = config.startup_warnings()
        self.assertTrue(any("TFTP" in w for w in warnings))
        self.assertTrue(any("anonymous" in w for w in warnings))

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
        with self.assertRaises(ValueError):
            Config.create(".", tftp_port=70000)
        with self.assertRaises(ValueError):
            Config.create(".", tftp_write=True)  # requires tftp

        bad_numeric: tuple[dict[str, Any], ...] = (
            {"max_request_body": 0},
            {"keepalive_drain_limit": -1},
            {"write_lock_timeout": -1},
            {"partial_upload_ttl": -1},
            {"max_partial_uploads": -1},
            {"keepalive_timeout": 0},
            {"request_head_timeout": 0},
            {"request_body_timeout": 0},
            {"write_timeout": 0},
            {"drain_timeout": -1},
            {"workers": 0},
            {"workers": "invalid"},
            {"worker_start_timeout": 0},
            {"force_timeout": -1},
            {"max_workers": 0},
            {"max_archive_streams": 0},
            {"max_connections": 0},
            {"max_requests_per_connection": -1},
            {"max_h2_streams": 0},
            {"max_tftp_transfers": 0},
            {"max_propfind_entries": 0},
            {"max_compress_size": -1},
            {"compression_cache_size": -1},
            {"max_buffered_response": -1},
            {"small_file_buffer_size": -1},
            {"max_listing_entries": 0},
            {"listing_page_size": 0},
            {"listing_details_threshold": 0},
            {"access_log_queue": -1},
            {"access_log_queue_bytes": 0},
            {"access_log_batch_size": 0},
            {"access_log_batch_wait": -1},
            {"access_log_drain_timeout": -1},
            {"http3_port": 70000},
        )
        for values in bad_numeric:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Config.create(".", **values)

    def test_rejects_bad_access_log_overflow(self):
        from servery.config import Config

        with self.assertRaisesRegex(ValueError, "access-log-overflow"):
            Config.create(".", access_log_overflow="stderr")

    def test_archive_stream_limit_preserves_a_connection_worker(self):
        from servery.config import Config

        with self.assertRaisesRegex(ValueError, "smaller than --max-workers"):
            Config.create(".", max_workers=2, max_archive_streams=2)
        with self.assertRaises(ValueError):
            Config.create(".", dav_lock_mode="fake")
        self.assertEqual(Config.create(".", max_listing_entries=5).max_listing_entries, 5)

    def test_http3_combinations_are_explicit(self):
        from servery.config import Config

        with self.assertRaises(ValueError):
            Config.create(".", http3=True)
        with self.assertRaises(ValueError):
            Config.create(".", http3_only=True)
        with self.assertRaises(ValueError):
            Config.create(".", http3=True, tls_self_signed=True, wsgi_app="m:a")
        with self.assertRaises(ValueError):
            Config.create(".", http3=True, tls_self_signed=True, proxy=["/=http://x"])
        config = Config.create(
            ".",
            http3=True,
            http3_only=True,
            http3_port=0,
            tls_self_signed=True,
            tftp=True,
        )
        self.assertTrue(config.http3_only)
        self.assertEqual(config.http3_port, 0)

    def test_accepts_ephemeral_port_zero(self):
        from servery.config import Config

        self.assertEqual(Config.create(".", port=0).port, 0)  # ephemeral is valid


if __name__ == "__main__":
    unittest.main()
