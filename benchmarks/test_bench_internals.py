"""Micro-benchmarks for the pure-function hot paths (no sockets, so very stable):
HPACK, the HTTP/2 frame codec, directory-listing rendering, Range parsing, the
HTTP/1.1 head builder, and self-signed cert generation.

    uv run --group bench pytest benchmarks/test_bench_internals.py
"""

from __future__ import annotations

from pathlib import Path

from servery import _certgen, _http1, ranges
from servery.http2 import frames, hpack
from servery.http2.frames import Flag

# A representative request header block (what an h2 client sends on stream 1).
_REQ_HEADERS = [
    (b":method", b"GET"),
    (b":path", b"/static/app.js"),
    (b":scheme", b"https"),
    (b":authority", b"localhost:8000"),
    (b"user-agent", b"benchmark/1.0"),
    (b"accept", b"*/*"),
    (b"accept-encoding", b"gzip, deflate, br"),
]
_REQ_BLOCK = hpack.Encoder().encode(_REQ_HEADERS)

_DATA_FRAME = frames.DataFrame(1, Flag(0), b"x" * 1024)
_DATA_WIRE = frames.serialize(_DATA_FRAME)
_DATA_HEAD, _DATA_PAYLOAD = _DATA_WIRE[:9], _DATA_WIRE[9:]

_RESP_HEADERS = [("Content-Type", "text/plain"), ("Content-Length", "1024")]


def test_hpack_encode_request(benchmark: object) -> None:
    benchmark(lambda: hpack.Encoder().encode(_REQ_HEADERS))


def test_hpack_decode_request(benchmark: object) -> None:
    benchmark(lambda: hpack.Decoder().decode(_REQ_BLOCK))


def test_frame_serialize_data(benchmark: object) -> None:
    benchmark(lambda: frames.serialize(_DATA_FRAME))


def test_frame_parse_data(benchmark: object) -> None:
    benchmark(lambda: frames.parse_frame(_DATA_HEAD, _DATA_PAYLOAD))


def test_ranges_parse(benchmark: object) -> None:
    benchmark(lambda: ranges.parse("bytes=0-1023", 1 << 20))


def test_build_head(benchmark: object) -> None:
    benchmark(
        lambda: _http1.build_head(
            version="HTTP/1.1",
            status="200 OK",
            headers=_RESP_HEADERS,
            is_head=False,
            keep_alive=True,
            server="servery",
            date="Sun, 21 Jun 2026 00:00:00 GMT",
            body_len=1024,
        )
    )


def test_listing_render(benchmark: object, tree: Path) -> None:
    # The whole-page render for a ~52-entry directory (facets, metrics, timeline).
    benchmark(lambda: listing_render(tree))


def listing_render(tree: Path) -> bytes:
    from servery import listing

    return listing.render(str(tree), "/", show_hidden=False)


def test_certgen_self_signed(benchmark: object) -> None:
    # RSA-2048 keygen + cert signing — the one-time cost of `--tls-self-signed`.
    benchmark(lambda: _certgen.generate(["localhost", "127.0.0.1"]))
