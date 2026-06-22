"""Unit tests for the HTTP/2 frame codec (RFC 9113 §4-6)."""

import unittest

from servery.http2 import frames
from servery.http2.frames import (
    CONNECTION_PREFACE,
    ContinuationFrame,
    DataFrame,
    Flag,
    FrameError,
    FrameReader,
    FrameType,
    GoAwayFrame,
    HeadersFrame,
    PingFrame,
    PriorityFrame,
    ProtocolError,
    RstStreamFrame,
    SettingsFrame,
    SettingsParameter,
    WindowUpdateFrame,
    build_header9,
    parse_frame,
)


def split(wire: bytes) -> tuple[bytes, bytes]:
    """Split a serialized frame into (header9, payload)."""
    return wire[:9], wire[9:]


class PrefaceTest(unittest.TestCase):
    def test_connection_preface_value(self):
        self.assertEqual(CONNECTION_PREFACE, b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")
        self.assertEqual(len(CONNECTION_PREFACE), 24)


class Header9Test(unittest.TestCase):
    def test_build_and_parse_round_trip(self):
        wire = build_header9(0x123456, FrameType.DATA, Flag.END_STREAM, 0x7FFFFFFF)
        self.assertEqual(len(wire), 9)
        header = frames.parse_header9(wire)
        self.assertEqual(header.length, 0x123456)
        self.assertEqual(header.type, FrameType.DATA)
        self.assertEqual(header.flags, Flag.END_STREAM)
        self.assertEqual(header.stream_id, 0x7FFFFFFF)

    def test_reserved_bit_masked_on_parse(self):
        # High bit of the stream id field must be ignored.
        raw = build_header9(0, FrameType.PING, 0, 0)
        raw = raw[:5] + bytes((0x80,)) + raw[6:]
        header = frames.parse_header9(raw)
        self.assertEqual(header.stream_id, 0)

    def test_reserved_bit_cleared_on_build(self):
        wire = build_header9(0, FrameType.DATA, 0, 1)
        self.assertEqual(wire[5] & 0x80, 0)

    def test_length_out_of_range(self):
        with self.assertRaises(FrameError):
            build_header9(1 << 24, FrameType.DATA, 0, 1)

    def test_stream_id_out_of_range(self):
        with self.assertRaises(FrameError):
            build_header9(0, FrameType.DATA, 0, 1 << 31)

    def test_bad_header_length(self):
        with self.assertRaises(FrameError):
            frames.parse_header9(b"\x00\x00")


class DataFrameTest(unittest.TestCase):
    def test_unpadded_round_trip(self):
        frame = DataFrame(1, Flag.END_STREAM, b"hello world")
        h9, payload = split(frames.serialize(frame))
        parsed = parse_frame(h9, payload)
        self.assertEqual(parsed, frame)
        assert isinstance(parsed, DataFrame)
        self.assertTrue(parsed.end_stream)

    def test_padding_stripped(self):
        # PADDED frame: pad length 4, data "abc", 4 zero pad octets.
        payload = bytes((4,)) + b"abc" + b"\x00\x00\x00\x00"
        h9 = build_header9(len(payload), FrameType.DATA, Flag.PADDED, 1)
        parsed = parse_frame(h9, payload)
        assert isinstance(parsed, DataFrame)
        self.assertEqual(parsed.data, b"abc")

    def test_pad_length_zero(self):
        payload = bytes((0,)) + b"abc"
        h9 = build_header9(len(payload), FrameType.DATA, Flag.PADDED, 1)
        parsed = parse_frame(h9, payload)
        assert isinstance(parsed, DataFrame)
        self.assertEqual(parsed.data, b"abc")

    def test_pad_length_consumes_all_data_is_legal(self):
        # Padding may consume the entire remainder: pad length 3 with 3 bytes
        # after the pad-length octet leaves an empty (but valid) data body.
        payload = bytes((3,)) + b"abc"
        h9 = build_header9(len(payload), FrameType.DATA, Flag.PADDED, 1)
        parsed = parse_frame(h9, payload)
        assert isinstance(parsed, DataFrame)
        self.assertEqual(parsed.data, b"")

    def test_pad_length_equals_payload_rejected(self):
        # pad length == full payload length (4) overruns: only 3 bytes remain.
        payload = bytes((4,)) + b"abc"
        h9 = build_header9(len(payload), FrameType.DATA, Flag.PADDED, 1)
        with self.assertRaises(ProtocolError):
            parse_frame(h9, payload)

    def test_pad_length_exceeds_payload_rejected(self):
        payload = bytes((200,)) + b"abc"
        h9 = build_header9(len(payload), FrameType.DATA, Flag.PADDED, 1)
        with self.assertRaises(ProtocolError):
            parse_frame(h9, payload)

    def test_padded_missing_pad_octet(self):
        h9 = build_header9(0, FrameType.DATA, Flag.PADDED, 1)
        with self.assertRaises(ProtocolError):
            parse_frame(h9, b"")

    def test_stream_id_zero_rejected(self):
        h9 = build_header9(3, FrameType.DATA, 0, 0)
        with self.assertRaises(ProtocolError):
            parse_frame(h9, b"abc")


class HeadersFrameTest(unittest.TestCase):
    def test_plain_round_trip(self):
        frame = HeadersFrame(3, Flag.END_HEADERS | Flag.END_STREAM, b"\x82\x84")
        h9, payload = split(frames.serialize(frame))
        parsed = parse_frame(h9, payload)
        self.assertEqual(parsed, frame)
        assert isinstance(parsed, HeadersFrame)
        self.assertTrue(parsed.end_headers)
        self.assertTrue(parsed.end_stream)

    def test_priority_fields_round_trip(self):
        frame = HeadersFrame(
            5,
            Flag.END_HEADERS | Flag.PRIORITY,
            b"block",
            exclusive=True,
            stream_dependency=42,
            weight=16,
        )
        h9, payload = split(frames.serialize(frame))
        parsed = parse_frame(h9, payload)
        assert isinstance(parsed, HeadersFrame)
        self.assertEqual(parsed.header_block, b"block")
        self.assertTrue(parsed.exclusive)
        self.assertEqual(parsed.stream_dependency, 42)
        self.assertEqual(parsed.weight, 16)

    def test_padded_with_priority(self):
        # Pad Length(1) + priority(5) + block + padding.
        priority = (0x80000000 | 7).to_bytes(4, "big") + bytes((9,))
        block = b"hdrs"
        pad = b"\x00\x00"
        payload = bytes((len(pad),)) + priority + block + pad
        h9 = build_header9(len(payload), FrameType.HEADERS, Flag.PADDED | Flag.PRIORITY, 1)
        parsed = parse_frame(h9, payload)
        assert isinstance(parsed, HeadersFrame)
        self.assertEqual(parsed.header_block, b"hdrs")
        self.assertTrue(parsed.exclusive)
        self.assertEqual(parsed.stream_dependency, 7)
        self.assertEqual(parsed.weight, 9)

    def test_priority_block_too_short(self):
        h9 = build_header9(3, FrameType.HEADERS, Flag.PRIORITY, 1)
        with self.assertRaises(FrameError):
            parse_frame(h9, b"abc")

    def test_stream_id_zero_rejected(self):
        h9 = build_header9(0, FrameType.HEADERS, Flag.END_HEADERS, 0)
        with self.assertRaises(ProtocolError):
            parse_frame(h9, b"")

    def test_serialize_clears_padded_flag(self):
        frame = HeadersFrame(1, Flag.PADDED | Flag.END_HEADERS, b"x")
        h9, _ = split(frames.serialize(frame))
        header = frames.parse_header9(h9)
        self.assertFalse(header.flags & Flag.PADDED)


class PriorityFrameTest(unittest.TestCase):
    def test_parse(self):
        payload = (0x80000000 | 3).to_bytes(4, "big") + bytes((11,))
        h9 = build_header9(5, FrameType.PRIORITY, 0, 7)
        parsed = parse_frame(h9, payload)
        self.assertEqual(parsed, PriorityFrame(7, Flag(0), True, 3, 11))

    def test_bad_length(self):
        h9 = build_header9(4, FrameType.PRIORITY, 0, 7)
        with self.assertRaises(FrameError):
            parse_frame(h9, b"\x00\x00\x00\x01")

    def test_stream_id_zero_rejected(self):
        h9 = build_header9(5, FrameType.PRIORITY, 0, 0)
        with self.assertRaises(ProtocolError):
            parse_frame(h9, b"\x00\x00\x00\x01\x05")


class RstStreamFrameTest(unittest.TestCase):
    def test_round_trip(self):
        frame = RstStreamFrame(3, Flag(0), frames.ErrorCode.CANCEL)
        h9, payload = split(frames.serialize(frame))
        parsed = parse_frame(h9, payload)
        assert isinstance(parsed, RstStreamFrame)
        self.assertEqual(parsed.stream_id, 3)
        self.assertEqual(parsed.error_code, frames.ErrorCode.CANCEL)

    def test_bad_length(self):
        h9 = build_header9(3, FrameType.RST_STREAM, 0, 1)
        with self.assertRaises(FrameError):
            parse_frame(h9, b"abc")

    def test_stream_id_zero_rejected(self):
        h9 = build_header9(4, FrameType.RST_STREAM, 0, 0)
        with self.assertRaises(ProtocolError):
            parse_frame(h9, b"\x00\x00\x00\x08")


class SettingsFrameTest(unittest.TestCase):
    def test_defaults_table(self):
        self.assertEqual(frames.SETTINGS_DEFAULTS[SettingsParameter.HEADER_TABLE_SIZE], 4096)
        self.assertEqual(frames.SETTINGS_DEFAULTS[SettingsParameter.ENABLE_PUSH], 1)
        self.assertIsNone(frames.SETTINGS_DEFAULTS[SettingsParameter.MAX_CONCURRENT_STREAMS])
        self.assertEqual(frames.SETTINGS_DEFAULTS[SettingsParameter.INITIAL_WINDOW_SIZE], 65535)
        self.assertEqual(frames.SETTINGS_DEFAULTS[SettingsParameter.MAX_FRAME_SIZE], 16384)
        self.assertIsNone(frames.SETTINGS_DEFAULTS[SettingsParameter.MAX_HEADER_LIST_SIZE])

    def test_multiple_params_round_trip(self):
        frame = SettingsFrame(
            0,
            Flag(0),
            (
                (SettingsParameter.MAX_CONCURRENT_STREAMS, 100),
                (SettingsParameter.INITIAL_WINDOW_SIZE, 65535),
                (SettingsParameter.MAX_FRAME_SIZE, 16384),
            ),
        )
        h9, payload = split(frames.serialize(frame))
        self.assertEqual(len(payload), 18)
        parsed = parse_frame(h9, payload)
        self.assertEqual(parsed, frame)

    def test_ack_round_trip(self):
        ack = frames.settings_ack()
        self.assertTrue(ack.ack)
        h9, payload = split(frames.serialize(ack))
        self.assertEqual(payload, b"")
        parsed = parse_frame(h9, payload)
        assert isinstance(parsed, SettingsFrame)
        self.assertTrue(parsed.ack)
        self.assertEqual(parsed.settings, ())

    def test_ack_with_payload_rejected(self):
        h9 = build_header9(6, FrameType.SETTINGS, Flag.ACK, 0)
        with self.assertRaises(FrameError):
            parse_frame(h9, b"\x00\x01\x00\x00\x00\x01")

    def test_length_not_multiple_of_six(self):
        h9 = build_header9(5, FrameType.SETTINGS, 0, 0)
        with self.assertRaises(FrameError):
            parse_frame(h9, b"abcde")

    def test_stream_id_nonzero_rejected(self):
        h9 = build_header9(0, FrameType.SETTINGS, 0, 1)
        with self.assertRaises(ProtocolError):
            parse_frame(h9, b"")

    def test_serialize_ack_with_params_rejected(self):
        bad = SettingsFrame(0, Flag.ACK, ((1, 2),))
        with self.assertRaises(FrameError):
            frames.serialize(bad)


class WindowUpdateFrameTest(unittest.TestCase):
    def test_round_trip_connection(self):
        frame = WindowUpdateFrame(0, Flag(0), 65535)
        h9, payload = split(frames.serialize(frame))
        parsed = parse_frame(h9, payload)
        self.assertEqual(parsed, frame)

    def test_round_trip_stream(self):
        frame = WindowUpdateFrame(7, Flag(0), 1024)
        h9, payload = split(frames.serialize(frame))
        parsed = parse_frame(h9, payload)
        assert isinstance(parsed, WindowUpdateFrame)
        self.assertEqual(parsed.stream_id, 7)
        self.assertEqual(parsed.window_size_increment, 1024)

    def test_reserved_bit_ignored(self):
        payload = (0x80000000 | 500).to_bytes(4, "big")
        h9 = build_header9(4, FrameType.WINDOW_UPDATE, 0, 1)
        parsed = parse_frame(h9, payload)
        assert isinstance(parsed, WindowUpdateFrame)
        self.assertEqual(parsed.window_size_increment, 500)

    def test_bad_length(self):
        h9 = build_header9(3, FrameType.WINDOW_UPDATE, 0, 1)
        with self.assertRaises(FrameError):
            parse_frame(h9, b"abc")


class PingFrameTest(unittest.TestCase):
    def test_ack_round_trip(self):
        frame = frames.ping_ack(b"12345678")
        self.assertTrue(frame.ack)
        h9, payload = split(frames.serialize(frame))
        parsed = parse_frame(h9, payload)
        assert isinstance(parsed, PingFrame)
        self.assertTrue(parsed.ack)
        self.assertEqual(parsed.opaque_data, b"12345678")

    def test_bad_length(self):
        h9 = build_header9(4, FrameType.PING, 0, 0)
        with self.assertRaises(FrameError):
            parse_frame(h9, b"abcd")

    def test_stream_id_nonzero_rejected(self):
        h9 = build_header9(8, FrameType.PING, 0, 1)
        with self.assertRaises(ProtocolError):
            parse_frame(h9, b"abcdefgh")

    def test_serialize_bad_opaque_length(self):
        with self.assertRaises(FrameError):
            frames.serialize(PingFrame(0, Flag.ACK, b"short"))

    def test_ping_ack_bad_length(self):
        with self.assertRaises(FrameError):
            frames.ping_ack(b"short")


class GoAwayFrameTest(unittest.TestCase):
    def test_round_trip_with_debug_data(self):
        frame = GoAwayFrame(0, Flag(0), 9, frames.ErrorCode.PROTOCOL_ERROR, b"the reason")
        h9, payload = split(frames.serialize(frame))
        parsed = parse_frame(h9, payload)
        assert isinstance(parsed, GoAwayFrame)
        self.assertEqual(parsed.last_stream_id, 9)
        self.assertEqual(parsed.error_code, frames.ErrorCode.PROTOCOL_ERROR)
        self.assertEqual(parsed.debug_data, b"the reason")

    def test_round_trip_no_debug_data(self):
        frame = GoAwayFrame(0, Flag(0), 0, frames.ErrorCode.NO_ERROR, b"")
        h9, payload = split(frames.serialize(frame))
        self.assertEqual(len(payload), 8)
        parsed = parse_frame(h9, payload)
        self.assertEqual(parsed, frame)

    def test_too_short(self):
        h9 = build_header9(7, FrameType.GOAWAY, 0, 0)
        with self.assertRaises(FrameError):
            parse_frame(h9, b"\x00" * 7)

    def test_stream_id_nonzero_rejected(self):
        h9 = build_header9(8, FrameType.GOAWAY, 0, 1)
        with self.assertRaises(ProtocolError):
            parse_frame(h9, b"\x00" * 8)


class ContinuationFrameTest(unittest.TestCase):
    def test_parse(self):
        h9 = build_header9(4, FrameType.CONTINUATION, Flag.END_HEADERS, 3)
        parsed = parse_frame(h9, b"more")
        self.assertEqual(parsed, ContinuationFrame(3, Flag.END_HEADERS, b"more"))
        assert isinstance(parsed, ContinuationFrame)
        self.assertTrue(parsed.end_headers)

    def test_stream_id_zero_rejected(self):
        h9 = build_header9(4, FrameType.CONTINUATION, 0, 0)
        with self.assertRaises(ProtocolError):
            parse_frame(h9, b"more")


class ParseFrameMiscTest(unittest.TestCase):
    def test_payload_length_mismatch(self):
        h9 = build_header9(10, FrameType.DATA, 0, 1)
        with self.assertRaises(FrameError):
            parse_frame(h9, b"abc")

    def test_unknown_frame_type_is_ignored(self):
        # RFC 9113 §5.5: an unknown/extension frame type must be ignored, not error.
        h9 = build_header9(0, 0xEE, 0, 0)
        frame = parse_frame(h9, b"")
        self.assertIsInstance(frame, frames.UnknownFrame)
        self.assertEqual(frame.frame_type, 0xEE)

    def test_zero_window_update_is_protocol_error(self):
        h9 = build_header9(4, 0x8, 0, 1)  # WINDOW_UPDATE (type 0x8), len 4, stream 1
        with self.assertRaises(FrameError):
            parse_frame(h9, b"\x00\x00\x00\x00")  # increment 0

    def test_serialize_unsupported(self):
        with self.assertRaises(FrameError):
            frames.serialize(PriorityFrame(1, Flag(0), False, 0, 0))


class FrameReaderTest(unittest.TestCase):
    def _ping(self, data: bytes) -> bytes:
        return frames.serialize(PingFrame(0, Flag.ACK, data))

    def test_multiple_frames_in_one_buffer(self):
        wire = self._ping(b"AAAAAAAA") + self._ping(b"BBBBBBBB")
        reader = FrameReader()
        reader.feed(wire)
        out = list(reader)
        self.assertEqual(len(out), 2)
        assert isinstance(out[0], PingFrame)
        assert isinstance(out[1], PingFrame)
        self.assertEqual(out[0].opaque_data, b"AAAAAAAA")
        self.assertEqual(out[1].opaque_data, b"BBBBBBBB")

    def test_frame_split_across_feeds(self):
        wire = self._ping(b"12345678")
        reader = FrameReader()
        reader.feed(wire[:4])
        self.assertEqual(list(reader), [])  # header incomplete
        reader.feed(wire[4:10])
        self.assertEqual(list(reader), [])  # payload incomplete
        reader.feed(wire[10:])
        out = list(reader)
        self.assertEqual(len(out), 1)
        assert isinstance(out[0], PingFrame)
        self.assertEqual(out[0].opaque_data, b"12345678")

    def test_partial_then_extra_frame(self):
        a = self._ping(b"AAAAAAAA")
        b = self._ping(b"BBBBBBBB")
        reader = FrameReader()
        reader.feed(a + b[:5])
        out = list(reader)
        self.assertEqual(len(out), 1)
        reader.feed(b[5:])
        out = list(reader)
        self.assertEqual(len(out), 1)
        assert isinstance(out[0], PingFrame)
        self.assertEqual(out[0].opaque_data, b"BBBBBBBB")

    def test_byte_at_a_time(self):
        wire = self._ping(b"12345678")
        reader = FrameReader()
        collected: list[object] = []
        for byte in wire:
            reader.feed(bytes((byte,)))
            collected.extend(reader)
        self.assertEqual(len(collected), 1)

    def test_oversized_frame_rejected(self):
        reader = FrameReader(max_frame_size=16)
        # Declared length 100 exceeds the cap; rejected as soon as header seen.
        reader.feed(build_header9(100, FrameType.DATA, 0, 1))
        with self.assertRaises(FrameError):
            list(reader)


if __name__ == "__main__":
    unittest.main()
