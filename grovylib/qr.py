"""A tiny byte-mode QR encoder (stdlib only), rendered as half-block characters for the terminal.

Just enough to turn the phone login link into a code you can scan off the screen: byte mode, versions 1-10,
error-correction level chosen by the caller. It follows ISO 18004, so its symbols read on any scanner; the data,
error correction and layout match `qrencode` module for module, and the mask too in all but a few boundary cases
where qrencode's own mask heuristic differs. `tests/test_qr.py` cross-checks both (round-trip, and against qrencode).
"""

# (ec codewords per block, [(block count, data codewords per block), ...]) for versions 1-10, levels L/M/Q/H.
_EC = {
    "L": [(7, [(1, 19)]), (10, [(1, 34)]), (15, [(1, 55)]), (20, [(1, 80)]), (26, [(1, 108)]),
          (18, [(2, 68)]), (20, [(2, 78)]), (24, [(2, 97)]), (30, [(2, 116)]), (18, [(2, 68), (2, 69)])],
    "M": [(10, [(1, 16)]), (16, [(1, 28)]), (26, [(1, 44)]), (18, [(2, 32)]), (24, [(2, 43)]),
          (16, [(4, 27)]), (18, [(4, 31)]), (22, [(2, 38), (2, 39)]), (22, [(3, 36), (2, 37)]),
          (26, [(4, 43), (1, 44)])],
    "Q": [(13, [(1, 13)]), (22, [(1, 22)]), (18, [(2, 17)]), (26, [(2, 24)]), (18, [(2, 15), (2, 16)]),
          (24, [(4, 19)]), (18, [(2, 14), (4, 15)]), (22, [(4, 18), (2, 19)]), (20, [(4, 16), (4, 17)]),
          (24, [(6, 19), (2, 20)])],
    "H": [(17, [(1, 9)]), (28, [(1, 16)]), (22, [(2, 13)]), (16, [(4, 9)]), (22, [(2, 11), (2, 12)]),
          (28, [(4, 15)]), (26, [(4, 13), (1, 14)]), (26, [(4, 14), (2, 15)]), (24, [(4, 12), (4, 13)]),
          (28, [(6, 15), (2, 16)])],
}
# Centre coordinates of the alignment patterns, by version (1-based index).
_ALIGN = [[], [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50]]
_EC_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}  # 2-bit error-correction level indicator for the format info.

# GF(256) with the QR primitive polynomial 0x11d.
_EXP = [0] * 255
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11d
_EXP += _EXP  # doubled (len 510) so _EXP[a + b] needs no modulo for any exponent sum 0..508


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(n):
    """Reed-Solomon generator polynomial of degree n (coefficients, high to low)."""
    g = [1]
    for i in range(n):
        g = [g[0]] + [g[j + 1] ^ _gf_mul(g[j], _EXP[i]) for j in range(len(g) - 1)] + [_gf_mul(g[-1], _EXP[i])]
    return g


def _rs_remainder(data, n):
    """The n error-correction codewords for a block of data codewords."""
    gen = _rs_generator(n)
    res = [0] * n
    for d in data:
        factor = d ^ res[0]
        res = res[1:] + [0]
        for i in range(n):
            res[i] ^= _gf_mul(gen[i + 1], factor)
    return res


def _choose_version(nbytes, ec):
    for v in range(1, 11):
        total = sum(c * d for c, d in _EC[ec][v - 1][1])
        count_bits = 16 if v >= 10 else 8
        if 4 + count_bits + 8 * nbytes <= total * 8:
            return v
    raise ValueError("data too long for a version-10 QR code")


def _encode_data(data, ec):
    v = _choose_version(len(data), ec)
    total_data = sum(c * d for c, d in _EC[ec][v - 1][1])
    count_bits = 16 if v >= 10 else 8
    bits = [0, 1, 0, 0]  # byte mode
    for i in range(count_bits - 1, -1, -1):
        bits.append((len(data) >> i) & 1)
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    bits += [0] * min(4, total_data * 8 - len(bits))   # terminator
    bits += [0] * (-len(bits) % 8)                      # pad to a byte boundary
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    while len(codewords) < total_data:                 # fill the rest with alternating pad bytes
        codewords.append(0xEC if len(codewords) % 2 == len(bits) // 8 % 2 else 0x11)
    return v, codewords


def _interleave(codewords, ec, version):
    ecw, groups = _EC[ec][version - 1]
    blocks, pos = [], 0
    for count, dlen in groups:
        for _ in range(count):
            d = codewords[pos:pos + dlen]
            pos += dlen
            blocks.append((d, _rs_remainder(d, ecw)))
    out = []
    for i in range(max(len(d) for d, _ in blocks)):
        for d, _ in blocks:
            if i < len(d):
                out.append(d[i])
    for i in range(ecw):
        for _, e in blocks:
            out.append(e[i])
    return out


class _Matrix:
    def __init__(self, version):
        self.version = version
        self.size = 17 + 4 * version
        self.mod = [[0] * self.size for _ in range(self.size)]        # 0/1, drawn modules
        self.fun = [[False] * self.size for _ in range(self.size)]    # True where a function pattern lives

    def _set(self, r, c, val):
        self.mod[r][c] = val
        self.fun[r][c] = True

    def _finder(self, r, c):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if not (0 <= rr < self.size and 0 <= cc < self.size):
                    continue
                if dr in (-1, 7) or dc in (-1, 7):
                    self._set(rr, cc, 0)                        # the one-module separator
                else:
                    ring = max(abs(dr - 3), abs(dc - 3))
                    self._set(rr, cc, 1 if ring != 2 else 0)    # dark, with a light ring at distance 2

    def draw_functions(self):
        n = self.size
        for pos in ((0, 0), (0, n - 7), (n - 7, 0)):
            self._finder(*pos)
        for i in range(8, n - 8):                   # timing patterns
            self._set(6, i, 1 - i % 2)
            self._set(i, 6, 1 - i % 2)
        centres = _ALIGN[self.version]
        corners = {(6, 6), (6, n - 7), (n - 7, 6)}      # these three coincide with the finders: skip them
        for r in centres:
            for c in centres:
                if (r, c) in corners:
                    continue
                for dr in range(-2, 3):                  # 5x5, overriding the timing pattern where they cross
                    for dc in range(-2, 3):
                        self._set(r + dr, c + dc, 1 if max(abs(dr), abs(dc)) != 1 else 0)
        for i in range(9):                           # reserve the format-info strips (filled later)
            if i != 6:
                self._set(8, i, 0)
                self._set(i, 8, 0)
        for i in range(8):                           # horizontal strip by the top-right finder
            self._set(8, n - 1 - i, 0)
        for i in range(7):                           # vertical strip by the bottom-left finder
            self._set(n - 1 - i, 8, 0)
        self._set(n - 8, 8, 1)                       # the always-dark module, just below that strip
        if self.version >= 7:
            for i in range(18):
                self._set(n - 11 + i % 3, i // 3, 0)
                self._set(i // 3, n - 11 + i % 3, 0)

    def place_data(self, bits):
        n, i = self.size, 0
        for right in range(n - 1, 0, -2):
            right = right - 1 if right <= 6 else right   # skip the vertical timing column (6) entirely
            for vert in range(n):
                for j in range(2):
                    c = right - j
                    upward = ((right + 1) & 2) == 0
                    r = (n - 1 - vert) if upward else vert
                    if not self.fun[r][c]:
                        self.mod[r][c] = bits[i] if i < len(bits) else 0
                        i += 1

    @staticmethod
    def _mask(m, r, c):
        return [((r + c) % 2 == 0), (r % 2 == 0), (c % 3 == 0), ((r + c) % 3 == 0),
                ((r // 2 + c // 3) % 2 == 0), ((r * c) % 2 + (r * c) % 3 == 0),
                (((r * c) % 2 + (r * c) % 3) % 2 == 0), (((r + c) % 2 + (r * c) % 3) % 2 == 0)][m]

    def apply_mask(self, m):
        for r in range(self.size):
            for c in range(self.size):
                if not self.fun[r][c] and self._mask(m, r, c):
                    self.mod[r][c] ^= 1

    def draw_format(self, ec, mask):
        data = (_EC_BITS[ec] << 3) | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412
        n = self.size
        for i in range(15):
            b = (bits >> i) & 1
            r = [0, 1, 2, 3, 4, 5, 7, 8, 8, 8, 8, 8, 8, 8, 8][i]
            c = [8, 8, 8, 8, 8, 8, 8, 8, 7, 5, 4, 3, 2, 1, 0][i]
            self._set(r, c, b)                                       # the copy around the top-left finder
            if i < 8:
                self._set(8, n - 1 - i, b)                          # bits 0-7 run along row 8 (top-right)
            else:
                self._set(n - 15 + i, 8, b)                         # bits 8-14 run up column 8 (bottom-left)

    def draw_version(self):
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1f25)
        bits = (self.version << 12) | rem
        n = self.size
        for i in range(18):
            b = (bits >> i) & 1
            a, c = i // 3, i % 3
            self._set(n - 11 + c, a, b)
            self._set(a, n - 11 + c, b)

    def penalty(self):
        n, score = self.size, 0
        for line in (self.mod, [list(col) for col in zip(*self.mod)]):
            for row in line:
                runs = [-1, 1] if row[0] else [1]        # -1 is a boundary sentinel before a leading dark run
                for x in range(1, n):
                    if row[x] == row[x - 1]:
                        runs[-1] += 1
                    else:
                        runs.append(1)
                for i, rl in enumerate(runs):
                    if rl >= 5:                          # rule 1: five or more equal modules in a row
                        score += 3 + (rl - 5)
                    if i & 1 and 3 <= i < len(runs) - 2 and rl % 3 == 0:  # rule 3: a 1:1:3:1:1 finder shape...
                        f = rl // 3
                        if runs[i - 2] == f == runs[i - 1] == runs[i + 1] == runs[i + 2]:
                            after = runs[i + 3] if i + 3 < len(runs) else 10 ** 9  # off the edge is light margin
                            if runs[i - 3] < 0 or runs[i - 3] >= 4 * f or after >= 4 * f:  # ...bordered by light
                                score += 40
        for r in range(n - 1):                           # rule 2: solid 2x2 blocks
            for c in range(n - 1):
                s = self.mod[r][c] + self.mod[r][c + 1] + self.mod[r + 1][c] + self.mod[r + 1][c + 1]
                if s in (0, 4):
                    score += 3
        dark = sum(sum(row) for row in self.mod)         # rule 4: deviation from 50% dark
        k = (abs(dark * 20 - n * n * 10) + n * n - 1) // (n * n) - 1
        score += 10 * max(k, 0)
        return score


def matrix(data, ec="M"):
    """Return the QR modules for `data` (bytes or str) as a list of rows of 0/1."""
    if isinstance(data, str):
        data = data.encode()
    version, codewords = _encode_data(data, ec)
    bits = []
    for cw in _interleave(codewords, ec, version):
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    bits += [0] * {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}[version]  # trailing remainder bits

    best, best_score = None, None
    for mask in range(8):
        m = _Matrix(version)
        m.draw_functions()
        m.draw_version()
        m.place_data(bits)
        m.apply_mask(mask)
        m.draw_format(ec, mask)
        s = m.penalty()
        if best_score is None or s < best_score:
            best, best_score = m, s
    return best.mod


def terminal(mods, quiet=4):
    """Render a module matrix as half-block lines with explicit black/white colours (theme-independent)."""
    n = len(mods)
    grid = [[0] * (n + 2 * quiet) for _ in range(quiet)]
    for row in mods:
        grid.append([0] * quiet + list(row) + [0] * quiet)
    grid += [[0] * (n + 2 * quiet) for _ in range(quiet)]
    if len(grid) % 2:
        grid.append([0] * len(grid[0]))
    lines = []
    for r in range(0, len(grid), 2):
        out = []
        for top, bot in zip(grid[r], grid[r + 1]):
            fg = 30 if top else 37
            bg = 40 if bot else 47
            out.append(f"\033[{fg};{bg}m▀")
        lines.append("".join(out) + "\033[0m")
    return "\n".join(lines)


def render(data, ec="M", quiet=4):
    return terminal(matrix(data, ec), quiet)
