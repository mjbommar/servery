"""Logging for servery.

Library code logs through the ``servery`` logger and never configures the root
logger; a :class:`~logging.NullHandler` is attached so importing servery is
silent until the application (or our CLI) opts in via :func:`configure_stderr`.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger("servery")
logger.addHandler(logging.NullHandler())

_stderr_handler: logging.Handler | None = None


def configure_stderr(level: int = logging.INFO) -> None:
    """Attach a stderr handler to servery's logger (idempotent). Used by the CLI."""
    global _stderr_handler
    if _stderr_handler is not None:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(level)
    _stderr_handler = handler
