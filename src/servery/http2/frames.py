"""HTTP/2 frame codec (RFC 9113 §4-6).

A pure-stdlib, zero-dependency reader/writer for the HTTP/2 binary framing
layer. This module knows nothing about HPACK, streams, or connection state; it
only turns bytes into typed :class:`Frame` dataclasses and back. It covers the
ten frame types defined in RFC 9113 §6, the 9-octet frame header (§4.1), the
frame-size limits (§4.2), and the client connection preface (§3.4).

Parsing is strict: padding that overruns the payload, frames that carry a stream
identifier where one is forbidden (or omit one where required), and frames whose
length contradicts a fixed-size type all raise :class:`ProtocolError` or
:class:`FrameError`. The serializers only emit the subset of frames a server
sends (HEADERS, DATA, SETTINGS, SETTINGS ACK, WINDOW_UPDATE, RST_STREAM, GOAWAY,
PING ACK); a server never originates PRIORITY or CONTINUATION-only sequences in
servery's model, so those are parse-only.
"""

from __future__ import annotations

import dataclasses
import enum
import struct
from collections.abc import Iterator

# RFC 9113 §3.4 — the fixed 24-octet client connection preface. A server reads
# these bytes verbatim before any frames; they are not a frame themselves.
CONNECTION_PREFACE: bytes = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

# RFC 9113 §4.1 — the frame header is always 9 octets.
FRAME_HEADER_SIZE: int = 9

# RFC 9113 §4.2 — frame payload size bounds. MAX_FRAME_SIZE may be negotiated
# anywhere in [2^14, 2^24 - 1]; the default (and minimum) is 2^14.
DEFAULT_MAX_FRAME_SIZE: int = 1 << 14
MIN_MAX_FRAME_SIZE: int = 1 << 14
MAX_MAX_FRAME_SIZE: int = (1 << 24) - 1

# 31-bit field masks (the high "reserved" bit of stream identifiers and of the
# WINDOW_UPDATE / GOAWAY / PRIORITY dependency fields MUST be ignored — §4.1).
_STREAM_ID_MASK: int = 0x7FFFFFFF
_RESERVED_BIT: int = 0x80000000

# struct formats (network byte order).
_U32 = struct.Struct("!I")
_SETTING_STRUCT = struct.Struct("!H I")  # 16-bit id + 32-bit value.


class FrameType(enum.IntEnum):
    """RFC 9113 §6 frame type codes."""

    DATA = 0x0
    HEADERS = 0x1
    PRIORITY = 0x2
    RST_STREAM = 0x3
    SETTINGS = 0x4
    PUSH_PROMISE = 0x5
    PING = 0x6
    GOAWAY = 0x7
    WINDOW_UPDATE = 0x8
    CONTINUATION = 0x9


class Flag(enum.IntFlag):
    """Frame flag bits (RFC 9113 §6).

    Flag values are type-specific and reused across types: ``0x1`` means
    END_STREAM on DATA/HEADERS but ACK on SETTINGS/PING. The bit positions are
    shared, so the same constant serves both roles.
    """

    END_STREAM = 0x1
    ACK = 0x1
    END_HEADERS = 0x4
    PADDED = 0x8
    PRIORITY = 0x20


class ErrorCode(enum.IntEnum):
    """RFC 9113 §7 error codes (32-bit, used by RST_STREAM and GOAWAY)."""

    NO_ERROR = 0x0
    PROTOCOL_ERROR = 0x1
    INTERNAL_ERROR = 0x2
    FLOW_CONTROL_ERROR = 0x3
    SETTINGS_TIMEOUT = 0x4
    STREAM_CLOSED = 0x5
    FRAME_SIZE_ERROR = 0x6
    REFUSED_STREAM = 0x7
    CANCEL = 0x8
    COMPRESSION_ERROR = 0x9
    CONNECT_ERROR = 0xA
    ENHANCE_YOUR_CALM = 0xB
    INADEQUATE_SECURITY = 0xC
    HTTP_1_1_REQUIRED = 0xD


class SettingsParameter(enum.IntEnum):
    """RFC 9113 §6.5.2 defined SETTINGS parameter identifiers."""

    HEADER_TABLE_SIZE = 0x1
    ENABLE_PUSH = 0x2
    MAX_CONCURRENT_STREAMS = 0x3
    INITIAL_WINDOW_SIZE = 0x4
    MAX_FRAME_SIZE = 0x5
    MAX_HEADER_LIST_SIZE = 0x6


# Convenience aliases for the parameter ids (so callers can write
# ``frames.SETTINGS_MAX_FRAME_SIZE`` without indexing the enum).
SETTINGS_HEADER_TABLE_SIZE = SettingsParameter.HEADER_TABLE_SIZE
SETTINGS_ENABLE_PUSH = SettingsParameter.ENABLE_PUSH
SETTINGS_MAX_CONCURRENT_STREAMS = SettingsParameter.MAX_CONCURRENT_STREAMS
SETTINGS_INITIAL_WINDOW_SIZE = SettingsParameter.INITIAL_WINDOW_SIZE
SETTINGS_MAX_FRAME_SIZE = SettingsParameter.MAX_FRAME_SIZE
SETTINGS_MAX_HEADER_LIST_SIZE = SettingsParameter.MAX_HEADER_LIST_SIZE

# RFC 9113 §6.5.2 — initial values for each parameter. MAX_CONCURRENT_STREAMS
# and MAX_HEADER_LIST_SIZE default to "unlimited" (no initial value); we expose
# them as ``None`` so callers can distinguish "unset" from a numeric default.
SETTINGS_DEFAULTS: dict[SettingsParameter, int | None] = {
    SettingsParameter.HEADER_TABLE_SIZE: 4096,
    SettingsParameter.ENABLE_PUSH: 1,
    SettingsParameter.MAX_CONCURRENT_STREAMS: None,
    SettingsParameter.INITIAL_WINDOW_SIZE: 65535,
    SettingsParameter.MAX_FRAME_SIZE: 16384,
    SettingsParameter.MAX_HEADER_LIST_SIZE: None,
}


class FrameError(Exception):
    """Base class for all framing errors."""


class ProtocolError(FrameError):
    """A frame violates an RFC 9113 protocol rule (bad padding, stream id, etc.)."""


# --------------------------------------------------------------------------- #
# Parsed frame dataclasses.
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True, slots=True)
class DataFrame:
    """A DATA frame (§6.1) with padding already stripped from ``data``."""

    stream_id: int
    flags: Flag
    data: bytes

    @property
    def end_stream(self) -> bool:
        """True if the END_STREAM flag is set."""
        return bool(self.flags & Flag.END_STREAM)


@dataclasses.dataclass(frozen=True, slots=True)
class HeadersFrame:
    """A HEADERS frame (§6.2).

    ``header_block`` holds the field block fragment with padding stripped. The
    optional priority fields (present only when the PRIORITY flag is set) are
    surfaced as ``exclusive`` / ``stream_dependency`` / ``weight``; they are
    ``None`` when the flag is absent.
    """

    stream_id: int
    flags: Flag
    header_block: bytes
    exclusive: bool | None = None
    stream_dependency: int | None = None
    weight: int | None = None

    @property
    def end_stream(self) -> bool:
        """True if the END_STREAM flag is set."""
        return bool(self.flags & Flag.END_STREAM)

    @property
    def end_headers(self) -> bool:
        """True if the END_HEADERS flag is set (no CONTINUATION follows)."""
        return bool(self.flags & Flag.END_HEADERS)


@dataclasses.dataclass(frozen=True, slots=True)
class PriorityFrame:
    """A PRIORITY frame (§6.3): a stream-dependency edge plus weight."""

    stream_id: int
    flags: Flag
    exclusive: bool
    stream_dependency: int
    weight: int


@dataclasses.dataclass(frozen=True, slots=True)
class RstStreamFrame:
    """A RST_STREAM frame (§6.4): abrupt stream termination with an error code."""

    stream_id: int
    flags: Flag
    error_code: int


@dataclasses.dataclass(frozen=True, slots=True)
class SettingsFrame:
    """A SETTINGS frame (§6.5).

    ``settings`` maps the raw 16-bit parameter id to its 32-bit value; unknown
    ids are preserved (the RFC requires receivers to ignore, not reject, them).
    An ACK SETTINGS frame carries the ACK flag and an empty ``settings`` map.
    """

    stream_id: int
    flags: Flag
    settings: tuple[tuple[int, int], ...]

    @property
    def ack(self) -> bool:
        """True if this is a SETTINGS acknowledgement."""
        return bool(self.flags & Flag.ACK)


@dataclasses.dataclass(frozen=True, slots=True)
class PushPromiseFrame:
    """A PUSH_PROMISE frame (§6.6) with padding stripped from ``header_block``."""

    stream_id: int
    flags: Flag
    promised_stream_id: int
    header_block: bytes

    @property
    def end_headers(self) -> bool:
        """True if the END_HEADERS flag is set."""
        return bool(self.flags & Flag.END_HEADERS)


@dataclasses.dataclass(frozen=True, slots=True)
class PingFrame:
    """A PING frame (§6.7): 8 octets of opaque data, optionally an ACK."""

    stream_id: int
    flags: Flag
    opaque_data: bytes

    @property
    def ack(self) -> bool:
        """True if this is a PING acknowledgement."""
        return bool(self.flags & Flag.ACK)


@dataclasses.dataclass(frozen=True, slots=True)
class GoAwayFrame:
    """A GOAWAY frame (§6.8): connection shutdown with diagnostics."""

    stream_id: int
    flags: Flag
    last_stream_id: int
    error_code: int
    debug_data: bytes


@dataclasses.dataclass(frozen=True, slots=True)
class WindowUpdateFrame:
    """A WINDOW_UPDATE frame (§6.9): a flow-control window increment."""

    stream_id: int
    flags: Flag
    window_size_increment: int


@dataclasses.dataclass(frozen=True, slots=True)
class ContinuationFrame:
    """A CONTINUATION frame (§6.10): more field-block fragment bytes."""

    stream_id: int
    flags: Flag
    header_block: bytes

    @property
    def end_headers(self) -> bool:
        """True if the END_HEADERS flag is set."""
        return bool(self.flags & Flag.END_HEADERS)


# The union of every parsed frame type.
Frame = (
    DataFrame
    | HeadersFrame
    | PriorityFrame
    | RstStreamFrame
    | SettingsFrame
    | PushPromiseFrame
    | PingFrame
    | GoAwayFrame
    | WindowUpdateFrame
    | ContinuationFrame
)


# --------------------------------------------------------------------------- #
# Header (de)serialization.
# --------------------------------------------------------------------------- #


def build_header9(length: int, frame_type: int, flags: int, stream_id: int) -> bytes:
    """Pack a 9-octet frame header (RFC 9113 §4.1).

    ``length`` is the payload length (0..2^24-1, excluding this header). The
    reserved high bit of ``stream_id`` is always emitted as 0.
    """
    if not 0 <= length <= 0xFFFFFF:
        raise FrameError(f"frame length {length} out of 24-bit range")
    if not 0 <= stream_id <= _STREAM_ID_MASK:
        raise FrameError(f"stream id {stream_id} out of 31-bit range")
    # Length is 24 bits: pack a 4-byte int and drop the leading octet.
    return (
        struct.pack("!I", length)[1:]
        + bytes((frame_type & 0xFF, flags & 0xFF))
        + _U32.pack(stream_id & _STREAM_ID_MASK)
    )


@dataclasses.dataclass(frozen=True, slots=True)
class FrameHeader:
    """The decoded 9-octet frame header fields."""

    length: int
    type: int
    flags: int
    stream_id: int


def parse_header9(header9: bytes) -> FrameHeader:
    """Decode a 9-octet frame header, masking off the reserved stream-id bit."""
    if len(header9) != FRAME_HEADER_SIZE:
        raise FrameError(f"frame header must be 9 octets, got {len(header9)}")
    # Reassemble the 24-bit length from a synthetic 4-byte big-endian int.
    length = (header9[0] << 16) | (header9[1] << 8) | header9[2]
    frame_type = header9[3]
    flags = header9[4]
    (raw_stream,) = _U32.unpack(header9[5:9])
    return FrameHeader(length, frame_type, flags, raw_stream & _STREAM_ID_MASK)


# --------------------------------------------------------------------------- #
# Payload parsing helpers.
# --------------------------------------------------------------------------- #


def _strip_padding(payload: bytes) -> bytes:
    """Strip a leading Pad Length octet and trailing padding (§6.1/§6.2).

    The payload must begin with the 1-octet Pad Length. A pad length that is not
    strictly less than the remaining bytes is a PROTOCOL_ERROR (§6.1).
    """
    if not payload:
        raise ProtocolError("PADDED frame missing pad length octet")
    pad_length = payload[0]
    # Remaining bytes after the pad-length octet must exceed the padding, so the
    # field block / data is at least zero bytes. pad_length == len-1 leaves an
    # empty body, which is legal; pad_length >= len would overrun.
    if pad_length >= len(payload):
        raise ProtocolError(f"pad length {pad_length} exceeds payload remainder {len(payload) - 1}")
    if pad_length == 0:
        return payload[1:]
    return payload[1:-pad_length]


def _require_stream(stream_id: int, frame_name: str) -> None:
    """Reject stream id 0 for frames that require a non-zero stream (§6)."""
    if stream_id == 0:
        raise ProtocolError(f"{frame_name} frame must not use stream id 0")


def _require_connection(stream_id: int, frame_name: str) -> None:
    """Reject non-zero stream id for connection-level frames (§6)."""
    if stream_id != 0:
        raise ProtocolError(f"{frame_name} frame must use stream id 0")


def _read_priority_fields(data: bytes) -> tuple[bool, int, int]:
    """Unpack the 5-octet priority block: exclusive bit, dependency, weight."""
    (dep_raw,) = _U32.unpack(data[:4])
    exclusive = bool(dep_raw & _RESERVED_BIT)
    stream_dependency = dep_raw & _STREAM_ID_MASK
    weight = data[4]
    return exclusive, stream_dependency, weight


def parse_frame(header9: bytes, payload: bytes) -> Frame:
    """Parse one frame from its 9-octet header and exact payload.

    ``payload`` must be exactly ``header.length`` bytes. Raises
    :class:`ProtocolError` / :class:`FrameError` on malformed frames (bad
    padding, forbidden/required stream id, fixed-size length mismatch).
    """
    header = parse_header9(header9)
    if len(payload) != header.length:
        raise FrameError(
            f"payload length {len(payload)} does not match header length {header.length}"
        )
    flags = Flag(header.flags)
    sid = header.stream_id

    if header.type == FrameType.DATA:
        _require_stream(sid, "DATA")
        data = _strip_padding(payload) if flags & Flag.PADDED else payload
        return DataFrame(sid, flags, data)

    if header.type == FrameType.HEADERS:
        _require_stream(sid, "HEADERS")
        return _parse_headers(sid, flags, payload)

    if header.type == FrameType.PRIORITY:
        _require_stream(sid, "PRIORITY")
        if header.length != 5:
            raise FrameError(f"PRIORITY frame length must be 5, got {header.length}")
        exclusive, dep, weight = _read_priority_fields(payload)
        return PriorityFrame(sid, flags, exclusive, dep, weight)

    if header.type == FrameType.RST_STREAM:
        _require_stream(sid, "RST_STREAM")
        if header.length != 4:
            raise FrameError(f"RST_STREAM frame length must be 4, got {header.length}")
        (error_code,) = _U32.unpack(payload)
        return RstStreamFrame(sid, flags, error_code)

    if header.type == FrameType.SETTINGS:
        _require_connection(sid, "SETTINGS")
        return _parse_settings(sid, flags, payload)

    if header.type == FrameType.PUSH_PROMISE:
        _require_stream(sid, "PUSH_PROMISE")
        return _parse_push_promise(sid, flags, payload)

    if header.type == FrameType.PING:
        _require_connection(sid, "PING")
        if header.length != 8:
            raise FrameError(f"PING frame length must be 8, got {header.length}")
        return PingFrame(sid, flags, payload)

    if header.type == FrameType.GOAWAY:
        _require_connection(sid, "GOAWAY")
        if header.length < 8:
            raise FrameError(f"GOAWAY frame length must be >= 8, got {header.length}")
        (last_raw,) = _U32.unpack(payload[:4])
        (error_code,) = _U32.unpack(payload[4:8])
        return GoAwayFrame(sid, flags, last_raw & _STREAM_ID_MASK, error_code, payload[8:])

    if header.type == FrameType.WINDOW_UPDATE:
        if header.length != 4:
            raise FrameError(f"WINDOW_UPDATE frame length must be 4, got {header.length}")
        (raw,) = _U32.unpack(payload)
        return WindowUpdateFrame(sid, flags, raw & _STREAM_ID_MASK)

    if header.type == FrameType.CONTINUATION:
        _require_stream(sid, "CONTINUATION")
        return ContinuationFrame(sid, flags, payload)

    raise FrameError(f"unknown frame type {header.type}")


def _parse_headers(sid: int, flags: Flag, payload: bytes) -> HeadersFrame:
    """Parse a HEADERS payload, stripping padding then peeling priority fields."""
    body = _strip_padding(payload) if flags & Flag.PADDED else payload
    if flags & Flag.PRIORITY:
        if len(body) < 5:
            raise FrameError("HEADERS with PRIORITY flag missing 5-octet priority block")
        exclusive, dep, weight = _read_priority_fields(body)
        return HeadersFrame(sid, flags, body[5:], exclusive, dep, weight)
    return HeadersFrame(sid, flags, body)


def _parse_settings(sid: int, flags: Flag, payload: bytes) -> SettingsFrame:
    """Parse a SETTINGS payload into (id, value) pairs, validating ACK/length."""
    if flags & Flag.ACK:
        if payload:
            raise FrameError("SETTINGS ACK frame must have empty payload")
        return SettingsFrame(sid, flags, ())
    if len(payload) % _SETTING_STRUCT.size != 0:
        raise FrameError(
            f"SETTINGS length {len(payload)} is not a multiple of {_SETTING_STRUCT.size}"
        )
    settings = tuple((ident, value) for ident, value in _SETTING_STRUCT.iter_unpack(payload))
    return SettingsFrame(sid, flags, settings)


def _parse_push_promise(sid: int, flags: Flag, payload: bytes) -> PushPromiseFrame:
    """Parse a PUSH_PROMISE payload: strip padding, read promised stream id."""
    body = _strip_padding(payload) if flags & Flag.PADDED else payload
    if len(body) < 4:
        raise FrameError("PUSH_PROMISE frame missing promised stream id")
    (raw,) = _U32.unpack(body[:4])
    return PushPromiseFrame(sid, flags, raw & _STREAM_ID_MASK, body[4:])


# --------------------------------------------------------------------------- #
# Streaming reader.
# --------------------------------------------------------------------------- #


class FrameReader:
    """Reassemble whole frames from a byte stream.

    Feed arbitrary chunks with :meth:`feed`; iterate to drain every complete
    frame currently buffered. Partial frames are retained until enough bytes
    arrive. A frame whose declared length exceeds ``max_frame_size`` raises
    :class:`FrameError` (FRAME_SIZE_ERROR) as soon as its header is seen.
    """

    __slots__ = ("_buffer", "max_frame_size")

    def __init__(self, max_frame_size: int = DEFAULT_MAX_FRAME_SIZE) -> None:
        """Create a reader bounding accepted frames to ``max_frame_size`` octets."""
        self.max_frame_size = max_frame_size
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        """Append received bytes to the internal buffer."""
        self._buffer += data

    def __iter__(self) -> Iterator[Frame]:
        """Yield every complete frame currently buffered, consuming its bytes."""
        while True:
            frame = self._next_frame()
            if frame is None:
                return
            yield frame

    def _next_frame(self) -> Frame | None:
        """Pop and parse the next complete frame, or None if more bytes are needed."""
        if len(self._buffer) < FRAME_HEADER_SIZE:
            return None
        header9 = bytes(self._buffer[:FRAME_HEADER_SIZE])
        length = (header9[0] << 16) | (header9[1] << 8) | header9[2]
        if length > self.max_frame_size:
            raise FrameError(f"frame length {length} exceeds max frame size {self.max_frame_size}")
        total = FRAME_HEADER_SIZE + length
        if len(self._buffer) < total:
            return None
        payload = bytes(self._buffer[FRAME_HEADER_SIZE:total])
        del self._buffer[:total]
        return parse_frame(header9, payload)


# --------------------------------------------------------------------------- #
# Serializers (server-sent frames).
# --------------------------------------------------------------------------- #


def _frame_bytes(frame_type: FrameType, flags: int, stream_id: int, payload: bytes) -> bytes:
    """Prepend a header to ``payload`` for the given type/flags/stream."""
    return build_header9(len(payload), frame_type, flags, stream_id) + payload


def serialize(frame: Frame) -> bytes:
    """Serialize a server-sent frame to wire bytes.

    Supports the frames servery's server originates: DATA, HEADERS, SETTINGS
    (including ACK), WINDOW_UPDATE, RST_STREAM, GOAWAY, and PING (including ACK).
    PRIORITY, PUSH_PROMISE, and bare CONTINUATION are not serialized (a server
    in this model never sends them) and raise :class:`FrameError`.
    """
    if isinstance(frame, DataFrame):
        return _frame_bytes(FrameType.DATA, frame.flags, frame.stream_id, frame.data)

    if isinstance(frame, HeadersFrame):
        return _serialize_headers(frame)

    if isinstance(frame, SettingsFrame):
        return _serialize_settings(frame)

    if isinstance(frame, WindowUpdateFrame):
        payload = _U32.pack(frame.window_size_increment & _STREAM_ID_MASK)
        return _frame_bytes(FrameType.WINDOW_UPDATE, frame.flags, frame.stream_id, payload)

    if isinstance(frame, RstStreamFrame):
        payload = _U32.pack(frame.error_code)
        return _frame_bytes(FrameType.RST_STREAM, frame.flags, frame.stream_id, payload)

    if isinstance(frame, GoAwayFrame):
        payload = (
            _U32.pack(frame.last_stream_id & _STREAM_ID_MASK)
            + _U32.pack(frame.error_code)
            + frame.debug_data
        )
        return _frame_bytes(FrameType.GOAWAY, frame.flags, frame.stream_id, payload)

    if isinstance(frame, PingFrame):
        if len(frame.opaque_data) != 8:
            raise FrameError("PING opaque data must be exactly 8 octets")
        return _frame_bytes(FrameType.PING, frame.flags, frame.stream_id, frame.opaque_data)

    raise FrameError(f"serialize() does not support {type(frame).__name__}")


def _serialize_headers(frame: HeadersFrame) -> bytes:
    """Serialize a HEADERS frame, including priority fields when present.

    Padding is never emitted (the server has no reason to pad), so the PADDED
    flag is cleared on output even if it was set on the parsed source frame.
    """
    flags = frame.flags & ~Flag.PADDED
    if frame.flags & Flag.PRIORITY:
        if frame.stream_dependency is None or frame.weight is None:
            raise FrameError("HEADERS with PRIORITY flag missing dependency/weight")
        dep = frame.stream_dependency & _STREAM_ID_MASK
        if frame.exclusive:
            dep |= _RESERVED_BIT
        prefix = _U32.pack(dep) + bytes((frame.weight & 0xFF,))
        payload = prefix + frame.header_block
    else:
        payload = frame.header_block
    return _frame_bytes(FrameType.HEADERS, flags, frame.stream_id, payload)


def _serialize_settings(frame: SettingsFrame) -> bytes:
    """Serialize a SETTINGS frame (or an empty ACK)."""
    if frame.flags & Flag.ACK:
        if frame.settings:
            raise FrameError("SETTINGS ACK frame must carry no parameters")
        return _frame_bytes(FrameType.SETTINGS, frame.flags, frame.stream_id, b"")
    payload = b"".join(_SETTING_STRUCT.pack(ident, value) for ident, value in frame.settings)
    return _frame_bytes(FrameType.SETTINGS, frame.flags, frame.stream_id, payload)


def settings_ack() -> SettingsFrame:
    """Build an empty SETTINGS ACK frame (stream 0, ACK flag set)."""
    return SettingsFrame(0, Flag.ACK, ())


def ping_ack(opaque_data: bytes) -> PingFrame:
    """Build a PING ACK echoing ``opaque_data`` (which must be 8 octets)."""
    if len(opaque_data) != 8:
        raise FrameError("PING opaque data must be exactly 8 octets")
    return PingFrame(0, Flag.ACK, opaque_data)
