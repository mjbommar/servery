"""Shared parsing for dynamic-app specs (``--wsgi`` / ``--asgi`` ``module:attr``).

WSGI and ASGI both load a callable from a ``"module:attribute"`` string; the only
differences are the default attribute name and the flag label used in errors.
"""

from __future__ import annotations

import importlib
from typing import Any


def load_app(spec: str, *, default_attr: str, label: str) -> Any:
    """Import a callable app from ``"module:attribute"`` (attr defaults to ``default_attr``)."""
    module_name, _, attr = spec.partition(":")
    if not module_name:
        raise ValueError(f"invalid {label} spec {spec!r} (expected 'module:app')")
    module = importlib.import_module(module_name)
    name = attr or default_attr
    app = getattr(module, name, None)
    if app is None:
        raise ValueError(f"{label}: {module_name!r} has no attribute {name!r}")
    if not callable(app):
        raise ValueError(f"{label}: {spec!r} is not callable")  # noqa: TRY004 (CLI value error)
    return app
