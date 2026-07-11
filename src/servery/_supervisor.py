"""Spawn-compatible, parent-owned-listener worker supervision.

The first supervisor layer starts one fixed generation and shuts it down to
bounded deadlines. Crash restart, recycling, reloads, and singleton services
remain separate work so readiness and ownership are never silently weakened.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import multiprocessing
import os
import signal
import socket
import sys
import threading
import time
import traceback
from multiprocessing.connection import Connection, wait
from typing import Any, cast

from servery import _listener

# A kill request is asynchronous even when the configured escalation waits are
# zero. Give the OS a small, fixed interval to publish and reap the final exit;
# this is cleanup after immediate escalation, not additional application grace.
_FINAL_KILL_REAP_TIMEOUT = 0.2


class SupervisorError(RuntimeError):
    """The fixed worker generation could not start or remain available."""


@dataclasses.dataclass(slots=True)
class _Worker:
    number: int
    process: Any
    status: Connection
    liveness: Connection | None
    commit: Any | None
    stop: Any | None
    prepared: bool = False
    ready: bool = False


def _close_quietly(value: Any) -> None:
    with contextlib.suppress(OSError):
        value.close()


def _isolate_process_tree() -> None:
    """Give a POSIX worker and descendants a force-cleanup boundary."""
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.setsid()


def _send_status(status: Connection, message: tuple[str, int, str]) -> None:
    with contextlib.suppress(BrokenPipeError, EOFError, OSError):
        status.send(message)


def _control_lost(liveness: Connection) -> bool:
    """Return true when the supervisor's write-only control end is gone."""
    try:
        if not liveness.poll():
            return False
    except (BrokenPipeError, EOFError, OSError):
        # POSIX poll() marks the read end ready and recv_bytes() observes EOF.
        # Windows' named-pipe poll can report the same state directly as
        # ERROR_BROKEN_PIPE instead.
        return True
    with contextlib.suppress(EOFError, OSError):
        # The parent never writes application data. Treat either EOF or an
        # unexpected control byte as a conservative shutdown request.
        liveness.recv_bytes()
    return True


def _shutdown_requested(stop: Any, liveness: Connection, timeout: float = 0.05) -> bool:
    """Wait for a normal stop or portable supervisor-control EOF."""
    return stop.wait(timeout) or _control_lost(liveness)


def _wait_for_commit(commit: Any, stop: Any, liveness: Connection) -> bool:
    """Wait portably for commit while allowing rollback or parent loss."""
    while not commit.wait(0.05):
        if stop.is_set() or _control_lost(liveness):
            return False
    return not stop.is_set() and not _control_lost(liveness)


def _raise_start_cancelled() -> None:
    raise SupervisorError("worker startup cancelled by shutdown request")


def _serve_threaded_worker(
    config: Any,
    listener: socket.socket,
    commit: Any,
    stop: Any,
    liveness: Connection,
    report: Any,
) -> None:
    from servery import server

    httpd = server.make_server(config, listener=listener)
    listener.close()
    report("prepared", httpd.server_address)
    if not _wait_for_commit(commit, stop, liveness):
        httpd.server_close()
        return

    def request_stop() -> None:
        while not _shutdown_requested(stop, liveness):
            pass
        httpd.shutdown()

    watcher = threading.Thread(target=request_stop, name="servery-worker-stop", daemon=True)
    watcher.start()
    report("ready", httpd.server_address)
    with httpd:
        httpd.serve_forever()


def _serve_asgi_worker(
    config: Any,
    listener: socket.socket,
    commit: Any,
    stop: Any,
    liveness: Connection,
    report: Any,
) -> None:
    from servery import asgi

    async def run() -> None:
        loop = asyncio.get_running_loop()
        async_stop = asyncio.Event()
        async_commit = asyncio.Event()

        def notify(event: asyncio.Event) -> None:
            # Startup/lifespan failure can close asyncio.run() while the portable
            # control thread is waking. A late notification then has no work to
            # do and must not leak a closed-loop RuntimeError from the daemon.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(event.set)

        def controls() -> None:
            if not _wait_for_commit(commit, stop, liveness):
                notify(async_stop)
                return
            notify(async_commit)
            while not _shutdown_requested(stop, liveness):
                pass
            notify(async_stop)

        threading.Thread(target=controls, name="servery-worker-control", daemon=True).start()

        def prepared(address: Any) -> None:
            listener.close()
            report("prepared", address)

        def started(address: Any) -> None:
            report("ready", address)

        await asgi.serve_forever(
            config,
            prepared=prepared,
            start=async_commit,
            started=started,
            stop=async_stop,
            listener=listener,
        )

    asyncio.run(run())


def _worker_main(
    config: Any,
    listener: socket.socket,
    status: Connection,
    commit: Any,
    stop: Any,
    liveness: Connection,
) -> None:
    """Worker process entry point; kept at module scope for ``spawn``."""
    _isolate_process_tree()
    reported_ready = False

    def report(kind: str, address: Any) -> None:
        nonlocal reported_ready
        host, port = address[:2]
        _send_status(status, (kind, os.getpid(), f"{host}:{port}"))
        reported_ready = reported_ready or kind == "ready"

    try:
        if config.asgi_app:
            _serve_asgi_worker(config, listener, commit, stop, liveness, report)
        else:
            _serve_threaded_worker(config, listener, commit, stop, liveness, report)
    except BaseException as exc:
        if not reported_ready:
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            _send_status(status, ("error", os.getpid(), detail))
            return
        raise
    finally:
        _close_quietly(listener)
        _close_quietly(status)
        _close_quietly(liveness)


class Supervisor:
    """Own one listener and one fixed generation of worker processes."""

    def __init__(self, config: Any) -> None:
        if config.workers <= 1:
            raise ValueError("Supervisor requires workers > 1")
        self.config = config
        self._context = multiprocessing.get_context("spawn")
        self._listener: socket.socket | None = None
        self._workers: list[_Worker] = []
        self._shutdown_requested = threading.Event()
        # start() holds this re-entrant lock through its readiness transaction.
        # shutdown() publishes its Event before waiting for the lock, allowing
        # startup to cancel and perform rollback without a spawn/append gap.
        self._lifecycle_lock = threading.RLock()
        self._started = False
        self._stopped = False

    @property
    def address(self) -> tuple[Any, ...]:
        if self._listener is None:
            raise RuntimeError("supervisor has not started")
        return self._listener.getsockname()

    @property
    def processes(self) -> tuple[Any, ...]:
        """Read-only worker process snapshot for operations integrations."""
        return tuple(worker.process for worker in self._workers)

    def request_shutdown(self) -> None:
        """Request portable graceful shutdown; safe from a signal handler."""
        self._shutdown_requested.set()

    def start(self) -> tuple[Any, ...]:
        """Bind and establish a PREPARED -> COMMIT -> READY generation."""
        with self._lifecycle_lock:
            if self._stopped:
                raise RuntimeError("supervisor has already stopped")
            if self._started:
                raise RuntimeError("supervisor already started")
            self._started = True
            listener = _listener.bind_tcp_listener(self.config.host, self.config.port, backlog=128)
            self._listener = listener
            worker_config = dataclasses.replace(self.config, port=int(listener.getsockname()[1]))
            try:
                for number in range(1, self.config.workers + 1):
                    if self._shutdown_requested.is_set():
                        _raise_start_cancelled()
                    parent_status, child_status = self._context.Pipe(duplex=False)
                    child_liveness, parent_liveness = self._context.Pipe(duplex=False)
                    commit = self._context.Event()
                    stop = self._context.Event()
                    process = self._context.Process(
                        target=_worker_main,
                        args=(
                            worker_config,
                            listener,
                            child_status,
                            commit,
                            stop,
                            child_liveness,
                        ),
                        name=f"servery-worker-{number}",
                    )
                    try:
                        process.start()
                    except BaseException:
                        parent_status.close()
                        child_status.close()
                        child_liveness.close()
                        parent_liveness.close()
                        raise
                    child_status.close()
                    child_liveness.close()
                    self._workers.append(
                        _Worker(number, process, parent_status, parent_liveness, commit, stop)
                    )
                self._wait_for_readiness()
            except BaseException:
                self.shutdown()
                raise
            return self.address

    def _wait_for_readiness(self) -> None:
        deadline = time.monotonic() + self.config.worker_start_timeout
        pending = {worker.status: worker for worker in self._workers}
        committed = False
        while pending:
            if self._shutdown_requested.is_set():
                raise SupervisorError("worker startup cancelled by shutdown request")
            for worker in pending.values():
                if worker.process.exitcode is not None and not worker.status.poll():
                    raise SupervisorError(
                        f"worker {worker.number} exited during startup "
                        f"with code {worker.process.exitcode}"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                phase = "readiness" if committed else "preparation"
                numbers = ", ".join(str(worker.number) for worker in pending.values())
                raise SupervisorError(f"worker {phase} timed out waiting for: {numbers}")
            readable = wait(tuple(pending), timeout=min(remaining, 0.1))
            for ready_object in readable:
                connection = cast(Connection, ready_object)
                worker = pending[connection]
                try:
                    kind, pid, detail = connection.recv()
                except EOFError as exc:
                    raise SupervisorError(
                        f"worker {worker.number} closed startup status without readiness"
                    ) from exc
                if kind == "error":
                    raise SupervisorError(
                        f"worker {worker.number} (pid {pid}) failed startup: {detail}"
                    )
                if kind == "prepared" and not committed:
                    worker.prepared = True
                elif kind == "ready" and committed:
                    worker.ready = True
                    connection.close()
                    pending.pop(connection)
                else:
                    raise SupervisorError(
                        f"worker {worker.number} sent unexpected {kind!r} startup status"
                    )
            if not committed and all(worker.prepared for worker in self._workers):
                committed = True
                for worker in self._workers:
                    commit = worker.commit
                    if commit is None:
                        raise SupervisorError(
                            f"worker {worker.number} lost its startup control handle"
                        )
                    commit.set()

    def wait(self) -> None:
        """Wait for control, failing if a worker exits unexpectedly."""
        if not self._started or self._stopped:
            raise RuntimeError("supervisor is not running")
        while not self._shutdown_requested.wait(0.1):
            with self._lifecycle_lock:
                for worker in self._workers:
                    if worker.process.exitcode is not None:
                        raise SupervisorError(
                            f"worker {worker.number} exited unexpectedly "
                            f"with code {worker.process.exitcode}"
                        )

    @staticmethod
    def _signal_tree(worker: _Worker, *, kill: bool) -> None:
        process = worker.process
        if process.exitcode is not None:
            return
        if os.name == "posix" and process.pid is not None:
            signum = signal.SIGKILL if kill else signal.SIGTERM
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass
            except OSError:
                pass
            else:
                return
        if kill and hasattr(process, "kill"):
            process.kill()
        else:
            process.terminate()

    def _join_until(self, deadline: float) -> list[_Worker]:
        alive = [worker for worker in self._workers if worker.process.is_alive()]
        while alive:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            alive[0].process.join(min(remaining, 0.05))
            alive = [worker for worker in alive if worker.process.is_alive()]
        return alive

    def shutdown(self) -> None:
        """Drain, terminate, then kill the generation to finite deadlines."""
        # Publish cancellation before taking the lock: if start() owns it while
        # awaiting readiness, that transaction observes the Event and rolls back.
        self._shutdown_requested.set()
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
            if self._listener is not None:
                self._listener.close()
                self._listener = None
            for worker in self._workers:
                liveness = worker.liveness
                if liveness is not None:
                    _close_quietly(liveness)
                    worker.liveness = None
                stop = worker.stop
                if stop is not None:
                    stop.set()
            alive = self._join_until(time.monotonic() + self.config.drain_timeout)
            for worker in alive:
                self._signal_tree(worker, kill=False)
            alive = self._join_until(time.monotonic() + self.config.force_timeout)
            for worker in alive:
                self._signal_tree(worker, kill=True)
            survivors = self._join_until(time.monotonic() + _FINAL_KILL_REAP_TIMEOUT)
            for worker in self._workers:
                _close_quietly(worker.status)
                # Events own multiprocessing synchronization handles. Drop the
                # generation's last references once no child can use them so their
                # finalizers can release those resources before this Supervisor is
                # itself collected.
                worker.commit = None
                worker.stop = None
            if survivors:
                numbers = ", ".join(str(worker.number) for worker in survivors)
                raise SupervisorError(f"workers survived final kill deadline: {numbers}")
            for worker in self._workers:
                worker.process.close()


def serve(config: Any) -> None:  # pragma: no cover - blocking CLI integration
    """Run a fixed supervised generation until interrupted or terminated."""
    supervisor = Supervisor(config)
    old_term: Any = None
    can_install_signal = threading.current_thread() is threading.main_thread()
    if can_install_signal:
        old_term = signal.signal(
            signal.SIGTERM, lambda _signum, _frame: supervisor.request_shutdown()
        )
    try:
        address = supervisor.start()
        if not config.quiet:
            host, port = address[:2]
            host_display = f"[{host}]" if ":" in str(host) else host
            scheme = "https" if config.uses_tls else "http"
            mode = f"ASGI app {config.asgi_app}" if config.asgi_app else config.directory
            print(
                f"servery: serving {mode} at {scheme}://{host_display}:{port}/ "
                f"with {config.workers} workers",
                file=sys.stderr,
            )
            for warning in config.startup_warnings():
                print(f"servery: WARNING {warning}", file=sys.stderr)
        try:
            supervisor.wait()
        except KeyboardInterrupt:
            supervisor.request_shutdown()
    finally:
        try:
            supervisor.shutdown()
        finally:
            if can_install_signal:
                signal.signal(signal.SIGTERM, old_term)
