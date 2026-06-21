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

    @property
    def is_loopback_bind(self) -> bool:
        """True when bound to a loopback address (the safe default)."""
        return self.host in {"127.0.0.1", "::1", "localhost"}

    @property
    def uses_tls(self) -> bool:
        """True when HTTPS is configured (a certificate was provided)."""
        return self.tls_cert is not None

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
        )
