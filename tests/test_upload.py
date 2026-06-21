"""Unit tests for the streaming multipart upload parser."""

import io
import tempfile
import unittest
from pathlib import Path

from servery import upload


def _multipart(boundary: str, parts: list[tuple[str, str | None, bytes]]) -> bytes:
    chunks: list[bytes] = []
    for name, filename, content in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        chunks.append(f"--{boundary}\r\nContent-Disposition: {disposition}\r\n\r\n".encode())
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


class BoundaryTest(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(upload.extract_boundary("multipart/form-data; boundary=abc"), b"abc")
        self.assertEqual(upload.extract_boundary('multipart/form-data; boundary="a b"'), b"a b")
        self.assertIsNone(upload.extract_boundary("multipart/form-data"))


class SafeNameTest(unittest.TestCase):
    def test_strips_directories(self):
        self.assertEqual(upload._safe_name("../../evil.txt"), "evil.txt")
        self.assertEqual(upload._safe_name(r"C:\\windows\\sys.ini"), "sys.ini")

    def test_rejects_dotted_and_empty(self):
        self.assertIsNone(upload._safe_name(".."))
        self.assertIsNone(upload._safe_name(""))
        self.assertIsNone(upload._safe_name("a\x00b"))


class SaveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _save(self, body: bytes, **kwargs: bool) -> list[upload.SavedFile]:
        return upload.save(io.BytesIO(body), b"B", str(self.dir), **kwargs)

    def test_single_file(self):
        saved = self._save(_multipart("B", [("file", "hello.txt", b"hi there")]))
        self.assertEqual([s.filename for s in saved], ["hello.txt"])
        self.assertEqual(saved[0].size, 8)
        self.assertEqual((self.dir / "hello.txt").read_bytes(), b"hi there")

    def test_multiple_files(self):
        saved = self._save(_multipart("B", [("a", "a.txt", b"AAA"), ("b", "b.bin", b"BBBB")]))
        self.assertEqual(len(saved), 2)
        self.assertEqual((self.dir / "a.txt").read_bytes(), b"AAA")
        self.assertEqual((self.dir / "b.bin").read_bytes(), b"BBBB")

    def test_non_file_field_ignored(self):
        saved = self._save(_multipart("B", [("token", None, b"secret"), ("file", "x.txt", b"d")]))
        self.assertEqual([s.filename for s in saved], ["x.txt"])
        self.assertFalse((self.dir / "token").exists())

    def test_traversal_filename_lands_in_dest(self):
        self._save(_multipart("B", [("file", "../escape.txt", b"x")]))
        self.assertTrue((self.dir / "escape.txt").exists())

    def test_conflict_without_overwrite(self):
        (self.dir / "x.txt").write_text("old")
        with self.assertRaises(upload.UploadConflictError):
            self._save(_multipart("B", [("file", "x.txt", b"new")]))
        self.assertEqual((self.dir / "x.txt").read_text(), "old")

    def test_overwrite_allowed(self):
        (self.dir / "x.txt").write_text("old")
        self._save(_multipart("B", [("file", "x.txt", b"new")]), allow_overwrite=True)
        self.assertEqual((self.dir / "x.txt").read_text(), "new")

    def test_empty_filename_is_skipped(self):
        saved = self._save(_multipart("B", [("file", "", b"ignored"), ("real", "r.txt", b"data")]))
        self.assertEqual([s.filename for s in saved], ["r.txt"])

    def test_unsafe_filename_raises(self):
        with self.assertRaises(upload.UploadError):
            self._save(_multipart("B", [("file", "..", b"x")]))

    def test_unterminated_raises(self):
        body = b'--B\r\nContent-Disposition: form-data; name="f"; filename="x"\r\n\r\nDATA'
        with self.assertRaises(upload.UploadError):
            self._save(body)

    def test_missing_initial_boundary(self):
        with self.assertRaises(upload.UploadError):
            self._save(b"garbage\r\n")


if __name__ == "__main__":
    unittest.main()
