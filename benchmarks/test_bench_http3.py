"""HTTP/3 benchmarks.

Two layers, because aioquic (the QUIC stack) has no free-threaded build and so
can't be imported on the default 3.14t interpreter:

* ``test_http3_build_response*`` — the servery-owned request→response resolution
  (path safety, listing/file read, header assembly). Pure stdlib, ALWAYS runs;
  this is the part of HTTP/3 servery actually implements.
* ``test_http3_end_to_end`` — a real QUIC client⇄server round-trip via aioquic.
  ``importorskip``-ed, so it skips unless aioquic is installed. Run it on a GIL
  build with the extra::

      uv run --python 3.13 --group bench --extra http3 pytest benchmarks/test_bench_http3.py
"""

from __future__ import annotations

import os
from pathlib import Path

from benchmarks.conftest import SMALL
from servery import http3
from servery.config import Config


def test_http3_build_response_file(benchmark: object, tree: Path) -> None:
    config = Config.create(str(tree), host="127.0.0.1", port=8443, quiet=True)
    root_real = os.path.realpath(str(tree))
    status, _headers, body = http3.build_response(config, root_real, "GET", "/small.txt")
    assert status == 200 and body == SMALL
    benchmark(lambda: http3.build_response(config, root_real, "GET", "/small.txt"))


def test_http3_build_response_listing(benchmark: object, tree: Path) -> None:
    config = Config.create(str(tree), host="127.0.0.1", port=8443, quiet=True)
    root_real = os.path.realpath(str(tree))
    benchmark(lambda: http3.build_response(config, root_real, "GET", "/"))


def test_http3_end_to_end(benchmark: object, tree: Path) -> None:
    import pytest

    pytest.importorskip("aioquic", reason="HTTP/3 e2e needs aioquic (GIL build + --extra http3)")

    from benchmarks._http3_client import http3_client, http3_server

    with http3_server(tree) as (host, port, cafile), http3_client(host, port, cafile) as get:
        assert get("/small.txt") == SMALL  # warm + correctness; handshake done here
        benchmark(lambda: get("/small.txt"))
