"""QR encoder tests. Correctness is pinned bit-for-bit against the ``qrcode`` lib."""

from __future__ import annotations

import unittest

from servery import _qr

try:
    import qrcode

    _HAVE_QRCODE = True
except ImportError:  # pragma: no cover
    _HAVE_QRCODE = False

# URLs sized to land on each version 1-10 (byte capacity at level L).
_BY_VERSION = {1: 15, 2: 30, 3: 50, 4: 75, 5: 100, 6: 130, 7: 150, 8: 190, 9: 225, 10: 270}


def _url(length: int) -> str:
    return ("http://x/" + "a" * length)[:length]


class EncoderUnitTest(unittest.TestCase):
    def test_rs_generator_alpha_exponents(self):
        # The degree-7 generator polynomial's known alpha-exponents (ISO/IEC 18004).
        gen = _qr._rs_generator(7)
        self.assertEqual([_qr._LOG[c] for c in gen], [0, 87, 229, 146, 149, 238, 102, 21])

    def test_version_selection(self):
        for version, length in _BY_VERSION.items():
            self.assertEqual(_qr._pick_version(len(_url(length).encode())), version)

    def test_too_long_raises(self):
        with self.assertRaises(_qr.QrError):
            _qr.generate("x" * 272)  # exceeds v10-L capacity (271)

    def test_render_shape(self):
        matrix = _qr.generate("http://127.0.0.1:8000/")
        rendered = _qr.render(matrix, quiet=2)
        # Two module-rows per text line, plus the quiet zone; non-empty lines.
        self.assertEqual(len(rendered.splitlines()), (len(matrix) + 2 * 2 + 1) // 2)
        self.assertTrue(all(rendered.splitlines()))


@unittest.skipUnless(_HAVE_QRCODE, "qrcode oracle not installed")
class OracleTest(unittest.TestCase):
    def _oracle(self, url: str, version: int, mask: int) -> list[list[int]]:
        q = qrcode.QRCode(
            version=version,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=0,
            mask_pattern=mask,
        )
        q.add_data(url, optimize=0)
        q.make(fit=False)
        return [[1 if cell else 0 for cell in row] for row in q.get_matrix()]

    def test_matches_oracle_all_versions_and_masks(self):
        for version, length in _BY_VERSION.items():
            url = _url(length)
            for mask in range(8):
                self.assertEqual(
                    _qr.generate(url, mask=mask),
                    self._oracle(url, version, mask),
                    f"v{version} mask{mask}",
                )

    def test_auto_mask_is_a_valid_mask(self):
        # The auto-selected matrix must equal the oracle at *some* mask (any of the 8
        # is scannable; the format info records which one).
        url = "http://192.168.1.50:8000/"
        version = _qr._pick_version(len(url.encode()))
        auto = _qr.generate(url)
        self.assertTrue(any(auto == self._oracle(url, version, m) for m in range(8)))


if __name__ == "__main__":
    unittest.main()
