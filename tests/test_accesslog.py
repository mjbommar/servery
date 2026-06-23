"""Access-log tests: the CLF/combined/JSON formatters + the end-to-end file write."""

from __future__ import annotations

import http.client
import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from servery import _accesslog
from servery.config import Config
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
        for handler in log._logger.handlers:
            handler.flush()
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


class IntegrationTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
