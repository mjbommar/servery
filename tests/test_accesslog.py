"""Access-log tests: the CLF/combined/JSON formatters + the end-to-end file write."""

from __future__ import annotations

import http.client
import json
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from servery import _accesslog
from servery.config import Config
from servery.server import make_server
from tests._harness import serving

_WHEN = 1_700_000_000.0  # fixed instant for deterministic formatting


class FormatTest(unittest.TestCase):
    def _line(self, fmt):
        path = Path(tempfile.mkdtemp()) / "access.log"
        log = _accesslog.AccessLog(str(path), fmt)
        log.record(
            "10.0.0.1",
            "GET /a%20b HTTP/1.1",
            200,
            1234,
            referer="http://ref/",
            user_agent="UA/1.0",
            when=_WHEN,
        )
        log.close()
        return path.read_text().strip()

    def test_clf(self):
        line = self._line("clf")
        self.assertRegex(line, r'^10\.0\.0\.1 - - \[.+\] "GET /a%20b HTTP/1\.1" 200 1234$')
        self.assertRegex(line, r"\[\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}\]")

    def test_combined_appends_referer_and_agent(self):
        self.assertTrue(self._line("combined").endswith('1234 "http://ref/" "UA/1.0"'))

    def test_json_fields(self):
        obj = json.loads(self._line("json"))
        self.assertEqual(obj["method"], "GET")
        self.assertEqual(obj["path"], "/a%20b")
        self.assertEqual(obj["status"], 200)
        self.assertEqual(obj["user_agent"], "UA/1.0")
        self.assertRegex(obj["time"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_invalid_format_rejected(self):
        path = Path(tempfile.mkdtemp()) / "access.log"
        with self.assertRaises(ValueError):
            _accesslog.AccessLog(str(path), "xml")

    def test_preformatted_lines_can_be_flushed_as_one_batch(self):
        path = Path(tempfile.mkdtemp()) / "access.log"
        log = _accesslog.AccessLog(str(path), "clf")
        lines = [
            log.format_line("a", "GET /one HTTP/1.1", 200, 1, when=_WHEN),
            log.format_line("b", "GET /two HTTP/1.1", 404, 0, when=_WHEN),
        ]
        log.write_lines(lines)
        log.close()
        self.assertEqual(path.read_text().splitlines(), lines)

    def test_opt_in_write_errors_propagate_to_async_transport_owner(self):
        path = Path(tempfile.mkdtemp()) / "access.log"
        log = _accesslog.AccessLog(str(path), raise_errors=True)
        handler = log._handler
        self.assertIsNotNone(handler)
        assert handler is not None
        real_stream = handler.stream
        failing_stream = mock.Mock()
        failing_stream.write.side_effect = OSError("full")
        handler.stream = failing_stream
        try:
            with self.assertRaisesRegex(OSError, "full"):
                log.write_lines(("record",))
        finally:
            handler.stream = real_stream
            log.close()

    def test_clf_escapes_quotes_backslashes_and_controls(self):
        path = Path(tempfile.mkdtemp()) / "access.log"
        log = _accesslog.AccessLog(str(path), "combined")
        line = log.format_line(
            "client\nname",
            'GET /say"hi\\x HTTP/1.1',
            200,
            1,
            referer='ref"\r',
            user_agent="agent\t\\",
            when=_WHEN,
        )
        log.close()
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertIn(r"client\x0aname", line)
        self.assertIn(r"GET /say\"hi\\x HTTP/1.1", line)
        self.assertIn(r"ref\"\x0d", line)
        self.assertIn(r"agent\x09\\", line)


class AsyncAccessLogTest(unittest.TestCase):
    def _log(self, directory: str, **kwargs) -> _accesslog.AsyncAccessLog:
        return _accesslog.AsyncAccessLog(str(Path(directory) / "access.log"), **kwargs)

    @staticmethod
    def _record(log: _accesslog.AsyncAccessLog, path: str = "/"):
        return log.record("client", f"GET {path} HTTP/1.1", 200, 1, when=_WHEN)

    def test_batches_and_accounts_for_normal_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp, batch_size=8, batch_wait=0)
            for index in range(10):
                self.assertEqual(
                    self._record(log, f"/{index}"),
                    _accesslog.AccessLogResult.ACCEPTED,
                )
            snapshot = log.close(timeout=2)
            self.assertFalse(snapshot.writer_alive)
            self.assertTrue(snapshot.closed)
            self.assertEqual(snapshot.accepted, 10)
            self.assertEqual(snapshot.written, 10)
            self.assertEqual(snapshot.write_failed, 0)
            self.assertEqual(snapshot.abandoned, 0)
            self.assertEqual(Path(tmp, "access.log").read_text().count("GET /"), 10)

    def test_drop_policy_has_exact_count_capacity_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp, queue_capacity=1, overflow="drop", batch_size=1)
            entered = threading.Event()
            release = threading.Event()
            original = log._sink.write_lines

            def blocked(lines):
                entered.set()
                release.wait(2)
                original(lines)

            with mock.patch.object(log._sink, "write_lines", side_effect=blocked):
                self.assertEqual(self._record(log, "/one"), "accepted")
                self.assertTrue(entered.wait(1))
                self.assertEqual(self._record(log, "/two"), "accepted")
                self.assertEqual(self._record(log, "/three"), "dropped_capacity")
                snapshot = log.snapshot()
                self.assertEqual(snapshot.high_water, 2)
                self.assertEqual(snapshot.dropped_capacity, 1)
                release.set()
                deadline = time.monotonic() + 1
                while log.snapshot().written < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertEqual(self._record(log, "/four"), "accepted")
            snapshot = log.close(timeout=2)
            self.assertEqual(snapshot.written, 3)
            self.assertEqual(snapshot.dropped_capacity, 1)

    def test_block_policy_backpressures_then_unblocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp, queue_capacity=0, overflow="block", batch_size=1)
            entered = threading.Event()
            release = threading.Event()
            original = log._sink.write_lines

            def blocked(lines):
                entered.set()
                release.wait(2)
                original(lines)

            with mock.patch.object(log._sink, "write_lines", side_effect=blocked):
                self.assertEqual(self._record(log, "/one"), "accepted")
                self.assertTrue(entered.wait(1))
                result: list[_accesslog.AccessLogResult] = []
                producer = threading.Thread(target=lambda: result.append(self._record(log, "/two")))
                producer.start()
                time.sleep(0.02)
                self.assertTrue(producer.is_alive())
                release.set()
                producer.join(1)
                self.assertFalse(producer.is_alive())
                self.assertEqual(result, [_accesslog.AccessLogResult.ACCEPTED])
            self.assertEqual(log.close(timeout=2).written, 2)

    def test_byte_budget_rejects_impossible_record_without_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp, queue_byte_capacity=300, overflow="block")
            result = log.record("client", "GET / HTTP/1.1", 200, 1, user_agent="x" * 1000)
            self.assertEqual(result, _accesslog.AccessLogResult.DROPPED_BYTES)
            snapshot = log.close(timeout=2)
            self.assertEqual(snapshot.accepted, 0)
            self.assertEqual(snapshot.dropped_bytes, 1)
            self.assertEqual(snapshot.byte_high_water, 0)

    def test_sink_failure_latches_and_rejects_later_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp, batch_wait=0)
            with (
                mock.patch.object(log._sink, "write_lines", side_effect=OSError("full")),
                self.assertLogs("servery", level="ERROR"),
            ):
                self.assertEqual(self._record(log), "accepted")
                deadline = time.monotonic() + 1
                while not log.snapshot().sink_failed and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertEqual(self._record(log), _accesslog.AccessLogResult.SINK_FAILED)
            snapshot = log.close(timeout=2)
            self.assertEqual(snapshot.write_failed, 1)
            self.assertEqual(snapshot.rejected_sink_failed, 1)
            self.assertEqual(snapshot.accepted, snapshot.write_failed + snapshot.abandoned)

    def test_sink_failure_accounts_for_queued_records_as_abandoned(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp, queue_capacity=2, batch_size=1, batch_wait=0)
            entered = threading.Event()
            release = threading.Event()

            def fail(_lines):
                entered.set()
                release.wait(2)
                raise OSError("full")

            with (
                mock.patch.object(log._sink, "write_lines", side_effect=fail),
                self.assertLogs("servery", level="ERROR"),
            ):
                self.assertEqual(self._record(log, "/one"), "accepted")
                self.assertTrue(entered.wait(1))
                self.assertEqual(self._record(log, "/two"), "accepted")
                self.assertEqual(self._record(log, "/three"), "accepted")
                release.set()
                deadline = time.monotonic() + 1
                while not log.snapshot().sink_failed and time.monotonic() < deadline:
                    time.sleep(0.01)
            snapshot = log.close(timeout=2)
            self.assertEqual(snapshot.write_failed, 1)
            self.assertEqual(snapshot.abandoned, 2)
            self.assertEqual(snapshot.accepted, 3)
            self.assertEqual(snapshot.outstanding_bytes, 0)

    def test_close_timeout_is_bounded_and_later_close_observes_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp, batch_wait=0)
            entered = threading.Event()
            release = threading.Event()

            def blocked(_lines):
                entered.set()
                release.wait(2)

            with mock.patch.object(log._sink, "write_lines", side_effect=blocked):
                self.assertEqual(self._record(log), "accepted")
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                snapshot = log.close(timeout=0.01)
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertTrue(snapshot.writer_alive)
                self.assertEqual(self._record(log), _accesslog.AccessLogResult.CLOSED)
                release.set()
            snapshot = log.close(timeout=2)
            self.assertFalse(snapshot.writer_alive)
            self.assertTrue(snapshot.closed)

    def test_close_wakes_a_blocked_producer(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp, queue_capacity=0, overflow="block", batch_size=1)
            entered = threading.Event()
            release = threading.Event()

            def blocked(_lines):
                entered.set()
                release.wait(2)

            with mock.patch.object(log._sink, "write_lines", side_effect=blocked):
                self.assertEqual(self._record(log, "/one"), "accepted")
                self.assertTrue(entered.wait(1))
                result: list[_accesslog.AccessLogResult] = []
                producer = threading.Thread(target=lambda: result.append(self._record(log, "/two")))
                producer.start()
                time.sleep(0.02)
                self.assertTrue(producer.is_alive())
                self.assertTrue(log.close(timeout=0.01).writer_alive)
                producer.join(1)
                self.assertEqual(result, [_accesslog.AccessLogResult.CLOSED])
                release.set()
            self.assertFalse(log.close(timeout=2).writer_alive)

    def test_concurrent_producers_conserve_delivery_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(tmp, queue_capacity=16, overflow="block", batch_wait=0)

            def produce(worker: int) -> None:
                for index in range(50):
                    self.assertEqual(self._record(log, f"/{worker}/{index}"), "accepted")

            threads = [threading.Thread(target=produce, args=(worker,)) for worker in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
            snapshot = log.close(timeout=5)
            self.assertEqual(snapshot.accepted, 800)
            self.assertEqual(snapshot.written, 800)
            self.assertEqual(snapshot.write_failed, 0)
            self.assertEqual(snapshot.abandoned, 0)
            self.assertEqual(snapshot.dropped_capacity + snapshot.dropped_bytes, 0)

    def test_invalid_policy_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "access.log")
            with self.assertRaises(ValueError):
                _accesslog.AsyncAccessLog(path, queue_capacity=-1)
            with self.assertRaises(ValueError):
                _accesslog.AsyncAccessLog(path, queue_byte_capacity=0)
            with self.assertRaises(ValueError):
                _accesslog.AsyncAccessLog(path, overflow="stderr")
            with self.assertRaises(ValueError):
                _accesslog.AsyncAccessLog(path, batch_size=0)
            with self.assertRaises(ValueError):
                _accesslog.AsyncAccessLog(path, batch_wait=-1)
            log = _accesslog.AsyncAccessLog(path)
            with self.assertRaises(ValueError):
                log.close(timeout=-1)
            log.close(timeout=2)


class IntegrationTest(unittest.TestCase):
    def test_instances_own_independent_files_and_close_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first.log"
            second_path = Path(tmp) / "second.log"
            first = _accesslog.AccessLog(str(first_path), "clf")
            second = _accesslog.AccessLog(str(second_path), "clf")
            first.record("a", "GET /one HTTP/1.1", 200, 1, when=_WHEN)
            second.record("b", "GET /two HTTP/1.1", 200, 2, when=_WHEN)
            first.close()
            second.record("b", "GET /three HTTP/1.1", 200, 3, when=_WHEN)
            second.close()
            self.assertIn("/one", first_path.read_text())
            self.assertNotIn("/two", first_path.read_text())
            self.assertEqual(second_path.read_text().count("GET /"), 2)

    def test_failed_bind_does_not_open_access_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "access.log"
            occupied = socket.socket()
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            self.addCleanup(occupied.close)
            cfg = Config.create(
                tmp,
                host="127.0.0.1",
                port=occupied.getsockname()[1],
                quiet=True,
                access_log=str(log_path),
            )
            with self.assertRaises(OSError):
                make_server(cfg, port_scan=0)
            self.assertFalse(log_path.exists())

    def test_requests_are_logged_to_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "f.txt").write_text("x" * 17)
        log_path = root / "access.log"
        cfg = Config.create(
            str(root), host="127.0.0.1", port=0, quiet=True, access_log=str(log_path)
        )
        with serving(cfg) as (host, port):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/f.txt")
            conn.getresponse().read()
            conn.request("GET", "/nope")
            conn.getresponse().read()
            conn.close()
        time.sleep(0.05)  # let the file handler flush
        lines = log_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r'"GET /f\.txt HTTP/1\.1" 200 17')  # real response size
        self.assertTrue(re.search(r'"GET /nope HTTP/1\.1" 404 ', lines[1]))

    def test_server_keeps_sync_default_and_selects_async_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            async_server = make_server(
                Config.create(
                    tmp,
                    host="127.0.0.1",
                    port=0,
                    quiet=True,
                    access_log=str(Path(tmp) / "async.log"),
                    access_log_queue=256,
                )
            )
            try:
                self.assertIsInstance(async_server.access_log, _accesslog.AsyncAccessLog)
            finally:
                async_server.server_close()

            sync_server = make_server(
                Config.create(
                    tmp,
                    host="127.0.0.1",
                    port=0,
                    quiet=True,
                    access_log=str(Path(tmp) / "sync.log"),
                )
            )
            try:
                self.assertIsInstance(sync_server.access_log, _accesslog.AccessLog)
                self.assertNotIsInstance(sync_server.access_log, _accesslog.AsyncAccessLog)
            finally:
                sync_server.server_close()


if __name__ == "__main__":
    unittest.main()
