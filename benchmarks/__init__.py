"""Performance benchmarks (pytest-benchmark). Not collected by ``unittest``.

Run with::

    uv run --group bench pytest benchmarks/

This package is intentionally separate from ``tests/`` (which is unittest-based and
run via ``python -m unittest``). It holds the bare WSGI/ASGI apps and the harness
used to spin each transport up in-process for measurement.
"""
