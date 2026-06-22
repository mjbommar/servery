"""Shared server-side TLS context construction.

Both servers use this: the threading server (``server.py``) wraps its listening
socket, and the asyncio ASGI server (``asgi.py``) hands the context to
``asyncio.start_server(ssl=...)``. Forward-secret AEAD-only cipher policy; a cert
is loaded from ``--tls-cert`` or generated ad-hoc (pure-stdlib, never persisted).
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import ssl
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from servery import _log

if TYPE_CHECKING:
    from servery.config import Config

# Client-side transport errors that are noise, not server faults: a failed TLS
# handshake from a scanner, a dropped connection, an idle timeout. Both servers
# swallow these (logging at DEBUG) instead of surfacing a traceback.
CLIENT_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    ssl.SSLError,
    ConnectionError,
    TimeoutError,
)


def is_wildcard_host(host: str) -> bool:
    """True for a bind-all address (0.0.0.0 / ::) — not a real SAN entry."""
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def build_context(config: Config, alpn: list[str]) -> ssl.SSLContext:
    """Build a server TLS context for ``config``, advertising ``alpn`` protocols."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2  # explicit floor, not OpenSSL's default
    # Restrict TLS 1.2 to forward-secret AEAD suites (drop CBC, so Lucky13/SWEET32
    # are off the table). TLS 1.3 suites are all-AEAD already and unaffected.
    try:
        context.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
    except ssl.SSLError as exc:  # e.g. a FIPS build rejects CHACHA20 — don't fail silently
        _log.logger.warning("TLS cipher hardening not applied (%s); using OpenSSL defaults", exc)
    if config.tls_cert is not None:
        context.load_cert_chain(config.tls_cert, config.tls_key, config.tls_password)
    else:
        _load_self_signed(config, context)
    context.set_alpn_protocols(alpn)
    return context


def _load_self_signed(config: Config, context: ssl.SSLContext) -> None:
    """Generate an ad-hoc self-signed cert and load it into ``context``.

    The cert/key are written to a private temp dir (0600), loaded by OpenSSL, then
    removed — nothing persists on disk past startup.
    """
    from servery import _certgen

    hosts = ["localhost", "127.0.0.1", "::1"]
    host = config.host
    if host and host not in hosts and not is_wildcard_host(host):
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
