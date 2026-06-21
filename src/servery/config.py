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
    auth: str | None = None
    upload: bool = False
    max_upload_size: int = 100 * 1024 * 1024
    allow_overwrite: bool = False
    cors: bool = False
    spa: bool = False
    cache_max_age: int | None = None
    security_headers: bool = True

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
        """True when HTTPS is configured (a certificate was provided)."""
        return self.tls_cert is not None

    def startup_warnings(self) -> list[str]:
        """Return human-readable warnings about an unsafe configuration."""
        warnings: list[str] = []
        if not self.is_loopback_bind:
            warnings.append(f"bound to {self.host} — reachable from the network")
        if self.auth is not None and not self.uses_tls:
            warnings.append("Basic auth is enabled without TLS — credentials travel in cleartext")
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
        auth: str | None = None,
        upload: bool = False,
        max_upload_size: int = 100 * 1024 * 1024,
        allow_overwrite: bool = False,
        cors: bool = False,
        spa: bool = False,
        cache_max_age: int | None = None,
        security_headers: bool = True,
    ) -> Config:
        """Build a Config, resolving ``directory`` to an absolute path."""
        return cls(
            directory=Path(directory).resolve(),
            host=host,
            port=port,
            show_hidden=show_hidden,
            quiet=quiet,
            tls_cert=tls_cert,
            tls_key=tls_key,
            tls_password=tls_password,
            auth=auth,
            upload=upload,
            max_upload_size=max_upload_size,
            allow_overwrite=allow_overwrite,
            cors=cors,
            spa=spa,
            cache_max_age=cache_max_age,
            security_headers=security_headers,
        )
