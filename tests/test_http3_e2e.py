"""Optional real-QUIC lifecycle, streaming, and TCP fallback tests."""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import ssl
import tempfile
import threading
import unittest
from pathlib import Path

_HAVE_AIOQUIC = importlib.util.find_spec("aioquic") is not None


@unittest.skipUnless(_HAVE_AIOQUIC, "HTTP/3 e2e needs the optional aioquic extra")
class Http3EndToEndTest(unittest.TestCase):
    def test_large_file_streams_and_listener_stops_cleanly(self):
        from benchmarks._http3_client import http3_client, http3_server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = bytes(range(256)) * 4096
            (root / "large.bin").write_bytes(payload)
            with (
                http3_server(root, max_buffered_response=1024) as (host, port, cafile),
                http3_client(host, port, cafile) as get,
            ):
                self.assertEqual(get("/large.bin"), payload)

    def test_tcp_fallback_advertises_actual_live_udp_port(self):
        from benchmarks._http3_client import http3_client

        from servery import _certgen, http3
        from servery.config import Config
        from servery.server import make_server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "f.txt").write_bytes(b"simultaneous")
            cert_pem, key_pem = _certgen.generate(["localhost", "127.0.0.1"])
            cert, key = root / "cert.pem", root / "key.pem"
            cert.write_text(cert_pem)
            key.write_text(key_pem)
            config = Config.create(
                root,
                host="127.0.0.1",
                port=0,
                quiet=True,
                tls_cert=str(cert),
                tls_key=str(key),
                http3=True,
                http3_port=0,
            )
            httpd = make_server(config)
            h3 = http3.start_http3(config, compression_cache=httpd.compression_cache)
            httpd.http3_port = h3.port
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                tls = ssl._create_unverified_context()
                conn = http.client.HTTPSConnection(
                    str(httpd.server_address[0]),
                    int(httpd.server_address[1]),
                    timeout=5,
                    context=tls,
                )
                conn.request("GET", "/f.txt", headers={"Connection": "close"})
                response = conn.getresponse()
                self.assertEqual(response.read(), b"simultaneous")
                self.assertEqual(response.getheader("Alt-Svc"), f'h3=":{h3.port}"; ma=86400')
                conn.close()
                with http3_client("127.0.0.1", h3.port, str(cert)) as get:
                    self.assertEqual(get("/f.txt"), b"simultaneous")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(5)
                with contextlib.suppress(Exception):
                    h3.close()
            self.assertFalse(thread.is_alive())
            self.assertFalse(h3.is_alive)


if __name__ == "__main__":
    unittest.main()
