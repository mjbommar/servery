"""Per-request latency benchmarks for the HTTP/1.1 transports: static files,
the directory listing, and the WSGI / CGI dynamic handlers.

    uv run --group bench pytest benchmarks/test_bench_http1.py
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from benchmarks._harness import client_get, threaded_server
from benchmarks.conftest import LARGE_MIB, SMALL
from servery.config import Config


@pytest.fixture(scope="module")
def http1(tree: Path) -> Iterator[tuple[str, int]]:
    config = Config.create(str(tree), host="127.0.0.1", port=0, quiet=True)
    with threaded_server(config) as addr:
        yield addr


def test_http1_small_file(benchmark: object, http1: tuple[str, int]) -> None:
    conn, do = client_get(*http1, "/small.txt")
    try:
        assert do() == SMALL  # warm + correctness gate before timing
        benchmark(do)
    finally:
        conn.close()


def test_http1_large_file_sendfile(benchmark: object, http1: tuple[str, int]) -> None:
    conn, do = client_get(*http1, "/large.bin")
    try:
        assert len(do()) == LARGE_MIB * 1024 * 1024
        benchmark(do)
    finally:
        conn.close()


def test_http1_directory_listing(benchmark: object, http1: tuple[str, int]) -> None:
    conn, do = client_get(*http1, "/")
    try:
        assert b"file00.txt" in do()
        benchmark(do)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def wsgi(tree: Path) -> Iterator[tuple[str, int]]:
    config = Config.create(
        str(tree), host="127.0.0.1", port=0, quiet=True, wsgi_app="benchmarks._apps:wsgi_app"
    )
    with threaded_server(config) as addr:
        yield addr


def test_wsgi_hello(benchmark: object, wsgi: tuple[str, int]) -> None:
    conn, do = client_get(*wsgi, "/")
    try:
        assert do() == b"ok"
        benchmark(do)
    finally:
        conn.close()


_CGI_SCRIPT = 'import sys\nsys.stdout.write("Content-Type: text/plain\\r\\n\\r\\nok")\n'


@pytest.fixture(scope="module")
def cgi(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, int]]:
    cgi_dir = tmp_path_factory.mktemp("bench-cgi")
    (cgi_dir / "hello.py").write_text(_CGI_SCRIPT)
    config = Config.create(".", host="127.0.0.1", port=0, quiet=True, cgi_dir=str(cgi_dir))
    with threaded_server(config) as addr:
        yield addr


def test_cgi_hello(benchmark: object, cgi: tuple[str, int]) -> None:
    # CGI forks an interpreter per request — expected to be ORDERS slower than WSGI.
    # The benchmark documents that cost rather than hiding it.
    conn, do = client_get(*cgi, "/hello.py")
    try:
        assert do() == b"ok"
        benchmark(do)
    finally:
        conn.close()
