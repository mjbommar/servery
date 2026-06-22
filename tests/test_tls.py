"""Stdlib self-signed cert generation (_certgen) + the --tls-self-signed path."""

from __future__ import annotations

import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path

from servery import _certgen
from servery.config import Config
from servery.server import make_server

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False


class CertGenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One keygen for all the structural tests (keeps the suite fast).
        cls.cert_pem, cls.key_pem = _certgen.generate(["localhost", "127.0.0.1", "::1"])
        cls._tmp = tempfile.TemporaryDirectory()
        cls.cert = Path(cls._tmp.name, "cert.pem")
        cls.key = Path(cls._tmp.name, "key.pem")
        cls.cert.write_text(cls.cert_pem)
        cls.key.write_text(cls.key_pem)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_pem_shapes(self):
        self.assertIn("-----BEGIN CERTIFICATE-----", self.cert_pem)
        self.assertIn("-----BEGIN RSA PRIVATE KEY-----", self.key_pem)

    def test_ssl_loads_the_chain(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)  # raises on a malformed cert/key

    def test_empty_hosts_rejected(self):
        with self.assertRaises(ValueError):
            _certgen.generate([])

    def test_verifies_when_trusted_and_san_is_correct(self):
        # A client that explicitly trusts the cert must verify it for "localhost"
        # (proves the chain + signature), and the SAN must carry what we asked for.
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(self.cert, self.key)
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def serve():
            conn, _ = srv.accept()
            with sctx.wrap_socket(conn, server_side=True) as tls:
                tls.recv(8)

        threading.Thread(target=serve, daemon=True).start()
        cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cctx.load_verify_locations(str(self.cert))
        cctx.check_hostname = True
        cctx.verify_mode = ssl.CERT_REQUIRED
        with cctx.wrap_socket(
            socket.create_connection(("127.0.0.1", port)), server_hostname="localhost"
        ) as client:
            peercert = client.getpeercert()
            client.sendall(b"x")
        srv.close()
        self.assertIsNotNone(peercert)  # CERT_REQUIRED verification succeeded
        sans = set((peercert or {}).get("subjectAltName", ()))
        self.assertIn(("DNS", "localhost"), sans)
        self.assertIn(("IP Address", "127.0.0.1"), sans)


class ConfigTest(unittest.TestCase):
    def test_self_signed_uses_tls_and_warns(self):
        cfg = Config.create(".", tls_self_signed=True, port=0)
        self.assertTrue(cfg.uses_tls)
        self.assertTrue(any("self-signed" in w for w in cfg.startup_warnings()))

    def test_self_signed_and_cert_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            Config.create(".", tls_self_signed=True, tls_cert="cert.pem")


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class SelfSignedServerTest(unittest.TestCase):
    def test_https_get_over_self_signed_server(self):
        tmp = tempfile.TemporaryDirectory()
        Path(tmp.name, "f.txt").write_text("secure hi")
        cfg = Config.create(tmp.name, host="127.0.0.1", port=0, quiet=True, tls_self_signed=True)
        httpd = make_server(cfg)  # generates the ad-hoc cert during activation
        host, port = httpd.server_address[0], httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(verify=False) as client:  # self-signed: trust skipped
                resp = client.get(f"https://{host}:{port}/f.txt")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.text, "secure hi")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            tmp.cleanup()


class TlsHardeningTest(unittest.TestCase):
    """The testssl.sh findings as a stdlib regression: only TLS 1.2/1.3 with
    forward-secret AEAD ciphers (no CBC -> no Lucky13/SWEET32 surface)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "f.txt").write_text("x")
        cfg = Config.create(
            self._tmp.name, host="127.0.0.1", port=0, quiet=True, tls_self_signed=True
        )
        self.httpd = make_server(cfg)
        self.host, self.port = self.httpd.server_address[0], self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
        self._tmp.cleanup()

    def _client_ctx(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed
        return ctx

    def _connect(self, ctx: ssl.SSLContext):
        sock = socket.create_connection((str(self.host), int(self.port)), timeout=5)
        return ctx.wrap_socket(sock, server_hostname="localhost")

    def test_negotiates_modern_tls_and_aead_cipher(self):
        with self._connect(self._client_ctx()) as tls:
            self.assertIn(tls.version(), ("TLSv1.2", "TLSv1.3"))
            name = tls.cipher()[0]
            self.assertTrue("GCM" in name or "CHACHA20" in name, name)
            self.assertNotIn("CBC", name)

    def test_tls12_is_forward_secret_aead(self):
        ctx = self._client_ctx()
        ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        with self._connect(ctx) as tls:
            self.assertEqual(tls.version(), "TLSv1.2")
            name = tls.cipher()[0]
            self.assertIn("ECDHE", name)  # forward secrecy
            self.assertTrue("GCM" in name or "CHACHA20" in name, name)
            self.assertNotIn("CBC", name)

    def test_legacy_tls_below_1_2_is_rejected(self):
        ctx = self._client_ctx()
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
            ctx.maximum_version = ssl.TLSVersion.TLSv1_1
        except (ValueError, OSError):  # pragma: no cover - build without TLS<1.2
            self.skipTest("client OpenSSL cannot offer TLS < 1.2")
        with self.assertRaises((ssl.SSLError, OSError)):
            self._connect(ctx)


if __name__ == "__main__":
    unittest.main()
