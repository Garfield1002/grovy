"""Tests for grovylib.qr, the little byte-mode QR encoder used for the setup login code.

These stay stdlib-only: the round-trip decoder below reads a symbol back the way a scanner would (read the
format bits, undo the mask, walk the zig-zag, de-interleave the blocks, strip the error correction, parse the
bytes), so a successful round-trip proves the whole pipeline is self-consistent and scannable. When the system
`qrencode` happens to be installed, we also check a few symbols match it module for module.
"""
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from grovylib import qr  # noqa: E402

_EC_FROM_BITS = {1: "L", 0: "M", 3: "Q", 2: "H"}
_REMAINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}


def decode(mods):
    """Decode a byte-mode QR matrix back to (bytes, ec_level, mask) the way a reader would."""
    n = len(mods)
    version = (n - 17) // 4
    rl = [0, 1, 2, 3, 4, 5, 7, 8, 8, 8, 8, 8, 8, 8, 8]
    cl = [8, 8, 8, 8, 8, 8, 8, 8, 7, 5, 4, 3, 2, 1, 0]
    fv = 0
    for i in range(15):
        fv |= mods[rl[i]][cl[i]] << i
    fv ^= 0x5412
    ec = _EC_FROM_BITS[(fv >> 13) & 3]
    mask = (fv >> 10) & 7

    base = qr._Matrix(version)
    base.draw_functions()
    base.draw_version()
    stream = []
    for right in range(n - 1, 0, -2):
        right = right - 1 if right <= 6 else right
        for vert in range(n):
            for j in range(2):
                c = right - j
                upward = ((right + 1) & 2) == 0
                r = (n - 1 - vert) if upward else vert
                if not base.fun[r][c]:
                    stream.append(mods[r][c] ^ (1 if qr._Matrix._mask(mask, r, c) else 0))

    ecw, groups = qr._EC[ec][version - 1]
    total = sum(c * d for c, d in groups)
    nblocks = sum(c for c, _ in groups)
    ncw = total + ecw * nblocks
    cws = [int("".join(map(str, stream[i:i + 8])), 2) for i in range(0, ncw * 8, 8)]

    dlens = [d for c, d in groups for _ in range(c)]
    blocks, idx = [[] for _ in range(nblocks)], 0
    for col in range(max(dlens)):
        for bi in range(nblocks):
            if col < dlens[bi]:
                blocks[bi].append(cws[idx])
                idx += 1
    data = [x for blk in blocks for x in blk]

    bits = [(cw >> i) & 1 for cw in data for i in range(7, -1, -1)]
    cnt_bits = 16 if version >= 10 else 8
    cnt = int("".join(map(str, bits[4:4 + cnt_bits])), 2)
    start = 4 + cnt_bits
    out = bytes(int("".join(map(str, bits[start + 8 * k:start + 8 * k + 8])), 2) for k in range(cnt))
    return out, ec, mask


def _block_valid(mods):
    """Every block's data+EC codewords must be divisible by the generator (i.e. the EC is correct)."""
    n = len(mods)
    version = (n - 17) // 4
    _, ec, mask = decode(mods)
    base = qr._Matrix(version)
    base.draw_functions()
    base.draw_version()
    stream = []
    for right in range(n - 1, 0, -2):
        right = right - 1 if right <= 6 else right
        for vert in range(n):
            for j in range(2):
                c = right - j
                upward = ((right + 1) & 2) == 0
                r = (n - 1 - vert) if upward else vert
                if not base.fun[r][c]:
                    stream.append(mods[r][c] ^ (1 if qr._Matrix._mask(mask, r, c) else 0))
    ecw, groups = qr._EC[ec][version - 1]
    total = sum(c * d for c, d in groups)
    nblocks = sum(c for c, _ in groups)
    cws = [int("".join(map(str, stream[i:i + 8])), 2) for i in range(0, (total + ecw * nblocks) * 8, 8)]
    dlens = [d for c, d in groups for _ in range(c)]
    dblocks, idx = [[] for _ in range(nblocks)], 0
    for col in range(max(dlens)):
        for bi in range(nblocks):
            if col < dlens[bi]:
                dblocks[bi].append(cws[idx])
                idx += 1
    eblocks, idx = [[] for _ in range(nblocks)], total
    for row in range(ecw):
        for bi in range(nblocks):
            eblocks[bi].append(cws[idx])
            idx += 1
    gen = qr._rs_generator(ecw)
    for d, e in zip(dblocks, eblocks):
        rem = list(d + e)
        for i in range(len(d)):
            coef = rem[i]
            if coef:
                for j in range(len(gen)):
                    rem[i + j] ^= qr._gf_mul(gen[j], coef)
        if any(rem[-ecw:]):
            return False
    return True


class TestQr(unittest.TestCase):
    def test_reed_solomon_known_vector(self):
        # Canonical 'HELLO WORLD' version 1-M data codewords and their known EC codewords.
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
        self.assertEqual(qr._rs_remainder(data, 10), [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])

    def test_version_selection(self):
        self.assertEqual(len(qr.matrix("x", "M")), 21)          # version 1 -> 21x21
        self.assertEqual(len(qr.matrix("x" * 40, "M")), 29)     # spills into version 3
        with self.assertRaises(ValueError):                     # beyond our version-10 ceiling
            qr.matrix("x" * 300, "H")

    def test_round_trip(self):
        samples = [
            "x", "HELLO", "http://192.168.1.23:7333/?token=a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
            "http://10.0.0.1:7333/", "".join(chr(33 + (i % 90)) for i in range(120)),
            bytes(range(60)),
        ]
        for ec in "LMQH":
            for s in samples:
                want = s.encode() if isinstance(s, str) else s
                try:
                    mods = qr.matrix(s, ec)
                except ValueError:
                    continue
                got, got_ec, mask = decode(mods)
                self.assertEqual(got, want, f"round-trip failed ec={ec} data={s!r}")
                self.assertEqual(got_ec, ec)
                self.assertIn(mask, range(8))
                self.assertTrue(_block_valid(mods), f"invalid EC ec={ec} data={s!r}")

    def test_render_is_theme_independent_half_blocks(self):
        text = qr.render("hello", quiet=2)
        lines = text.splitlines()
        self.assertTrue(all("▀" in ln for ln in lines))      # every row drawn with the upper half block
        self.assertTrue(all(ln.endswith("\033[0m") for ln in lines))  # and reset so colours don't bleed
        self.assertIn("\033[30;40m", text)                        # black-on-black (a dark module over a dark one)
        self.assertIn("\033[37;47m", text)                        # white-on-white (the quiet zone)

    @unittest.skipUnless(shutil.which("qrencode"), "qrencode not installed")
    def test_matches_qrencode(self):
        def theirs(data, ec, v):
            out = subprocess.run(["qrencode", "-t", "ASCII", "-m", "0", "-8", "--strict-version",
                                  "-l", ec, "-v", str(v), data], capture_output=True, text=True)
            return [[1 if ln[i:i + 2] == "##" else 0 for i in range(0, len(ln), 2)]
                    for ln in out.stdout.splitlines() if ln]
        # Module-for-module identical (same spec, same deterministic mask choice) on representative symbols.
        exact = [("HELLO", "L", 1), ("grovy", "M", 1), ("http://10.0.0.5:7333/?token=deadbeef", "M", 3),
                 ("ABCDEFGHIJKLMNOPQ", "L", 1)]
        for data, ec, v in exact:
            self.assertEqual(qr.matrix(data, ec), theirs(data, ec, v), f"differs from qrencode: {data!r} {ec} v{v}")
        # Carry the same data as qrencode even where the mask choice may differ (rare boundary cases).
        for data, ec, v in exact + [("x" * 50, "Q", 5), ("x" * 40, "H", 5)]:
            self.assertEqual(decode(qr.matrix(data, ec))[0], decode(theirs(data, ec, v))[0])


if __name__ == "__main__":
    unittest.main()
