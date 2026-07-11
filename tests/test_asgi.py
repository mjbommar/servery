"""ASGI hosting (--asgi) tests — the asyncio server, run in a background loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import ssl
import threading
import time
import unittest
from collections.abc import Iterator
from typing import Any, cast
from unittest import mock

from servery import _body, _listener, asgi
from servery.config import Config
from tests import _asgiapp
from tests._harness import capturing_logs, raw_exchange, wait_for

try:
    import httpx

    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False


@contextlib.contextmanager
def serving_asgi(
    spec: str,
    *,
    tls: bool = False,
    auth: str | None = None,
    timeout: float = 30.0,
    keepalive_timeout: float | None = None,
    request_head_timeout: float | None = None,
    request_body_timeout: float | None = None,
    write_timeout: float | None = None,
    max_request_body: int = 100 * 1024 * 1024,
    max_connections: int | None = 256,
    max_requests_per_connection: int = 0,
    lifespan: str = "auto",
    lifespan_timeout: float = 5.0,
    listener: socket.socket | None = None,
) -> Iterator[tuple[str, int]]:
    """Run the ASGI server for ``spec`` in a background event loop; yield (host, port)."""
    config = Config.create(
        ".",
        host="127.0.0.1",
        port=0,
        quiet=True,
        asgi_app=spec,
        tls_self_signed=tls,
        auth=auth,
        timeout=timeout,
        keepalive_timeout=keepalive_timeout,
        request_head_timeout=request_head_timeout,
        request_body_timeout=request_body_timeout,
        write_timeout=write_timeout,
        max_request_body=max_request_body,
        max_connections=max_connections,
        max_requests_per_connection=max_requests_per_connection,
        lifespan=lifespan,
        lifespan_timeout=lifespan_timeout,
    )
    holder: dict[str, Any] = {}
    ready = threading.Event()
    loop = asyncio.new_event_loop()
    box: dict[str, asyncio.Event] = {}

    def runner() -> None:
        asyncio.set_event_loop(loop)
        box["stop"] = asyncio.Event()

        def on_ready(addr: tuple[Any, ...]) -> None:
            holder["addr"] = addr
            ready.set()

        try:
            loop.run_until_complete(
                asgi.serve_forever(
                    config,
                    started=on_ready,
                    stop=box["stop"],
                    listener=listener,
                )
            )
        finally:
            loop.close()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    # Generous startup budget: under CI load (free-threaded / macOS runners) and with
    # TLS — which generates a pure-Python self-signed cert — a 5 s wait flaked.
    if not ready.wait(30):
        raise RuntimeError("ASGI server did not start")
    addr = holder["addr"]
    try:
        yield str(addr[0]), int(addr[1])
    finally:
        loop.call_soon_threadsafe(box["stop"].set)
        thread.join(5)


class LoadAppTest(unittest.TestCase):
    def test_loads_and_rejects(self):
        self.assertTrue(callable(asgi.load_app("tests._asgiapp:echo")))
        with self.assertRaises(ValueError):
            asgi.load_app("tests._asgiapp:nope")


class ListenerAdoptionTest(unittest.TestCase):
    def test_tcp_nodelay_is_explicit_for_protocol_zero_listener(self) -> None:
        transport_socket = mock.Mock()
        writer = mock.Mock()
        writer.get_extra_info.return_value = transport_socket

        asgi._set_tcp_nodelay(writer)

        transport_socket.setsockopt.assert_called_once_with(
            socket.IPPROTO_TCP, socket.TCP_NODELAY, 1
        )

    def test_adopted_listener_is_nonblocking_for_asyncio(self) -> None:
        listener = _listener.bind_tcp_listener("127.0.0.1", 0)
        observed: list[bool] = []

        async def inspect_start_server(*_args: Any, **kwargs: Any) -> None:
            observed.append(kwargs["sock"].getblocking())
            raise RuntimeError("inspected")

        config = Config.create(
            ".",
            host="127.0.0.1",
            port=0,
            quiet=True,
            asgi_app="tests._asgiapp:echo",
            lifespan="off",
        )
        try:
            with (
                mock.patch.object(asyncio, "start_server", side_effect=inspect_start_server),
                self.assertRaisesRegex(RuntimeError, "inspected"),
            ):
                asyncio.run(asgi.serve_forever(config, listener=listener))
            self.assertEqual(observed, [False])
            # Adoption changes only the runtime-owned duplicate.
            self.assertTrue(listener.getblocking())
        finally:
            listener.close()

    def test_runtime_close_does_not_close_callers_listener(self) -> None:
        listener = _listener.bind_tcp_listener("127.0.0.1", 0)
        try:
            expected = listener.getsockname()
            with serving_asgi("tests._asgiapp:echo", listener=listener) as address:
                self.assertEqual(address, expected)
                response = raw_exchange(
                    *address,
                    b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                )
                self.assertIn(b"200 OK", response)
                self.assertTrue(response.endswith(b"asgi GET / "), response)
            self.assertGreaterEqual(listener.fileno(), 0)
            self.assertTrue(listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
        finally:
            listener.close()

    def test_callers_close_does_not_stop_adopted_runtime(self) -> None:
        listener = _listener.bind_tcp_listener("127.0.0.1", 0)
        with serving_asgi("tests._asgiapp:echo", listener=listener) as address:
            listener.close()
            response = raw_exchange(
                *address,
                b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
            )
            self.assertIn(b"200 OK", response)
            self.assertTrue(response.endswith(b"asgi GET / "), response)


class ConfigTest(unittest.TestCase):
    def test_asgi_exclusivity(self):
        with self.assertRaises(ValueError):
            Config.create(".", asgi_app="m:a", wsgi_app="m:b")
        with self.assertRaises(ValueError):
            Config.create(".", asgi_app="m:a", http2=True)

    def test_asgi_allows_tls(self):
        cfg = Config.create(".", asgi_app="m:a", tls_self_signed=True)
        self.assertTrue(cfg.uses_tls)

    def test_lifespan_policy_is_validated_and_asgi_scoped(self):
        cfg = Config.create(
            ".",
            asgi_app="m:a",
            lifespan="on",
            lifespan_timeout=2.5,
        )
        self.assertEqual(cfg.lifespan, "on")
        self.assertEqual(cfg.lifespan_timeout, 2.5)
        with self.assertRaisesRegex(ValueError, "lifespan must be"):
            Config.create(".", asgi_app="m:a", lifespan="sometimes")
        with self.assertRaisesRegex(ValueError, "lifespan-timeout"):
            Config.create(".", asgi_app="m:a", lifespan_timeout=0)
        with self.assertRaisesRegex(ValueError, "require --asgi"):
            Config.create(".", lifespan="off")


class LifespanPolicyTest(unittest.TestCase):
    def test_auto_mode_treats_initial_exception_as_unsupported(self) -> None:
        async def exercise() -> None:
            lifespan = asgi._Lifespan(_asgiapp.echo, mode="auto", timeout=0.1)
            self.assertFalse(await lifespan.startup())

        asyncio.run(exercise())

    def test_on_mode_requires_protocol_support(self) -> None:
        async def exercise() -> None:
            lifespan = asgi._Lifespan(_asgiapp.echo, mode="on", timeout=0.1)
            with self.assertRaisesRegex(asgi.LifespanError, "without completing"):
                await lifespan.startup()

        asyncio.run(exercise())

    def test_explicit_startup_failure_prevents_server_bind(self) -> None:
        config = Config.create(
            ".",
            host="127.0.0.1",
            port=0,
            asgi_app="tests._asgiapp:lifespan_startup_failed",
            lifespan_timeout=0.1,
        )
        with self.assertRaisesRegex(asgi.LifespanError, "database unavailable"):
            asyncio.run(asgi.serve_forever(config))

    def test_startup_and_shutdown_waits_are_bounded(self) -> None:
        async def exercise() -> None:
            lifespan = asgi._Lifespan(_asgiapp.lifespan_hangs, timeout=0.01)
            with self.assertRaisesRegex(asgi.LifespanError, "startup timed out"):
                await lifespan.startup()

        asyncio.run(exercise())

    def test_explicit_shutdown_failure_is_surfaced(self) -> None:
        async def exercise() -> None:
            lifespan = asgi._Lifespan(_asgiapp.lifespan_shutdown_failed, timeout=0.1)
            self.assertTrue(await lifespan.startup())
            with self.assertRaisesRegex(asgi.LifespanError, "cleanup failed"):
                await lifespan.shutdown()

        asyncio.run(exercise())

    def test_bind_failure_runs_shutdown_after_successful_startup(self) -> None:
        _asgiapp._lifespan_log.clear()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            config = Config.create(
                ".",
                host="127.0.0.1",
                port=occupied.getsockname()[1],
                asgi_app="tests._asgiapp:with_lifespan",
                lifespan_timeout=0.1,
            )
            with self.assertRaises(OSError):
                asyncio.run(asgi.serve_forever(config))
        self.assertEqual(_asgiapp._lifespan_log, ["startup", "shutdown"])

    def test_listener_validation_failure_runs_shutdown(self) -> None:
        _asgiapp._lifespan_log.clear()
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", 0))
            config = Config.create(
                ".",
                host="127.0.0.1",
                port=0,
                asgi_app="tests._asgiapp:with_lifespan",
                lifespan_timeout=0.1,
            )
            with self.assertRaisesRegex(ValueError, "already be listening"):
                asyncio.run(asgi.serve_forever(config, listener=listener))
            self.assertGreaterEqual(listener.fileno(), 0)
        finally:
            listener.close()
        self.assertEqual(_asgiapp._lifespan_log, ["startup", "shutdown"])

    def test_lifespan_state_is_shallow_copied_into_each_request(self) -> None:
        request = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        with serving_asgi("tests._asgiapp:lifespan_state") as (host, port):
            first = raw_exchange(host, port, request)
            second = raw_exchange(host, port, request)
        self.assertTrue(first.endswith(b"yes:missing:0"), first)
        self.assertTrue(second.endswith(b"yes:missing:1"), second)

    def test_off_mode_skips_failed_lifespan_app(self) -> None:
        request = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        with serving_asgi(
            "tests._asgiapp:lifespan_startup_failed",
            lifespan="off",
        ) as (host, port):
            response = raw_exchange(host, port, request)
        self.assertTrue(response.endswith(b"ok"), response)


class GracefulDrainTest(unittest.TestCase):
    @staticmethod
    def _config(*, timeout: float, lifespan: str = "on") -> Config:
        return Config.create(
            ".",
            host="127.0.0.1",
            port=0,
            quiet=True,
            asgi_app="tests._asgiapp:echo",
            lifespan=lifespan,
            drain_timeout=timeout,
        )

    def test_admitted_request_completes_before_lifespan_shutdown(self) -> None:
        async def exercise() -> None:
            events: list[str] = []
            request_started = asyncio.Event()
            release = asyncio.Event()

            async def app(scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] == "lifespan":
                    while True:
                        message = await receive()
                        if message["type"] == "lifespan.startup":
                            await send({"type": "lifespan.startup.complete"})
                        else:
                            events.append("lifespan.shutdown")
                            await send({"type": "lifespan.shutdown.complete"})
                            return
                events.append("request.started")
                request_started.set()
                await release.wait()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-length", b"2")],
                    }
                )
                await send({"type": "http.response.body", "body": b"ok"})
                events.append("request.complete")

            ready: asyncio.Future[tuple[Any, ...]] = asyncio.get_running_loop().create_future()
            stop = asyncio.Event()
            with mock.patch.object(asgi, "load_app", return_value=app):
                server = asyncio.create_task(
                    asgi.serve_forever(
                        self._config(timeout=0.5),
                        started=ready.set_result,
                        stop=stop,
                    )
                )
                host, port = (await ready)[:2]
                reader, writer = await asyncio.open_connection(host, port)
                writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                await writer.drain()
                await request_started.wait()
                stop.set()
                await asyncio.sleep(0)
                self.assertFalse(server.done())
                release.set()
                response = await reader.read()
                await server
                writer.close()
                await writer.wait_closed()
            self.assertTrue(response.endswith(b"ok"), response)
            self.assertEqual(
                events,
                ["request.started", "request.complete", "lifespan.shutdown"],
            )

        asyncio.run(exercise())

    def test_deadline_cancels_app_and_aborts_transport_before_lifespan(self) -> None:
        async def exercise() -> None:
            events: list[str] = []
            request_started = asyncio.Event()

            async def app(scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] == "lifespan":
                    while True:
                        message = await receive()
                        if message["type"] == "lifespan.startup":
                            await send({"type": "lifespan.startup.complete"})
                        else:
                            events.append("lifespan.shutdown")
                            await send({"type": "lifespan.shutdown.complete"})
                            return
                request_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    events.append("request.cancelled")
                    raise

            ready: asyncio.Future[tuple[Any, ...]] = asyncio.get_running_loop().create_future()
            stop = asyncio.Event()
            with mock.patch.object(asgi, "load_app", return_value=app):
                server = asyncio.create_task(
                    asgi.serve_forever(
                        self._config(timeout=0.01),
                        started=ready.set_result,
                        stop=stop,
                    )
                )
                host, port = (await ready)[:2]
                reader, writer = await asyncio.open_connection(host, port)
                writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                await writer.drain()
                await request_started.wait()
                stop.set()
                await asyncio.wait_for(server, 0.5)
                self.assertEqual(await reader.read(), b"")
                writer.close()
                await writer.wait_closed()
            self.assertEqual(events, ["request.cancelled", "lifespan.shutdown"])

        asyncio.run(exercise())

    def test_streaming_and_post_response_work_drain_before_lifespan(self) -> None:
        async def exercise() -> None:
            events: list[str] = []
            stream_started = asyncio.Event()
            release_stream = asyncio.Event()
            background_started = asyncio.Event()
            release_background = asyncio.Event()

            async def app(scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] == "lifespan":
                    while True:
                        message = await receive()
                        if message["type"] == "lifespan.startup":
                            await send({"type": "lifespan.startup.complete"})
                        else:
                            events.append("lifespan.shutdown")
                            await send({"type": "lifespan.shutdown.complete"})
                            return
                await send({"type": "http.response.start", "status": 200})
                await send({"type": "http.response.body", "body": b"first", "more_body": True})
                stream_started.set()
                await release_stream.wait()
                await send({"type": "http.response.body", "body": b"second"})
                events.append("stream.complete")
                background_started.set()
                await release_background.wait()
                events.append("background.complete")

            ready: asyncio.Future[tuple[Any, ...]] = asyncio.get_running_loop().create_future()
            stop = asyncio.Event()
            with mock.patch.object(asgi, "load_app", return_value=app):
                server = asyncio.create_task(
                    asgi.serve_forever(
                        self._config(timeout=0.5),
                        started=ready.set_result,
                        stop=stop,
                    )
                )
                host, port = (await ready)[:2]
                reader, writer = await asyncio.open_connection(host, port)
                writer.write(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                await writer.drain()
                await stream_started.wait()
                stop.set()
                await asyncio.sleep(0)
                self.assertFalse(server.done())
                release_stream.set()
                await background_started.wait()
                self.assertFalse(server.done())
                release_background.set()
                response = await reader.read()
                await server
                writer.close()
                await writer.wait_closed()
            self.assertIn(b"first", response)
            self.assertIn(b"second", response)
            self.assertEqual(
                events,
                ["stream.complete", "background.complete", "lifespan.shutdown"],
            )

        asyncio.run(exercise())

    def test_cancellation_resistant_task_cannot_extend_deadline(self) -> None:
        async def exercise() -> None:
            class Transport:
                def __init__(self) -> None:
                    self.aborted = False

                def abort(self) -> None:
                    self.aborted = True

            class Writer:
                def __init__(self) -> None:
                    self.transport = Transport()

                def close(self) -> None:
                    pass

            drain = asgi._Drain()
            registered = asyncio.Event()
            release = asyncio.Event()
            writer = Writer()

            async def resistant() -> None:
                connection = drain.register(cast(asyncio.StreamWriter, writer))
                assert connection is not None
                connection.phase = "http"
                registered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                finally:
                    drain.unregister(connection)

            task = asyncio.create_task(resistant())
            await registered.wait()
            await asyncio.wait_for(drain.finish(0), 0.1)
            self.assertFalse(task.done())
            self.assertTrue(writer.transport.aborted)
            release.set()
            await task

        with self.assertLogs("servery", level="WARNING") as logs:
            asyncio.run(exercise())
        output = "\n".join(logs.output)
        self.assertIn("force-cancelling 1 connection task", output)
        self.assertIn("suppressed forced cancellation", output)

    def test_idle_keepalive_is_closed_when_drain_starts(self) -> None:
        async def exercise() -> None:
            async def app(scope: Any, receive: Any, send: Any) -> None:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-length", b"0")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})

            ready: asyncio.Future[tuple[Any, ...]] = asyncio.get_running_loop().create_future()
            stop = asyncio.Event()
            with mock.patch.object(asgi, "load_app", return_value=app):
                server = asyncio.create_task(
                    asgi.serve_forever(
                        self._config(timeout=0.5, lifespan="off"),
                        started=ready.set_result,
                        stop=stop,
                    )
                )
                host, port = (await ready)[:2]
                reader, writer = await asyncio.open_connection(host, port)
                writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                await writer.drain()
                await reader.readuntil(b"\r\n\r\n")
                stop.set()
                await asyncio.wait_for(server, 0.5)
                self.assertEqual(await reader.read(), b"")
                writer.close()
                await writer.wait_closed()

        asyncio.run(exercise())

    def test_websocket_receives_service_restart_close(self) -> None:
        async def exercise() -> None:
            disconnects: list[int] = []
            accepted = asyncio.Event()

            async def app(scope: Any, receive: Any, send: Any) -> None:
                self.assertEqual((await receive())["type"], "websocket.connect")
                await send({"type": "websocket.accept"})
                accepted.set()
                disconnects.append((await receive())["code"])

            ready: asyncio.Future[tuple[Any, ...]] = asyncio.get_running_loop().create_future()
            stop = asyncio.Event()
            with mock.patch.object(asgi, "load_app", return_value=app):
                server = asyncio.create_task(
                    asgi.serve_forever(
                        self._config(timeout=0.5, lifespan="off"),
                        started=ready.set_result,
                        stop=stop,
                    )
                )
                host, port = (await ready)[:2]
                reader, writer = await asyncio.open_connection(host, port)
                writer.write(
                    b"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                    b"Connection: Upgrade\r\nSec-WebSocket-Key: "
                    b"dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
                )
                await writer.drain()
                await reader.readuntil(b"\r\n\r\n")
                await accepted.wait()
                stop.set()
                frame = await reader.readexactly(4)
                await asyncio.wait_for(server, 0.5)
                writer.close()
                await writer.wait_closed()
            self.assertEqual(frame, b"\x88\x02\x03\xf4")
            self.assertEqual(disconnects, [1012])

        asyncio.run(exercise())


class AsyncBodyUnitTest(unittest.TestCase):
    @staticmethod
    def _reader(data: bytes) -> asyncio.StreamReader:
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return reader

    def test_empty_body_waits_for_real_disconnect(self):
        async def exercise() -> None:
            disconnected = asgi._DisconnectState()
            body = asgi._AsyncBody(
                self._reader(b""),
                _body.BodyPlan(0),
                max_body=10,
                timeout=1,
                disconnected=disconnected,
            )
            self.assertEqual(
                await body.receive(),
                {"type": "http.request", "body": b"", "more_body": False},
            )
            terminal = asyncio.create_task(body.receive())
            await asyncio.sleep(0)
            self.assertFalse(terminal.done())
            disconnected.disconnect()
            self.assertEqual(await terminal, {"type": "http.disconnect"})

        asyncio.run(exercise())

    def test_peer_disconnect_during_declared_body_is_an_asgi_event(self):
        async def exercise() -> None:
            body = asgi._AsyncBody(
                self._reader(b"short"),
                _body.BodyPlan(10),
                max_body=10,
                timeout=1,
            )
            self.assertEqual(await body.receive(), {"type": "http.disconnect"})
            self.assertEqual(await body.receive(), {"type": "http.disconnect"})

        asyncio.run(exercise())

    def test_pending_terminal_receives_wake_together_on_response_completion(self):
        async def exercise() -> None:
            class Writer:
                def write(self, _data: bytes) -> None:
                    pass

                def is_closing(self) -> bool:
                    return False

            disconnected = asgi._DisconnectState()
            body = asgi._AsyncBody(
                self._reader(b""),
                _body.BodyPlan(0),
                max_body=10,
                timeout=1,
                disconnected=disconnected,
            )
            state = asgi._ResponseState(
                cast(asyncio.StreamWriter, Writer()),
                "GET",
                True,
                body_complete=body,
            )
            body._response = state
            await body.receive()
            listeners = (asyncio.create_task(body.receive()), asyncio.create_task(body.receive()))
            await asyncio.sleep(0)
            self.assertTrue(all(not listener.done() for listener in listeners))
            await state.send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"0")],
                }
            )
            await state.send({"type": "http.response.body", "body": b""})
            self.assertEqual(
                await asyncio.gather(*listeners),
                [{"type": "http.disconnect"}, {"type": "http.disconnect"}],
            )

        asyncio.run(exercise())

    def test_chunk_extensions_and_trailers_finish_cleanly(self):
        async def exercise() -> None:
            body = asgi._AsyncBody(
                self._reader(b"1;name=value\r\nx\r\n0\r\nX-Test: yes\r\n\r\n"),
                _body.BodyPlan(None, chunked=True),
                max_body=10,
                timeout=1,
            )
            self.assertEqual((await body.receive())["body"], b"x")
            self.assertFalse((await body.receive())["more_body"])

        asyncio.run(exercise())

    def test_total_deadline_spans_chunk_events_and_buffered_bytes(self):
        async def exercise() -> None:
            body = asgi._TimedAsyncBody(
                self._reader(b"1\r\nx\r\n0\r\n\r\n"),
                _body.BodyPlan(None, chunked=True),
                max_body=10,
                timeout=30,
                body_timeout=1,
            )
            body._deadline = time.monotonic() + 1
            self.assertEqual((await body.receive())["body"], b"x")
            body._deadline = time.monotonic() - 1
            with self.assertRaises(_body.BodyTimeoutError):
                await body.receive()

        asyncio.run(exercise())

    def test_invalid_chunk_forms_and_limits_are_explicit(self):
        cases = (
            (b"z\r\n", 10, 400),
            (b"4\r\ndata\r\n", 3, 413),
            (b"1\r\nxXX", 10, 400),
        )

        async def exercise(data: bytes, maximum: int, status: int) -> None:
            body = asgi._AsyncBody(
                self._reader(data),
                _body.BodyPlan(None, chunked=True),
                max_body=maximum,
                timeout=1,
            )
            with self.assertRaises(asgi._BodyReadError) as error:
                await body.receive()
            self.assertEqual(error.exception.status, status)

        for data, maximum, status in cases:
            with self.subTest(data=data):
                asyncio.run(exercise(data, maximum, status))

    def test_trailer_budget_is_enforced(self):
        trailers = (b"x" * 1000 + b"\r\n") * 66

        async def exercise() -> None:
            body = asgi._AsyncBody(
                self._reader(b"0\r\n" + trailers + b"\r\n"),
                _body.BodyPlan(None, chunked=True),
                max_body=10,
                timeout=1,
            )
            with self.assertRaises(asgi._BodyReadError) as error:
                await body.receive()
            self.assertEqual(error.exception.status, 431)

        asyncio.run(exercise())


class ResponseStateUnitTest(unittest.TestCase):
    class Writer:
        def __init__(self) -> None:
            self.data = bytearray()
            self.drains = 0

        def write(self, data: bytes) -> None:
            self.data.extend(data)

        async def drain(self) -> None:
            self.drains += 1

        def is_closing(self) -> bool:
            return False

    def test_send_after_final_response_raises_oserror(self):
        async def exercise() -> None:
            writer = self.Writer()
            state = asgi._ResponseState(
                cast(asyncio.StreamWriter, writer),
                "GET",
                True,
            )
            await state.send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"0")],
                }
            )
            await state.send({"type": "http.response.body", "body": b""})
            with self.assertRaises(asgi.ClientDisconnectError):
                await state.send({"type": "http.response.body", "body": b"late"})

        asyncio.run(exercise())

    def test_send_to_closing_or_failed_peer_raises_client_disconnected(self):
        async def exercise() -> None:
            class ClosingWriter(self.Writer):
                def is_closing(self) -> bool:
                    return True

            closing = asgi._ResponseState(
                cast(asyncio.StreamWriter, ClosingWriter()),
                "GET",
                True,
            )
            with self.assertRaises(asgi.ClientDisconnectError):
                await closing.send({"type": "http.response.start", "status": 200})

            class FailedWriter(self.Writer):
                def write(self, data: bytes) -> None:
                    del data
                    raise BrokenPipeError

            failed = asgi._ResponseState(
                cast(asyncio.StreamWriter, FailedWriter()),
                "GET",
                True,
            )
            await failed.send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"0")],
                }
            )
            with self.assertRaises(asgi.ClientDisconnectError):
                await failed.send({"type": "http.response.body", "body": b""})

        asyncio.run(exercise())

    def test_unknown_status_chunking_and_policy_deduplication(self):
        async def exercise() -> bytes:
            writer = self.Writer()
            state = asgi._ResponseState(
                cast(asyncio.StreamWriter, writer),
                "GET",
                True,
                [(b"X-Policy", b"default")],
                lambda: True,
            )
            await state.send(
                {
                    "type": "http.response.start",
                    "status": 599,
                    "headers": [(b"x-policy", b"app")],
                }
            )
            await state.send({"type": "http.response.body", "body": b"data"})
            self.assertEqual(writer.drains, 0)
            return bytes(writer.data)

        wire = asyncio.run(exercise())
        self.assertIn(b"HTTP/1.1 599 \r\n", wire)
        self.assertEqual(wire.lower().count(b"x-policy:"), 1)
        self.assertIn(b"Transfer-Encoding: chunked", wire)
        self.assertTrue(wire.endswith(b"4\r\ndata\r\n0\r\n\r\n"))

    def test_response_ordering_is_strict(self):
        async def exercise() -> None:
            writer = self.Writer()
            before_start = asgi._ResponseState(cast(asyncio.StreamWriter, writer), "GET", True)
            with self.assertRaisesRegex(RuntimeError, "before response start"):
                await before_start.send({"type": "http.response.body", "body": b"x"})

            duplicate = asgi._ResponseState(cast(asyncio.StreamWriter, writer), "GET", True)
            await duplicate.send({"type": "http.response.start", "status": 200})
            with self.assertRaisesRegex(RuntimeError, "more than once"):
                await duplicate.send({"type": "http.response.start", "status": 201})

            unknown = asgi._ResponseState(cast(asyncio.StreamWriter, writer), "GET", True)
            with self.assertRaisesRegex(RuntimeError, "Unexpected ASGI response event"):
                await unknown.send({"type": "custom.response"})

            trailers = asgi._ResponseState(cast(asyncio.StreamWriter, writer), "GET", True)
            await trailers.send({"type": "http.response.start", "status": 200, "trailers": True})
            with self.assertRaisesRegex(RuntimeError, "outside the trailer phase"):
                await trailers.send({"type": "http.response.trailers", "headers": []})

        asyncio.run(exercise())

    def test_content_length_is_exact_and_transfer_encoding_is_server_owned(self):
        async def exercise() -> bytes:
            exact_writer = self.Writer()
            exact = asgi._ResponseState(cast(asyncio.StreamWriter, exact_writer), "GET", True)
            await exact.send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-length", b"4"),
                        (b"transfer-encoding", b"identity"),
                    ],
                }
            )
            await exact.send({"type": "http.response.body", "body": b"data"})

            repeated_writer = self.Writer()
            repeated = asgi._ResponseState(cast(asyncio.StreamWriter, repeated_writer), "GET", True)
            await repeated.send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-length", b"4, 4"),
                        (b"content-length", b"4"),
                    ],
                }
            )
            await repeated.send({"type": "http.response.body", "body": b"data"})
            self.assertEqual(bytes(repeated_writer.data).lower().count(b"content-length:"), 1)
            self.assertIn(b"content-length: 4\r\n", bytes(repeated_writer.data).lower())

            short = asgi._ResponseState(cast(asyncio.StreamWriter, self.Writer()), "GET", True)
            await short.send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"4")],
                }
            )
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                await short.send({"type": "http.response.body", "body": b"x"})

            long = asgi._ResponseState(cast(asyncio.StreamWriter, self.Writer()), "GET", True)
            await long.send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"1")],
                }
            )
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                await long.send({"type": "http.response.body", "body": b"xx"})
            return bytes(exact_writer.data)

        wire = asyncio.run(exercise())
        self.assertEqual(wire.lower().count(b"transfer-encoding:"), 0)
        self.assertTrue(wire.endswith(b"\r\n\r\ndata"))

    def test_response_fields_are_strict_and_connection_close_is_server_owned(self):
        async def exercise() -> bytes:
            for headers in (
                [(b"Upper", b"value")],
                [(b"bad field", b"value")],
                [(b"x-test", b"one\r\ntwo")],
                [("x-test", b"value")],
                [b"raw-header-is-not-a-pair"],
            ):
                invalid = asgi._ResponseState(
                    cast(asyncio.StreamWriter, self.Writer()), "GET", True
                )
                await invalid.send(
                    {"type": "http.response.start", "status": 200, "headers": headers}
                )
                with self.subTest(headers=headers), self.assertRaises((TypeError, ValueError)):
                    await invalid.send({"type": "http.response.body", "body": b""})

            writer = self.Writer()
            state = asgi._ResponseState(cast(asyncio.StreamWriter, writer), "GET", True)
            await state.send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"0"), (b"connection", b"close")],
                }
            )
            await state.send({"type": "http.response.body", "body": b""})
            return bytes(writer.data)

        wire = asyncio.run(exercise()).lower()
        self.assertEqual(wire.count(b"connection:"), 1)
        self.assertIn(b"connection: close", wire)

    def test_negotiated_trailers_are_chunked_and_streamed(self):
        async def exercise(allow: bool) -> tuple[bytes, asgi._ResponseState]:
            writer = self.Writer()
            state = asgi._ResponseState(
                cast(asyncio.StreamWriter, writer),
                "GET",
                True,
                allow_trailers=allow,
            )
            await state.send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"4"), (b"trailer", b"x-check")],
                    "trailers": True,
                }
            )
            await state.send({"type": "http.response.body", "body": b"data"})
            self.assertFalse(state.response_complete)
            await state.send(
                {
                    "type": "http.response.trailers",
                    "headers": [(b"x-check", b"done")],
                }
            )
            self.assertTrue(state.response_complete)
            return bytes(writer.data), state

        negotiated, _ = asyncio.run(exercise(True))
        head, body = negotiated.split(b"\r\n\r\n", 1)
        self.assertIn(b"Transfer-Encoding: chunked", head)
        self.assertNotIn(b"Content-Length:", head)
        self.assertEqual(body, b"4\r\ndata\r\n0\r\nx-check: done\r\n\r\n")

        ignored, _ = asyncio.run(exercise(False))
        head, body = ignored.split(b"\r\n\r\n", 1)
        self.assertIn(b"content-length: 4", head.lower())
        self.assertEqual(body, b"data")

    def test_trailers_cannot_change_message_framing(self):
        async def exercise() -> None:
            state = asgi._ResponseState(
                cast(asyncio.StreamWriter, self.Writer()),
                "GET",
                True,
                allow_trailers=True,
            )
            await state.send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "trailers": True,
                }
            )
            await state.send({"type": "http.response.body", "body": b"data"})
            with self.assertRaisesRegex(ValueError, "routing or framing"):
                await state.send(
                    {
                        "type": "http.response.trailers",
                        "headers": [(b"content-length", b"4")],
                    }
                )

        asyncio.run(exercise())

    def test_head_without_length_remains_framed_and_suppresses_body(self):
        async def exercise() -> bytes:
            writer = self.Writer()
            state = asgi._ResponseState(cast(asyncio.StreamWriter, writer), "HEAD", True, [], True)
            await state.send({"type": "http.response.start", "status": 200})
            await state.send({"type": "http.response.body", "body": b"hidden"})
            return bytes(writer.data)

        wire = asyncio.run(exercise())
        self.assertNotIn(b"Transfer-Encoding", wire)
        self.assertNotIn(b"Connection: close", wire)
        self.assertNotIn(b"hidden", wire)

    def test_terminal_response_overrides_app_keep_alive_field(self):
        async def exercise() -> bytes:
            writer = self.Writer()
            state = asgi._ResponseState(cast(asyncio.StreamWriter, writer), "GET", False, [], True)
            await state.send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-length", b"0"),
                        (b"connection", b"keep-alive"),
                    ],
                }
            )
            await state.send({"type": "http.response.body", "body": b""})
            return bytes(writer.data)

        head = asyncio.run(exercise()).split(b"\r\n\r\n", 1)[0].lower()
        self.assertEqual(head.count(b"connection:"), 1)
        self.assertIn(b"connection: close", head)

    def test_streaming_drains_each_body_event(self):
        async def exercise() -> int:
            writer = self.Writer()
            state = asgi._ResponseState(cast(asyncio.StreamWriter, writer), "GET", True)
            await state.send({"type": "http.response.start", "status": 200})
            await state.send({"type": "http.response.body", "body": b"one", "more_body": True})
            await state.send({"type": "http.response.body", "body": b"two"})
            return writer.drains

        self.assertEqual(asyncio.run(exercise()), 1)

    def test_blocked_streaming_drain_is_cancellable(self):
        async def exercise() -> None:
            entered = asyncio.Event()

            class BlockingWriter(self.Writer):
                async def drain(self) -> None:
                    self.drains += 1
                    entered.set()
                    await asyncio.Future()

            writer = BlockingWriter()
            state = asgi._ResponseState(cast(asyncio.StreamWriter, writer), "GET", True)
            await state.send({"type": "http.response.start", "status": 200})
            task = asyncio.create_task(
                state.send({"type": "http.response.body", "body": b"data", "more_body": True})
            )
            await entered.wait()
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(exercise())

    def test_blocked_streaming_drain_honors_write_timeout(self):
        async def exercise() -> None:
            class BlockingWriter(self.Writer):
                async def drain(self) -> None:
                    self.drains += 1
                    await asyncio.Future()

            writer = BlockingWriter()
            state = asgi._ResponseState(
                cast(asyncio.StreamWriter, writer),
                "GET",
                True,
                write_timeout=0.01,
            )
            await state.send({"type": "http.response.start", "status": 200})
            with self.assertRaises(TimeoutError):
                await state.send({"type": "http.response.body", "body": b"data", "more_body": True})
            self.assertEqual(writer.drains, 1)

        asyncio.run(exercise())


class ASGIDisconnectTest(unittest.TestCase):
    def test_application_can_catch_send_after_response_oserror(self) -> None:
        _asgiapp.closed_send_error_received.clear()
        with serving_asgi("tests._asgiapp:send_after_response") as (host, port):
            response = raw_exchange(
                host,
                port,
                b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
            )
        self.assertIn(b"\r\n\r\nok", response)
        self.assertTrue(_asgiapp.closed_send_error_received.is_set())

    def test_uncaught_closed_send_is_quiet_and_preserves_pipeline(self) -> None:
        request = (
            b"GET /one HTTP/1.1\r\nHost: x\r\n\r\n"
            b"GET /two HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        with (
            capturing_logs(logging.ERROR) as captured,
            serving_asgi("tests._asgiapp:uncaught_send_after_response") as (host, port),
        ):
            response = raw_exchange(host, port, request)
        self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 2)
        self.assertFalse(any("ASGI app error" in message for message in captured.messages()))

    def test_response_trailers_are_negotiated_and_pipeline_safe(self) -> None:
        with serving_asgi("tests._asgiapp:response_trailers") as (host, port):
            negotiated = raw_exchange(
                host,
                port,
                b"GET / HTTP/1.1\r\nHost: x\r\nTE: trailers\r\nTE: gzip\r\n"
                b"Connection: close\r\n\r\n",
            )
        head, body = negotiated.split(b"\r\n\r\n", 1)
        self.assertIn(b"Transfer-Encoding: chunked", head)
        self.assertEqual(body, b"4\r\ndata\r\n0\r\nx-one: a\r\nx-two: b\r\n\r\n")

        request = (
            b"GET /one HTTP/1.1\r\nHost: x\r\n\r\n"
            b"GET /two HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        with serving_asgi("tests._asgiapp:response_trailers") as (host, port):
            response = raw_exchange(host, port, request)
        self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 2)
        self.assertNotIn(b"x-one: a", response)

    def test_incomplete_application_response_becomes_500(self) -> None:
        with serving_asgi("tests._asgiapp:incomplete_response") as (host, port):
            response = raw_exchange(
                host,
                port,
                b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
            )
        self.assertTrue(response.startswith(b"HTTP/1.1 500"), response)

    def test_receive_after_response_completion_is_synthetic_disconnect(self) -> None:
        _asgiapp.post_response_disconnect_received.clear()
        request = (
            b"GET /one HTTP/1.1\r\nHost: x\r\n\r\n"
            b"GET /two HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        with serving_asgi("tests._asgiapp:wait_after_response") as (host, port):
            response = raw_exchange(host, port, request)
        self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 2)
        self.assertTrue(_asgiapp.post_response_disconnect_received.is_set())

    def test_final_receive_blocks_until_the_peer_really_disconnects(self) -> None:
        _asgiapp.peer_disconnect_received.clear()
        with serving_asgi("tests._asgiapp:wait_for_peer_disconnect") as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            sock.sendall(b"GET /wait HTTP/1.1\r\nHost: x\r\n\r\n")
            time.sleep(0.05)
            self.assertFalse(_asgiapp.peer_disconnect_received.is_set())
            sock.close()
            self.assertTrue(_asgiapp.peer_disconnect_received.wait(2))

    def test_cancelled_listener_restores_protocol_and_preserves_pipeline(self) -> None:
        request = (
            b"GET /one HTTP/1.1\r\nHost: x\r\n\r\n"
            b"GET /two HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        with serving_asgi("tests._asgiapp:cancel_disconnect_listener") as (host, port):
            response = raw_exchange(host, port, request)
        self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 2)
        self.assertEqual(response.count(b"\r\n\r\nok"), 2)

    def test_tls_peer_disconnect_uses_the_same_lazy_signal(self) -> None:
        _asgiapp.peer_disconnect_received.clear()
        context = ssl._create_unverified_context()
        with serving_asgi("tests._asgiapp:wait_for_peer_disconnect", tls=True) as (host, port):
            raw = socket.create_connection((host, port), timeout=5)
            sock = context.wrap_socket(raw, server_hostname="localhost")
            sock.sendall(b"GET /tls-wait HTTP/1.1\r\nHost: localhost\r\n\r\n")
            time.sleep(0.05)
            self.assertFalse(_asgiapp.peer_disconnect_received.is_set())
            sock.close()
            self.assertTrue(_asgiapp.peer_disconnect_received.wait(2))


class ASGIWriteTimeoutTest(unittest.TestCase):
    def test_nonreading_peer_releases_stalled_application(self):
        _asgiapp.write_timeout_finished.clear()
        with serving_asgi("tests._asgiapp:write_until_blocked", write_timeout=0.05) as (
            host,
            port,
        ):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                self.assertTrue(_asgiapp.write_timeout_finished.wait(3))
            finally:
                sock.close()
            time.sleep(0.1)


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class ASGIServerTest(unittest.TestCase):
    def test_methods_and_body(self):
        with serving_asgi("tests._asgiapp:echo") as (host, port), httpx.Client() as client:
            got = client.get(f"http://{host}:{port}/hi?q=1")
            self.assertEqual(got.status_code, 200)
            self.assertEqual(got.text, "asgi GET /hi ")
            posted = client.post(f"http://{host}:{port}/up", content=b"DATA")
            self.assertEqual(posted.text, "asgi POST /up DATA")

    def test_keep_alive_two_requests_one_connection(self):
        # httpx reuses the connection across both requests on a keep-alive server.
        with serving_asgi("tests._asgiapp:echo") as (host, port), httpx.Client() as client:
            self.assertEqual(client.get(f"http://{host}:{port}/a").text, "asgi GET /a ")
            self.assertEqual(client.get(f"http://{host}:{port}/b").text, "asgi GET /b ")

    def test_request_limit_closes_after_final_response(self):
        request = b"GET /a HTTP/1.1\r\nHost: x\r\n\r\nGET /b HTTP/1.1\r\nHost: x\r\n\r\n"
        with serving_asgi("tests._asgiapp:echo", max_requests_per_connection=1) as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(request)
                response = _read_to_close(sock)
            finally:
                sock.close()
        self.assertIn(b"Connection: close", response)
        self.assertIn(b"asgi GET /a", response)
        self.assertNotIn(b"asgi GET /b", response)

    def test_keepalive_idle_timeout_closes_after_a_response(self):
        with serving_asgi(
            "tests._asgiapp:ignores_body",
            timeout=2.0,
            keepalive_timeout=0.1,
        ) as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(b"GET /a HTTP/1.1\r\nHost: x\r\n\r\n")
                response = b""
                while b"ignored" not in response:
                    response += sock.recv(4096)
                sock.settimeout(2)
                self.assertEqual(sock.recv(1), b"")
            finally:
                sock.close()

    def test_total_request_head_timeout_aborts_slow_progress(self):
        with serving_asgi(
            "tests._asgiapp:ignores_body",
            timeout=1.0,
            request_head_timeout=0.15,
        ) as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(b"G")
                time.sleep(0.07)
                sock.sendall(b"ET /a HTTP/1.1\r\n")
                time.sleep(0.07)
                sock.sendall(b"Host: x")
                sock.settimeout(2)
                self.assertEqual(sock.recv(1), b"")
            finally:
                sock.close()

    def test_keepalive_idle_budget_ends_at_first_byte_of_slow_head(self):
        with serving_asgi(
            "tests._asgiapp:ignores_body",
            timeout=1.0,
            keepalive_timeout=0.1,
        ) as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(b"GET /a HTTP/1.1\r\nHost: x\r\n\r\n")
                response = b""
                while b"ignored" not in response:
                    response += sock.recv(4096)
                time.sleep(0.04)
                sock.sendall(b"G")
                time.sleep(0.08)
                sock.sendall(b"ET /b HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                response = _read_to_close(sock)
                self.assertIn(b"ignored", response)
            finally:
                sock.close()

    def test_http_1_1_host_is_strict(self):
        invalid_blocks = (
            b"User-Agent: test\r\n",
            b"Host: one\r\nHost: two\r\n",
            b"Host: user@example.test\r\n",
            b"Host: bad host\r\n",
            b"Host: example.test:bad\r\n",
            b"Host : example.test\r\n",
            b"Host: example.test\x0b\r\n",
        )
        with serving_asgi("tests._asgiapp:echo") as (host, port):
            for block in invalid_blocks:
                with self.subTest(block=block):
                    response = raw_exchange(
                        host,
                        port,
                        b"GET / HTTP/1.1\r\n" + block + b"\r\n",
                    )
                    self.assertTrue(response.startswith(b"HTTP/1.1 400"), response)
                    self.assertNotIn(b"asgi GET", response)

    def test_non_host_field_syntax_is_strict(self):
        bulk_prefix = b"".join(f"X-Bulk-{index}: value\r\n".encode() for index in range(9))
        invalid_blocks = (
            b"No-Colon\r\n",
            b": empty-name\r\n",
            b"Bad Field: value\r\n",
            b"Bad(Name): value\r\n",
            b"X-Test: one\x00two\r\n",
            b"X-Test: one\x0btwo\r\n",
            b"X-Test: one\x7ftwo\r\n",
            b"X-Test: one\r\r\n",
            b"X-Test: one\r\n folded\r\n",
            bulk_prefix + b"Bad Field: value\r\n",
            bulk_prefix + b"X-Control: one\x00two\r\n",
            bulk_prefix + b"X-Bare-Cr: one\r\r\n",
            bulk_prefix + b"X-Bulk-0\r\n",
        )
        with serving_asgi("tests._asgiapp:echo") as (host, port):
            for block in invalid_blocks:
                with self.subTest(block=block):
                    response = raw_exchange(
                        host,
                        port,
                        b"GET / HTTP/1.1\r\nHost: x\r\n" + block + b"\r\n",
                    )
                    self.assertTrue(response.startswith(b"HTTP/1.1 400"), response)
                    self.assertNotIn(b"asgi GET", response)

            valid = raw_exchange(
                host,
                port,
                b"GET /valid HTTP/1.1\r\n"
                b"Host: x\r\n"
                b"!#$%&'*+-.^_`|~09AZaz: \tvisible\x80\t\r\n"
                b"Connection: close\r\n\r\n",
            )
        self.assertTrue(valid.startswith(b"HTTP/1.1 200"), valid)
        self.assertIn(b"asgi GET /valid", valid)

        with serving_asgi("tests._asgiapp:echo") as (host, port):
            valid_bulk = raw_exchange(
                host,
                port,
                b"GET /bulk HTTP/1.1\r\nHost: x\r\n" + bulk_prefix + b"Connection: close\r\n\r\n",
            )
        self.assertTrue(valid_bulk.startswith(b"HTTP/1.1 200"), valid_bulk)

    def test_field_syntax_validation_survives_byte_fragmentation(self):
        cases = (
            (
                b"GET /valid HTTP/1.1\r\nHost: x\r\nX-Test: value\r\nConnection: close\r\n\r\n",
                b"HTTP/1.1 200",
            ),
            (
                b"GET /invalid HTTP/1.1\r\nHost: x\r\nBad Field: value\r\n\r\n",
                b"HTTP/1.1 400",
            ),
        )
        with serving_asgi("tests._asgiapp:echo") as (host, port):
            for request, expected in cases:
                with self.subTest(expected=expected):
                    sock = socket.create_connection((host, port), timeout=5)
                    try:
                        for byte in request:
                            sock.sendall(bytes((byte,)))
                        sock.settimeout(5)
                        response = _read_to_close(sock)
                    finally:
                        sock.close()
                    self.assertTrue(response.startswith(expected), response)

    def test_http_1_0_does_not_require_host(self):
        with serving_asgi("tests._asgiapp:echo") as (host, port):
            response = raw_exchange(
                host,
                port,
                b"GET /old HTTP/1.0\r\nConnection: close\r\n\r\n",
            )
        self.assertTrue(response.startswith(b"HTTP/1.1 200"), response)
        self.assertIn(b"asgi GET /old", response)

    def test_header_count_limit_is_431(self):
        fields = b"".join(f"X-{index}: v\r\n".encode() for index in range(100))
        request = b"GET / HTTP/1.1\r\nHost: x\r\n" + fields + b"\r\n"
        with serving_asgi("tests._asgiapp:echo") as (host, port):
            response = raw_exchange(host, port, request)
        self.assertTrue(response.startswith(b"HTTP/1.1 431"), response)

    def test_lifespan_startup_ran(self):
        with serving_asgi("tests._asgiapp:with_lifespan") as (host, port):
            with httpx.Client() as client:
                resp = client.get(f"http://{host}:{port}/")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("startup", resp.text)


class ASGIChunkedTest(unittest.TestCase):
    def test_large_content_length_body_is_delivered_in_bounded_chunks(self):
        payload = b"x" * 150_000
        with serving_asgi("tests._asgiapp:body_shape") as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(
                    f"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: {len(payload)}\r\n"
                    "Connection: close\r\n\r\n".encode()
                    + payload
                )
                response = _read_to_close(sock)
            finally:
                sock.close()
        self.assertIn(b"3:65536:150000", response)

    def test_oversized_content_length_gets_explicit_413(self):
        with serving_asgi("tests._asgiapp:echo", max_request_body=3) as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(
                    b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n"
                    b"Connection: close\r\n\r\nDATA"
                )
                response = _read_to_close(sock)
            finally:
                sock.close()
        self.assertIn(b"HTTP/1.1 413", response)

    def test_total_body_timeout_stops_a_progressing_client(self):
        with serving_asgi(
            "tests._asgiapp:echo",
            timeout=1.0,
            request_body_timeout=0.15,
        ) as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(
                    b"POST /slow HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n"
                    b"Connection: close\r\n\r\na"
                )
                for byte in b"bcd":
                    time.sleep(0.07)
                    with contextlib.suppress(OSError):
                        sock.sendall(bytes((byte,)))
                sock.settimeout(2)
                with contextlib.suppress(OSError):
                    self.assertEqual(sock.recv(4096), b"")
            finally:
                sock.close()

    def test_duplicate_length_and_te_plus_length_are_rejected(self):
        requests = (
            b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\n"
            b"Content-Length: 2\r\nConnection: close\r\n\r\nX",
            b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
            b"Content-Length: 1\r\nConnection: close\r\n\r\n0\r\n\r\n",
        )
        with serving_asgi("tests._asgiapp:echo") as (host, port):
            for request in requests:
                with self.subTest(request=request[:30]):
                    sock = socket.create_connection((host, port), timeout=5)
                    try:
                        sock.sendall(request)
                        response = _read_to_close(sock)
                    finally:
                        sock.close()
                    self.assertIn(b"HTTP/1.1 400", response)

    def test_app_that_ignores_body_forces_close_before_pipeline(self):
        with serving_asgi("tests._asgiapp:ignores_body") as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(
                    b"POST /a HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\nDATA"
                    b"GET /b HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
                )
                response = _read_to_close(sock)
            finally:
                sock.close()
        self.assertEqual(response.count(b"HTTP/1.1 200 OK"), 1)
        self.assertIn(b"Connection: close", response)

    def test_streaming_uses_chunked(self):
        with serving_asgi("tests._asgiapp:streaming") as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                sock.settimeout(3)
                data = b""
                while b"0\r\n\r\n" not in data:
                    piece = sock.recv(4096)
                    if not piece:
                        break
                    data += piece
            finally:
                sock.close()
            self.assertIn(b"transfer-encoding: chunked", data.split(b"\r\n\r\n", 1)[0].lower())
            self.assertIn(b"part1", data)
            self.assertIn(b"part2", data)

    def test_unmasked_client_frame_is_rejected(self):
        # RFC 6455 §5.1: the server MUST close on an unmasked client frame.
        with serving_asgi("tests._asgiapp:ws_echo") as (host, port):
            sock, _key, resp = _ws_open(host, port)
            try:
                self.assertIn(b"101", resp)
                sock.sendall(bytes((0x81, 2)) + b"hi")  # unmasked text frame
                sock.settimeout(3)
                data = b""
                with contextlib.suppress(OSError):
                    while True:
                        piece = sock.recv(4096)
                        if not piece:
                            break
                        data += piece
                self.assertNotIn(b"echo:hi", data)  # rejected, not echoed
            finally:
                sock.close()

    def test_slow_client_head_times_out(self):
        # A trickled/never-completed request head must not pin the event loop.
        with serving_asgi("tests._asgiapp:echo", timeout=0.5) as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(b"GET / HTTP/1.1\r\n")  # partial head, no terminator
                sock.settimeout(3)
                self.assertEqual(sock.recv(4096), b"")  # server closed after timeout
            finally:
                sock.close()

    def test_chunked_request_body_is_reassembled(self):
        # A client chunked request body (no Content-Length) must reach the app
        # whole — and must not desync the connection (FastAPI streaming uploads).
        with serving_asgi("tests._asgiapp:echo") as (host, port):
            sock = socket.create_connection((host, port), timeout=5)
            try:
                sock.sendall(
                    b"POST /p HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
                    b"Connection: close\r\n\r\n5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n"
                )
                sock.settimeout(3)
                data = b""
                while True:
                    piece = sock.recv(4096)
                    if not piece:
                        break
                    data += piece
            finally:
                sock.close()
            self.assertIn(b"asgi POST /p helloworld", data)


class WebSocketHandshakeTest(unittest.TestCase):
    def test_accept_key_matches_rfc6455_example(self):
        from servery import _websocket

        # The canonical example from RFC 6455 §1.3.
        self.assertEqual(
            _websocket.accept_key(b"dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )


class WebSocketFramingTest(unittest.TestCase):
    class Writer:
        def __init__(self) -> None:
            self.data = bytearray()
            self.drains = 0

        def write(self, data: bytes) -> None:
            self.data.extend(data)

        async def drain(self) -> None:
            self.drains += 1

    @staticmethod
    def _reader(data: bytes) -> asyncio.StreamReader:
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return reader

    @staticmethod
    def _client_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
        import struct

        first = (0x80 if fin else 0) | opcode
        length = len(payload)
        if length < 126:
            head = bytes((first, 0x80 | length))
        elif length < 65536:
            head = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            head = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        mask = b"mask"
        encoded = bytes(value ^ mask[index & 3] for index, value in enumerate(payload))
        return head + mask + encoded

    def test_server_frame_encodes_all_length_tiers(self) -> None:
        from servery import _websocket

        self.assertEqual(_websocket._frame(1, b"x")[1], 1)
        self.assertEqual(_websocket._frame(2, b"x" * 126)[1], 126)
        self.assertEqual(_websocket._frame(2, b"x" * 65536)[1], 127)

    def test_read_frame_decodes_extended_masked_lengths(self) -> None:
        from servery import _websocket

        async def exercise() -> None:
            for payload in (b"x" * 126, b"y" * 65536):
                fin, opcode, decoded = await _websocket._read_frame(
                    self._reader(self._client_frame(_websocket._BINARY, payload))
                )
                self.assertTrue(fin)
                self.assertEqual(opcode, _websocket._BINARY)
                self.assertEqual(decoded, payload)

        asyncio.run(exercise())

    def test_read_frame_rejects_protocol_and_size_errors(self) -> None:
        import struct

        from servery import _websocket

        async def code(data: bytes) -> int:
            with self.assertRaises(_websocket._ClosedError) as raised:
                await _websocket._read_frame(self._reader(data))
            return raised.exception.code

        async def exercise() -> None:
            self.assertEqual(await code(b"\xc1\x80"), 1002)  # RSV bit
            self.assertEqual(await code(b"\x09\x80"), 1002)  # fragmented ping
            self.assertEqual(await code(b"\x81\x00"), 1002)  # unmasked client
            too_large = b"\x82\xff" + struct.pack("!Q", _websocket._MAX_PAYLOAD + 1)
            self.assertEqual(await code(too_large), 1009)

        asyncio.run(exercise())

    def test_read_message_handles_ping_pong_fragmentation_and_close(self) -> None:
        import struct

        from servery import _websocket

        async def exercise() -> None:
            wire = b"".join(
                (
                    self._client_frame(_websocket._PING, b"p"),
                    self._client_frame(_websocket._PONG, b"ignored"),
                    self._client_frame(_websocket._TEXT, b"hel", fin=False),
                    self._client_frame(_websocket._CONT, b"lo"),
                )
            )
            writer = self.Writer()
            opcode, payload = await _websocket._read_message(
                self._reader(wire), cast(Any, writer), None
            )
            self.assertEqual((opcode, payload), (_websocket._TEXT, b"hello"))
            self.assertIn(_websocket._frame(_websocket._PONG, b"p"), writer.data)

            close_writer = self.Writer()
            with self.assertRaises(_websocket._ClosedError) as raised:
                await _websocket._read_message(
                    self._reader(self._client_frame(_websocket._CLOSE, struct.pack("!H", 1001))),
                    cast(Any, close_writer),
                    None,
                )
            self.assertEqual(raised.exception.code, 1001)
            self.assertIn(struct.pack("!H", 1001), close_writer.data)

        asyncio.run(exercise())

    def test_serve_accepts_sends_and_closes_all_message_types(self) -> None:
        from servery import _websocket

        async def exercise() -> None:
            writer = self.Writer()

            async def app(scope: Any, receive: Any, send: Any) -> None:
                self.assertEqual((await receive())["type"], "websocket.connect")
                await send(
                    {
                        "type": "websocket.accept",
                        "subprotocol": "chat",
                        "headers": [(b"X-Test", b"yes")],
                    }
                )
                await send({"type": "websocket.send", "text": "hello"})
                await send({"type": "websocket.send", "bytes": b"bytes"})
                await send({"type": "websocket.close", "code": 1001})

            await _websocket.serve(
                self._reader(b""),
                cast(Any, writer),
                {},
                app,
                b"key",
            )
            output = bytes(writer.data)
            self.assertIn(b"Sec-WebSocket-Protocol: chat", output)
            self.assertIn(b"X-Test: yes", output)
            self.assertIn(_websocket._frame(_websocket._TEXT, b"hello"), output)
            self.assertIn(_websocket._frame(_websocket._BINARY, b"bytes"), output)

        asyncio.run(exercise())

    def test_serve_rejection_invalid_utf8_and_shutdown_paths(self) -> None:
        from servery import _websocket

        async def exercise() -> None:
            reject_writer = self.Writer()

            async def reject(_scope: Any, receive: Any, send: Any) -> None:
                await receive()
                await send({"type": "websocket.close"})

            await _websocket.serve(self._reader(b""), cast(Any, reject_writer), {}, reject, b"key")
            self.assertIn(b"403 Forbidden", reject_writer.data)

            invalid_writer = self.Writer()

            async def invalid(_scope: Any, receive: Any, _send: Any) -> None:
                await receive()
                event = await receive()
                self.assertEqual(event, {"type": "websocket.disconnect", "code": 1007})

            await _websocket.serve(
                self._reader(self._client_frame(_websocket._TEXT, b"\xff")),
                cast(Any, invalid_writer),
                {},
                invalid,
                b"key",
            )

            shutdown_writer = self.Writer()
            shutdown = asyncio.Event()
            shutdown.set()

            async def stopping(_scope: Any, receive: Any, _send: Any) -> None:
                await receive()
                first = await receive()
                second = await receive()
                self.assertEqual(first["code"], 1012)
                self.assertEqual(second["code"], 1012)

            await _websocket.serve(
                self._reader(b""),
                cast(Any, shutdown_writer),
                {},
                stopping,
                b"key",
                shutdown=shutdown,
            )

        asyncio.run(exercise())


class ASGIConnectionBudgetTest(unittest.TestCase):
    def test_saturation_rejects_quickly_and_recovers(self):
        with serving_asgi("tests._asgiapp:echo", max_connections=1, timeout=2.0) as (host, port):
            held = socket.create_connection((host, port), timeout=5)
            held.sendall(b"GET / HTTP/1.1\r\n")  # keep the sole task in head parsing
            time.sleep(0.1)
            rejected = socket.create_connection((host, port), timeout=5)
            try:
                rejected.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                rejected.settimeout(2)
                try:
                    data = rejected.recv(4096)
                except ConnectionResetError:
                    data = b""
                self.assertEqual(data, b"")
            finally:
                rejected.close()
                held.close()
            time.sleep(0.1)
            recovered = socket.create_connection((host, port), timeout=5)
            try:
                recovered.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                self.assertIn(b"200 OK", _read_to_close(recovered))
            finally:
                recovered.close()


def _ws_open(host: str, port: int, path: str = "/") -> tuple[socket.socket, bytes, bytes]:
    """Open a WebSocket: do the upgrade, return (sock, key, handshake_response)."""
    import base64
    import os

    key = base64.b64encode(os.urandom(16))
    sock = socket.create_connection((host, port), timeout=5)
    sock.sendall(
        b"GET " + path.encode() + b" HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Key: " + key + b"\r\n"
        b"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += sock.recv(4096)
    return sock, key, resp


def _read_to_close(sock: socket.socket) -> bytes:
    sock.settimeout(5)
    data = bytearray()
    while True:
        piece = sock.recv(65536)
        if not piece:
            return bytes(data)
        data += piece


def _ws_send_text(sock: socket.socket, text: str) -> None:
    import os

    payload = text.encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes((0x81, 0x80 | len(payload))) + mask + masked)  # FIN+text, masked


def _ws_read_text(sock: socket.socket, initial: bytes = b"") -> str:
    data = bytearray(initial)
    while len(data) < 2:
        data += sock.recv(2 - len(data))
    length = data[1] & 0x7F  # server frames are unmasked; payloads here are small
    while len(data) < 2 + length:
        data += sock.recv(2 + length - len(data))
    return bytes(data[2 : 2 + length]).decode()


class WebSocketWireTest(unittest.TestCase):
    def test_echo_over_the_wire(self):
        from servery import _websocket

        with serving_asgi("tests._asgiapp:ws_echo") as (host, port):
            sock, key, resp = _ws_open(host, port)
            try:
                self.assertIn(b"101 Switching Protocols", resp)
                # base64 accept value is case-sensitive — match it verbatim.
                self.assertIn(_websocket.accept_key(key).encode(), resp)
                _ws_send_text(sock, "hi")
                self.assertEqual(_ws_read_text(sock), "echo:hi")
                _ws_send_text(sock, "again")
                self.assertEqual(_ws_read_text(sock), "echo:again")
            finally:
                sock.close()

    def test_lifespan_state_reaches_websocket_scope(self):
        with serving_asgi("tests._asgiapp:lifespan_state") as (host, port):
            sock, _key, response = _ws_open(host, port)
            try:
                self.assertIn(b"101 Switching Protocols", response)
                _head, _separator, initial_frame = response.partition(b"\r\n\r\n")
                self.assertEqual(_ws_read_text(sock, initial_frame), "yes")
            finally:
                sock.close()


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class ASGIHeadersTest(unittest.TestCase):
    def test_date_and_server_headers_present(self):
        # An origin server MUST send Date (RFC 7231 §7.1.1.2); add Server for parity
        # with the threading handler. Both come from the shared per-second cache.
        from servery import __version__

        with serving_asgi("tests._asgiapp:echo") as (host, port), httpx.Client() as c:
            r = c.get(f"http://{host}:{port}/x")
            self.assertIn("date", r.headers)
            self.assertEqual(r.headers.get("server"), f"servery/{__version__}")


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class ASGIAuthTest(unittest.TestCase):
    def test_auth_is_enforced(self):
        with serving_asgi("tests._asgiapp:echo", auth="u:p") as (host, port), httpx.Client() as c:
            self.assertEqual(c.get(f"http://{host}:{port}/x").status_code, 401)
            self.assertEqual(c.get(f"http://{host}:{port}/x", auth=("u", "p")).status_code, 200)


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class ASGITLSTest(unittest.TestCase):
    def test_serves_over_https(self):
        with serving_asgi("tests._asgiapp:echo", tls=True) as (host, port):
            with httpx.Client(verify=False) as client:
                resp = client.get(f"https://{host}:{port}/secure?q=1")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.text, "asgi GET /secure ")


@unittest.skipUnless(_HAVE_HTTPX, "httpx not installed")
class ASGITelemetryTest(unittest.TestCase):
    def test_app_error_returns_500_and_is_logged(self):
        with (
            capturing_logs(logging.ERROR) as cap,
            serving_asgi("tests._asgiapp:crashing") as (
                host,
                port,
            ),
        ):
            with httpx.Client() as client:
                resp = client.get(f"http://{host}:{port}/x")
            self.assertEqual(resp.status_code, 500)
            self.assertTrue(
                wait_for(lambda: any(r.levelno == logging.ERROR for r in cap.records)),
                "expected an ERROR log for the app crash",
            )
            self.assertTrue(any("ASGI app error" in r.getMessage() for r in cap.records))

    def test_request_is_access_logged(self):
        with (
            capturing_logs(logging.INFO) as cap,
            serving_asgi("tests._asgiapp:echo") as (
                host,
                port,
            ),
        ):
            with httpx.Client() as client:
                client.get(f"http://{host}:{port}/hello?q=1")
            self.assertTrue(
                wait_for(lambda: any('"GET /hello?q=1' in r.getMessage() for r in cap.records)),
                "expected an INFO access-log line",
            )


if __name__ == "__main__":
    unittest.main()
