#!/usr/bin/env python3
"""Functional smoke for an installed wheel, amalgamation, or zipapp.

Run this script from outside the repository with the Python whose installation
is under test. It starts two-worker static, WSGI, and ASGI servers, verifies one
real HTTP response from each, and exercises graceful supervisor termination.
"""

from __future__ import annotations

import argparse
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import TextIO


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launcher(artifact: Path | None) -> list[str]:
    if artifact is None:
        return [sys.executable, "-m", "servery"]
    return [sys.executable, str(artifact.resolve())]


def _read_output(output: TextIO) -> str:
    output.flush()
    output.seek(0)
    return output.read()


def _wait_response(
    port: int,
    path: str,
    expected: bytes,
    process: subprocess.Popen[str],
    output: TextIO,
) -> None:
    deadline = time.monotonic() + 15
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"server exited {process.returncode} before readiness\n"
                f"output:\n{_read_output(output)}"
            )
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", path, headers={"Connection": "close"})
            response = connection.getresponse()
            body = response.read()
            if response.status != 200 or body != expected:
                raise RuntimeError(f"unexpected response: status={response.status}, body={body!r}")
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
            time.sleep(0.05)
        else:
            return
        finally:
            connection.close()
    raise TimeoutError(f"server did not become ready: {last_error}")


def _run_mode(
    launcher: list[str],
    root: Path,
    *,
    extra: tuple[str, ...],
    expected: bytes,
) -> None:
    port = _free_port()
    command = [
        *launcher,
        str(root),
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--quiet",
        "--workers",
        "2",
        "--drain-timeout",
        "2",
        "--force-timeout",
        "2",
        *extra,
    ]
    python_path = os.environ.get("PYTHONPATH")
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(part for part in (str(root), python_path) if part),
    }
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_response(port, "/payload", expected, process, output)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.returncode not in {0, -15}:
                raise RuntimeError(
                    f"server shutdown returned {process.returncode}\n"
                    f"output:\n{_read_output(output)}"
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, help="servery.py or servery.pyz; omit for wheel")
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()

    if args.artifact is None:
        import servery

        actual = servery.__version__
        if actual != args.expected_version:
            raise RuntimeError(f"installed version {actual!r} != {args.expected_version!r}")
    else:
        version = subprocess.run(
            [*_launcher(args.artifact), "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if version != f"servery {args.expected_version}":
            raise RuntimeError(f"artifact version {version!r} != {args.expected_version!r}")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "payload").write_bytes(b"static-smoke")
        (root / "smoke_apps.py").write_text(
            textwrap.dedent(
                """
                def wsgi(environ, start_response):
                    body = b"wsgi-smoke"
                    start_response("200 OK", [("Content-Length", str(len(body)))])
                    return [body]

                async def asgi(scope, receive, send):
                    if scope["type"] == "lifespan":
                        while True:
                            message = await receive()
                            if message["type"] == "lifespan.startup":
                                await send({"type": "lifespan.startup.complete"})
                            elif message["type"] == "lifespan.shutdown":
                                await send({"type": "lifespan.shutdown.complete"})
                                return
                    body = b"asgi-smoke"
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-length", str(len(body)).encode())],
                    })
                    await send({"type": "http.response.body", "body": body})
                """
            ),
            encoding="utf-8",
        )
        launcher = _launcher(args.artifact)
        _run_mode(launcher, root, extra=(), expected=b"static-smoke")
        _run_mode(
            launcher,
            root,
            extra=("--wsgi", "smoke_apps:wsgi"),
            expected=b"wsgi-smoke",
        )
        _run_mode(
            launcher,
            root,
            extra=("--asgi", "smoke_apps:asgi", "--lifespan", "on"),
            expected=b"asgi-smoke",
        )
    print("installed artifact smoke: static, WSGI, ASGI, workers=2: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
