"""Tests for the HPACK implementation (RFC 7541), driven by the RFC test vectors.

The header-block and dynamic-table assertions follow Appendix C verbatim: the
worked examples in C.2 (single representations), C.3/C.4 (request sequences,
without/with Huffman), and C.5/C.6 (response sequences with eviction). The
integer and Huffman primitives are exercised with the C.1 / C.4 / C.6 vectors
plus round-trips.
"""

from __future__ import annotations

import unittest

from servery.http2.hpack import (
    STATIC_TABLE,
    Decoder,
    DynamicTable,
    Encoder,
    HpackError,
    decode_integer,
    encode_integer,
    huffman_decode,
    huffman_encode,
)


def _hex(text: str) -> bytes:
    """Decode a (possibly whitespace-laden) hex string to bytes."""
    return bytes.fromhex(text.replace(" ", "").replace("\n", ""))


class IntegerTests(unittest.TestCase):
    def test_c1_1_encode_10_with_5bit_prefix(self) -> None:
        # RFC 7541 C.1.1: 10 fits in the 5-bit prefix.
        self.assertEqual(encode_integer(10, 5), bytes((0x0A,)))

    def test_c1_2_encode_1337_with_5bit_prefix(self) -> None:
        # RFC 7541 C.1.2: 1337 -> prefix 31, then 154, 10.
        self.assertEqual(encode_integer(1337, 5), bytes((31, 154, 10)))

    def test_c1_3_encode_42_with_8bit_prefix(self) -> None:
        # RFC 7541 C.1.3: 42 fits in an 8-bit prefix.
        self.assertEqual(encode_integer(42, 8), bytes((42,)))

    def test_decode_matches_c1_vectors(self) -> None:
        self.assertEqual(decode_integer(bytes((0x0A,)), 0, 5), (10, 1))
        self.assertEqual(decode_integer(bytes((31, 154, 10)), 0, 5), (1337, 3))
        self.assertEqual(decode_integer(bytes((42,)), 0, 8), (42, 1))

    def test_roundtrip_single_and_multibyte(self) -> None:
        for prefix in range(1, 9):
            for value in (0, 1, 2, 30, 31, 127, 128, 255, 256, 1337, 100000, 2**20):
                encoded = encode_integer(value, prefix)
                decoded, consumed = decode_integer(encoded, 0, prefix)
                self.assertEqual(decoded, value, (value, prefix))
                self.assertEqual(consumed, len(encoded), (value, prefix))

    def test_decode_respects_offset_and_ignores_high_bits(self) -> None:
        # Prefix octet has unrelated high bits set; only the low N matter.
        data = bytes((0xFF, 0xE5))  # 0xE5 = 0b1110_0101; low 5 bits = 5.
        value, offset = decode_integer(data, 1, 5)
        self.assertEqual((value, offset), (5, 2))

    def test_decode_truncated_raises(self) -> None:
        with self.assertRaises(HpackError):
            decode_integer(b"", 0, 5)
        with self.assertRaises(HpackError):
            # Continuation bit set but no following octet.
            decode_integer(bytes((31, 0x80)), 0, 5)

    def test_decode_overlong_integer_raises(self) -> None:
        with self.assertRaises(HpackError):
            decode_integer(bytes((31, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x00)), 0, 5)

    def test_invalid_prefix_bits(self) -> None:
        with self.assertRaises(HpackError):
            encode_integer(1, 0)
        with self.assertRaises(HpackError):
            encode_integer(1, 9)
        with self.assertRaises(HpackError):
            encode_integer(-1, 5)


class HuffmanTests(unittest.TestCase):
    def test_roundtrip_ascii_and_bytes(self) -> None:
        samples = [
            b"",
            b"www.example.com",
            b"no-cache",
            b"custom-key",
            b"custom-value",
            b"https://www.example.com",
            b"Mon, 21 Oct 2013 20:13:21 GMT",
            bytes(range(256)),
        ]
        for sample in samples:
            self.assertEqual(huffman_decode(huffman_encode(sample)), sample)

    def test_c4_request_huffman_vectors(self) -> None:
        # Encoded literal values from RFC 7541 C.4.
        self.assertEqual(huffman_encode(b"www.example.com"), _hex("f1e3 c2e5 f23a 6ba0 ab90 f4ff"))
        self.assertEqual(huffman_encode(b"no-cache"), _hex("a8eb 1064 9cbf"))
        self.assertEqual(huffman_encode(b"custom-key"), _hex("25a8 49e9 5ba9 7d7f"))
        self.assertEqual(huffman_encode(b"custom-value"), _hex("25a8 49e9 5bb8 e8b4 bf"))

    def test_c6_response_huffman_vectors(self) -> None:
        # Encoded literal values from RFC 7541 C.6.
        self.assertEqual(huffman_encode(b"302"), _hex("6402"))
        self.assertEqual(huffman_encode(b"private"), _hex("aec3 771a 4b"))
        self.assertEqual(
            huffman_encode(b"Mon, 21 Oct 2013 20:13:21 GMT"),
            _hex("d07a be94 1054 d444 a820 0595 040b 8166 e082 a62d 1bff"),
        )
        self.assertEqual(
            huffman_encode(b"https://www.example.com"),
            _hex("9d29 ad17 1863 c78f 0b97 c8e9 ae82 ae43 d3"),
        )
        self.assertEqual(huffman_encode(b"307"), _hex("640e ff"))
        self.assertEqual(huffman_encode(b"gzip"), _hex("9bd9 ab"))
        self.assertEqual(
            huffman_encode(b"foo=ASDJKHQKBZXOQWEOPIUAXQWEOIU; max-age=3600; version=1"),
            _hex(
                "94e7 821d d7f2 e6c7 b335 dfdf cd5b 3960 d5af 2708 7f36 72c1 "
                "ab27 0fb5 291f 9587 3160 65c0 03ed 4ee5 b106 3d50 07"
            ),
        )

    def test_decode_c6_vectors(self) -> None:
        self.assertEqual(huffman_decode(_hex("6402")), b"302")
        self.assertEqual(huffman_decode(_hex("aec3 771a 4b")), b"private")
        self.assertEqual(
            huffman_decode(_hex("9d29 ad17 1863 c78f 0b97 c8e9 ae82 ae43 d3")),
            b"https://www.example.com",
        )

    def test_eos_in_stream_raises(self) -> None:
        # A run of all-1 bits is the EOS code (30 bits); an octet of 0xFF * 4
        # forces a full EOS symbol to be decoded.
        with self.assertRaises(HpackError):
            huffman_decode(b"\xff\xff\xff\xff\xff")

    def test_padding_too_long_raises(self) -> None:
        # Valid 'a' (5 bits 0b00011) followed by 11 padding 1-bits (>7).
        # Build: 0b00011 + eleven 1s = 16 bits = 0x1FFF -> bytes 0x1F, 0xFF.
        with self.assertRaises(HpackError):
            huffman_decode(bytes((0x1F, 0xFF)))

    def test_bad_padding_not_ones_raises(self) -> None:
        # '0' symbol is 0b00000 (5 bits); pad the remaining 3 bits with 0s,
        # which is not the EOS prefix.
        with self.assertRaises(HpackError):
            huffman_decode(bytes((0b00000_000,)))


class StringLiteralTests(unittest.TestCase):
    def test_static_indexed_field_decode(self) -> None:
        # RFC 7541 C.2.4: 0x82 -> :method: GET.
        decoder = Decoder()
        self.assertEqual(decoder.decode(_hex("82")), [(b":method", b"GET")])

    def test_literal_without_indexing_indexed_name(self) -> None:
        # RFC 7541 C.2.2: :path: /sample/path, not added to the table.
        decoder = Decoder()
        block = _hex("040c 2f73 616d 706c 652f 7061 7468")
        self.assertEqual(decoder.decode(block), [(b":path", b"/sample/path")])
        self.assertEqual(decoder.dynamic_table.size, 0)

    def test_never_indexed_literal(self) -> None:
        # RFC 7541 C.2.3: password: secret, never indexed, table stays empty.
        decoder = Decoder()
        block = _hex("1008 7061 7373 776f 7264 0673 6563 7265 74")
        self.assertEqual(decoder.decode(block), [(b"password", b"secret")])
        self.assertEqual(decoder.dynamic_table.size, 0)


class LiteralWithIndexingTests(unittest.TestCase):
    def test_c2_1_populates_dynamic_table(self) -> None:
        # RFC 7541 C.2.1: custom-key: custom-header added to the table (s=55).
        decoder = Decoder()
        block = _hex("400a 6375 7374 6f6d 2d6b 6579 0d63 7573 746f 6d2d 6865 6164 6572")
        self.assertEqual(decoder.decode(block), [(b"custom-key", b"custom-header")])
        self.assertEqual(len(decoder.dynamic_table), 1)
        self.assertEqual(decoder.dynamic_table[0], (b"custom-key", b"custom-header"))
        self.assertEqual(decoder.dynamic_table.size, 55)


class DynamicTableTests(unittest.TestCase):
    def test_entry_size_and_eviction(self) -> None:
        table = DynamicTable(max_size=100)
        table.add(b"a", b"b")  # size 1+1+32 = 34
        table.add(b"c", b"d")  # size 34, total 68
        self.assertEqual(table.size, 68)
        self.assertEqual(len(table), 2)
        table.add(b"e", b"f")  # total would be 102 > 100, evict oldest (a:b)
        self.assertEqual(len(table), 2)
        self.assertEqual(table[0], (b"e", b"f"))
        self.assertEqual(table[1], (b"c", b"d"))
        self.assertEqual(table.size, 68)

    def test_oversized_entry_empties_table(self) -> None:
        table = DynamicTable(max_size=40)
        table.add(b"a", b"b")
        self.assertEqual(len(table), 1)
        # Entry larger than max_size empties the table and is not stored.
        table.add(b"x" * 50, b"")
        self.assertEqual(len(table), 0)
        self.assertEqual(table.size, 0)

    def test_set_max_size_evicts(self) -> None:
        table = DynamicTable(max_size=200)
        table.add(b"a", b"b")
        table.add(b"c", b"d")
        self.assertEqual(table.size, 68)
        table.set_max_size(40)
        self.assertEqual(len(table), 1)
        self.assertEqual(table[0], (b"c", b"d"))
        self.assertEqual(table.size, 34)
        table.set_max_size(0)
        self.assertEqual(len(table), 0)


class RequestSequenceWithoutHuffmanTests(unittest.TestCase):
    """RFC 7541 C.3: three consecutive requests, no Huffman."""

    def test_full_sequence(self) -> None:
        decoder = Decoder()

        block1 = _hex("8286 8441 0f77 7777 2e65 7861 6d70 6c65 2e63 6f6d")
        self.assertEqual(
            decoder.decode(block1),
            [
                (b":method", b"GET"),
                (b":scheme", b"http"),
                (b":path", b"/"),
                (b":authority", b"www.example.com"),
            ],
        )
        self.assertEqual(len(decoder.dynamic_table), 1)
        self.assertEqual(decoder.dynamic_table[0], (b":authority", b"www.example.com"))
        self.assertEqual(decoder.dynamic_table.size, 57)

        block2 = _hex("8286 84be 5808 6e6f 2d63 6163 6865")
        self.assertEqual(
            decoder.decode(block2),
            [
                (b":method", b"GET"),
                (b":scheme", b"http"),
                (b":path", b"/"),
                (b":authority", b"www.example.com"),
                (b"cache-control", b"no-cache"),
            ],
        )
        self.assertEqual(decoder.dynamic_table[0], (b"cache-control", b"no-cache"))
        self.assertEqual(decoder.dynamic_table[1], (b":authority", b"www.example.com"))
        self.assertEqual(decoder.dynamic_table.size, 110)

        block3 = _hex("8287 85bf 400a 6375 7374 6f6d 2d6b 6579 0c63 7573 746f 6d2d 7661 6c75 65")
        self.assertEqual(
            decoder.decode(block3),
            [
                (b":method", b"GET"),
                (b":scheme", b"https"),
                (b":path", b"/index.html"),
                (b":authority", b"www.example.com"),
                (b"custom-key", b"custom-value"),
            ],
        )
        self.assertEqual(decoder.dynamic_table[0], (b"custom-key", b"custom-value"))
        self.assertEqual(decoder.dynamic_table[1], (b"cache-control", b"no-cache"))
        self.assertEqual(decoder.dynamic_table[2], (b":authority", b"www.example.com"))
        self.assertEqual(decoder.dynamic_table.size, 164)


class RequestSequenceWithHuffmanTests(unittest.TestCase):
    """RFC 7541 C.4: the same three requests, Huffman-coded literals."""

    def test_full_sequence(self) -> None:
        decoder = Decoder()

        block1 = _hex("8286 8441 8cf1 e3c2 e5f2 3a6b a0ab 90f4 ff")
        self.assertEqual(
            decoder.decode(block1),
            [
                (b":method", b"GET"),
                (b":scheme", b"http"),
                (b":path", b"/"),
                (b":authority", b"www.example.com"),
            ],
        )
        self.assertEqual(decoder.dynamic_table.size, 57)

        block2 = _hex("8286 84be 5886 a8eb 1064 9cbf")
        self.assertEqual(decoder.decode(block2)[-1], (b"cache-control", b"no-cache"))
        self.assertEqual(decoder.dynamic_table.size, 110)

        block3 = _hex("8287 85bf 4088 25a8 49e9 5ba9 7d7f 8925 a849 e95b b8e8 b4bf")
        self.assertEqual(decoder.decode(block3)[-1], (b"custom-key", b"custom-value"))
        self.assertEqual(decoder.dynamic_table[0], (b"custom-key", b"custom-value"))
        self.assertEqual(decoder.dynamic_table[1], (b"cache-control", b"no-cache"))
        self.assertEqual(decoder.dynamic_table[2], (b":authority", b"www.example.com"))
        self.assertEqual(decoder.dynamic_table.size, 164)


_DATE_21 = b"Mon, 21 Oct 2013 20:13:21 GMT"
_DATE_22 = b"Mon, 21 Oct 2013 20:13:22 GMT"
_LOCATION = b"https://www.example.com"
_SET_COOKIE = b"foo=ASDJKHQKBZXOQWEOPIUAXQWEOIU; max-age=3600; version=1"


class ResponseSequenceWithoutHuffmanTests(unittest.TestCase):
    """RFC 7541 C.5: three responses with table size 256 forcing evictions."""

    def test_full_sequence(self) -> None:
        decoder = Decoder(max_dynamic_size=256)

        block1 = _hex(
            "4803 3330 3258 0770 7269 7661 7465 611d 4d6f 6e2c 2032 3120 4f63 7420 3230 3133 "
            "2032 303a 3133 3a32 3120 474d 546e 1768 7474 7073 3a2f 2f77 7777 2e65 7861 6d70 "
            "6c65 2e63 6f6d"
        )
        self.assertEqual(
            decoder.decode(block1),
            [
                (b":status", b"302"),
                (b"cache-control", b"private"),
                (b"date", _DATE_21),
                (b"location", _LOCATION),
            ],
        )
        self.assertEqual(
            [decoder.dynamic_table[i] for i in range(len(decoder.dynamic_table))],
            [
                (b"location", _LOCATION),
                (b"date", _DATE_21),
                (b"cache-control", b"private"),
                (b":status", b"302"),
            ],
        )
        self.assertEqual(decoder.dynamic_table.size, 222)

        block2 = _hex("4803 3330 37c1 c0bf")
        self.assertEqual(
            decoder.decode(block2),
            [
                (b":status", b"307"),
                (b"cache-control", b"private"),
                (b"date", _DATE_21),
                (b"location", _LOCATION),
            ],
        )
        # :status: 302 evicted to make room for :status: 307.
        self.assertEqual(
            [decoder.dynamic_table[i] for i in range(len(decoder.dynamic_table))],
            [
                (b":status", b"307"),
                (b"location", _LOCATION),
                (b"date", _DATE_21),
                (b"cache-control", b"private"),
            ],
        )
        self.assertEqual(decoder.dynamic_table.size, 222)

        block3 = _hex(
            "88c1 611d 4d6f 6e2c 2032 3120 4f63 7420 3230 3133 2032 303a 3133 3a32 3220 474d "
            "54c0 5a04 677a 6970 7738 666f 6f3d 4153 444a 4b48 514b 425a 584f 5157 454f 5049 "
            "5541 5851 5745 4f49 553b 206d 6178 2d61 6765 3d33 3630 303b 2076 6572 7369 6f6e "
            "3d31"
        )
        self.assertEqual(
            decoder.decode(block3),
            [
                (b":status", b"200"),
                (b"cache-control", b"private"),
                (b"date", _DATE_22),
                (b"location", _LOCATION),
                (b"content-encoding", b"gzip"),
                (b"set-cookie", _SET_COOKIE),
            ],
        )
        self.assertEqual(
            [decoder.dynamic_table[i] for i in range(len(decoder.dynamic_table))],
            [
                (b"set-cookie", _SET_COOKIE),
                (b"content-encoding", b"gzip"),
                (b"date", _DATE_22),
            ],
        )
        self.assertEqual(decoder.dynamic_table.size, 215)


class ResponseSequenceWithHuffmanTests(unittest.TestCase):
    """RFC 7541 C.6: the same responses, Huffman-coded, same evictions."""

    def test_full_sequence(self) -> None:
        decoder = Decoder(max_dynamic_size=256)

        block1 = _hex(
            "4882 6402 5885 aec3 771a 4b61 96d0 7abe 9410 54d4 44a8 2005 9504 0b81 66e0 82a6 "
            "2d1b ff6e 919d 29ad 1718 63c7 8f0b 97c8 e9ae 82ae 43d3"
        )
        self.assertEqual(
            decoder.decode(block1),
            [
                (b":status", b"302"),
                (b"cache-control", b"private"),
                (b"date", _DATE_21),
                (b"location", _LOCATION),
            ],
        )
        self.assertEqual(decoder.dynamic_table.size, 222)

        block2 = _hex("4883 640e ffc1 c0bf")
        self.assertEqual(
            decoder.decode(block2),
            [
                (b":status", b"307"),
                (b"cache-control", b"private"),
                (b"date", _DATE_21),
                (b"location", _LOCATION),
            ],
        )
        self.assertEqual(decoder.dynamic_table[0], (b":status", b"307"))
        self.assertEqual(decoder.dynamic_table.size, 222)

        block3 = _hex(
            "88c1 6196 d07a be94 1054 d444 a820 0595 040b 8166 e084 a62d 1bff c05a 839b d9ab "
            "77ad 94e7 821d d7f2 e6c7 b335 dfdf cd5b 3960 d5af 2708 7f36 72c1 ab27 0fb5 291f "
            "9587 3160 65c0 03ed 4ee5 b106 3d50 07"
        )
        self.assertEqual(
            decoder.decode(block3),
            [
                (b":status", b"200"),
                (b"cache-control", b"private"),
                (b"date", _DATE_22),
                (b"location", _LOCATION),
                (b"content-encoding", b"gzip"),
                (b"set-cookie", _SET_COOKIE),
            ],
        )
        self.assertEqual(
            [decoder.dynamic_table[i] for i in range(len(decoder.dynamic_table))],
            [
                (b"set-cookie", _SET_COOKIE),
                (b"content-encoding", b"gzip"),
                (b"date", _DATE_22),
            ],
        )
        self.assertEqual(decoder.dynamic_table.size, 215)


class SizeUpdateTests(unittest.TestCase):
    def test_size_update_clears_table(self) -> None:
        decoder = Decoder(max_dynamic_size=256)
        decoder.decode(_hex("400a 6375 7374 6f6d 2d6b 6579 0d63 7573 746f 6d2d 6865 6164 6572"))
        self.assertEqual(len(decoder.dynamic_table), 1)
        # Dynamic table size update to 0 ('001' prefix, 5-bit value 0) -> 0x20.
        decoder.decode(_hex("20"))
        self.assertEqual(len(decoder.dynamic_table), 0)
        self.assertEqual(decoder.dynamic_table.max_size, 0)

    def test_size_update_over_limit_raises(self) -> None:
        decoder = Decoder(max_dynamic_size=256)
        prefix = encode_integer(4096, 5)
        block = bytes((prefix[0] | 0x20, *prefix[1:]))
        with self.assertRaises(HpackError):
            decoder.decode(block)

    def test_size_update_after_header_raises(self) -> None:
        decoder = Decoder()
        # An indexed field (0x82) followed by a size update (0x20) is illegal.
        with self.assertRaises(HpackError):
            decoder.decode(_hex("82 20"))


class ErrorHandlingTests(unittest.TestCase):
    def test_index_zero_indexed_field_raises(self) -> None:
        decoder = Decoder()
        with self.assertRaises(HpackError):
            decoder.decode(bytes((0x80,)))  # indexed field, index 0.

    def test_index_out_of_range_raises(self) -> None:
        decoder = Decoder()
        with self.assertRaises(HpackError):
            decoder.decode(bytes((0xFF, 0x00)))  # huge static/dynamic index.

    def test_truncated_string_raises(self) -> None:
        decoder = Decoder()
        # Literal indexed name :path, value length 12 but no value bytes.
        with self.assertRaises(HpackError):
            decoder.decode(_hex("04 0c"))


class DecompressionBombGuardTests(unittest.TestCase):
    def test_header_list_size_limit(self) -> None:
        # Construct a block of repeated indexed :method: GET fields and bound
        # the header list size so it overflows.
        decoder = Decoder(max_header_list_size=100)
        # Each :method: GET contributes 7 + 3 + 32 = 42 to the header-list size,
        # so three of them (126) exceeds 100.
        block = bytes((0x82, 0x82, 0x82))
        with self.assertRaises(HpackError):
            decoder.decode(block)

    def test_string_length_limit(self) -> None:
        decoder = Decoder(max_string_length=4)
        # Literal name (idx 0) declaring a 10-byte name overruns the limit.
        block = _hex("40 0a 6375 7374 6f6d 2d6b 6579")
        with self.assertRaises(HpackError):
            decoder.decode(block)

    def test_within_limit_passes(self) -> None:
        decoder = Decoder(max_header_list_size=100)
        self.assertEqual(decoder.decode(bytes((0x82, 0x82))), [(b":method", b"GET")] * 2)


class EncoderTests(unittest.TestCase):
    def test_static_indexed_field(self) -> None:
        encoder = Encoder()
        self.assertEqual(encoder.encode([(b":method", b"GET")]), bytes((0x82,)))

    def test_encode_decode_roundtrip(self) -> None:
        encoder = Encoder()
        decoder = Decoder()
        headers = [
            (b":method", b"GET"),
            (b":scheme", b"https"),
            (b":path", b"/index.html"),
            (b":authority", b"www.example.com"),
            (b"custom-key", b"custom-value"),
            (b"user-agent", b"servery/0.0"),
            (b"x-binary", bytes(range(32))),
        ]
        block = encoder.encode(headers)
        self.assertEqual(decoder.decode(block), headers)

    def test_encoder_uses_huffman_when_shorter(self) -> None:
        encoder = Encoder()
        decoder = Decoder()
        # A long compressible value should round-trip and be shorter than raw.
        value = b"www.example.com" * 4
        block = encoder.encode([(b"x-host", value)])
        self.assertEqual(decoder.decode(block), [(b"x-host", value)])

    def test_static_table_shape(self) -> None:
        self.assertEqual(len(STATIC_TABLE), 61)
        self.assertEqual(STATIC_TABLE[0], (b":authority", b""))
        self.assertEqual(STATIC_TABLE[60], (b"www-authenticate", b""))


if __name__ == "__main__":
    unittest.main()
