"""Latency benchmarks for the TLS / HTTP-2 / ASGI / reverse-proxy transports.

    uv run --group bench pytest benchmarks/test_bench_secure.py

HTTP/2 (ALPN + h2c) and TLS are driven with httpx; ASGI and the proxy use the
stdlib keep-alive client. All hit a 1 KiB small file (or a fixed app body) so the
numbers isolate the transport, not the payload.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from benchmarks._harness import (
    asgi_server,
    client_get,
    httpx_get,
    threaded_server,
    upstream_server,
)
from benchmarks.conftest import SMALL
from servery.config import Config

pytest.importorskip("httpx")  # TLS/H2 cases need the interop client


@pytest.fixture(scope="module")
def tls(tree: Path) -> Iterator[tuple[str, int]]:
    config = Config.create(str(tree), host="127.0.0.1", port=0, quiet=True, tls_self_signed=True)
    with threaded_server(config) as addr:
        yield addr


def test_tls_small_file(benchmark: object, tls: tuple[str, int]) -> None:
    host, port = tls
    client, do = httpx_get(f"https://{host}:{port}/small.txt", verify=False)
    try:
        assert do() == SMALL
        benchmark(do)
    finally:
        client.close()


@pytest.fixture(scope="module")
def h2(tree: Path) -> Iterator[tuple[str, int]]:
    config = Config.create(
        str(tree), host="127.0.0.1", port=0, quiet=True, http2=True, tls_self_signed=True
    )
    with threaded_server(config) as addr:
        yield addr


def test_http2_alpn_small_file(benchmark: object, h2: tuple[str, int]) -> None:
    host, port = h2
    client, do = httpx_get(f"https://{host}:{port}/small.txt", http2=True, verify=False)
    try:
        assert client.get(f"https://{host}:{port}/small.txt").http_version == "HTTP/2"
        assert do() == SMALL
        benchmark(do)
    finally:
        client.close()


@pytest.fixture(scope="module")
def h2c(tree: Path) -> Iterator[tuple[str, int]]:
    config = Config.create(str(tree), host="127.0.0.1", port=0, quiet=True, http2=True)
    with threaded_server(config) as addr:
        yield addr


def test_http2_cleartext_small_file(benchmark: object, h2c: tuple[str, int]) -> None:
    host, port = h2c
    client, do = httpx_get(f"http://{host}:{port}/small.txt", http2=True, http1=False)
    try:
        assert client.get(f"http://{host}:{port}/small.txt").http_version == "HTTP/2"
        assert do() == SMALL
        benchmark(do)
    finally:
        client.close()


@pytest.fixture(scope="module")
def asgi() -> Iterator[tuple[str, int]]:
    config = Config.create(
        ".", host="127.0.0.1", port=0, quiet=True, asgi_app="benchmarks._apps:asgi_app"
    )
    with asgi_server(config) as addr:
        yield addr


def test_asgi_hello(benchmark: object, asgi: tuple[str, int]) -> None:
    conn, do = client_get(*asgi, "/")
    try:
        assert do() == b"ok"
        benchmark(do)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def proxy(tree: Path) -> Iterator[tuple[str, int]]:
    with upstream_server(b"upstream-ok") as up_port:
        config = Config.create(
            str(tree),
            host="127.0.0.1",
            port=0,
            quiet=True,
            proxy=[f"/api=http://127.0.0.1:{up_port}"],
        )
        with threaded_server(config) as addr:
            yield addr


def test_proxy_forward(benchmark: object, proxy: tuple[str, int]) -> None:
    conn, do = client_get(*proxy, "/api/thing")
    try:
        assert do() == b"upstream-ok"
        benchmark(do)
    finally:
        conn.close()
