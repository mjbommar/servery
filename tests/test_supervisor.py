"""Fixed-generation worker supervisor lifecycle tests."""

from __future__ import annotations

import asyncio
import dataclasses
import http.client
import multiprocessing
import os
import signal
import socket
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from servery import _supervisor, _tls
from servery.config import Config
from servery.server import make_server


def wsgi_metadata(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    payload = f"{environ['wsgi.multiprocess']}:{environ['wsgi.multithread']}".encode()
    start_response(
        "200 OK",
        [("Content-Type", "text/plain"), ("Content-Length", str(len(payload)))],
    )
    return [payload]


async def gated_asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        message = await receive()
        if message["type"] == "lifespan.startup":
            gate = Path(os.environ["SERVERY_TEST_SUPERVISOR_GATE"])
            claim = gate.with_suffix(".claim")
            try:
                claim.touch(exist_ok=False)
            except FileExistsError:
                deadline = time.monotonic() + 5
                while not gate.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
            await send({"type": "lifespan.startup.complete"})
        message = await receive()
        if message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
        return
    await receive()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"2")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


async def cancellation_resistant_lifespan(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Model application code that requires the supervisor's kill boundary."""
    if scope["type"] != "lifespan":
        return
    message = await receive()
    if message["type"] != "lifespan.startup":
        return
    if os.name == "posix":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    await send({"type": "lifespan.startup.complete"})
    message = await receive()
    if message["type"] != "lifespan.shutdown":
        return
    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            continue


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "hello.txt").write_text("hello", encoding="utf-8")
        self.supervisors: list[_supervisor.Supervisor] = []

    def tearDown(self) -> None:
        for supervisor in self.supervisors:
            supervisor.request_shutdown()
            supervisor.shutdown()
        self._tmp.cleanup()

    def _make_supervisor(self, **kwargs: Any) -> _supervisor.Supervisor:
        drain_timeout = kwargs.pop("drain_timeout", 1)
        force_timeout = kwargs.pop("force_timeout", 0.2)
        worker_start_timeout = kwargs.pop("worker_start_timeout", 5)
        config = Config.create(
            self.root,
            host="127.0.0.1",
            port=0,
            quiet=True,
            drain_timeout=drain_timeout,
            force_timeout=force_timeout,
            worker_start_timeout=worker_start_timeout,
            **kwargs,
        )
        supervisor = _supervisor.Supervisor(config)
        self.supervisors.append(supervisor)
        return supervisor

    @staticmethod
    def _get(address: tuple[Any, ...], path: str = "/hello.txt") -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(address[0], address[1], timeout=3)
        connection.request("GET", path)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    @staticmethod
    def _active_worker_pids() -> set[int | None]:
        return {
            process.pid
            for process in multiprocessing.active_children()
            if process.name.startswith("servery-worker-")
        }

    def test_prepared_worker_treats_supervisor_control_eof_as_cancel(self) -> None:
        context = multiprocessing.get_context("spawn")
        commit = context.Event()
        stop = context.Event()
        child_liveness, parent_liveness = context.Pipe(duplex=False)
        outcome: list[bool] = []
        waiter = threading.Thread(
            target=lambda: outcome.append(
                _supervisor._wait_for_commit(commit, stop, child_liveness)
            )
        )
        waiter.start()
        parent_liveness.close()
        waiter.join(1)
        child_liveness.close()
        self.assertFalse(waiter.is_alive())
        self.assertEqual(outcome, [False])

    def test_ready_workers_drain_when_supervisor_controls_close(self) -> None:
        supervisor = self._make_supervisor(workers=2)
        supervisor.start()
        processes = supervisor.processes
        for worker in supervisor._workers:
            liveness = worker.liveness
            self.assertIsNotNone(liveness)
            assert liveness is not None
            liveness.close()
            worker.liveness = None
        deadline = time.monotonic() + 5
        for process in processes:
            process.join(max(0, deadline - time.monotonic()))
        self.assertTrue(
            all(not process.is_alive() for process in processes),
            [(process.pid, process.exitcode, process.is_alive()) for process in processes],
        )
        supervisor.shutdown()

    def test_two_and_four_workers_share_parent_listener(self) -> None:
        for count in (2, 4):
            with self.subTest(workers=count):
                supervisor = self._make_supervisor(workers=count)
                address = supervisor.start()
                self.assertEqual(len(supervisor.processes), count)
                self.assertTrue(all(process.is_alive() for process in supervisor.processes))
                self.assertEqual(self._get(address), (200, b"hello"))
                supervisor.shutdown()

    def test_one_worker_remains_the_direct_runtime(self) -> None:
        config = Config.create(self.root, host="127.0.0.1", port=0, quiet=True, workers=1)
        with make_server(config) as httpd:
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(self._get(httpd.server_address), (200, b"hello"))
            finally:
                httpd.shutdown()
                thread.join(3)

    def test_wsgi_reports_multiprocess_metadata(self) -> None:
        supervisor = self._make_supervisor(
            workers=2,
            wsgi_app="tests.test_supervisor:wsgi_metadata",
        )
        address = supervisor.start()
        self.assertEqual(self._get(address, "/"), (200, b"True:True"))

    def test_workers_share_one_parent_materialized_tls_identity(self) -> None:
        source = Config.create(
            self.root,
            host="127.0.0.1",
            port=0,
            quiet=True,
            workers=2,
            tls_self_signed=True,
            drain_timeout=1,
            force_timeout=0.2,
            worker_start_timeout=5,
        )
        with _tls.self_signed_files(source) as (cert, key):
            config = dataclasses.replace(
                source,
                tls_cert=cert,
                tls_key=key,
                tls_self_signed=False,
            )
            supervisor = _supervisor.Supervisor(config)
            self.supervisors.append(supervisor)
            address = supervisor.start()
            identities: set[bytes] = set()
            context = ssl._create_unverified_context()
            for _ in range(8):
                with (
                    socket.create_connection(address, timeout=3) as raw,
                    context.wrap_socket(raw, server_hostname="localhost") as secured,
                ):
                    certificate = secured.getpeercert(binary_form=True)
                    assert certificate is not None
                    identities.add(certificate)
                    secured.sendall(
                        b"GET /hello.txt HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                    )
                    self.assertIn(b"HTTP/1.1 200 OK", secured.recv(4096))
            self.assertEqual(len(identities), 1)

    def test_asgi_does_not_admit_before_prepared_quorum(self) -> None:
        gate = self.root / "release"
        old_gate = os.environ.get("SERVERY_TEST_SUPERVISOR_GATE")
        os.environ["SERVERY_TEST_SUPERVISOR_GATE"] = os.fspath(gate)
        try:
            supervisor = self._make_supervisor(
                workers=2,
                asgi_app="tests.test_supervisor:gated_asgi",
                lifespan="on",
            )
            outcome: list[BaseException] = []

            def start() -> None:
                try:
                    supervisor.start()
                except BaseException as exc:  # preserve assertion evidence from the thread
                    outcome.append(exc)

            starter = threading.Thread(target=start)
            starter.start()
            deadline = time.monotonic() + 3
            while supervisor._listener is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(supervisor._listener)
            address = supervisor.address
            sock = socket.create_connection(address, timeout=2)
            sock.settimeout(0.2)
            sock.sendall(b"GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
            with self.assertRaises(TimeoutError):
                sock.recv(1)
            gate.touch()
            starter.join(5)
            self.assertFalse(starter.is_alive())
            self.assertEqual(outcome, [])
            sock.settimeout(3)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            sock.close()
            self.assertIn(b"HTTP/1.1 200 OK", response)
            self.assertTrue(response.endswith(b"ok"))
        finally:
            if old_gate is None:
                os.environ.pop("SERVERY_TEST_SUPERVISOR_GATE", None)
            else:
                os.environ["SERVERY_TEST_SUPERVISOR_GATE"] = old_gate

    def test_asgi_startup_failure_rolls_back_every_worker(self) -> None:
        supervisor = self._make_supervisor(
            workers=2,
            asgi_app="tests._asgiapp:lifespan_startup_failed",
            lifespan="on",
        )
        with self.assertRaisesRegex(_supervisor.SupervisorError, "failed startup"):
            supervisor.start()
        self.assertEqual(self._active_worker_pids(), set())

    def test_invalid_wsgi_import_rolls_back_and_closes_controls(self) -> None:
        before = self._active_worker_pids()
        supervisor = self._make_supervisor(
            workers=2,
            wsgi_app="tests.module_that_does_not_exist:application",
        )
        with self.assertRaisesRegex(_supervisor.SupervisorError, "failed startup"):
            supervisor.start()
        self.assertEqual(self._active_worker_pids(), before)
        for worker in supervisor._workers:
            self.assertTrue(worker.status.closed)
            self.assertIsNone(worker.liveness)
            self.assertIsNone(worker.commit)
            self.assertIsNone(worker.stop)
            with self.assertRaises(ValueError):
                _ = worker.process.sentinel

    def test_shutdown_request_during_prepared_barrier_reaps_generation(self) -> None:
        gate = self.root / "never-release"
        old_gate = os.environ.get("SERVERY_TEST_SUPERVISOR_GATE")
        os.environ["SERVERY_TEST_SUPERVISOR_GATE"] = os.fspath(gate)
        try:
            supervisor = self._make_supervisor(
                workers=2,
                asgi_app="tests.test_supervisor:gated_asgi",
                lifespan="on",
            )
            outcome: list[BaseException] = []

            def start() -> None:
                try:
                    supervisor.start()
                except BaseException as exc:
                    outcome.append(exc)

            starter = threading.Thread(target=start)
            starter.start()
            deadline = time.monotonic() + 3
            while not any(worker.prepared for worker in supervisor._workers):
                if time.monotonic() >= deadline:
                    self.fail("no worker reached the PREPARED barrier")
                time.sleep(0.01)
            supervisor.request_shutdown()
            starter.join(3)
            self.assertFalse(starter.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], _supervisor.SupervisorError)
            self.assertIn("cancelled", str(outcome[0]))
            self.assertEqual(self._active_worker_pids(), set())
        finally:
            if old_gate is None:
                os.environ.pop("SERVERY_TEST_SUPERVISOR_GATE", None)
            else:
                os.environ["SERVERY_TEST_SUPERVISOR_GATE"] = old_gate

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "requires a force-kill signal")
    def test_cancellation_resistant_worker_is_killed_within_deadline(self) -> None:
        supervisor = self._make_supervisor(
            workers=2,
            asgi_app="tests.test_supervisor:cancellation_resistant_lifespan",
            lifespan="on",
            lifespan_timeout=0.05,
            drain_timeout=0.05,
            force_timeout=0.1,
        )
        supervisor.start()
        pids = {process.pid for process in supervisor.processes}
        started = time.monotonic()
        supervisor.shutdown()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5)
        self.assertTrue(pids.isdisjoint(self._active_worker_pids()))
        for worker in supervisor._workers:
            self.assertTrue(worker.status.closed)
            self.assertIsNone(worker.liveness)
            self.assertIsNone(worker.commit)
            self.assertIsNone(worker.stop)

    def test_shutdown_is_idempotent_and_reaps_children(self) -> None:
        supervisor = self._make_supervisor(workers=2)
        supervisor.start()
        pids = {process.pid for process in supervisor.processes}
        supervisor.shutdown()
        supervisor.shutdown()
        self.assertTrue(pids.isdisjoint(self._active_worker_pids()))
        for worker in supervisor._workers:
            self.assertTrue(worker.status.closed)
            self.assertIsNone(worker.liveness)
            self.assertIsNone(worker.commit)
            self.assertIsNone(worker.stop)

    def test_shutdown_before_start_rejects_start_without_binding(self) -> None:
        supervisor = self._make_supervisor(workers=2)
        before = self._active_worker_pids()
        supervisor.shutdown()
        with self.assertRaisesRegex(RuntimeError, "already stopped"):
            supervisor.start()
        self.assertIsNone(supervisor._listener)
        self.assertEqual(supervisor.processes, ())
        self.assertEqual(self._active_worker_pids(), before)

    def test_concurrent_shutdown_cancels_start_and_reaps_generation(self) -> None:
        gate = self.root / "never-release-concurrent"
        old_gate = os.environ.get("SERVERY_TEST_SUPERVISOR_GATE")
        os.environ["SERVERY_TEST_SUPERVISOR_GATE"] = os.fspath(gate)
        try:
            supervisor = self._make_supervisor(
                workers=2,
                asgi_app="tests.test_supervisor:gated_asgi",
                lifespan="on",
            )
            outcome: list[BaseException] = []

            def start() -> None:
                try:
                    supervisor.start()
                except BaseException as exc:
                    outcome.append(exc)

            starter = threading.Thread(target=start)
            starter.start()
            deadline = time.monotonic() + 3
            while not any(worker.prepared for worker in supervisor._workers):
                if time.monotonic() >= deadline:
                    self.fail("no worker reached the PREPARED barrier")
                time.sleep(0.01)
            stopper = threading.Thread(target=supervisor.shutdown)
            stopper.start()
            starter.join(3)
            stopper.join(3)
            self.assertFalse(starter.is_alive())
            self.assertFalse(stopper.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], _supervisor.SupervisorError)
            self.assertEqual(self._active_worker_pids(), set())
            self.assertIsNone(supervisor._listener)
        finally:
            if old_gate is None:
                os.environ.pop("SERVERY_TEST_SUPERVISOR_GATE", None)
            else:
                os.environ["SERVERY_TEST_SUPERVISOR_GATE"] = old_gate

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "requires a force-kill signal")
    def test_zero_force_timeout_escalates_immediately_and_reaps(self) -> None:
        supervisor = self._make_supervisor(
            workers=2,
            asgi_app="tests.test_supervisor:cancellation_resistant_lifespan",
            lifespan="on",
            lifespan_timeout=0.05,
            drain_timeout=0,
            force_timeout=0,
        )
        supervisor.start()
        pids = {process.pid for process in supervisor.processes}
        started = time.monotonic()
        supervisor.shutdown()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(pids.isdisjoint(self._active_worker_pids()))

    def test_serve_sigterm_requests_shutdown_and_restores_handler(self) -> None:
        config = Config.create(self.root, quiet=True, workers=2)
        installed: list[Any] = []
        previous = object()

        class FakeSupervisor:
            def __init__(self) -> None:
                self.requests = 0
                self.shutdowns = 0

            def start(self) -> tuple[str, int]:
                return ("127.0.0.1", 8080)

            def wait(self) -> None:
                installed[0](signal.SIGTERM, None)

            def request_shutdown(self) -> None:
                self.requests += 1

            def shutdown(self) -> None:
                self.shutdowns += 1

        fake = FakeSupervisor()

        def install(_signum: int, handler: Any) -> Any:
            installed.append(handler)
            return previous

        with (
            mock.patch.object(_supervisor, "Supervisor", return_value=fake),
            mock.patch.object(_supervisor.signal, "signal", side_effect=install),
        ):
            _supervisor.serve(config)

        self.assertEqual(fake.requests, 1)
        self.assertEqual(fake.shutdowns, 1)
        self.assertIs(installed[-1], previous)

    def test_serve_ctrl_c_requests_shutdown_and_restores_handler(self) -> None:
        config = Config.create(self.root, quiet=True, workers=2)
        installed: list[Any] = []
        previous = object()

        class FakeSupervisor:
            def __init__(self) -> None:
                self.requests = 0
                self.shutdowns = 0

            def start(self) -> tuple[str, int]:
                return ("127.0.0.1", 8080)

            def wait(self) -> None:
                raise KeyboardInterrupt

            def request_shutdown(self) -> None:
                self.requests += 1

            def shutdown(self) -> None:
                self.shutdowns += 1

        fake = FakeSupervisor()

        def install(_signum: int, handler: Any) -> Any:
            installed.append(handler)
            return previous

        with (
            mock.patch.object(_supervisor, "Supervisor", return_value=fake),
            mock.patch.object(_supervisor.signal, "signal", side_effect=install),
        ):
            _supervisor.serve(config)

        self.assertEqual(fake.requests, 1)
        self.assertEqual(fake.shutdowns, 1)
        self.assertIs(installed[-1], previous)


class SupervisorUnitTests(unittest.TestCase):
    """Deterministic coverage of supervisor failure and defensive states."""

    @staticmethod
    def _config(**changes: Any) -> Config:
        defaults: dict[str, Any] = {
            "workers": 2,
            "quiet": True,
            "worker_start_timeout": 1,
            "drain_timeout": 0,
            "force_timeout": 0,
        }
        defaults.update(changes)
        return Config.create(".", **defaults)

    def test_control_helpers_cover_data_stop_and_parent_loss(self) -> None:
        liveness = mock.Mock()
        liveness.poll.return_value = False
        self.assertFalse(_supervisor._control_lost(liveness))

        liveness.poll.return_value = True
        self.assertTrue(_supervisor._control_lost(liveness))
        liveness.recv_bytes.assert_called_once_with()

        # Windows named-pipe poll reports a closed write end as ERROR_BROKEN_PIPE
        # instead of readiness followed by EOF from recv_bytes().
        liveness.poll.side_effect = BrokenPipeError
        self.assertTrue(_supervisor._control_lost(liveness))

        stop = threading.Event()
        stop.set()
        self.assertTrue(_supervisor._shutdown_requested(stop, liveness, timeout=0))

        commit = threading.Event()
        commit.set()
        clear_stop = threading.Event()
        live = mock.Mock()
        live.poll.return_value = False
        self.assertTrue(_supervisor._wait_for_commit(commit, clear_stop, live))

        with self.assertRaisesRegex(_supervisor.SupervisorError, "cancelled"):
            _supervisor._raise_start_cancelled()

    def test_isolation_and_status_helpers_suppress_os_failures(self) -> None:
        status = mock.Mock()
        status.send.side_effect = BrokenPipeError
        _supervisor._send_status(status, ("ready", 1, "address"))

        with (
            mock.patch.object(_supervisor.os, "name", "posix"),
            mock.patch.object(_supervisor.os, "setsid", side_effect=OSError, create=True),
        ):
            _supervisor._isolate_process_tree()
        with mock.patch.object(_supervisor.os, "name", "nt"):
            _supervisor._isolate_process_tree()

    def test_threaded_worker_rolls_back_when_commit_is_cancelled(self) -> None:
        httpd = mock.Mock()
        listener = mock.Mock()
        with (
            mock.patch("servery.server.make_server", return_value=httpd),
            mock.patch.object(_supervisor, "_wait_for_commit", return_value=False),
        ):
            _supervisor._serve_threaded_worker(
                self._config(),
                listener,
                mock.Mock(),
                mock.Mock(),
                mock.Mock(),
                mock.Mock(),
            )
        listener.close.assert_called_once_with()
        httpd.server_close.assert_called_once_with()
        httpd.serve_forever.assert_not_called()

    def test_asgi_worker_control_thread_forwards_commit_and_stop(self) -> None:
        commit = threading.Event()
        commit.set()
        stop = threading.Event()
        liveness = mock.Mock()
        liveness.poll.return_value = False
        listener = mock.Mock()
        report = mock.Mock()

        async def serve_forever(
            _config: Any,
            *,
            prepared: Any,
            start: asyncio.Event,
            started: Any,
            stop: asyncio.Event,
            listener: Any,
        ) -> None:
            prepared(("127.0.0.1", 8000))
            await asyncio.wait_for(start.wait(), 1)
            started(("127.0.0.1", 8000))
            # Publish normal supervisor shutdown after admission is committed.
            globals_stop.set()
            await asyncio.wait_for(stop.wait(), 1)

        globals_stop = stop
        with mock.patch("servery.asgi.serve_forever", side_effect=serve_forever):
            _supervisor._serve_asgi_worker(
                self._config(asgi_app="tests._asgiapp:echo"),
                listener,
                commit,
                stop,
                liveness,
                report,
            )
        listener.close.assert_called_once_with()
        self.assertEqual([call.args[0] for call in report.call_args_list], ["prepared", "ready"])

    def test_worker_main_reports_startup_error_but_reraises_post_ready_error(self) -> None:
        config = self._config()
        listener = mock.Mock()
        status = mock.Mock()
        liveness = mock.Mock()

        with mock.patch.object(
            _supervisor, "_serve_threaded_worker", side_effect=ValueError("before")
        ):
            _supervisor._worker_main(config, listener, status, mock.Mock(), mock.Mock(), liveness)
        self.assertEqual(status.send.call_args.args[0][0], "error")

        def ready_then_fail(
            _config: Any,
            _listener: Any,
            _commit: Any,
            _stop: Any,
            _liveness: Any,
            report: Any,
        ) -> None:
            report("ready", ("127.0.0.1", 80))
            raise ValueError("after")

        status = mock.Mock()
        with (
            mock.patch.object(_supervisor, "_serve_threaded_worker", side_effect=ready_then_fail),
            self.assertRaisesRegex(ValueError, "after"),
        ):
            _supervisor._worker_main(
                config, mock.Mock(), status, mock.Mock(), mock.Mock(), mock.Mock()
            )

    def test_lifecycle_guards_and_worker_exit_detection(self) -> None:
        with self.assertRaisesRegex(ValueError, "workers > 1"):
            _supervisor.Supervisor(self._config(workers=1))

        supervisor = _supervisor.Supervisor(self._config())
        with self.assertRaisesRegex(RuntimeError, "has not started"):
            _ = supervisor.address
        with self.assertRaisesRegex(RuntimeError, "not running"):
            supervisor.wait()

        supervisor._started = True
        with self.assertRaisesRegex(RuntimeError, "already started"):
            supervisor.start()

        process = mock.Mock(exitcode=7)
        supervisor._workers = [_supervisor._Worker(1, process, mock.Mock(), None, None, None)]
        with self.assertRaisesRegex(_supervisor.SupervisorError, "exited unexpectedly"):
            supervisor.wait()

    def test_wait_returns_after_shutdown_and_scans_healthy_workers(self) -> None:
        supervisor = _supervisor.Supervisor(self._config())
        supervisor._started = True
        process_a = mock.Mock(exitcode=None)
        process_b = mock.Mock(exitcode=None)
        supervisor._workers = [
            _supervisor._Worker(1, process_a, mock.Mock(), None, None, None),
            _supervisor._Worker(2, process_b, mock.Mock(), None, None, None),
        ]
        with mock.patch.object(supervisor._shutdown_requested, "wait", side_effect=(False, True)):
            supervisor.wait()

    def test_shutdown_request_before_first_spawn_rolls_back_listener(self) -> None:
        supervisor = _supervisor.Supervisor(self._config())
        supervisor.request_shutdown()
        with self.assertRaisesRegex(_supervisor.SupervisorError, "cancelled"):
            supervisor.start()
        self.assertIsNone(supervisor._listener)
        self.assertEqual(supervisor.processes, ())

    def test_process_start_failure_closes_unpublished_generation_handles(self) -> None:
        supervisor = _supervisor.Supervisor(self._config())
        context = mock.Mock()
        parent_status, child_status = mock.Mock(), mock.Mock()
        child_liveness, parent_liveness = mock.Mock(), mock.Mock()
        context.Pipe.side_effect = (
            (parent_status, child_status),
            (child_liveness, parent_liveness),
        )
        context.Event.side_effect = (mock.Mock(), mock.Mock())
        process = mock.Mock()
        process.start.side_effect = RuntimeError("spawn failed")
        context.Process.return_value = process
        supervisor._context = context

        listener = mock.Mock()
        listener.getsockname.return_value = ("127.0.0.1", 12345)
        with (
            mock.patch.object(_supervisor._listener, "bind_tcp_listener", return_value=listener),
            self.assertRaisesRegex(RuntimeError, "spawn failed"),
        ):
            supervisor.start()

        for connection in (parent_status, child_status, child_liveness, parent_liveness):
            connection.close.assert_called_once_with()
        listener.close.assert_called_once_with()
        self.assertEqual(supervisor.processes, ())

    def test_readiness_reports_exit_timeout_eof_unexpected_and_lost_commit(self) -> None:
        cases = (
            ("exit", "exited during startup"),
            ("timeout", "preparation timed out"),
            ("eof", "closed startup status"),
            ("unexpected", "unexpected 'ready'"),
            ("lost_commit", "lost its startup control"),
        )
        for case, message in cases:
            with self.subTest(case=case):
                supervisor = _supervisor.Supervisor(
                    dataclasses.replace(
                        self._config(), worker_start_timeout=0 if case == "timeout" else 1
                    )
                )
                connection = mock.Mock()
                connection.poll.return_value = False
                process = mock.Mock(exitcode=3 if case == "exit" else None)
                commit = None if case == "lost_commit" else mock.Mock()
                worker = _supervisor._Worker(1, process, connection, None, commit, mock.Mock())
                supervisor._workers = [worker]
                if case == "eof":
                    connection.recv.side_effect = EOFError
                elif case == "unexpected":
                    connection.recv.return_value = ("ready", 10, "detail")
                elif case == "lost_commit":
                    connection.recv.return_value = ("prepared", 10, "detail")
                with (
                    mock.patch.object(
                        _supervisor,
                        "wait",
                        return_value=[connection]
                        if case in {"eof", "unexpected", "lost_commit"}
                        else [],
                    ),
                    self.assertRaisesRegex(_supervisor.SupervisorError, message),
                ):
                    supervisor._wait_for_readiness()

    def test_signal_tree_falls_back_and_shutdown_reports_survivors(self) -> None:
        process = mock.Mock(exitcode=None, pid=123)
        worker = _supervisor._Worker(1, process, mock.Mock(), None, None, None)
        with (
            mock.patch.object(_supervisor.os, "name", "posix"),
            mock.patch.object(_supervisor.os, "killpg", side_effect=OSError, create=True),
        ):
            _supervisor.Supervisor._signal_tree(worker, kill=False)
        process.terminate.assert_called_once_with()

        process.reset_mock()
        with (
            mock.patch.object(_supervisor.os, "name", "posix"),
            mock.patch.object(
                _supervisor.os, "killpg", side_effect=ProcessLookupError, create=True
            ),
        ):
            _supervisor.Supervisor._signal_tree(worker, kill=False)
        process.terminate.assert_called_once_with()

        process.reset_mock()
        with mock.patch.object(_supervisor.os, "name", "nt"):
            _supervisor.Supervisor._signal_tree(worker, kill=True)
        process.kill.assert_called_once_with()

        process.exitcode = 0
        _supervisor.Supervisor._signal_tree(worker, kill=True)
        process.kill.assert_called_once_with()

        supervisor = _supervisor.Supervisor(self._config())
        supervisor._started = True
        process = mock.Mock(exitcode=None)
        worker = _supervisor._Worker(1, process, mock.Mock(), None, None, None)
        supervisor._workers = [worker]
        with (
            mock.patch.object(supervisor, "_join_until", return_value=[worker]),
            mock.patch.object(supervisor, "_signal_tree"),
            self.assertRaisesRegex(_supervisor.SupervisorError, "survived final kill"),
        ):
            supervisor.shutdown()

    def test_asgi_tcp_nodelay_ignores_absent_socket_and_socket_error(self) -> None:
        writer = mock.Mock()
        writer.get_extra_info.return_value = None
        from servery import asgi

        asgi._set_tcp_nodelay(writer)

        transport_socket = mock.Mock()
        transport_socket.setsockopt.side_effect = OSError
        writer.get_extra_info.return_value = transport_socket
        asgi._set_tcp_nodelay(writer)


if __name__ == "__main__":
    unittest.main()
