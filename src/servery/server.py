"""The servery HTTP server.

``ServeryHTTPServer`` is a threading server (one thread per connection); its
configuration is an immutable :class:`~servery.config.Config`, and the resolved
root real-path is computed once so the per-request containment check is cheap.
"""

from __future__ import annotations

import contextlib
import os
import socket
import ssl
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from typing import Any

from servery import _log, auth
from servery.config import Config
from servery.handler import ServeryHandler


class ServeryHTTPServer(ThreadingHTTPServer):
    """Threading HTTP/1.1 server bound to a :class:`Config`."""

    daemon_threads = True
    allow_reuse_address = True

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
        super().__init__((config.host, config.port), ServeryHandler)

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
        cert = self.config.tls_cert
        if cert is not None:
            # create_default_context already enforces a sane minimum (TLS 1.2+)
            # and a secure cipher set; we only advertise HTTP/1.1 over ALPN.
            context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            context.load_cert_chain(cert, self.config.tls_key, self.config.tls_password)
            protocols = ["h2", "http/1.1"] if self.config.http2 else ["http/1.1"]
            context.set_alpn_protocols(protocols)
            self.socket = context.wrap_socket(self.socket, server_side=True)

    def finish_request(self, request: Any, client_address: Any) -> None:
        ServeryHandler(
            request,
            client_address,
            self,
            directory=os.fspath(self.config.directory),
        )


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
