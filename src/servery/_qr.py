"""Pure-stdlib QR Code generator (ISO/IEC 18004) for terminal display.

Scoped to what a "scan this URL" feature needs: **byte mode**, **error-correction
level L**, **versions 1-10** (up to 271 bytes — any LAN URL). No third-party
dependency. Structure follows Nayuki's reference encoder; correctness is pinned by
comparing the generated module matrix bit-for-bit against the ``qrcode`` library in
the tests.
"""

from __future__ import annotations

# --- Galois field GF(2^8), primitive polynomial 0x11D (RS for the EC codewords) ---


def _build_gf() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_EXP, _LOG = _build_gf()


def _gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    """The generator polynomial (coefficients) for ``degree`` EC codewords."""
    poly = [1]
    for i in range(degree):
        # multiply poly by (x - alpha^i), alpha^i == _EXP[i]
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= _gf_mul(coef, 1)
            nxt[j + 1] ^= _gf_mul(coef, _EXP[i])
        poly = nxt
    return poly


def _rs_encode(data: list[int], ec_len: int) -> list[int]:
    """Reed-Solomon EC codewords for ``data`` (polynomial division by the generator)."""
    gen = _rs_generator(ec_len)
    rem = [0] * ec_len
    for byte in data:
        factor = byte ^ rem[0]
        rem = [*rem[1:], 0]
        for i in range(ec_len):
            rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


# --- per-version characteristics for EC level L: (data_cw, ec_per_block, blocks, byte_cap) ---
_L = {
    1: (19, 7, 1, 17),
    2: (34, 10, 1, 32),
    3: (55, 15, 1, 53),
    4: (80, 20, 1, 78),
    5: (108, 26, 1, 106),
    6: (136, 18, 2, 134),
    7: (156, 20, 2, 154),
    8: (194, 24, 2, 192),
    9: (232, 30, 2, 230),
    10: (274, 18, 4, 271),
}
_ALIGN = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}
_REMAINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}


class QrError(ValueError):
    """The data does not fit in a supported QR version (1-10, level L)."""


def _pick_version(length: int) -> int:
    for version in range(1, 11):
        if length <= _L[version][3]:
            return version
    raise QrError(f"{length} bytes exceeds QR v10-L capacity (271)")


def _assemble_codewords(data: bytes, version: int) -> list[int]:
    """Build the final interleaved data+EC codeword stream for ``data``."""
    total_data, ec_per_block, num_blocks, _cap = _L[version]
    count_bits = 8 if version < 10 else 16

    # Bit stream: mode (byte=0100) + char count + payload bytes.
    bits: list[int] = [0, 1, 0, 0]
    bits.extend((len(data) >> i) & 1 for i in range(count_bits - 1, -1, -1))
    bits.extend((byte >> i) & 1 for byte in data for i in range(7, -1, -1))
    # Terminator (up to 4 zero bits), then pad to a byte boundary.
    bits.extend([0] * min(4, total_data * 8 - len(bits)))
    bits.extend([0] * (-len(bits) % 8))
    codewords = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]
    # Pad codewords with the alternating 0xEC / 0x11 pattern.
    codewords.extend(0xEC if i % 2 == 0 else 0x11 for i in range(total_data - len(codewords)))

    # Split into blocks, EC-encode each, then interleave (data columns, then EC columns).
    base, extra = divmod(total_data, num_blocks)
    sizes = [base] * (num_blocks - extra) + [base + 1] * extra
    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    pos = 0
    for size in sizes:
        block = codewords[pos : pos + size]
        pos += size
        data_blocks.append(block)
        ec_blocks.append(_rs_encode(block, ec_per_block))

    result: list[int] = []
    for i in range(max(sizes)):
        result.extend(block[i] for block in data_blocks if i < len(block))
    for i in range(ec_per_block):
        result.extend(block[i] for block in ec_blocks)
    return result


# --- matrix construction --------------------------------------------------------


def _new_matrix(size: int) -> tuple[list[list[int | None]], list[list[bool]]]:
    return [[None] * size for _ in range(size)], [[False] * size for _ in range(size)]


def _place_finder(mod: list[list[int | None]], fn: list[list[bool]], row: int, col: int) -> None:
    for r in range(-1, 8):
        for c in range(-1, 8):
            rr, cc = row + r, col + c
            if not (0 <= rr < len(mod) and 0 <= cc < len(mod)):
                continue
            inring = 0 <= r <= 6 and 0 <= c <= 6
            dark = inring and (r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4))
            mod[rr][cc] = 1 if dark else 0
            fn[rr][cc] = True


def _place_functions(version: int) -> tuple[list[list[int | None]], list[list[bool]]]:
    size = 21 + 4 * (version - 1)
    mod, fn = _new_matrix(size)
    for r, c in ((0, 0), (0, size - 7), (size - 7, 0)):
        _place_finder(mod, fn, r, c)
    # Timing patterns.
    for i in range(size):
        if mod[6][i] is None:
            mod[6][i] = 1 if i % 2 == 0 else 0
            fn[6][i] = True
        if mod[i][6] is None:
            mod[i][6] = 1 if i % 2 == 0 else 0
            fn[i][6] = True
    # Alignment patterns (every centre combo except where they'd hit a finder).
    centers = _ALIGN[version]
    for r in centers:
        for c in centers:
            if (r, c) in ((6, 6), (6, size - 7), (size - 7, 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    ring = dr in (-2, 2) or dc in (-2, 2) or (dr == 0 and dc == 0)
                    mod[r + dr][c + dc] = 1 if ring else 0
                    fn[r + dr][c + dc] = True
    # Dark module + reserve the format/version areas (filled later).
    mod[4 * version + 9][8] = 1
    fn[4 * version + 9][8] = True
    for i in range(9):  # format info around the top-left finder
        for rr, cc in ((8, i), (i, 8)):
            if mod[rr][cc] is None:
                fn[rr][cc] = True
    for i in range(8):
        fn[8][size - 1 - i] = True
        fn[size - 1 - i][8] = True
    if version >= 7:
        for i in range(6):
            for j in range(3):
                fn[i][size - 11 + j] = True
                fn[size - 11 + j][i] = True
    return mod, fn


def _place_data(mod: list[list[int | None]], fn: list[list[bool]], codewords: list[int]) -> None:
    size = len(mod)
    bits = [(cw >> i) & 1 for cw in codewords for i in range(7, -1, -1)]
    idx = 0
    # Two columns at a time, right to left; within a pair, zig-zag up then down.
    for right in range(size - 1, 0, -2):
        rcol = right - 1 if right <= 6 else right  # the vertical timing column (6) is skipped
        upward = ((rcol + 1) & 2) == 0
        for vert in range(size):
            row = size - 1 - vert if upward else vert
            for col in (rcol, rcol - 1):
                if not fn[row][col] and idx < len(bits):
                    mod[row][col] = bits[idx]
                    idx += 1


_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


def _format_value(mask: int) -> int:
    """The 15-bit format-information value for EC level L (01) + ``mask`` (BCH 15,5)."""
    data = (0b01 << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _version_value(version: int) -> int:
    """The 18-bit version-information value for ``version`` (BCH 18,6)."""
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


def _apply_format(mod: list[list[int]], version: int, mask: int) -> None:
    size = len(mod)
    value = _format_value(mask)

    def bit(i: int) -> int:
        return (value >> i) & 1

    # Copy 1: down the left of the top-left finder, then across its bottom.
    for i in range(6):
        mod[i][8] = bit(i)
    mod[7][8] = bit(6)
    mod[8][8] = bit(7)
    mod[8][7] = bit(8)
    for i in range(9, 15):
        mod[8][14 - i] = bit(i)
    # Copy 2: across the top-right finder, then up the bottom-left finder.
    for i in range(8):
        mod[8][size - 1 - i] = bit(i)
    for i in range(8, 15):
        mod[size - 15 + i][8] = bit(i)
    if version >= 7:
        vvalue = _version_value(version)
        for i in range(18):
            vbit = (vvalue >> i) & 1
            a, b = size - 11 + i % 3, i // 3
            mod[a][b] = vbit
            mod[b][a] = vbit


def _penalty(mod: list[list[int]]) -> int:
    size = len(mod)
    score = 0
    # Rule 1: runs of >= 5 same-colour modules (rows + columns).
    for line in (mod, [list(col) for col in zip(*mod, strict=True)]):
        for row in line:
            run = 1
            for i in range(1, size):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run = 1
            if run >= 5:
                score += run - 2
    # Rule 2: 2x2 same-colour blocks.
    for r in range(size - 1):
        for c in range(size - 1):
            if mod[r][c] == mod[r][c + 1] == mod[r + 1][c] == mod[r + 1][c + 1]:
                score += 3
    # Rule 3: finder-like 1:1:3:1:1 patterns flanked by light, in rows and columns.
    patterns = ([1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1])
    for line in (mod, [list(col) for col in zip(*mod, strict=True)]):
        for row in line:
            for c in range(size - 10):
                if row[c : c + 11] in patterns:
                    score += 40
    # Rule 4: deviation of the dark-module proportion from 50% (10 points per 5%).
    dark = sum(sum(row) for row in mod)
    percent = dark * 100 / (size * size)
    score += int(abs(percent - 50) / 5) * 10
    return score


def _render_mask(
    base_mod: list[list[int | None]], fn: list[list[bool]], version: int, mask: int
) -> list[list[int]]:
    # Unplaced cells are remainder bits: they are 0 and, like all data modules, get
    # masked — so fill None->0 first, then mask every non-function module.
    trial = [[0 if v is None else v for v in row] for row in base_mod]
    for r in range(len(trial)):
        for c in range(len(trial)):
            if not fn[r][c] and _MASKS[mask](r, c):
                trial[r][c] ^= 1
    _apply_format(trial, version, mask)
    return trial


def generate(text: str, mask: int | None = None) -> list[list[int]]:
    """Return the QR module matrix (1 = dark, 0 = light) for ``text`` (byte mode, L).

    ``mask`` forces a specific mask pattern (0-7); the default picks the lowest-
    penalty mask per the spec.
    """
    data = text.encode("utf-8")
    version = _pick_version(len(data))
    codewords = _assemble_codewords(data, version)
    codewords += [0] * (_REMAINDER[version] // 8)  # whole remainder bytes, if any
    base_mod, fn = _place_functions(version)
    _place_data(base_mod, fn, codewords)

    masks = [mask] if mask is not None else range(8)
    best: list[list[int]] | None = None
    best_score = -1
    for m in masks:
        solid = _render_mask(base_mod, fn, version, m)
        score = _penalty(solid)
        if best is None or score < best_score:
            best, best_score = solid, score
    assert best is not None
    return best


def render(matrix: list[list[int]], quiet: int = 2) -> str:
    """Render a module matrix to a string using Unicode half-blocks (2 rows/line)."""
    size = len(matrix)
    blank = [0] * (size + 2 * quiet)
    padded = [blank[:] for _ in range(quiet)]
    padded.extend([0] * quiet + list(row) + [0] * quiet for row in matrix)
    padded += [blank[:] for _ in range(quiet)]
    glyphs = {(0, 0): "█", (0, 1): "▀", (1, 0): "▄", (1, 1): " "}
    lines = []
    for r in range(0, len(padded), 2):
        top = padded[r]
        bottom = padded[r + 1] if r + 1 < len(padded) else [0] * len(top)
        lines.append("".join(glyphs[(top[c], bottom[c])] for c in range(len(top))))
    return "\n".join(lines)
