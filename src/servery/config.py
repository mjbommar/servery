"""Runtime configuration for servery.

``Config`` is a frozen dataclass — the single, immutable source of truth shared
across request-handler threads. Immutability is deliberate: it makes the server
safe to run under free-threaded (no-GIL) CPython without locks.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path


@dataclasses.dataclass(frozen=True, slots=True)
class Config:
    """Immutable server configuration.

    New fields are added as features land; everything here is safe to read
    concurrently from many threads.
    """

    directory: Path
    host: str = "127.0.0.1"
    port: int = 8000
    show_hidden: bool = False
    quiet: bool = False
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_password: str | None = None
    tls_self_signed: bool = False
    auth: str | None = None
    upload: bool = False
    max_upload_size: int = 100 * 1024 * 1024
    allow_overwrite: bool = False
    upload_extract: bool = False  # expand uploaded archives (requires upload)
    cors: bool = False
    spa: bool = False
    cache_max_age: int | None = None
    security_headers: bool = True
    timeout: float = 30.0
    max_workers: int | None = None
    http2: bool = False
    wsgi_app: str | None = None  # "module:callable" — opt-in dynamic handler
    cgi_dir: str | None = None  # cgi-bin directory — opt-in dynamic handler
    asgi_app: str | None = None  # "module:callable" — opt-in async dynamic handler
    proxy_routes: tuple[tuple[str, str], ...] = ()  # (path-prefix, upstream-url) pairs

    @property
    def cache_control(self) -> str:
        """The Cache-Control value for file responses."""
        if self.cache_max_age is None:
            return "no-cache"
        return f"max-age={self.cache_max_age}"

    @property
    def is_loopback_bind(self) -> bool:
        """True when bound to a loopback address (the safe default)."""
        return self.host in {"127.0.0.1", "::1", "localhost"}

    @property
    def uses_tls(self) -> bool:
        """True when HTTPS is configured (a provided or self-signed certificate)."""
        return self.tls_cert is not None or self.tls_self_signed

    def startup_warnings(self) -> list[str]:
        """Return human-readable warnings about an unsafe configuration."""
        warnings: list[str] = []
        if not self.is_loopback_bind:
            warnings.append(f"bound to {self.host} — reachable from the network")
        if self.auth is not None and not self.uses_tls:
            warnings.append("Basic auth is enabled without TLS — credentials travel in cleartext")
        if self.tls_self_signed:
            warnings.append(
                "using a self-signed certificate — clients will see an "
                "'untrusted certificate' warning (fine for a dev box or LAN)"
            )
        return warnings

    @classmethod
    def create(
        cls,
        directory: str | os.PathLike[str] = ".",
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        show_hidden: bool = False,
        quiet: bool = False,
        tls_cert: str | None = None,
        tls_key: str | None = None,
        tls_password: str | None = None,
        tls_self_signed: bool = False,
        auth: str | None = None,
        upload: bool = False,
        max_upload_size: int = 100 * 1024 * 1024,
        allow_overwrite: bool = False,
        upload_extract: bool = False,
        cors: bool = False,
        spa: bool = False,
        cache_max_age: int | None = None,
        security_headers: bool = True,
        timeout: float = 30.0,
        max_workers: int | None = None,
        http2: bool = False,
        wsgi_app: str | None = None,
        cgi_dir: str | None = None,
        asgi_app: str | None = None,
        proxy: list[str] | None = None,
    ) -> Config:
        """Build a Config, resolving ``directory`` to an absolute path."""
        proxy_routes = _parse_proxy_routes(proxy or [])
        if tls_self_signed and tls_cert is not None:
            raise ValueError("--tls-self-signed cannot be combined with --tls-cert")
        dynamic = [
            name
            for name, value in (("--wsgi", wsgi_app), ("--cgi", cgi_dir), ("--asgi", asgi_app))
            if value
        ]
        if len(dynamic) > 1:
            raise ValueError(f"choose only one dynamic handler: {' / '.join(dynamic)}")
        if dynamic and http2:
            raise ValueError(f"{dynamic[0]} is HTTP/1.1 only and cannot be combined with --http2")
        if upload_extract and not upload:
            raise ValueError("--upload-extract requires --upload")
        return cls(
            directory=Path(directory).resolve(),
            host=host,
            port=port,
            show_hidden=show_hidden,
            quiet=quiet,
            tls_cert=tls_cert,
            tls_key=tls_key,
            tls_password=tls_password,
            tls_self_signed=tls_self_signed,
            auth=auth,
            upload=upload,
            max_upload_size=max_upload_size,
            allow_overwrite=allow_overwrite,
            upload_extract=upload_extract,
            cors=cors,
            spa=spa,
            cache_max_age=cache_max_age,
            security_headers=security_headers,
            timeout=timeout,
            max_workers=max_workers,
            http2=http2,
            wsgi_app=wsgi_app,
            cgi_dir=cgi_dir,
            asgi_app=asgi_app,
            proxy_routes=proxy_routes,
        )


def _parse_proxy_routes(specs: list[str]) -> tuple[tuple[str, str], ...]:
    """Parse ``["/api=http://host:port", ...]`` into validated (prefix, url) pairs."""
    routes: list[tuple[str, str]] = []
    for spec in specs:
        prefix, sep, upstream = spec.partition("=")
        if not sep or not prefix.startswith("/"):
            raise ValueError(f"--proxy {spec!r}: expected '/prefix=http://upstream'")
        if not upstream.startswith(("http://", "https://")):
            raise ValueError(f"--proxy {spec!r}: upstream must be an http(s) URL")
        routes.append((prefix, upstream))
    # Longest prefix first, so /api/v2 wins over /api.
    routes.sort(key=lambda route: len(route[0]), reverse=True)
    return tuple(routes)
