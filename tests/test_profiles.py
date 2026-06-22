"""Launch-profile (--profile) tests."""

from __future__ import annotations

import os
import unittest

from servery import cli


def _config(*argv: str):
    return cli.config_from_args(cli.parse_args([*argv]))


class ProfileTest(unittest.TestCase):
    def test_share_binds_public_with_tls_readonly(self):
        cfg = _config("--profile", "share")
        self.assertEqual(cfg.host, "0.0.0.0")
        self.assertTrue(cfg.tls_self_signed)
        self.assertFalse(cfg.upload)

    def test_explicit_flag_overrides_profile(self):
        # profile sets host 0.0.0.0; explicit -b wins.
        cfg = _config("--profile", "share", "-b", "127.0.0.1")
        self.assertEqual(cfg.host, "127.0.0.1")

    def test_real_cert_supersedes_self_signed(
        self,
    ):
        cfg = _config("--profile", "public-readonly", "--tls-cert", "c.pem", "--tls-key", "k.pem")
        self.assertFalse(cfg.tls_self_signed)
        self.assertEqual(cfg.tls_cert, "c.pem")
        self.assertEqual(cfg.cache_max_age, 3600)  # profile default still applied

    def test_inbox_requires_auth(self):
        with self.assertRaises(ValueError):
            _config("--profile", "inbox")  # no --auth -> rejected

    def test_inbox_with_auth_ok(self):
        cfg = _config("--profile", "inbox", "--auth", "u:p")
        self.assertTrue(cfg.upload)
        self.assertEqual(cfg.host, "0.0.0.0")
        self.assertTrue(cfg.tls_self_signed)

    def test_public_readwrite_requires_auth(self):
        with self.assertRaises(ValueError):
            _config("--profile", "public-readwrite")

    def test_cdn_bundle(self):
        cfg = _config("--profile", "cdn")
        self.assertEqual(cfg.cache_max_age, 31536000)
        self.assertTrue(cfg.cors)
        self.assertTrue(cfg.http2)

    def test_dev_is_local(self):
        cfg = _config("--profile", "dev")
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertTrue(cfg.spa)
        self.assertTrue(cfg.cors)
        self.assertFalse(cfg.tls_self_signed)

    def test_app_sets_workers(self):
        cfg = _config("--profile", "app")
        self.assertEqual(cfg.max_workers, os.cpu_count() or 4)

    def test_no_profile_is_default(self):
        cfg = _config()
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertFalse(cfg.tls_self_signed)


if __name__ == "__main__":
    unittest.main()
