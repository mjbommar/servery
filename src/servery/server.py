"""The servery HTTP server.

``ServeryHTTPServer`` is a threading server (one thread per connection); its
configuration is an immutable :class:`~servery.config.Config`, and the resolved
root real-path is computed once so the per-request containment check is cheap.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import shutil
import socket
import ssl
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from servery import _log, auth
from servery.config import Config
from servery.handler import ServeryHandler


class ServeryHTTPServer(ThreadingHTTPServer):
    """Threading HTTP/1.1 server bound to a :class:`Config`."""

    daemon_threads = True
    allow_reuse_address = True

    wsgi_app: Any = None
    cgi_root: str = ""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.root_real = os.path.realpath(config.directory)
        self.credential = auth.parse(config.auth)
        self._executor = (
            ThreadPoolExecutor(max_workers=config.max_workers) if config.max_workers else None
        )
        # Bound accepted-but-queued connections too, not just running workers, so
        # a flood can't grow the executor queue (and held sockets) without limit.
        self._slots = threading.Semaphore(config.max_workers * 4) if config.max_workers else None
        if ":" in config.host:
            self.address_family = socket.AF_INET6
        # Opt-in dynamic handlers replace file serving entirely (loaded up front
        # so an import error surfaces at startup, not mid-request).
        self._handler_cls: type[ServeryHandler] = ServeryHandler
        if config.wsgi_app:
            from servery import wsgi

            self.wsgi_app = wsgi.load_app(config.wsgi_app)
            self._handler_cls = wsgi.WSGIHandler
        elif config.cgi_dir:
            from servery import cgi

            self.cgi_root = os.path.realpath(config.cgi_dir)
            if not Path(self.cgi_root).is_dir():
                raise ValueError(f"--cgi: {config.cgi_dir!r} is not a directory")
            self._handler_cls = cgi.CGIHandler
        super().__init__((config.host, config.port), self._handler_cls)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Default: a thread per connection (ThreadingMixIn). With --max-workers,
        # bound concurrency through a shared pool instead.
        if self._executor is not None and self._slots is not None:
            self._slots.acquire()
            self._executor.submit(self._process_request_pooled, request, client_address)
        else:
            super().process_request(request, client_address)

    def _process_request_pooled(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:  # mirror ThreadingMixIn: never let a worker thread die
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            if self._slots is not None:
                self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A failed TLS handshake or a dropped connection is a client-side problem,
        # not a server fault — don't spew a traceback for every old/scanning peer.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def server_close(self) -> None:
        super().server_close()
        if self._executor is not None:
            self._executor.shutdown(wait=True)

    def server_bind(self) -> None:
        # Accept both IPv4 and IPv6 when bound to an IPv6 wildcard.
        if self.address_family == socket.AF_INET6:
            with contextlib.suppress(OSError):
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def server_activate(self) -> None:
        super().server_activate()
        config = self.config
        if not config.uses_tls:
            return
        # create_default_context already enforces a sane minimum (TLS 1.2+) and a
        # secure cipher set; we only advertise HTTP/1.1 (+ h2) over ALPN.
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        # Restrict TLS 1.2 to forward-secret AEAD suites (drop CBC, so the whole
        # Lucky13/SWEET32 class is off the table). TLS 1.3 suites are all-AEAD
        # already and unaffected by set_ciphers.
        with contextlib.suppress(ssl.SSLError):
            context.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
        if config.tls_cert is not None:
            context.load_cert_chain(config.tls_cert, config.tls_key, config.tls_password)
        else:
            self._load_self_signed(context)
        protocols = ["h2", "http/1.1"] if config.http2 else ["http/1.1"]
        context.set_alpn_protocols(protocols)
        self.socket = context.wrap_socket(self.socket, server_side=True)

    def _load_self_signed(self, context: ssl.SSLContext) -> None:
        """Generate an ad-hoc self-signed cert and load it into ``context``.

        The cert/key are written to a private temp dir (0600), loaded by OpenSSL,
        then removed — nothing persists on disk past startup.
        """
        from servery import _certgen

        hosts = ["localhost", "127.0.0.1", "::1"]
        host = self.config.host
        if host and host not in hosts and not _is_wildcard_host(host):
            hosts.append(host)
        cert_pem, key_pem = _certgen.generate(hosts)
        tmp = Path(tempfile.mkdtemp(prefix="servery-tls-"))
        try:
            cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
            for path, data in ((cert_path, cert_pem), (key_path, key_pem)):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(data)
            context.load_cert_chain(cert_path, key_path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def finish_request(self, request: Any, client_address: Any) -> None:
        # Use the selected handler class (ServeryHandler, or WSGIHandler in
        # --wsgi mode), not a hardcoded one.
        self._handler_cls(
            request,
            client_address,
            self,
            directory=os.fspath(self.config.directory),
        )


def _is_wildcard_host(host: str) -> bool:
    """True for a bind-all address (0.0.0.0 / ::) — not a real SAN entry."""
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def make_server(config: Config) -> ServeryHTTPServer:
    """Create (bind + activate) a server for ``config``."""
    return ServeryHTTPServer(config)


def server_url(server: ServeryHTTPServer) -> str:
    """Return the URL the server is actually listening on."""
    host, port = server.server_address[:2]
    host_display = f"[{host}]" if ":" in str(host) else host
    scheme = "https" if server.config.uses_tls else "http"
    return f"{scheme}://{host_display}:{port}/"


def serve(config: Config) -> None:  # pragma: no cover - blocking server loop (CLI entry)
    """Run the server until interrupted. Blocks the calling thread."""
    if not config.quiet:
        _log.configure_stderr()
    with make_server(config) as httpd:
        if not config.quiet:
            print(f"servery: serving {config.directory} at {server_url(httpd)}", file=sys.stderr)
            for warning in config.startup_warnings():
                print(f"servery: WARNING {warning}", file=sys.stderr)
        with contextlib.suppress(KeyboardInterrupt):
            httpd.serve_forever()
