#!/usr/bin/env python3
"""Fair, repeatable Docker comparison of servery and purpose-built alternatives.

The common baseline is plaintext HTTP/1.1 over Linux host networking. Every server
gets the same CPU set, corpus, client, warmup, trial duration, and response checks.
Static scenarios compare servery/nginx/Caddy; dynamic scenarios compare the same
WSGI or ASGI application under servery and a dedicated app server.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import gzip
import hashlib
import http.client
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.comparison.apps import expected_body  # noqa: E402
from scripts.loadgen import run_load  # noqa: E402

from servery import _static, listing  # noqa: E402

DEFAULT_PYTHON_IMAGE = "python:3.15.0b3-slim"
DEFAULT_NGINX_IMAGE = "nginx:1.29.8-alpine"
DEFAULT_CADDY_IMAGE = "caddy:2.11-alpine"
DEFAULT_COMPARE_IMAGE = "servery-comparison:local"
GUNICORN_VERSION = "26.0.0"
UVICORN_VERSION = "0.51.0"
STARLETTE_VERSION = "1.3.1"
FASTAPI_VERSION = "0.139.0"
UVLOOP_VERSION = "0.22.1"
HTTPTOOLS_VERSION = "0.8.0"
_COMPRESSIBLE_64K = (b"servery compression benchmark payload\n" * 2048)[: 64 * 1024]
_GZIP_64K = gzip.compress(_COMPRESSIBLE_64K, compresslevel=6, mtime=0)
APP_FIXTURE = ROOT / "benchmarks" / "comparison" / "apps.py"
STARLETTE_APP_FIXTURE = ROOT / "benchmarks" / "comparison" / "starlette_apps.py"
FASTAPI_APP_FIXTURE = ROOT / "benchmarks" / "comparison" / "fastapi_apps.py"
ACCESS_LOG_SERVERS = frozenset(
    {
        "servery-access-log",
        "servery-access-log-sync",
        "servery-access-log-drop",
        "servery-access-log-batch64",
        "servery-access-log-drop-batch64",
        "servery-access-log-q8",
        "servery-access-log-q32",
        "servery-access-log-baseline",
        "servery-selector-access-drop",
        "servery-selector-access-wait",
    }
)
SERVER_FAMILIES = {
    "static": frozenset(
        {
            "servery",
            "servery-baseline",
            "servery-selector-spike",
            "servery-selector-prototype",
            "servery-selector-prototype-buffer-64k",
            "servery-selector-prototype-fs4",
            "servery-selector-prototype-slow-inline",
            "servery-selector-prototype-slow-fs4",
            "servery-selector-prototype-slow-fs16",
            "servery-spa",
            "servery-selector-prototype-spa",
            "servery-gzip-cache",
            "servery-gzip-cache-baseline",
            "servery-selector-gzip-cache",
            "servery-gzip-miss",
            "servery-gzip-miss-baseline",
            "servery-selector-gzip-miss",
            "servery-digest-miss",
            "servery-digest-miss-baseline",
            "servery-selector-digest-miss",
            "servery-selector-listing-w1",
            "servery-selector-listing-w4",
            "servery-access-log",
            "servery-access-log-sync",
            "servery-access-log-drop",
            "servery-access-log-batch64",
            "servery-access-log-drop-batch64",
            "servery-access-log-q8",
            "servery-access-log-q32",
            "servery-access-log-baseline",
            "servery-selector-access-drop",
            "servery-selector-access-wait",
            "nginx",
            "caddy",
        }
    ),
    "wsgi": frozenset({"servery-wsgi", "servery-wsgi-baseline", "gunicorn-gthread"}),
    "asgi": frozenset({"servery-asgi", "servery-asgi-baseline", "uvicorn", "uvicorn-native"}),
}


@dataclass(frozen=True, slots=True)
class Scenario:
    """One workload with a byte-identical response across its participating servers."""

    name: str
    family: str
    path: str
    expected_length: int
    expected_sha256: str
    connection_close: bool = False
    concurrency_cap: int | None = None
    read_chunk_size: int | None = None
    read_delay: float = 0.0
    request_headers: tuple[tuple[str, str], ...] = ()
    expected_headers: tuple[tuple[str, str], ...] = ()
    expected_status: int = 200
    request_body_size: int = 0
    servers: tuple[str, ...] | None = None
    expected_fixture: str | None = None
    app_spec: str | None = None
    default: bool = True


@dataclass(frozen=True, slots=True)
class ServerSpec:
    """A containerized server adapter."""

    name: str
    image: str
    command: tuple[str, ...]
    mounts: tuple[tuple[Path, str, bool], ...] = ()
    environment: tuple[tuple[str, str], ...] = ()


def _scenario_allows_server(scenario: Scenario, server: str) -> bool:
    """Apply a capability allowlist while retaining a selected frozen baseline."""
    allowed = scenario.servers
    return (
        allowed is None
        or server in allowed
        or (server.endswith("-baseline") and server.removesuffix("-baseline") in allowed)
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_scenario_expectation(scenario: Scenario, corpus: Path) -> Scenario:
    """Resolve generated response bytes against a fixed corpus before timing."""
    if scenario.expected_fixture is None:
        return scenario
    listing_fixtures = {
        "listing-100": ("listing", "/listing/"),
        "listing-1000": ("listing-1000", "/listing-1000/"),
    }
    fixture = listing_fixtures.get(scenario.expected_fixture)
    if fixture is None:
        raise ValueError(f"unknown expected fixture: {scenario.expected_fixture}")
    directory_name, display = fixture
    body = listing.render(
        str(corpus / directory_name),
        display,
        show_hidden=False,
        per_page=1000,
        max_entries=100_000,
        details_threshold=10_000,
        utc_timestamps=True,
    )
    return replace(
        scenario,
        expected_length=len(body),
        expected_sha256=_sha256(body),
        expected_fixture=None,
    )


def scenarios() -> tuple[Scenario, ...]:
    """Return the declared comparison matrix in stable presentation order."""
    dynamic = expected_body("/bytes/1024")
    sleeping = expected_body("/sleep/10")
    streaming_64k = expected_body("/stream/65536")
    streaming = expected_body("/stream/1048576")
    slow_stream = expected_body("/stream/67108864")
    fastapi_validation = (
        b'{"detail":[{"type":"int_parsing","loc":["path","item_id"],'
        b'"msg":"Input should be a valid integer, unable to parse string as an integer",'
        b'"input":"not-an-int"}]}'
    )
    return (
        Scenario(
            "static-empty",
            "static",
            "/empty.bin",
            0,
            _sha256(b""),
            default=False,
        ),
        Scenario("static-1k", "static", "/small.bin", 1024, _sha256(b"s" * 1024)),
        Scenario(
            "static-access-log-1k",
            "static",
            "/small.bin",
            1024,
            _sha256(b"s" * 1024),
            servers=(
                "servery",
                "servery-baseline",
                "servery-access-log",
                "servery-access-log-sync",
                "servery-access-log-drop",
                "servery-access-log-batch64",
                "servery-access-log-drop-batch64",
                "servery-access-log-q8",
                "servery-access-log-q32",
                "servery-access-log-baseline",
                "servery-selector-prototype",
                "servery-selector-access-drop",
                "servery-selector-access-wait",
            ),
            default=False,
        ),
        Scenario(
            "static-download-1k",
            "static",
            "/small.bin?download=1",
            1024,
            _sha256(b"s" * 1024),
            expected_headers=(
                (
                    "Content-Disposition",
                    "attachment; filename=\"small.bin\"; filename*=UTF-8''small.bin",
                ),
            ),
            servers=(
                "servery",
                "servery-baseline",
                "servery-selector-prototype",
            ),
            default=False,
        ),
        Scenario(
            "static-4k",
            "static",
            "/small-4k.bin",
            4 * 1024,
            _sha256(b"f" * (4 * 1024)),
            default=False,
        ),
        Scenario(
            "static-16k",
            "static",
            "/small-16k.bin",
            16 * 1024,
            _sha256(b"a" * (16 * 1024)),
            default=False,
        ),
        Scenario(
            "static-64k",
            "static",
            "/small-64k.bin",
            64 * 1024,
            _sha256(b"b" * (64 * 1024)),
            default=False,
        ),
        Scenario(
            "static-gzip-cache-64k",
            "static",
            "/compressible-64k.txt",
            len(_GZIP_64K),
            _sha256(_GZIP_64K),
            request_headers=(("Accept-Encoding", "gzip"),),
            expected_headers=(
                ("Content-Encoding", "gzip"),
                ("Vary", "Accept-Encoding"),
            ),
            servers=(
                "servery-gzip-cache",
                "servery-gzip-cache-baseline",
                "servery-selector-gzip-cache",
            ),
            default=False,
        ),
        Scenario(
            "static-gzip-miss-64k",
            "static",
            "/compressible-64k.txt",
            len(_GZIP_64K),
            _sha256(_GZIP_64K),
            request_headers=(("Accept-Encoding", "gzip"),),
            expected_headers=(
                ("Content-Encoding", "gzip"),
                ("Vary", "Accept-Encoding"),
            ),
            servers=(
                "servery-gzip-miss",
                "servery-gzip-miss-baseline",
                "servery-selector-gzip-miss",
            ),
            default=False,
        ),
        Scenario(
            "static-digest-miss-64k",
            "static",
            "/small-64k.bin",
            64 * 1024,
            _sha256(b"b" * (64 * 1024)),
            request_headers=(("Want-Repr-Digest", "sha-256"),),
            expected_headers=(
                (
                    "Repr-Digest",
                    "sha-256=:"
                    + base64.b64encode(hashlib.sha256(b"b" * (64 * 1024)).digest()).decode()
                    + ":",
                ),
            ),
            servers=(
                "servery-digest-miss",
                "servery-digest-miss-baseline",
                "servery-selector-digest-miss",
            ),
            default=False,
        ),
        Scenario(
            "static-range-64k",
            "static",
            "/small-64k.bin",
            1024,
            _sha256(b"b" * 1024),
            request_headers=(("Range", "bytes=0-1023"),),
            expected_status=206,
            default=False,
        ),
        Scenario(
            "static-not-modified",
            "static",
            "/small.bin",
            0,
            _sha256(b""),
            request_headers=(("If-Modified-Since", "Wed, 21 Oct 2099 07:28:00 GMT"),),
            expected_status=304,
            default=False,
        ),
        Scenario(
            "static-index-1k",
            "static",
            "/indexed/",
            1024,
            _sha256(b"i" * 1024),
            default=False,
        ),
        Scenario(
            "static-listing-100",
            "static",
            "/listing/",
            0,
            _sha256(b""),
            expected_headers=(
                ("Vary", "Accept-Encoding"),
                ("Content-Security-Policy", _static.GENERATED_CSP),
                ("Referrer-Policy", "no-referrer"),
            ),
            servers=(
                "servery",
                "servery-baseline",
                "servery-selector-listing-w1",
                "servery-selector-listing-w4",
            ),
            expected_fixture="listing-100",
            default=False,
        ),
        Scenario(
            "static-listing-1000",
            "static",
            "/listing-1000/",
            0,
            _sha256(b""),
            concurrency_cap=16,
            expected_headers=(
                ("Vary", "Accept-Encoding"),
                ("Content-Security-Policy", _static.GENERATED_CSP),
                ("Referrer-Policy", "no-referrer"),
            ),
            servers=(
                "servery",
                "servery-baseline",
                "servery-selector-listing-w1",
                "servery-selector-listing-w4",
            ),
            expected_fixture="listing-1000",
            default=False,
        ),
        Scenario(
            "static-spa-1k",
            "static",
            "/client/side/route",
            1024,
            _sha256(b"p" * 1024),
            servers=("servery-spa", "servery-selector-prototype-spa"),
            default=False,
        ),
        Scenario(
            "static-1m",
            "static",
            "/medium-1m.bin",
            1024 * 1024,
            _sha256(b"m" * (1024 * 1024)),
            concurrency_cap=32,
            default=False,
        ),
        Scenario(
            "static-8m",
            "static",
            "/large.bin",
            8 * 1024 * 1024,
            _sha256(b"L" * (8 * 1024 * 1024)),
            concurrency_cap=16,
        ),
        Scenario(
            "static-churn-1k",
            "static",
            "/small.bin",
            1024,
            _sha256(b"s" * 1024),
            connection_close=True,
            concurrency_cap=32,
        ),
        Scenario("wsgi-1k", "wsgi", "/bytes/1024", len(dynamic), _sha256(dynamic)),
        Scenario(
            "wsgi-body-64k",
            "wsgi",
            "/body/65536",
            len(dynamic),
            _sha256(dynamic),
            request_body_size=64 * 1024,
            concurrency_cap=32,
            default=False,
        ),
        Scenario("wsgi-wait-10ms", "wsgi", "/sleep/10", len(sleeping), _sha256(sleeping)),
        Scenario(
            "wsgi-stream-64k",
            "wsgi",
            "/stream/65536",
            len(streaming_64k),
            _sha256(streaming_64k),
            concurrency_cap=32,
            default=False,
        ),
        Scenario(
            "wsgi-stream-1m",
            "wsgi",
            "/stream/1048576",
            len(streaming),
            _sha256(streaming),
            concurrency_cap=16,
            default=False,
        ),
        Scenario("asgi-1k", "asgi", "/bytes/1024", len(dynamic), _sha256(dynamic)),
        Scenario(
            "asgi-body-64k",
            "asgi",
            "/body/65536",
            len(dynamic),
            _sha256(dynamic),
            request_body_size=64 * 1024,
            concurrency_cap=32,
            default=False,
        ),
        Scenario(
            "asgi-churn-1k",
            "asgi",
            "/bytes/1024",
            len(dynamic),
            _sha256(dynamic),
            connection_close=True,
            concurrency_cap=32,
            default=False,
        ),
        Scenario(
            "asgi-headers-32",
            "asgi",
            "/bytes/1024",
            len(dynamic),
            _sha256(dynamic),
            request_headers=tuple((f"X-Benchmark-{index}", "value") for index in range(30)),
            default=False,
        ),
        Scenario("asgi-wait-10ms", "asgi", "/sleep/10", len(sleeping), _sha256(sleeping)),
        Scenario(
            "asgi-stream-64k",
            "asgi",
            "/stream/65536",
            len(streaming_64k),
            _sha256(streaming_64k),
            concurrency_cap=32,
            default=False,
        ),
        Scenario(
            "asgi-stream-1m",
            "asgi",
            "/stream/1048576",
            len(streaming),
            _sha256(streaming),
            concurrency_cap=16,
            default=False,
        ),
        Scenario(
            "asgi-slow-reader-64m",
            "asgi",
            "/stream/67108864",
            len(slow_stream),
            _sha256(slow_stream),
            concurrency_cap=4,
            read_chunk_size=16 * 1024,
            read_delay=0.001,
            default=False,
        ),
        Scenario(
            "asgi-starlette-json",
            "asgi",
            "/starlette/json?q=benchmark",
            len(b'{"framework":"starlette","path":"/starlette/json","q":"benchmark"}'),
            _sha256(b'{"framework":"starlette","path":"/starlette/json","q":"benchmark"}'),
            servers=("servery-asgi", "uvicorn", "uvicorn-native"),
            app_spec="benchmarks.comparison.starlette_apps:app",
            default=False,
        ),
        Scenario(
            "asgi-starlette-stream-64k",
            "asgi",
            "/starlette/stream",
            64 * 1024,
            _sha256((b"framework-stream\n" * 256)[:4096] * 16),
            concurrency_cap=32,
            servers=("servery-asgi", "uvicorn", "uvicorn-native"),
            app_spec="benchmarks.comparison.starlette_apps:app",
            default=False,
        ),
        Scenario(
            "asgi-fastapi-json",
            "asgi",
            "/fastapi/items/42?q=benchmark",
            len(b'{"framework":"fastapi","item_id":42,"q":"benchmark"}'),
            _sha256(b'{"framework":"fastapi","item_id":42,"q":"benchmark"}'),
            servers=("servery-asgi", "uvicorn", "uvicorn-native"),
            app_spec="benchmarks.comparison.fastapi_apps:app",
            default=False,
        ),
        Scenario(
            "asgi-fastapi-validation",
            "asgi",
            "/fastapi/items/not-an-int?q=benchmark",
            len(fastapi_validation),
            _sha256(fastapi_validation),
            servers=("servery-asgi", "uvicorn", "uvicorn-native"),
            expected_status=422,
            app_spec="benchmarks.comparison.fastapi_apps:app",
            default=False,
        ),
    )


def parse_cpu_set(value: str) -> set[int]:
    """Parse Linux cpuset syntax such as ``0,2-4``."""
    cpus: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            raise ValueError("empty CPU-set component")
        first, dash, last = item.partition("-")
        if not first.isdigit() or (dash and not last.isdigit()):
            raise ValueError(f"invalid CPU-set component: {item!r}")
        start, stop = int(first), int(last) if dash else int(first)
        if stop < start:
            raise ValueError(f"descending CPU range: {item!r}")
        cpus.update(range(start, stop + 1))
    if not cpus:
        raise ValueError("CPU set cannot be empty")
    return cpus


def format_cpu_set(cpus: set[int]) -> str:
    """Return a simple Docker-compatible comma-separated CPU set."""
    return ",".join(str(cpu) for cpu in sorted(cpus))


def _size_label(value: int) -> str:
    for divisor, suffix in ((1024 * 1024, "m"), (1024, "k")):
        if value and value % divisor == 0:
            return f"{value // divisor}{suffix}"
    return str(value)


def _available_cpus() -> set[int]:
    try:
        return set(os.sched_getaffinity(0))
    except AttributeError:  # pragma: no cover - comparison Docker mode is Linux-only
        return set(range(os.cpu_count() or 1))


def select_cpu_sets(
    server_value: str | None, client_value: str | None
) -> tuple[set[int], set[int]]:
    """Choose non-overlapping server/client CPU sets, or validate explicit choices."""
    available = _available_cpus()
    server = parse_cpu_set(server_value) if server_value else {min(available)}
    if not server <= available:
        raise ValueError(f"server CPUs {sorted(server - available)} are unavailable")
    remaining = available - server
    if client_value:
        client = parse_cpu_set(client_value)
    elif remaining:
        client = set(sorted(remaining)[: min(4, len(remaining))])
    else:
        client = set(server)
    if not client <= available:
        raise ValueError(f"client CPUs {sorted(client - available)} are unavailable")
    return server, client


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=True)


def _docker_image_id(image: str) -> str:
    return _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"]).stdout.strip()


def _docker_version(image: str, command: list[str]) -> str:
    result = _run(["docker", "run", "--rm", image, *command], check=False)
    output = (result.stdout + result.stderr).strip()
    if result.returncode or not output:
        raise RuntimeError(f"could not identify {image}: {output or 'no version output'}")
    return output.splitlines()[-1]


def _docker_optional_version(image: str, command: list[str]) -> str:
    """Identify an opt-in benchmark dependency without breaking older images."""
    result = _run(["docker", "run", "--rm", image, *command], check=False)
    output = (result.stdout + result.stderr).strip()
    return output.splitlines()[-1] if result.returncode == 0 and output else "not installed"


def _python_runtime() -> dict[str, object]:
    """Return structured interpreter/GIL identity for reproducible comparisons."""
    gil_probe = getattr(sys, "_is_gil_enabled", None)
    return {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "gil_api_available": gil_probe is not None,
        "gil_enabled": bool(gil_probe()) if gil_probe is not None else True,
    }


def _docker_python_runtime(image: str) -> dict[str, object]:
    script = (
        "import json,platform,sys; "
        "probe=getattr(sys,'_is_gil_enabled',None); "
        "print(json.dumps({'implementation':platform.python_implementation(),"
        "'version':platform.python_version(),'gil_api_available':probe is not None,"
        "'gil_enabled':bool(probe()) if probe is not None else True}))"
    )
    result = _run(["docker", "run", "--rm", image, "python", "-c", script], check=False)
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"could not inspect Python runtime in {image}: {detail}")
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid Python runtime metadata from {image}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("gil_enabled"), bool):
        raise TypeError(f"incomplete Python runtime metadata from {image}")
    return cast(dict[str, object], value)


def _source_identity() -> dict[str, object]:
    revision = _run(["git", "rev-parse", "HEAD"], check=False)
    status = _run(["git", "status", "--porcelain", "--untracked-files=all"], check=False)
    evidence_files = (
        "scripts/compare_servers.py",
        "scripts/loadgen.py",
        "benchmarks/comparison/Dockerfile",
        "benchmarks/comparison/apps.py",
        "benchmarks/comparison/starlette_apps.py",
        "benchmarks/comparison/fastapi_apps.py",
    )
    product_files = [ROOT / "pyproject.toml", *sorted((ROOT / "src" / "servery").rglob("*.py"))]
    product_digest = hashlib.sha256()
    for path in product_files:
        relative = path.relative_to(ROOT).as_posix()
        product_digest.update(relative.encode())
        product_digest.update(b"\0")
        product_digest.update(path.read_bytes())
        product_digest.update(b"\0")
    return {
        "git_commit": revision.stdout.strip() if revision.returncode == 0 else None,
        "git_dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
        "product_tree_sha256": product_digest.hexdigest(),
        "file_sha256": {
            relative: _sha256((ROOT / relative).read_bytes()) for relative in evidence_files
        },
    }


def prepare_images(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    """Pull external images, build the common Python image, and capture exact identities."""
    if not args.no_pull:
        for image in (args.python_image, args.nginx_image, args.caddy_image):
            subprocess.run(["docker", "pull", image], check=True)
    if not args.no_build:
        fastapi_version = FASTAPI_VERSION if getattr(args, "include_fastapi", False) else ""
        include_native = getattr(args, "include_uvicorn_native", False)
        uvloop_version = UVLOOP_VERSION if include_native else ""
        httptools_version = HTTPTOOLS_VERSION if include_native else ""
        subprocess.run(
            [
                "docker",
                "build",
                "--file",
                str(ROOT / "benchmarks" / "comparison" / "Dockerfile"),
                "--tag",
                args.compare_image,
                "--build-arg",
                f"PYTHON_IMAGE={args.python_image}",
                "--build-arg",
                f"GUNICORN_VERSION={GUNICORN_VERSION}",
                "--build-arg",
                f"UVICORN_VERSION={UVICORN_VERSION}",
                "--build-arg",
                f"STARLETTE_VERSION={STARLETTE_VERSION}",
                "--build-arg",
                f"FASTAPI_VERSION={fastapi_version}",
                "--build-arg",
                f"UVLOOP_VERSION={uvloop_version}",
                "--build-arg",
                f"HTTPTOOLS_VERSION={httptools_version}",
                str(ROOT),
            ],
            check=True,
        )
    candidate_runtime = _docker_python_runtime(args.compare_image)
    images: dict[str, dict[str, Any]] = {
        "servery-python": {
            "reference": args.compare_image,
            "image_id": _docker_image_id(args.compare_image),
            "version": _docker_version(
                args.compare_image,
                [
                    "python",
                    "-c",
                    "import platform,servery,sys; "
                    "gil=getattr(sys,'_is_gil_enabled',lambda:True)(); "
                    "print(f'Python {platform.python_version()}; gil={gil}; '"
                    "f'servery={servery.__version__}')",
                ],
            ),
            "gunicorn": GUNICORN_VERSION,
            "uvicorn": UVICORN_VERSION,
            "starlette": _docker_version(
                args.compare_image,
                ["python", "-c", "import starlette; print(starlette.__version__)"],
            ),
            "fastapi": _docker_optional_version(
                args.compare_image,
                [
                    "python",
                    "-c",
                    "import fastapi,pydantic; "
                    "print(f'{fastapi.__version__}; pydantic={pydantic.__version__}')",
                ],
            ),
            "uvloop": _docker_optional_version(
                args.compare_image,
                ["python", "-c", "import uvloop; print(uvloop.__version__)"],
            ),
            "httptools": _docker_optional_version(
                args.compare_image,
                ["python", "-c", "import httptools; print(httptools.__version__)"],
            ),
            "python_runtime": candidate_runtime,
        },
        "nginx": {
            "reference": args.nginx_image,
            "image_id": _docker_image_id(args.nginx_image),
            "version": _docker_version(args.nginx_image, ["nginx", "-v"]),
        },
        "caddy": {
            "reference": args.caddy_image,
            "image_id": _docker_image_id(args.caddy_image),
            "version": _docker_version(args.caddy_image, ["caddy", "version"]),
        },
    }
    if args.servery_baseline_image:
        baseline_runtime = _docker_python_runtime(args.servery_baseline_image)
        images["servery-baseline"] = {
            "reference": args.servery_baseline_image,
            "image_id": _docker_image_id(args.servery_baseline_image),
            "version": _docker_version(
                args.servery_baseline_image,
                [
                    "python",
                    "-c",
                    "import platform,servery,sys; "
                    "gil=getattr(sys,'_is_gil_enabled',lambda:True)(); "
                    "print(f'Python {platform.python_version()}; gil={gil}; '"
                    "f'servery={servery.__version__}')",
                ],
            ),
            "python_runtime": baseline_runtime,
        }
    return images


def nginx_config(port: int, workers: int) -> str:
    """Build the intentionally small common-baseline nginx configuration."""
    return f"""worker_processes {workers};
error_log /dev/stderr warn;
pid /tmp/nginx.pid;
events {{ worker_connections 4096; }}
http {{
    access_log off;
    sendfile on;
    keepalive_timeout 30;
    server {{
        listen 127.0.0.1:{port};
        root /srv;
        location / {{ try_files $uri =404; }}
    }}
}}
"""


def caddy_config(port: int) -> str:
    """Build a Caddy static configuration without HTTPS or response encoding."""
    return f"""{{
    admin off
    auto_https off
}}
http://127.0.0.1:{port} {{
    root * /srv
    file_server
}}
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def server_specs(
    scenario: Scenario,
    *,
    port: int,
    concurrency: int,
    corpus: Path,
    config_dir: Path,
    args: argparse.Namespace,
) -> tuple[ServerSpec, ...]:
    """Return adapters participating in ``scenario``."""
    common = (
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--quiet",
        "--max-connections",
        str(max(4096, concurrency * 2)),
    )
    if args.servery_max_workers is not None:
        common += ("--max-workers", str(args.servery_max_workers))
    # ``app_workers`` is the process-count control shared by the production
    # server adapters.  Keep it distinct from Servery's ``--max-workers``,
    # which bounds reusable worker *threads* inside each process.  Frozen
    # baseline images may predate the process supervisor, so only the current
    # candidate receives this option.
    candidate_common = (*common, "--workers", str(args.app_workers))
    servery_write_timeout = getattr(args, "servery_write_timeout", None)
    if servery_write_timeout is not None:
        candidate_common += ("--write-timeout", str(servery_write_timeout))
    servery_request_body_timeout = getattr(args, "servery_request_body_timeout", None)
    if servery_request_body_timeout is not None:
        candidate_common += ("--request-body-timeout", str(servery_request_body_timeout))
    servery_request_head_timeout = getattr(args, "servery_request_head_timeout", None)
    if servery_request_head_timeout is not None:
        candidate_common += ("--request-head-timeout", str(servery_request_head_timeout))
    if scenario.family == "static":
        selected_static_servers = set(getattr(args, "server", None) or scenario.servers or ())
        nginx_path = config_dir / "nginx.conf"
        nginx_path.write_text(nginx_config(port, args.app_workers))
        caddy_path = config_dir / "Caddyfile"
        caddy_path.write_text(caddy_config(port))
        if args.servery_small_file_buffer:
            servery_specs = tuple(
                ServerSpec(
                    "servery-sendfile" if value == 0 else f"servery-buffer-{_size_label(value)}",
                    args.compare_image,
                    (
                        "python",
                        "-m",
                        "servery",
                        "/srv",
                        *candidate_common,
                        "--small-file-buffer-size",
                        str(value),
                    ),
                    ((corpus, "/srv", True),),
                )
                for value in args.servery_small_file_buffer
            )
        else:
            servery_specs = (
                ServerSpec(
                    "servery",
                    args.compare_image,
                    ("python", "-m", "servery", "/srv", *candidate_common),
                    ((corpus, "/srv", True),),
                ),
            )
        if getattr(args, "servery_baseline_image", None):
            servery_specs += (
                ServerSpec(
                    "servery-baseline",
                    args.servery_baseline_image,
                    ("python", "-m", "servery", "/srv", *common),
                    ((corpus, "/srv", True),),
                ),
            )
        if (
            getattr(args, "include_selector_spike", False)
            or "servery-selector-spike" in selected_static_servers
        ):
            servery_specs += (
                ServerSpec(
                    "servery-selector-spike",
                    args.compare_image,
                    (
                        "python",
                        "-m",
                        "benchmarks.comparison.selector_spike",
                        "/srv",
                        "--bind",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ),
                    ((corpus, "/srv", True),),
                ),
            )
        if "servery-selector-prototype" in selected_static_servers:
            servery_specs += (
                ServerSpec(
                    "servery-selector-prototype",
                    args.compare_image,
                    (
                        "python",
                        "-m",
                        "benchmarks.comparison.selector_prototype",
                        "/srv",
                        "--bind",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--max-connections",
                        str(max(4096, concurrency * 2)),
                    ),
                    ((corpus, "/srv", True),),
                ),
            )
        if "servery-selector-prototype-buffer-64k" in selected_static_servers:
            servery_specs += (
                ServerSpec(
                    "servery-selector-prototype-buffer-64k",
                    args.compare_image,
                    (
                        "python",
                        "-m",
                        "benchmarks.comparison.selector_prototype",
                        "/srv",
                        "--bind",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--max-connections",
                        str(max(4096, concurrency * 2)),
                        "--small-file-buffer-size",
                        str(64 * 1024),
                    ),
                    ((corpus, "/srv", True),),
                ),
            )
        prototype_variants = {
            "servery-selector-prototype-fs4": (
                "--filesystem-workers",
                "4",
                "--filesystem-queue",
                "64",
            ),
            "servery-selector-prototype-slow-inline": ("--filesystem-delay-ms", "10"),
            "servery-selector-prototype-slow-fs4": (
                "--filesystem-workers",
                "4",
                "--filesystem-queue",
                "64",
                "--filesystem-delay-ms",
                "10",
            ),
            "servery-selector-prototype-slow-fs16": (
                "--filesystem-workers",
                "16",
                "--filesystem-queue",
                "64",
                "--filesystem-delay-ms",
                "10",
            ),
        }
        selected_prototypes = selected_static_servers & prototype_variants.keys()
        servery_specs += tuple(
            ServerSpec(
                name,
                args.compare_image,
                (
                    "python",
                    "-m",
                    "benchmarks.comparison.selector_prototype",
                    "/srv",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--max-connections",
                    str(max(4096, concurrency * 2)),
                    *prototype_variants[name],
                ),
                ((corpus, "/srv", True),),
            )
            for name in sorted(selected_prototypes)
        )
        listing_variants = {
            "servery-selector-listing-w1": "1",
            "servery-selector-listing-w4": "4",
        }
        servery_specs += tuple(
            ServerSpec(
                name,
                args.compare_image,
                (
                    "python",
                    "-m",
                    "benchmarks.comparison.selector_prototype",
                    "/srv",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--max-connections",
                    str(max(4096, concurrency * 2)),
                    "--listing-workers",
                    workers,
                    "--listing-queue",
                    "64",
                    "--max-listing-entries",
                    "100000",
                    "--listing-page-size",
                    "1000",
                    "--listing-details-threshold",
                    "10000",
                ),
                ((corpus, "/srv", True),),
            )
            for name, workers in listing_variants.items()
            if name in selected_static_servers
        )
        if "servery-spa" in selected_static_servers:
            servery_specs += (
                ServerSpec(
                    "servery-spa",
                    args.compare_image,
                    ("python", "-m", "servery", "/srv", *candidate_common, "--spa"),
                    ((corpus, "/srv", True),),
                ),
            )
        if "servery-selector-prototype-spa" in selected_static_servers:
            servery_specs += (
                ServerSpec(
                    "servery-selector-prototype-spa",
                    args.compare_image,
                    (
                        "python",
                        "-m",
                        "benchmarks.comparison.selector_prototype",
                        "/srv",
                        "--bind",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--max-connections",
                        str(max(4096, concurrency * 2)),
                        "--spa",
                    ),
                    ((corpus, "/srv", True),),
                ),
            )
        gzip_variants = {
            "servery-gzip-cache": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "servery",
                    "/srv",
                    *candidate_common,
                    "--compression-cache-size",
                    str(32 * 1024 * 1024),
                ),
            ),
            "servery-selector-gzip-cache": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "benchmarks.comparison.selector_prototype",
                    "/srv",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--max-connections",
                    str(max(4096, concurrency * 2)),
                    "--compress",
                    "--compression-workers",
                    "1",
                    "--compression-queue",
                    "64",
                    "--compression-cache-size",
                    str(32 * 1024 * 1024),
                ),
            ),
            "servery-gzip-miss": (
                args.compare_image,
                ("python", "-m", "servery", "/srv", *candidate_common),
            ),
            "servery-selector-gzip-miss": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "benchmarks.comparison.selector_prototype",
                    "/srv",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--max-connections",
                    str(max(4096, concurrency * 2)),
                    "--compress",
                    "--compression-workers",
                    "1",
                    "--compression-queue",
                    "64",
                ),
            ),
        }
        servery_specs += tuple(
            ServerSpec(name, image, command, ((corpus, "/srv", True),))
            for name, (image, command) in gzip_variants.items()
            if name in selected_static_servers
        )
        if "servery-gzip-cache-baseline" in selected_static_servers and getattr(
            args, "servery_baseline_image", None
        ):
            servery_specs += (
                ServerSpec(
                    "servery-gzip-cache-baseline",
                    args.servery_baseline_image,
                    (
                        "python",
                        "-m",
                        "servery",
                        "/srv",
                        *common,
                        "--compression-cache-size",
                        str(32 * 1024 * 1024),
                    ),
                    ((corpus, "/srv", True),),
                ),
            )
        if "servery-gzip-miss-baseline" in selected_static_servers and getattr(
            args, "servery_baseline_image", None
        ):
            servery_specs += (
                ServerSpec(
                    "servery-gzip-miss-baseline",
                    args.servery_baseline_image,
                    ("python", "-m", "servery", "/srv", *common),
                    ((corpus, "/srv", True),),
                ),
            )
        digest_variants = {
            "servery-digest-miss": (
                args.compare_image,
                ("python", "-m", "servery", "/srv", *candidate_common),
            ),
            "servery-selector-digest-miss": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "benchmarks.comparison.selector_prototype",
                    "/srv",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--max-connections",
                    str(max(4096, concurrency * 2)),
                    "--digest-workers",
                    "4",
                    "--digest-queue",
                    "64",
                ),
            ),
        }
        servery_specs += tuple(
            ServerSpec(name, image, command, ((corpus, "/srv", True),))
            for name, (image, command) in digest_variants.items()
            if name in selected_static_servers
        )
        if "servery-digest-miss-baseline" in selected_static_servers and getattr(
            args, "servery_baseline_image", None
        ):
            servery_specs += (
                ServerSpec(
                    "servery-digest-miss-baseline",
                    args.servery_baseline_image,
                    ("python", "-m", "servery", "/srv", *common),
                    ((corpus, "/srv", True),),
                ),
            )
        access_variants = {
            "servery-access-log": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "servery",
                    "/srv",
                    *candidate_common,
                    "--access-log",
                    "/tmp/servery-access.log",
                    "--access-log-queue",
                    "256",
                ),
            ),
            "servery-access-log-sync": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "servery",
                    "/srv",
                    *candidate_common,
                    "--access-log",
                    "/tmp/servery-access.log",
                    "--access-log-queue",
                    "0",
                ),
            ),
            "servery-access-log-drop": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "servery",
                    "/srv",
                    *candidate_common,
                    "--access-log",
                    "/tmp/servery-access.log",
                    "--access-log-queue",
                    "256",
                    "--access-log-overflow",
                    "drop",
                ),
            ),
            "servery-access-log-batch64": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "servery",
                    "/srv",
                    *candidate_common,
                    "--access-log",
                    "/tmp/servery-access.log",
                    "--access-log-queue",
                    "256",
                    "--access-log-batch-size",
                    "64",
                    "--access-log-batch-wait",
                    "0",
                ),
            ),
            "servery-access-log-drop-batch64": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "servery",
                    "/srv",
                    *candidate_common,
                    "--access-log",
                    "/tmp/servery-access.log",
                    "--access-log-queue",
                    "256",
                    "--access-log-overflow",
                    "drop",
                    "--access-log-batch-size",
                    "64",
                    "--access-log-batch-wait",
                    "0",
                ),
            ),
            "servery-access-log-q8": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "servery",
                    "/srv",
                    *candidate_common,
                    "--access-log",
                    "/tmp/servery-access.log",
                    "--access-log-queue",
                    "8",
                    "--access-log-batch-size",
                    "8",
                    "--access-log-batch-wait",
                    "0",
                ),
            ),
            "servery-access-log-q32": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "servery",
                    "/srv",
                    *candidate_common,
                    "--access-log",
                    "/tmp/servery-access.log",
                    "--access-log-queue",
                    "32",
                    "--access-log-batch-size",
                    "32",
                    "--access-log-batch-wait",
                    "0",
                ),
            ),
            "servery-selector-access-drop": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "benchmarks.comparison.selector_prototype",
                    "/srv",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--max-connections",
                    str(max(4096, concurrency * 2)),
                    "--access-log",
                    "/tmp/servery-access.log",
                    "--access-log-queue",
                    "256",
                    "--access-log-overflow",
                    "drop",
                    "--access-log-batch-size",
                    "8",
                    "--access-log-batch-wait-ms",
                    "1",
                ),
            ),
            "servery-selector-access-wait": (
                args.compare_image,
                (
                    "python",
                    "-m",
                    "benchmarks.comparison.selector_prototype",
                    "/srv",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--max-connections",
                    str(max(4096, concurrency * 2)),
                    "--access-log",
                    "/tmp/servery-access.log",
                    "--access-log-queue",
                    "256",
                    "--access-log-overflow",
                    "wait",
                    "--access-log-batch-size",
                    "8",
                    "--access-log-batch-wait-ms",
                    "1",
                ),
            ),
        }
        servery_specs += tuple(
            ServerSpec(name, image, command, ((corpus, "/srv", True),))
            for name, (image, command) in access_variants.items()
            if name in selected_static_servers
        )
        if "servery-access-log-baseline" in selected_static_servers and getattr(
            args, "servery_baseline_image", None
        ):
            servery_specs += (
                ServerSpec(
                    "servery-access-log-baseline",
                    args.servery_baseline_image,
                    (
                        "python",
                        "-m",
                        "servery",
                        "/srv",
                        *common,
                        "--access-log",
                        "/tmp/servery-access.log",
                    ),
                    ((corpus, "/srv", True),),
                ),
            )
        return (
            *servery_specs,
            ServerSpec(
                "nginx",
                args.nginx_image,
                ("nginx", "-c", "/etc/nginx/nginx.conf", "-g", "daemon off;"),
                ((corpus, "/srv", True), (nginx_path, "/etc/nginx/nginx.conf", True)),
            ),
            ServerSpec(
                "caddy",
                args.caddy_image,
                ("caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"),
                ((corpus, "/srv", True), (caddy_path, "/etc/caddy/Caddyfile", True)),
                (("GOMAXPROCS", str(args.app_workers)),),
            ),
        )
    if scenario.family == "wsgi":
        threads = max(1, (concurrency + args.app_workers - 1) // args.app_workers)
        specs = (
            ServerSpec(
                "servery-wsgi",
                args.compare_image,
                (
                    "python",
                    "-m",
                    "servery",
                    "--wsgi",
                    "benchmarks.comparison.apps:wsgi_app",
                    *candidate_common,
                ),
                ((APP_FIXTURE, "/opt/servery/benchmarks/comparison/apps.py", True),),
            ),
        )
        if getattr(args, "servery_baseline_image", None):
            specs += (
                ServerSpec(
                    "servery-wsgi-baseline",
                    args.servery_baseline_image,
                    (
                        "python",
                        "-m",
                        "servery",
                        "--wsgi",
                        "benchmarks.comparison.apps:wsgi_app",
                        *common,
                    ),
                    ((APP_FIXTURE, "/opt/servery/benchmarks/comparison/apps.py", True),),
                ),
            )
        return (
            *specs,
            ServerSpec(
                "gunicorn-gthread",
                args.compare_image,
                (
                    "gunicorn",
                    "benchmarks.comparison.apps:wsgi_app",
                    "--bind",
                    f"127.0.0.1:{port}",
                    "--workers",
                    str(args.app_workers),
                    "--worker-class",
                    "gthread",
                    "--threads",
                    str(threads),
                    "--keep-alive",
                    "30",
                    "--access-logfile",
                    "/dev/null",
                    "--error-logfile",
                    "-",
                    "--log-level",
                    "warning",
                ),
                ((APP_FIXTURE, "/opt/servery/benchmarks/comparison/apps.py", True),),
            ),
        )
    if scenario.family == "asgi":
        app_spec = scenario.app_spec or "benchmarks.comparison.apps:asgi_app"
        asgi_candidate_common = candidate_common
        servery_lifespan = getattr(args, "servery_lifespan", None)
        if servery_lifespan is not None:
            asgi_candidate_common += ("--lifespan", servery_lifespan)
        servery_lifespan_timeout = getattr(args, "servery_lifespan_timeout", None)
        if servery_lifespan_timeout is not None:
            asgi_candidate_common += (
                "--lifespan-timeout",
                str(servery_lifespan_timeout),
            )
        if scenario.app_spec and ".starlette_apps:" in scenario.app_spec:
            app_fixture = STARLETTE_APP_FIXTURE
        elif scenario.app_spec and ".fastapi_apps:" in scenario.app_spec:
            app_fixture = FASTAPI_APP_FIXTURE
        else:
            app_fixture = APP_FIXTURE
        specs = (
            ServerSpec(
                "servery-asgi",
                args.compare_image,
                (
                    "python",
                    "-m",
                    "servery",
                    "--asgi",
                    app_spec,
                    *asgi_candidate_common,
                ),
                ((app_fixture, f"/opt/servery/benchmarks/comparison/{app_fixture.name}", True),),
            ),
        )
        if getattr(args, "servery_baseline_image", None):
            specs += (
                ServerSpec(
                    "servery-asgi-baseline",
                    args.servery_baseline_image,
                    (
                        "python",
                        "-m",
                        "servery",
                        "--asgi",
                        app_spec,
                        *common,
                    ),
                    (
                        (
                            app_fixture,
                            f"/opt/servery/benchmarks/comparison/{app_fixture.name}",
                            True,
                        ),
                    ),
                ),
            )
        adapters = (
            *specs,
            ServerSpec(
                "uvicorn",
                args.compare_image,
                (
                    "python",
                    "-m",
                    "uvicorn",
                    app_spec,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--workers",
                    str(args.app_workers),
                    "--limit-concurrency",
                    str(max(4096, concurrency * 2)),
                    "--timeout-keep-alive",
                    "30",
                    "--loop",
                    "asyncio",
                    "--http",
                    "h11",
                    "--no-access-log",
                    "--log-level",
                    "warning",
                ),
                ((app_fixture, f"/opt/servery/benchmarks/comparison/{app_fixture.name}", True),),
            ),
        )
        if getattr(args, "include_uvicorn_native", False):
            adapters += (
                ServerSpec(
                    "uvicorn-native",
                    args.compare_image,
                    (
                        "python",
                        "-m",
                        "uvicorn",
                        app_spec,
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--workers",
                        str(args.app_workers),
                        "--limit-concurrency",
                        str(max(4096, concurrency * 2)),
                        "--timeout-keep-alive",
                        "30",
                        "--loop",
                        "uvloop",
                        "--http",
                        "httptools",
                        "--no-access-log",
                        "--log-level",
                        "warning",
                    ),
                    (
                        (
                            app_fixture,
                            f"/opt/servery/benchmarks/comparison/{app_fixture.name}",
                            True,
                        ),
                    ),
                ),
            )
        return adapters
    raise ValueError(f"unknown scenario family: {scenario.family}")


def _proc_stat(path: Path) -> tuple[str, int, int] | None:
    """Read process name, parent PID, and CPU ticks from one Linux stat file."""
    try:
        value = path.read_text()
        close = value.rindex(")")
        name = value[value.index("(") + 1 : close]
        fields = value[close + 2 :].split()
        return name, int(fields[1]), int(fields[11]) + int(fields[12])
    except (OSError, ValueError, IndexError):
        return None


def _proc_kib(path: Path, field: str) -> int | None:
    try:
        for line in path.read_text().splitlines():
            key, separator, value = line.partition(":")
            if separator and key == field:
                return int(value.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return None


def process_tree_snapshot(root_pid: int, *, proc_root: Path = Path("/proc")) -> dict[str, object]:
    """Snapshot a Linux process tree using host procfs, without in-container helpers."""
    table: dict[int, tuple[str, int, int]] = {}
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        entries = ()
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        stat = _proc_stat(entry / "stat")
        if stat is not None:
            table[int(entry.name)] = stat
    selected = {root_pid}
    changed = True
    while changed:
        previous = len(selected)
        selected.update(pid for pid, (_name, ppid, _ticks) in table.items() if ppid in selected)
        changed = len(selected) != previous
    processes: list[dict[str, object]] = []
    for pid in sorted(selected):
        stat = table.get(pid)
        if stat is None:
            continue
        name, ppid, ticks = stat
        rss_kib = _proc_kib(proc_root / str(pid) / "status", "VmRSS")
        pss_kib = _proc_kib(proc_root / str(pid) / "smaps_rollup", "Pss")
        processes.append(
            {
                "pid": pid,
                "ppid": ppid,
                "name": name,
                "cpu_ticks": ticks,
                "rss_kib": rss_kib,
                "pss_kib": pss_kib,
            }
        )
    return {
        "root_pid": root_pid,
        "process_count": len(processes),
        "rss_mib": sum(cast(int, row["rss_kib"]) for row in processes if row["rss_kib"] is not None)
        / 1024,
        "pss_mib": (
            sum(cast(int, row["pss_kib"]) for row in processes if row["pss_kib"] is not None) / 1024
            if any(row["pss_kib"] is not None for row in processes)
            else None
        ),
        "processes": processes,
    }


def process_tree_cpu(
    before: dict[str, object], after: dict[str, object], wall_s: float
) -> dict[str, float | None]:
    """Compute process-tree CPU seconds and average cores over an observation window."""
    if wall_s <= 0:
        return {"cpu_seconds": None, "average_cores": None}
    before_ticks = {
        cast(int, row["pid"]): cast(int, row["cpu_ticks"])
        for row in cast(list[dict[str, object]], before["processes"])
    }
    delta_ticks = sum(
        max(0, cast(int, row["cpu_ticks"]) - before_ticks.get(cast(int, row["pid"]), 0))
        for row in cast(list[dict[str, object]], after["processes"])
    )
    try:
        ticks_per_second = os.sysconf("SC_CLK_TCK")
    except (AttributeError, OSError, ValueError):
        return {"cpu_seconds": None, "average_cores": None}
    cpu_seconds = delta_ticks / ticks_per_second
    return {"cpu_seconds": cpu_seconds, "average_cores": cpu_seconds / wall_s}


class DockerServer:
    """Managed one-shot container with best-effort cgroup memory accounting."""

    def __init__(self, spec: ServerSpec, cpus: set[int]) -> None:
        self.spec = spec
        self.cpus = cpus
        self.container_id = ""
        self.cgroup: Path | None = None
        self.root_pid: int | None = None
        self.started_at: float | None = None
        self.container_start_ms: float | None = None
        self.readiness_ms: float | None = None

    def start(self) -> None:
        self.started_at = time.monotonic()
        name = f"servery-compare-{self.spec.name}-{uuid.uuid4().hex[:10]}"
        command = [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            "host",
            "--cpuset-cpus",
            format_cpu_set(self.cpus),
        ]
        for key, value in self.spec.environment:
            command.extend(("--env", f"{key}={value}"))
        for source, target, readonly in self.spec.mounts:
            mount = f"type=bind,source={source.resolve()},target={target}"
            if readonly:
                mount += ",readonly"
            command.extend(("--mount", mount))
        result = _run([*command, self.spec.image, *self.spec.command], check=False)
        if result.returncode:
            _run(["docker", "rm", "--force", name], check=False)
            detail = (result.stdout + result.stderr).strip()
            raise RuntimeError(f"could not start {self.spec.name} container: {detail}")
        self.container_id = result.stdout.strip()
        self.container_start_ms = (time.monotonic() - self.started_at) * 1000
        self.cgroup = self._find_cgroup()

    def _find_cgroup(self) -> Path | None:
        try:
            pid = int(
                _run(["docker", "inspect", self.container_id, "--format", "{{.State.Pid}}"]).stdout
            )
            self.root_pid = pid
            for line in (Path("/proc") / str(pid) / "cgroup").read_text().splitlines():
                hierarchy, _controllers, relative = line.split(":", 2)
                if hierarchy == "0":
                    return Path("/sys/fs/cgroup") / relative.lstrip("/")
        except (OSError, ValueError, subprocess.CalledProcessError):
            return None
        return None

    def mark_ready(self) -> None:
        if self.started_at is not None:
            self.readiness_ms = (time.monotonic() - self.started_at) * 1000

    def startup(self) -> dict[str, float | None]:
        return {
            "container_start_ms": self.container_start_ms,
            "readiness_ms": self.readiness_ms,
        }

    def process_tree(self) -> dict[str, object] | None:
        return process_tree_snapshot(self.root_pid) if self.root_pid is not None else None

    def memory(self) -> dict[str, float | None]:
        result: dict[str, float | None] = {"current_mib": None, "peak_mib": None}
        if self.cgroup is None:
            return result
        for field, filename in (("current_mib", "memory.current"), ("peak_mib", "memory.peak")):
            try:
                value = int((self.cgroup / filename).read_text().strip())
            except (OSError, ValueError):
                continue
            result[field] = value / 1024 / 1024
        return result

    def logs(self) -> str:
        if not self.container_id:
            return ""
        result = _run(["docker", "logs", self.container_id], check=False)
        return (result.stdout + result.stderr)[-4000:]

    def _exec(self, *command: str) -> subprocess.CompletedProcess[str]:
        if not self.container_id:
            raise RuntimeError("container is not running")
        return _run(["docker", "exec", self.container_id, *command], check=False)

    def access_log_lines(self, timeout: float = 2.0) -> int:
        """Wait for the benchmark access log to quiesce, then return its line count."""
        deadline = time.monotonic() + timeout
        previous: int | None = None
        stable = 0
        while True:
            result = self._exec(
                "python",
                "-c",
                "print(sum(1 for _ in open('/tmp/servery-access.log', encoding='utf-8')))",
            )
            if result.returncode:
                raise RuntimeError(f"could not inspect access log: {result.stderr.strip()}")
            count = int(result.stdout.strip())
            stable = stable + 1 if count == previous else 0
            if stable >= 2 or time.monotonic() >= deadline:
                return count
            previous = count
            time.sleep(0.05)

    def reset_access_log(self) -> None:
        """Truncate the access log between untimed warmup and the timed trial."""
        result = self._exec(
            "python",
            "-c",
            "open('/tmp/servery-access.log', 'w', encoding='utf-8').close()",
        )
        if result.returncode:
            raise RuntimeError(f"could not reset access log: {result.stderr.strip()}")

    def stop(self) -> None:
        if self.container_id:
            _run(["docker", "rm", "--force", self.container_id], check=False)
            self.container_id = ""

    def __enter__(self) -> DockerServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _validate_probe(response: http.client.HTTPResponse, scenario: Scenario) -> None:
    body = response.read()
    if response.status != scenario.expected_status:
        raise RuntimeError(f"probe returned HTTP {response.status}")
    digest = _sha256(body)
    if len(body) != scenario.expected_length or digest != scenario.expected_sha256:
        raise RuntimeError(f"probe body mismatch: length={len(body)}, sha256={digest}")
    for name, expected in scenario.expected_headers:
        actual = response.getheader(name)
        if actual != expected:
            raise RuntimeError(f"probe header {name!r} mismatch: {actual!r} != {expected!r}")


def _probe(port: int, scenario: Scenario, server: DockerServer, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request(
                "POST" if scenario.request_body_size else "GET",
                scenario.path,
                body=b"u" * scenario.request_body_size or None,
                headers={"Connection": "close", **dict(scenario.request_headers)},
            )
            response = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
            time.sleep(0.05)
        else:
            _validate_probe(response, scenario)
            server.mark_ready()
            return
        finally:
            conn.close()
    raise RuntimeError(
        f"{server.spec.name} did not pass the {scenario.name} probe: {last_error}\n"
        f"container logs:\n{server.logs()}"
    )


@contextlib.contextmanager
def _client_affinity(cpus: set[int]) -> Iterator[None]:
    try:
        original = set(os.sched_getaffinity(0))
        os.sched_setaffinity(0, cpus)
    except AttributeError:  # pragma: no cover - comparison Docker mode is Linux-only
        yield
        return
    try:
        yield
    finally:
        os.sched_setaffinity(0, original)


def _rotated[T](values: tuple[T, ...], amount: int) -> tuple[T, ...]:
    if not values:
        return values
    offset = amount % len(values)
    return values[offset:] + values[:offset]


def _summary(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for result in results:
        key = (result["scenario"], result["concurrency"], result["server"])
        grouped.setdefault(key, []).append(result)
    rows: list[dict[str, Any]] = []
    for (scenario, concurrency, server), samples in grouped.items():
        rps_values = [sample["rps"] for sample in samples]
        p99_values = [sample["p99_ms"] for sample in samples]
        median_rps = statistics.median(rps_values)
        median_p99 = statistics.median(p99_values)
        rps_mad = statistics.median(abs(value - median_rps) for value in rps_values)
        p99_mad = statistics.median(abs(value - median_p99) for value in p99_values)
        memory_peaks = [
            sample["container_memory"]["peak_mib"]
            for sample in samples
            if sample["container_memory"]["peak_mib"] is not None
        ]
        median_client_cpu = statistics.median(
            sample["client_cpu_utilization_pct"] for sample in samples
        )
        access_delivery = [
            sample["access_log_delivery_pct"]
            for sample in samples
            if "access_log_delivery_pct" in sample
        ]
        rows.append(
            {
                "scenario": scenario,
                "concurrency": concurrency,
                "server": server,
                "trials": len(samples),
                "median_rps": median_rps,
                "min_rps": min(rps_values),
                "max_rps": max(rps_values),
                "rps_mad": rps_mad,
                "rps_mad_pct": rps_mad / median_rps * 100 if median_rps else 0.0,
                "median_mb_s": statistics.median(sample["mb_s"] for sample in samples),
                "median_p99_ms": median_p99,
                "min_p99_ms": min(p99_values),
                "max_p99_ms": max(p99_values),
                "p99_mad_ms": p99_mad,
                "median_client_cpu_pct": median_client_cpu,
                # At this point the load generator, not necessarily the server,
                # may be the bottleneck. Keep the measurement, but mark it so it
                # is not used to rank servers without a stronger client tier.
                "client_limited": median_client_cpu >= 90.0,
                "total_errors": sum(sample["errors"] for sample in samples),
                "median_peak_mib": statistics.median(memory_peaks) if memory_peaks else None,
                "median_access_log_delivery_pct": (
                    statistics.median(access_delivery) if access_delivery else None
                ),
            }
        )
    return rows


def _paired_summary(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Summarize rotated per-trial ratios for frozen and external comparisons."""
    rows: list[dict[str, Any]] = []
    servers = {str(result["server"]) for result in results}
    pairs = {
        (baseline.removesuffix("-baseline"), baseline)
        for baseline in servers
        if baseline.endswith("-baseline") and baseline.removesuffix("-baseline") in servers
    }
    for external_pair in (
        ("servery-asgi", "uvicorn"),
        ("servery-asgi", "uvicorn-native"),
        ("servery-wsgi", "gunicorn-gthread"),
    ):
        if set(external_pair) <= servers:
            pairs.add(external_pair)
    for candidate, baseline in sorted(pairs):
        indexed = {
            (result["scenario"], result["concurrency"], result["trial"], result["server"]): result
            for result in results
            if result["server"] in {candidate, baseline}
        }
        cohorts = sorted({(key[0], key[1]) for key in indexed})
        for scenario, concurrency in cohorts:
            trials = sorted(
                {
                    key[2]
                    for key in indexed
                    if key[0] == scenario
                    and key[1] == concurrency
                    and (scenario, concurrency, key[2], candidate) in indexed
                    and (scenario, concurrency, key[2], baseline) in indexed
                }
            )
            if not trials:
                continue
            rps_changes = []
            p99_changes = []
            for trial in trials:
                candidate_row = indexed[(scenario, concurrency, trial, candidate)]
                baseline_row = indexed[(scenario, concurrency, trial, baseline)]
                rps_changes.append(candidate_row["rps"] / baseline_row["rps"] * 100 - 100)
                p99_changes.append(candidate_row["p99_ms"] / baseline_row["p99_ms"] * 100 - 100)
            median_rps_change = statistics.median(rps_changes)
            median_p99_change = statistics.median(p99_changes)
            rows.append(
                {
                    "scenario": scenario,
                    "concurrency": concurrency,
                    "candidate": candidate,
                    "baseline": baseline,
                    "paired_trials": len(trials),
                    "median_rps_change_pct": median_rps_change,
                    "rps_change_mad_pct": statistics.median(
                        abs(value - median_rps_change) for value in rps_changes
                    ),
                    "min_rps_change_pct": min(rps_changes),
                    "max_rps_change_pct": max(rps_changes),
                    "median_p99_change_pct": median_p99_change,
                    "p99_change_mad_pct": statistics.median(
                        abs(value - median_p99_change) for value in p99_changes
                    ),
                    "min_p99_change_pct": min(p99_changes),
                    "max_p99_change_pct": max(p99_changes),
                }
            )
    return rows


def _print_summary(rows: list[dict[str, Any]]) -> None:
    print(
        f"{'scenario':<22}{'conns':>7}  {'server':<20}{'req/s':>12}{'MB/s':>12}"
        f"{'RPS MAD':>10}{'p99 ms':>12}{'p99 MAD':>10}{'client CPU':>12}"
        f"{'peak MiB':>12}{'errors':>9}"
    )
    for row in rows:
        peak = row["median_peak_mib"]
        peak_text = f"{peak:.1f}" if peak is not None else "n/a"
        print(
            f"{row['scenario']:<22}{row['concurrency']:>7}  {row['server']:<20}"
            f"{row['median_rps']:>12.0f}"
            f"{row['median_mb_s']:>12.1f}{row['rps_mad_pct']:>9.1f}%"
            f"{row['median_p99_ms']:>12.2f}{row['p99_mad_ms']:>10.2f}"
            f"{row['median_client_cpu_pct']:>10.0f}%"
            f"{'*' if row['client_limited'] else ' ':1}{peak_text:>11}{row['total_errors']:>9}"
        )
    if any(row["client_limited"] for row in rows):
        print("* client CPU >= 90%; treat throughput as client-limited, not a server ranking")
    logged = [row for row in rows if row["median_access_log_delivery_pct"] is not None]
    if logged:
        print("\naccess-log delivery during timed trials")
        for row in logged:
            print(f"{row['server']:<36}{row['median_access_log_delivery_pct']:>8.2f}% median")


def _print_paired_summary(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    print("\npaired candidate change versus comparison (median of per-trial ratios)")
    print(
        f"{'scenario':<22}{'conns':>7}{'trials':>8}{'RPS change':>13}"
        f"{'RPS MAD':>11}{'p99 change':>13}{'p99 MAD':>11}"
    )
    for row in rows:
        print(
            f"{row['scenario']:<22}{row['concurrency']:>7}{row['paired_trials']:>8}"
            f"{row['median_rps_change_pct']:>12.1f}%{row['rps_change_mad_pct']:>10.1f}%"
            f"{row['median_p99_change_pct']:>12.1f}%{row['p99_change_mad_pct']:>10.1f}%"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append", help="scenario name (repeatable)")
    parser.add_argument("--server", action="append", help="server adapter name (repeatable)")
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--warmup", type=float, default=1.0)
    parser.add_argument(
        "--persistent-warmup",
        action="store_true",
        help="keep warmup connections for the timed run instead of reconnecting",
    )
    parser.add_argument(
        "--connection-ramp",
        type=float,
        default=0.0,
        help="seconds over which each client process staggers connection starts",
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument(
        "--concurrency",
        type=int,
        action="append",
        help="connection count (repeatable; default: 1 and 64)",
    )
    parser.add_argument("--client-procs", type=int, default=2)
    parser.add_argument(
        "--max-latency-samples",
        type=int,
        help="cap retained latency observations; exact counts and throughput are unchanged",
    )
    parser.add_argument("--server-cpus", help="Docker cpuset; default: first available CPU")
    parser.add_argument("--client-cpus", help="load-generator cpuset; default: up to four others")
    parser.add_argument("--app-workers", type=int, default=1)
    parser.add_argument(
        "--expected-gil",
        choices=("enabled", "disabled"),
        help="fail preflight unless the candidate Python image has this GIL state",
    )
    parser.add_argument(
        "--servery-baseline-image",
        help="optional prebuilt servery image for rotated same-trial candidate A/B tests",
    )
    parser.add_argument(
        "--include-selector-spike",
        action="store_true",
        help="include the benchmark-only static selector architecture spike",
    )
    parser.add_argument(
        "--servery-max-workers",
        type=int,
        help="Servery reusable worker-thread count (experimental comparison control)",
    )
    parser.add_argument(
        "--servery-small-file-buffer",
        type=int,
        action="append",
        help="Servery --small-file-buffer-size variant (repeatable; 0 is sendfile)",
    )
    parser.add_argument(
        "--servery-write-timeout",
        type=float,
        help="candidate-only Servery --write-timeout control (baseline remains unchanged)",
    )
    parser.add_argument(
        "--servery-request-body-timeout",
        type=float,
        help="candidate-only Servery --request-body-timeout control (baseline unchanged)",
    )
    parser.add_argument(
        "--servery-request-head-timeout",
        type=float,
        help="candidate-only Servery --request-head-timeout control (baseline unchanged)",
    )
    parser.add_argument(
        "--servery-lifespan",
        choices=("auto", "on", "off"),
        help="candidate-only Servery ASGI lifespan policy (baseline unchanged)",
    )
    parser.add_argument(
        "--servery-lifespan-timeout",
        type=float,
        help="candidate-only Servery ASGI lifespan wait (baseline unchanged)",
    )
    parser.add_argument("--python-image", default=DEFAULT_PYTHON_IMAGE)
    parser.add_argument("--nginx-image", default=DEFAULT_NGINX_IMAGE)
    parser.add_argument("--caddy-image", default=DEFAULT_CADDY_IMAGE)
    parser.add_argument("--compare-image", default=DEFAULT_COMPARE_IMAGE)
    parser.add_argument(
        "--include-fastapi",
        action="store_true",
        help="install pinned FastAPI in the comparison image (currently use Python 3.14)",
    )
    parser.add_argument(
        "--include-uvicorn-native",
        action="store_true",
        help="install and compare pinned uvloop/httptools (currently use Python 3.14)",
    )
    parser.add_argument("--no-pull", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    declared = scenarios()
    if args.list:
        for item in declared:
            print(f"{item.name:<22} {item.family:<7} {item.path}")
        return
    if shutil.which("docker") is None:
        parser.error("docker is required")
    requested_concurrencies = tuple(dict.fromkeys(args.concurrency or (1, 64)))
    if min(args.duration, args.warmup, args.trials, args.client_procs) <= 0 or any(
        value <= 0 for value in requested_concurrencies
    ):
        parser.error("duration, warmup, trials, concurrency, and client-procs must be positive")
    if args.app_workers <= 0 or (
        args.servery_max_workers is not None and args.servery_max_workers <= 0
    ):
        parser.error("--app-workers and --servery-max-workers must be positive")
    if args.servery_small_file_buffer and any(
        value < 0 for value in args.servery_small_file_buffer
    ):
        parser.error("--servery-small-file-buffer must be non-negative")
    if args.servery_write_timeout is not None and args.servery_write_timeout <= 0:
        parser.error("--servery-write-timeout must be positive")
    if args.servery_request_body_timeout is not None and args.servery_request_body_timeout <= 0:
        parser.error("--servery-request-body-timeout must be positive")
    if args.servery_request_head_timeout is not None and args.servery_request_head_timeout <= 0:
        parser.error("--servery-request-head-timeout must be positive")
    if args.servery_lifespan_timeout is not None and args.servery_lifespan_timeout <= 0:
        parser.error("--servery-lifespan-timeout must be positive")
    if args.max_latency_samples is not None and args.max_latency_samples <= 0:
        parser.error("--max-latency-samples must be positive")
    if args.connection_ramp < 0:
        parser.error("--connection-ramp cannot be negative")
    selected_names = set(args.scenario or (item.name for item in declared if item.default))
    unknown = selected_names - {item.name for item in declared}
    if unknown:
        parser.error(f"unknown scenarios: {', '.join(sorted(unknown))}")
    selected = tuple(item for item in declared if item.name in selected_names)
    selected_servers = set(args.server or ())
    known_servers = set().union(*SERVER_FAMILIES.values())
    unknown_servers = selected_servers - known_servers
    if unknown_servers:
        parser.error(f"unknown servers: {', '.join(sorted(unknown_servers))}")
    if "uvicorn-native" in selected_servers and not args.include_uvicorn_native:
        parser.error("--server uvicorn-native requires --include-uvicorn-native")
    for family in {item.family for item in selected}:
        if selected_servers and not selected_servers & SERVER_FAMILIES[family]:
            parser.error(f"--server selection has no {family} adapter")
    for scenario in selected:
        if (
            scenario.servers
            and selected_servers
            and not selected_servers.intersection(scenario.servers)
        ):
            parser.error(f"--server selection has no adapter for {scenario.name}")
    try:
        server_cpus, client_cpus = select_cpu_sets(args.server_cpus, args.client_cpus)
    except ValueError as exc:
        parser.error(str(exc))
    if server_cpus & client_cpus:
        parser.error("server and client CPU sets must not overlap")

    images = prepare_images(args)
    candidate_gil = cast(
        bool, cast(dict[str, object], images["servery-python"]["python_runtime"])["gil_enabled"]
    )
    if args.expected_gil is not None and candidate_gil != (args.expected_gil == "enabled"):
        parser.error(
            f"candidate Python GIL is {'enabled' if candidate_gil else 'disabled'}, "
            f"expected {args.expected_gil}"
        )
    if any(item.app_spec and ".fastapi_apps:" in item.app_spec for item in selected) and (
        images["servery-python"]["fastapi"] == "not installed"
    ):
        parser.error(
            "FastAPI scenario selected but FastAPI is absent; build with --include-fastapi "
            "and a compatible Python image (currently python:3.14.3-slim)"
        )
    if args.include_uvicorn_native and (
        images["servery-python"]["uvloop"] == "not installed"
        or images["servery-python"]["httptools"] == "not installed"
    ):
        parser.error(
            "native Uvicorn selected but uvloop/httptools are absent; use a compatible "
            "Python image (currently python:3.14.3-slim)"
        )
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="servery-comparison-") as temporary:
        workspace = Path(temporary)
        corpus = workspace / "corpus"
        corpus.mkdir()
        (corpus / "small.bin").write_bytes(b"s" * 1024)
        (corpus / "small-4k.bin").write_bytes(b"f" * (4 * 1024))
        (corpus / "empty.bin").write_bytes(b"")
        (corpus / "small-16k.bin").write_bytes(b"a" * (16 * 1024))
        (corpus / "small-64k.bin").write_bytes(b"b" * (64 * 1024))
        (corpus / "compressible-64k.txt").write_bytes(_COMPRESSIBLE_64K)
        (corpus / "indexed").mkdir()
        (corpus / "indexed" / "index.html").write_bytes(b"i" * 1024)
        (corpus / "index.html").write_bytes(b"p" * 1024)
        (corpus / "medium-1m.bin").write_bytes(b"m" * (1024 * 1024))
        (corpus / "large.bin").write_bytes(b"L" * (8 * 1024 * 1024))
        selected_fixtures = {item.expected_fixture for item in selected}
        fixed_listing_mtime = 1_700_000_000
        if "listing-100" in selected_fixtures:
            listing_dir = corpus / "listing"
            listing_dir.mkdir()
            for index in range(100):
                entry = listing_dir / f"entry-{index:03d}.txt"
                entry.write_text(f"listing entry {index}\n")
                os.utime(entry, (fixed_listing_mtime, fixed_listing_mtime))
        if "listing-1000" in selected_fixtures:
            listing_1000_dir = corpus / "listing-1000"
            listing_1000_dir.mkdir()
            for index in range(1000):
                entry = listing_1000_dir / f"entry-{index:04d}.txt"
                entry.write_text(f"listing entry {index}\n")
                os.utime(entry, (fixed_listing_mtime, fixed_listing_mtime))
        selected = tuple(resolve_scenario_expectation(item, corpus) for item in selected)
        config_dir = workspace / "config"
        config_dir.mkdir()

        with _client_affinity(client_cpus):
            for scenario_index, scenario in enumerate(selected):
                for concurrency_index, requested_concurrency in enumerate(requested_concurrencies):
                    concurrency = min(
                        requested_concurrency,
                        scenario.concurrency_cap or requested_concurrency,
                    )
                    for trial in range(1, args.trials + 1):
                        port = _free_port()
                        specs = server_specs(
                            scenario,
                            port=port,
                            concurrency=concurrency,
                            corpus=corpus,
                            config_dir=config_dir,
                            args=args,
                        )
                        if scenario.servers is not None:
                            specs = tuple(
                                spec
                                for spec in specs
                                if _scenario_allows_server(scenario, spec.name)
                            )
                        if selected_servers:
                            specs = tuple(
                                spec
                                for spec in specs
                                if spec.name in selected_servers
                                or (
                                    spec.name.endswith("-baseline")
                                    and spec.name.removesuffix("-baseline") in selected_servers
                                )
                                or (
                                    "servery" in selected_servers
                                    and spec.name.startswith("servery-")
                                )
                            )
                        rotation = scenario_index + concurrency_index + trial - 1
                        for order, spec in enumerate(_rotated(specs, rotation), start=1):
                            client_procs = min(args.client_procs, concurrency)
                            url = f"http://127.0.0.1:{port}{scenario.path}"
                            print(
                                f">> {scenario.name} concurrency={concurrency} "
                                f"trial={trial}/{args.trials} server={spec.name} "
                                f"order={order}/{len(specs)}",
                                flush=True,
                            )
                            with DockerServer(spec, server_cpus) as server:
                                _probe(port, scenario, server)
                                tree_before = server.process_tree()
                                tree_started_at = time.monotonic()
                                if not args.persistent_warmup:
                                    run_load(
                                        url,
                                        concurrency=concurrency,
                                        connection_ramp=args.connection_ramp,
                                        duration=args.warmup,
                                        procs=client_procs,
                                        close=scenario.connection_close,
                                        expected_status=scenario.expected_status,
                                        request_headers=scenario.request_headers,
                                        request_body_size=scenario.request_body_size,
                                        read_chunk_size=scenario.read_chunk_size,
                                        read_delay=scenario.read_delay,
                                        max_latency_samples=args.max_latency_samples,
                                    )
                                measures_access_log = spec.name in ACCESS_LOG_SERVERS
                                if measures_access_log:
                                    server.access_log_lines()
                                    server.reset_access_log()
                                load = run_load(
                                    url,
                                    concurrency=concurrency,
                                    warmup=args.warmup if args.persistent_warmup else 0.0,
                                    connection_ramp=args.connection_ramp,
                                    duration=args.duration,
                                    procs=client_procs,
                                    close=scenario.connection_close,
                                    expected_status=scenario.expected_status,
                                    request_headers=scenario.request_headers,
                                    request_body_size=scenario.request_body_size,
                                    read_chunk_size=scenario.read_chunk_size,
                                    read_delay=scenario.read_delay,
                                    max_latency_samples=args.max_latency_samples,
                                )
                                # Snapshot cgroup peak before the out-of-band log audit starts a
                                # short-lived helper process inside the measured container.
                                container_memory = server.memory()
                                tree_after = server.process_tree()
                                tree_wall_s = time.monotonic() - tree_started_at
                                if measures_access_log:
                                    access_log_lines = server.access_log_lines()
                                    load["access_log_lines"] = access_log_lines
                                    requests = cast(int, load["requests"])
                                    load["access_log_delivery_pct"] = (
                                        access_log_lines / requests * 100 if requests else 100.0
                                    )
                                load.update(
                                    {
                                        "scenario": scenario.name,
                                        "family": scenario.family,
                                        "server": spec.name,
                                        "requested_concurrency": requested_concurrency,
                                        "trial": trial,
                                        "order": order,
                                        "client_cpu_capacity": min(len(client_cpus), client_procs),
                                        "client_cpu_utilization_pct": cast(
                                            float, load["client_cpu_cores"]
                                        )
                                        / min(len(client_cpus), client_procs)
                                        * 100,
                                        "container_memory": container_memory,
                                        "startup": server.startup(),
                                        "process_tree": {
                                            "ready": tree_before,
                                            "post_trial": tree_after,
                                            "observation_wall_s": tree_wall_s,
                                            "cpu": (
                                                process_tree_cpu(
                                                    tree_before, tree_after, tree_wall_s
                                                )
                                                if tree_before is not None
                                                and tree_after is not None
                                                else {
                                                    "cpu_seconds": None,
                                                    "average_cores": None,
                                                }
                                            ),
                                        },
                                    }
                                )
                                results.append(load)

    rows = _summary(results)
    paired_rows = _paired_summary(results)
    _print_summary(rows)
    _print_paired_summary(paired_rows)
    evidence = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "host": {
            "platform": platform.platform(),
            "python": sys.version,
            "available_cpus": sorted(_available_cpus()),
            "docker_server": _run(
                ["docker", "version", "--format", "{{.Server.Version}}"]
            ).stdout.strip(),
        },
        "source": _source_identity(),
        "controls": {
            "runtime": "docker-host-network",
            "protocol": "HTTP/1.1 plaintext",
            "server_cpus": sorted(server_cpus),
            "client_cpus": sorted(client_cpus),
            "app_workers": args.app_workers,
            "expected_gil": args.expected_gil,
            "servery_max_workers": args.servery_max_workers,
            "servery_small_file_buffer": args.servery_small_file_buffer,
            "servery_write_timeout": args.servery_write_timeout,
            "servery_request_body_timeout": args.servery_request_body_timeout,
            "servery_request_head_timeout": args.servery_request_head_timeout,
            "servery_lifespan": args.servery_lifespan,
            "servery_lifespan_timeout": args.servery_lifespan_timeout,
            "servery_baseline_image": args.servery_baseline_image,
            "server_filter": sorted(selected_servers),
            "include_selector_spike": args.include_selector_spike,
            "include_fastapi": args.include_fastapi,
            "include_uvicorn_native": args.include_uvicorn_native,
            "requested_concurrencies": requested_concurrencies,
            "client_processes": args.client_procs,
            "max_latency_samples": args.max_latency_samples,
            "warmup_s": args.warmup,
            "warmup_mode": "persistent-connections" if args.persistent_warmup else "separate-run",
            "connection_ramp_s": args.connection_ramp,
            "duration_s": args.duration,
            "trials": args.trials,
            "cache_state": "warm after an untimed workload-specific warmup",
            "order_policy": "deterministic rotation per scenario/trial",
        },
        "images": images,
        "benchmark_python_runtime": _python_runtime(),
        "scenarios": [asdict(item) for item in selected],
        "results": results,
        "summary": rows,
        "paired_summary": paired_rows,
        "limitations": [
            "Loopback measures server and client on one host; it is not WAN latency.",
            "Warm-cache static results do not measure disk cold-start behavior.",
            "Container memory.peak includes runtime and cgroup-accounted cache where available.",
            "WSGI and ASGI results are separate interface comparisons, not one combined ranking.",
            "HTTP/1.1 is the common baseline; TLS, HTTP/2, and HTTP/3 need separate tiers.",
        ],
    }
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(evidence, indent=2) + "\n")
        print(f">> wrote {args.json}")
    if any(row["total_errors"] for row in rows):
        raise SystemExit("comparison invalid: at least one timed trial recorded errors")


if __name__ == "__main__":
    main()
