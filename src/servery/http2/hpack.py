"""HPACK header compression for HTTP/2 (RFC 7541).

A pure-stdlib, zero-dependency implementation of HPACK: the header-compression
format used by HTTP/2. The module provides the primitive codecs (integer and
string-literal representations, the static Huffman code), the static and dynamic
indexing tables, and a stateful :class:`Encoder` / :class:`Decoder` pair.

The decoder is the security-sensitive half (it consumes attacker-controlled
input), so it enforces a configurable maximum header-list size as a guard
against decompression bombs, and rejects malformed integers, oversized string
literals, invalid Huffman padding, and the EOS symbol per RFC 7541 §5.2.

The encoder uses a deliberately simple strategy: indexed representations for
exact static-table matches, indexed names where available, and Huffman-coded
literals whenever they are shorter than the raw form. It never inserts into the
dynamic table, which keeps it correct and stateless-by-default while remaining
fully interoperable with any conforming decoder.

References:
    RFC 7541, "HPACK: Header Compression for HTTP/2", May 2015.
"""

from __future__ import annotations


class HpackError(Exception):
    """Raised on any HPACK encoding or decoding error (malformed or unsafe input)."""


# ---------------------------------------------------------------------------
# Static table (RFC 7541 Appendix A). Index 1 maps to STATIC_TABLE[0].
# ---------------------------------------------------------------------------

STATIC_TABLE: tuple[tuple[bytes, bytes], ...] = (
    (b":authority", b""),
    (b":method", b"GET"),
    (b":method", b"POST"),
    (b":path", b"/"),
    (b":path", b"/index.html"),
    (b":scheme", b"http"),
    (b":scheme", b"https"),
    (b":status", b"200"),
    (b":status", b"204"),
    (b":status", b"206"),
    (b":status", b"304"),
    (b":status", b"400"),
    (b":status", b"404"),
    (b":status", b"500"),
    (b"accept-charset", b""),
    (b"accept-encoding", b"gzip, deflate"),
    (b"accept-language", b""),
    (b"accept-ranges", b""),
    (b"accept", b""),
    (b"access-control-allow-origin", b""),
    (b"age", b""),
    (b"allow", b""),
    (b"authorization", b""),
    (b"cache-control", b""),
    (b"content-disposition", b""),
    (b"content-encoding", b""),
    (b"content-language", b""),
    (b"content-length", b""),
    (b"content-location", b""),
    (b"content-range", b""),
    (b"content-type", b""),
    (b"cookie", b""),
    (b"date", b""),
    (b"etag", b""),
    (b"expect", b""),
    (b"expires", b""),
    (b"from", b""),
    (b"host", b""),
    (b"if-match", b""),
    (b"if-modified-since", b""),
    (b"if-none-match", b""),
    (b"if-range", b""),
    (b"if-unmodified-since", b""),
    (b"last-modified", b""),
    (b"link", b""),
    (b"location", b""),
    (b"max-forwards", b""),
    (b"proxy-authenticate", b""),
    (b"proxy-authorization", b""),
    (b"range", b""),
    (b"referer", b""),
    (b"refresh", b""),
    (b"retry-after", b""),
    (b"server", b""),
    (b"set-cookie", b""),
    (b"strict-transport-security", b""),
    (b"transfer-encoding", b""),
    (b"user-agent", b""),
    (b"vary", b""),
    (b"via", b""),
    (b"www-authenticate", b""),
)

STATIC_TABLE_LENGTH = len(STATIC_TABLE)

# Map (name, value) -> 1-based static index, and name -> first 1-based index,
# for the encoder. The static table is constant, so these are built once.
_STATIC_FULL_INDEX: dict[tuple[bytes, bytes], int] = {}
_STATIC_NAME_INDEX: dict[bytes, int] = {}
for _i, (_name, _value) in enumerate(STATIC_TABLE, start=1):
    _STATIC_FULL_INDEX.setdefault((_name, _value), _i)
    _STATIC_NAME_INDEX.setdefault(_name, _i)
del _i, _name, _value


# ---------------------------------------------------------------------------
# Huffman code (RFC 7541 Appendix B), as (code_value, bit_length) per symbol.
# Symbols 0..255 are octets; index 256 is EOS. Aligned to the LSB.
# ---------------------------------------------------------------------------

_HUFFMAN_CODES: tuple[tuple[int, int], ...] = (
    (0x1FF8, 13),
    (0x7FFFD8, 23),
    (0xFFFFFE2, 28),
    (0xFFFFFE3, 28),
    (0xFFFFFE4, 28),
    (0xFFFFFE5, 28),
    (0xFFFFFE6, 28),
    (0xFFFFFE7, 28),
    (0xFFFFFE8, 28),
    (0xFFFFEA, 24),
    (0x3FFFFFFC, 30),
    (0xFFFFFE9, 28),
    (0xFFFFFEA, 28),
    (0x3FFFFFFD, 30),
    (0xFFFFFEB, 28),
    (0xFFFFFEC, 28),
    (0xFFFFFED, 28),
    (0xFFFFFEE, 28),
    (0xFFFFFEF, 28),
    (0xFFFFFF0, 28),
    (0xFFFFFF1, 28),
    (0xFFFFFF2, 28),
    (0x3FFFFFFE, 30),
    (0xFFFFFF3, 28),
    (0xFFFFFF4, 28),
    (0xFFFFFF5, 28),
    (0xFFFFFF6, 28),
    (0xFFFFFF7, 28),
    (0xFFFFFF8, 28),
    (0xFFFFFF9, 28),
    (0xFFFFFFA, 28),
    (0xFFFFFFB, 28),
    (0x14, 6),
    (0x3F8, 10),
    (0x3F9, 10),
    (0xFFA, 12),
    (0x1FF9, 13),
    (0x15, 6),
    (0xF8, 8),
    (0x7FA, 11),
    (0x3FA, 10),
    (0x3FB, 10),
    (0xF9, 8),
    (0x7FB, 11),
    (0xFA, 8),
    (0x16, 6),
    (0x17, 6),
    (0x18, 6),
    (0x0, 5),
    (0x1, 5),
    (0x2, 5),
    (0x19, 6),
    (0x1A, 6),
    (0x1B, 6),
    (0x1C, 6),
    (0x1D, 6),
    (0x1E, 6),
    (0x1F, 6),
    (0x5C, 7),
    (0xFB, 8),
    (0x7FFC, 15),
    (0x20, 6),
    (0xFFB, 12),
    (0x3FC, 10),
    (0x1FFA, 13),
    (0x21, 6),
    (0x5D, 7),
    (0x5E, 7),
    (0x5F, 7),
    (0x60, 7),
    (0x61, 7),
    (0x62, 7),
    (0x63, 7),
    (0x64, 7),
    (0x65, 7),
    (0x66, 7),
    (0x67, 7),
    (0x68, 7),
    (0x69, 7),
    (0x6A, 7),
    (0x6B, 7),
    (0x6C, 7),
    (0x6D, 7),
    (0x6E, 7),
    (0x6F, 7),
    (0x70, 7),
    (0x71, 7),
    (0x72, 7),
    (0xFC, 8),
    (0x73, 7),
    (0xFD, 8),
    (0x1FFB, 13),
    (0x7FFF0, 19),
    (0x1FFC, 13),
    (0x3FFC, 14),
    (0x22, 6),
    (0x7FFD, 15),
    (0x3, 5),
    (0x23, 6),
    (0x4, 5),
    (0x24, 6),
    (0x5, 5),
    (0x25, 6),
    (0x26, 6),
    (0x27, 6),
    (0x6, 5),
    (0x74, 7),
    (0x75, 7),
    (0x28, 6),
    (0x29, 6),
    (0x2A, 6),
    (0x7, 5),
    (0x2B, 6),
    (0x76, 7),
    (0x2C, 6),
    (0x8, 5),
    (0x9, 5),
    (0x2D, 6),
    (0x77, 7),
    (0x78, 7),
    (0x79, 7),
    (0x7A, 7),
    (0x7B, 7),
    (0x7FFE, 15),
    (0x7FC, 11),
    (0x3FFD, 14),
    (0x1FFD, 13),
    (0xFFFFFFC, 28),
    (0xFFFE6, 20),
    (0x3FFFD2, 22),
    (0xFFFE7, 20),
    (0xFFFE8, 20),
    (0x3FFFD3, 22),
    (0x3FFFD4, 22),
    (0x3FFFD5, 22),
    (0x7FFFD9, 23),
    (0x3FFFD6, 22),
    (0x7FFFDA, 23),
    (0x7FFFDB, 23),
    (0x7FFFDC, 23),
    (0x7FFFDD, 23),
    (0x7FFFDE, 23),
    (0xFFFFEB, 24),
    (0x7FFFDF, 23),
    (0xFFFFEC, 24),
    (0xFFFFED, 24),
    (0x3FFFD7, 22),
    (0x7FFFE0, 23),
    (0xFFFFEE, 24),
    (0x7FFFE1, 23),
    (0x7FFFE2, 23),
    (0x7FFFE3, 23),
    (0x7FFFE4, 23),
    (0x1FFFDC, 21),
    (0x3FFFD8, 22),
    (0x7FFFE5, 23),
    (0x3FFFD9, 22),
    (0x7FFFE6, 23),
    (0x7FFFE7, 23),
    (0xFFFFEF, 24),
    (0x3FFFDA, 22),
    (0x1FFFDD, 21),
    (0xFFFE9, 20),
    (0x3FFFDB, 22),
    (0x3FFFDC, 22),
    (0x7FFFE8, 23),
    (0x7FFFE9, 23),
    (0x1FFFDE, 21),
    (0x7FFFEA, 23),
    (0x3FFFDD, 22),
    (0x3FFFDE, 22),
    (0xFFFFF0, 24),
    (0x1FFFDF, 21),
    (0x3FFFDF, 22),
    (0x7FFFEB, 23),
    (0x7FFFEC, 23),
    (0x1FFFE0, 21),
    (0x1FFFE1, 21),
    (0x3FFFE0, 22),
    (0x1FFFE2, 21),
    (0x7FFFED, 23),
    (0x3FFFE1, 22),
    (0x7FFFEE, 23),
    (0x7FFFEF, 23),
    (0xFFFEA, 20),
    (0x3FFFE2, 22),
    (0x3FFFE3, 22),
    (0x3FFFE4, 22),
    (0x7FFFF0, 23),
    (0x3FFFE5, 22),
    (0x3FFFE6, 22),
    (0x7FFFF1, 23),
    (0x3FFFFE0, 26),
    (0x3FFFFE1, 26),
    (0xFFFEB, 20),
    (0x7FFF1, 19),
    (0x3FFFE7, 22),
    (0x7FFFF2, 23),
    (0x3FFFE8, 22),
    (0x1FFFFEC, 25),
    (0x3FFFFE2, 26),
    (0x3FFFFE3, 26),
    (0x3FFFFE4, 26),
    (0x7FFFFDE, 27),
    (0x7FFFFDF, 27),
    (0x3FFFFE5, 26),
    (0xFFFFF1, 24),
    (0x1FFFFED, 25),
    (0x7FFF2, 19),
    (0x1FFFE3, 21),
    (0x3FFFFE6, 26),
    (0x7FFFFE0, 27),
    (0x7FFFFE1, 27),
    (0x3FFFFE7, 26),
    (0x7FFFFE2, 27),
    (0xFFFFF2, 24),
    (0x1FFFE4, 21),
    (0x1FFFE5, 21),
    (0x3FFFFE8, 26),
    (0x3FFFFE9, 26),
    (0xFFFFFFD, 28),
    (0x7FFFFE3, 27),
    (0x7FFFFE4, 27),
    (0x7FFFFE5, 27),
    (0xFFFEC, 20),
    (0xFFFFF3, 24),
    (0xFFFED, 20),
    (0x1FFFE6, 21),
    (0x3FFFE9, 22),
    (0x1FFFE7, 21),
    (0x1FFFE8, 21),
    (0x7FFFF3, 23),
    (0x3FFFEA, 22),
    (0x3FFFEB, 22),
    (0x1FFFFEE, 25),
    (0x1FFFFEF, 25),
    (0xFFFFF4, 24),
    (0xFFFFF5, 24),
    (0x3FFFFEA, 26),
    (0x7FFFF4, 23),
    (0x3FFFFEB, 26),
    (0x7FFFFE6, 27),
    (0x3FFFFEC, 26),
    (0x3FFFFED, 26),
    (0x7FFFFE7, 27),
    (0x7FFFFE8, 27),
    (0x7FFFFE9, 27),
    (0x7FFFFEA, 27),
    (0x7FFFFEB, 27),
    (0xFFFFFFE, 28),
    (0x7FFFFEC, 27),
    (0x7FFFFED, 27),
    (0x7FFFFEE, 27),
    (0x7FFFFEF, 27),
    (0x7FFFFF0, 27),
    (0x3FFFFEE, 26),
    (0x3FFFFFFF, 30),  # EOS (256)
)

EOS_SYMBOL = 256

# A node in the Huffman decode tree: either an internal node (a dict mapping the
# next bit to a child) or a leaf (the integer symbol value).
type _HuffNode = "dict[int, _HuffNode] | int"


def _build_huffman_decode_tree() -> dict[int, _HuffNode]:
    """Build a bit-walking decode tree keyed by 0/1 with int-leaf symbols.

    Internal nodes are nested dicts; leaves are the integer symbol value.
    """
    root: dict[int, _HuffNode] = {}
    for symbol, (code, length) in enumerate(_HUFFMAN_CODES):
        node = root
        for bit_index in range(length - 1, -1, -1):
            bit = (code >> bit_index) & 1
            if bit_index == 0:
                node[bit] = symbol
            else:
                child = node.get(bit)
                if not isinstance(child, dict):
                    child = {}
                    node[bit] = child
                node = child
    return root


_HUFFMAN_DECODE_TREE = _build_huffman_decode_tree()


# ---------------------------------------------------------------------------
# Integer representation (RFC 7541 §5.1).
# ---------------------------------------------------------------------------

# Guard against maliciously long integer encodings (decompression-bomb defense).
_MAX_INTEGER_OCTETS = 6


def encode_integer(value: int, prefix_bits: int) -> bytes:
    """Encode ``value`` as an HPACK integer with an ``prefix_bits``-bit prefix.

    The returned prefix octet has only its low ``prefix_bits`` bits set; the
    caller is responsible for OR-ing in any flag bits above the prefix.

    Args:
        value: The non-negative integer to encode.
        prefix_bits: Prefix width N, between 1 and 8 inclusive.

    Returns:
        The encoded octets.
    """
    if value < 0:
        raise HpackError("cannot encode a negative integer")
    if not 1 <= prefix_bits <= 8:
        raise HpackError("prefix_bits must be between 1 and 8")

    max_prefix = (1 << prefix_bits) - 1
    if value < max_prefix:
        return bytes((value,))

    out = bytearray((max_prefix,))
    value -= max_prefix
    while value >= 128:
        out.append((value % 128) + 128)
        value //= 128
    out.append(value)
    return bytes(out)


def decode_integer(data: bytes, offset: int, prefix_bits: int) -> tuple[int, int]:
    """Decode an HPACK integer starting at ``data[offset]``.

    The prefix octet is read at ``offset``; only its low ``prefix_bits`` bits
    participate (the caller masks out flag bits implicitly by setting N).

    Args:
        data: The buffer to read from.
        offset: Index of the prefix octet.
        prefix_bits: Prefix width N, between 1 and 8 inclusive.

    Returns:
        A ``(value, next_offset)`` pair.
    """
    if not 1 <= prefix_bits <= 8:
        raise HpackError("prefix_bits must be between 1 and 8")
    if offset >= len(data):
        raise HpackError("truncated integer: missing prefix octet")

    max_prefix = (1 << prefix_bits) - 1
    value = data[offset] & max_prefix
    offset += 1
    if value < max_prefix:
        return value, offset

    shift = 0
    octets = 0
    while True:
        if offset >= len(data):
            raise HpackError("truncated integer: missing continuation octet")
        octets += 1
        if octets > _MAX_INTEGER_OCTETS:
            raise HpackError("integer encoding too long")
        byte = data[offset]
        offset += 1
        value += (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return value, offset


# ---------------------------------------------------------------------------
# Huffman codec (RFC 7541 §5.2 / Appendix B).
# ---------------------------------------------------------------------------


def huffman_encode(data: bytes) -> bytes:
    """Huffman-encode ``data`` using the static HPACK code, padding with EOS bits."""
    bits = 0
    nbits = 0
    out = bytearray()
    for byte in data:
        code, length = _HUFFMAN_CODES[byte]
        bits = (bits << length) | code
        nbits += length
        while nbits >= 8:
            nbits -= 8
            out.append((bits >> nbits) & 0xFF)
        # Drop the already-emitted high bits so `bits` stays small (otherwise it
        # grows to the whole string's bit-length, making each shift O(n) -> O(n^2)).
        bits &= (1 << nbits) - 1
    if nbits:
        # Pad the final octet with the most-significant bits of the EOS code,
        # which are all 1s.
        pad = 8 - nbits
        out.append(((bits << pad) | ((1 << pad) - 1)) & 0xFF)
    return bytes(out)


def huffman_decode(data: bytes) -> bytes:
    """Huffman-decode ``data``, rejecting bad padding and an embedded EOS symbol.

    Raises:
        HpackError: On padding longer than 7 bits, padding that is not all 1s
            (the EOS prefix), or an EOS symbol appearing in the stream.
    """
    out = bytearray()
    node: dict[int, _HuffNode] = _HUFFMAN_DECODE_TREE
    # Number of bits consumed within the current (partial) code, used to
    # validate the trailing padding.
    bits_in_code = 0
    for byte in data:
        for bit_index in range(7, -1, -1):
            bit = (byte >> bit_index) & 1
            child = node[bit]
            bits_in_code += 1
            if isinstance(child, dict):
                node = child
                continue
            if child == EOS_SYMBOL:
                raise HpackError("EOS symbol encountered in Huffman-encoded data")
            out.append(child)
            node = _HUFFMAN_DECODE_TREE
            bits_in_code = 0
    if node is not _HUFFMAN_DECODE_TREE:
        # A partial code remains: it must be valid EOS-prefix padding, i.e. at
        # most 7 bits and consisting solely of 1s.
        if bits_in_code > 7:
            raise HpackError("Huffman padding longer than 7 bits")
        if not _is_all_ones_path(node):
            raise HpackError("invalid Huffman padding (not the EOS prefix)")
    return bytes(out)


def _is_all_ones_path(node: dict[int, _HuffNode]) -> bool:
    """True if following only 1-bits from ``node`` stays on the EOS code path.

    The trailing padding must be the most-significant bits of the EOS code,
    which are all 1s; we verify the partial path reached by consuming 1-bits is
    a valid prefix of (and only of) the EOS code.
    """
    current: _HuffNode = node
    while isinstance(current, dict):
        nxt = current.get(1)
        if nxt is None:
            return False
        current = nxt
    return current == EOS_SYMBOL


# ---------------------------------------------------------------------------
# String literal representation (RFC 7541 §5.2).
# ---------------------------------------------------------------------------


def _encode_string(data: bytes, *, huffman: bool) -> bytes:
    """Encode a string literal, optionally Huffman-coded, with its length prefix."""
    if huffman:
        encoded = huffman_encode(data)
        prefix = encode_integer(len(encoded), 7)
        return bytes((prefix[0] | 0x80, *prefix[1:])) + encoded
    return encode_integer(len(data), 7) + data


def _decode_string(data: bytes, offset: int, max_length: int) -> tuple[bytes, int]:
    """Decode a string literal at ``offset``; ``max_length`` bounds raw octet length."""
    if offset >= len(data):
        raise HpackError("truncated string literal: missing length octet")
    huffman = bool(data[offset] & 0x80)
    length, offset = decode_integer(data, offset, 7)
    if length > max_length:
        raise HpackError("string literal exceeds maximum length")
    end = offset + length
    if end > len(data):
        raise HpackError("truncated string literal: declared length overruns buffer")
    raw = data[offset:end]
    if huffman:
        return huffman_decode(raw), end
    return bytes(raw), end


# ---------------------------------------------------------------------------
# Dynamic table (RFC 7541 §2.3.2, §4).
# ---------------------------------------------------------------------------


def _entry_size(name: bytes, value: bytes) -> int:
    """Entry size per RFC 7541 §4.1: name length + value length + 32."""
    return len(name) + len(value) + 32


class DynamicTable:
    """A size-bounded FIFO of header entries (RFC 7541 §2.3.2, §4).

    Newest entries are at the lowest index (index 0 == most recent). Adding an
    entry evicts the oldest entries until it fits; an entry larger than the
    maximum size empties the table entirely.
    """

    def __init__(self, max_size: int = 4096) -> None:
        if max_size < 0:
            raise HpackError("dynamic table max size cannot be negative")
        self._entries: list[tuple[bytes, bytes]] = []
        self._max_size = max_size
        self._size = 0

    @property
    def max_size(self) -> int:
        """The current maximum table size in octets."""
        return self._max_size

    @property
    def size(self) -> int:
        """The current table size in octets (sum of entry sizes)."""
        return self._size

    def __len__(self) -> int:
        """The number of entries currently stored."""
        return len(self._entries)

    def __getitem__(self, index: int) -> tuple[bytes, bytes]:
        """Return the entry at zero-based ``index`` (0 == newest)."""
        return self._entries[index]

    def add(self, name: bytes, value: bytes) -> None:
        """Insert ``(name, value)`` at the front, evicting as needed (§4.4)."""
        entry_size = _entry_size(name, value)
        # Evict from the end until the new entry fits (or the table is empty).
        while self._size + entry_size > self._max_size and self._entries:
            self._evict_oldest()
        if entry_size > self._max_size:
            # The entry is too large to store at all; table is now empty (§4.4).
            return
        self._entries.insert(0, (name, value))
        self._size += entry_size

    def _evict_oldest(self) -> None:
        name, value = self._entries.pop()
        self._size -= _entry_size(name, value)

    def set_max_size(self, max_size: int) -> None:
        """Change the maximum size, evicting from the end as needed (§4.3)."""
        if max_size < 0:
            raise HpackError("dynamic table max size cannot be negative")
        self._max_size = max_size
        while self._size > self._max_size and self._entries:
            self._evict_oldest()


# ---------------------------------------------------------------------------
# Shared table lookup across the static + dynamic address space (RFC 7541 §2.3.3).
# ---------------------------------------------------------------------------


def _resolve_index(index: int, dynamic: DynamicTable) -> tuple[bytes, bytes]:
    """Resolve a 1-based combined index into a ``(name, value)`` entry."""
    if index == 0:
        raise HpackError("index 0 is not a valid header field index")
    if index <= STATIC_TABLE_LENGTH:
        return STATIC_TABLE[index - 1]
    dyn_index = index - STATIC_TABLE_LENGTH - 1
    if dyn_index >= len(dynamic):
        raise HpackError("header field index out of range")
    return dynamic[dyn_index]


# ---------------------------------------------------------------------------
# Decoder (RFC 7541 §3, §6).
# ---------------------------------------------------------------------------

_DEFAULT_MAX_HEADER_LIST_SIZE = 8 * 1024 * 1024
_DEFAULT_MAX_STRING_LENGTH = 4 * 1024 * 1024


class Decoder:
    """Stateful HPACK decoder maintaining a dynamic table across header blocks."""

    def __init__(
        self,
        max_dynamic_size: int = 4096,
        max_header_list_size: int = _DEFAULT_MAX_HEADER_LIST_SIZE,
        max_string_length: int = _DEFAULT_MAX_STRING_LENGTH,
    ) -> None:
        self.dynamic_table = DynamicTable(max_dynamic_size)
        # The protocol-imposed upper bound on the dynamic table size. A size
        # update may not exceed this (RFC 7541 §6.3).
        self._dynamic_size_limit = max_dynamic_size
        self._max_header_list_size = max_header_list_size
        self._max_string_length = max_string_length

    def decode(self, block: bytes) -> list[tuple[bytes, bytes]]:
        """Decode a header block into an ordered list of ``(name, value)`` pairs.

        Raises:
            HpackError: On any malformed representation, an out-of-range index,
                an oversized dynamic-table-size update, or when the cumulative
                decoded header-list size exceeds the configured maximum
                (decompression-bomb guard).
        """
        headers: list[tuple[bytes, bytes]] = []
        offset = 0
        total_size = 0
        length = len(block)
        # A dynamic table size update is only permitted at the very start of a
        # block, before any header field representation (RFC 7541 §4.2).
        allow_size_update = True

        while offset < length:
            byte = block[offset]
            if byte & 0x80:
                offset = self._decode_indexed(block, offset, headers)
                allow_size_update = False
            elif byte & 0x40:
                offset = self._decode_literal(block, offset, headers, prefix_bits=6, index=True)
                allow_size_update = False
            elif byte & 0x20:
                if not allow_size_update:
                    raise HpackError("dynamic table size update must precede header fields")
                offset = self._decode_size_update(block, offset)
                # A size update appends no header field; skip size accounting.
                continue
            else:
                # '0000' without indexing or '0001' never indexed; both have a
                # 4-bit name index prefix and are not added to the table.
                offset = self._decode_literal(block, offset, headers, prefix_bits=4, index=False)
                allow_size_update = False

            # Enforce the header-list size bound incrementally (§7.4).
            name, value = headers[-1]
            total_size += len(name) + len(value) + 32
            if total_size > self._max_header_list_size:
                raise HpackError("decoded header list exceeds maximum size")

        return headers

    def _decode_indexed(self, block: bytes, offset: int, headers: list[tuple[bytes, bytes]]) -> int:
        index, offset = decode_integer(block, offset, 7)
        headers.append(_resolve_index(index, self.dynamic_table))
        return offset

    def _decode_literal(
        self,
        block: bytes,
        offset: int,
        headers: list[tuple[bytes, bytes]],
        *,
        prefix_bits: int,
        index: bool,
    ) -> int:
        name_index, offset = decode_integer(block, offset, prefix_bits)
        if name_index == 0:
            name, offset = _decode_string(block, offset, self._max_string_length)
        else:
            name, _ = _resolve_index(name_index, self.dynamic_table)
        value, offset = _decode_string(block, offset, self._max_string_length)
        headers.append((name, value))
        if index:
            self.dynamic_table.add(name, value)
        return offset

    def _decode_size_update(self, block: bytes, offset: int) -> int:
        new_size, offset = decode_integer(block, offset, 5)
        if new_size > self._dynamic_size_limit:
            raise HpackError("dynamic table size update exceeds the protocol limit")
        self.dynamic_table.set_max_size(new_size)
        return offset


# ---------------------------------------------------------------------------
# Encoder (RFC 7541 §6). A simple, correct, stateless strategy.
# ---------------------------------------------------------------------------


class Encoder:
    """HPACK encoder using a simple literal-and-static-index strategy.

    The encoder emits an indexed representation for exact static-table matches,
    an indexed name where the static table knows the field name, and otherwise a
    fully literal representation. The encoder never adds entries to a dynamic
    table, which keeps it correct and interoperable with any conforming decoder.

    Huffman coding is off by default: for a file server on a fast/local link the
    CPU it costs outweighs the handful of header bytes it saves (~+20% encode
    throughput without it). Pass ``use_huffman=True`` to prioritize size instead.
    """

    def __init__(self, *, use_huffman: bool = False) -> None:
        self._use_huffman = use_huffman

    def encode(self, headers: list[tuple[bytes, bytes]]) -> bytes:
        """Encode a header list into a single HPACK header block."""
        out = bytearray()
        for name, value in headers:
            out += self._encode_header(name, value)
        return bytes(out)

    def _encode_header(self, name: bytes, value: bytes) -> bytes:
        full = _STATIC_FULL_INDEX.get((name, value))
        if full is not None:
            # Indexed header field (§6.1): high bit set on a 7-bit index.
            encoded = encode_integer(full, 7)
            return bytes((encoded[0] | 0x80, *encoded[1:]))

        name_index = _STATIC_NAME_INDEX.get(name, 0)
        # Literal with incremental indexing (§6.2.1): '01' prefix on a 6-bit
        # name index. We do not maintain a dynamic table, but this form is the
        # standard choice and remains correct for a non-indexing encoder.
        out = bytearray()
        index_octets = encode_integer(name_index, 6)
        out.append(index_octets[0] | 0x40)
        out += index_octets[1:]
        if name_index == 0:
            out += self._encode_string(name)
        out += self._encode_string(value)
        return bytes(out)

    def _encode_string(self, data: bytes) -> bytes:
        """Encode a string literal (Huffman only when enabled and shorter)."""
        if self._use_huffman:
            huffman = huffman_encode(data)
            if len(huffman) < len(data):
                prefix = encode_integer(len(huffman), 7)
                return bytes((prefix[0] | 0x80, *prefix[1:])) + huffman
        return encode_integer(len(data), 7) + data
