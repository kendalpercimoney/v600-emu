"""Port names from the ROKR E1 R373 build database (docs/re, extracted with the IDB scanner)
to our V600 0B.09.38R build. Same platform (Neptune LTE), same compiler; most library and
driver code is identical apart from branch offsets and literal pools.

1. Mask branch offsets (Thumb BL/BLX pairs, ARM B/BL) in both images.
2. Seed: a named E1 function whose first WINDOW masked bytes are unique in E1 and occur at
   exactly one place in our image.
3. Grow: for seeded pairs whose masked bodies agree, pair the targets of their calls in order.

Output (git-ignored): out/symbols_e1_exact.json {name: address|1 for Thumb}.
"""
import bisect
import difflib
import json
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from v600 import superfile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RE = ROOT / 'docs/re'
WINDOW = 24
AUTO = re.compile(r'^(a[A-Z0-9_]|sub_|loc_|off_|unk_|byte_|word_|dword_|stru_|asc_|nullsub|j_|def_|'
                  r'jpt_|locret_|flt_|dbl_|var_|arg_|case_|\$|\.|_stru_|jstr_|Bytecode_|Method_|'
                  r'StackMap_|Handlers_|Consts_|Class_)')


def load_ours():
    return {a: bytes(b) for a, b in superfile.load(ROOT / 'firmware/v600.shx')}


def load_e1():
    out = {}
    for f in sorted(RE.glob('e1_*.bin')):
        out[int(f.stem[3:], 16)] = f.read_bytes()
    return out


def mask(img):
    """Return (thumb-masked, arm-masked) copies of a big-endian code image."""
    a = np.frombuffer(img, dtype=np.uint8).copy()
    n = len(a) & ~3
    t = a.copy()
    hw = (a[:n:2].astype(np.uint16) << 8) | a[1:n:2]
    bl = ((hw[:-1] & 0xF800) == 0xF000) & ((hw[1:] & 0xE800) == 0xE800)
    idx = np.nonzero(bl)[0] * 2
    for k in (1, 2, 3):                     # keep the 0xF0/0xF8 prefixes' top 5 bits only
        t[idx + k] = 0
    t[idx] &= 0xF8
    t[idx + 2] &= 0xF8
    r = a.copy()
    w0 = a[:n:4]
    br = (w0 & 0x0E) == 0x0A                # B / BL (any condition)
    idx = np.nonzero(br)[0] * 4
    r[idx + 1] = r[idx + 2] = r[idx + 3] = 0
    return t.tobytes(), r.tobytes()


def is_arm(img, off):
    """ARM code: two leading words with condition 'always' (Thumb rarely looks like that)."""
    if off % 4:
        return False
    return img[off] >> 4 == 0xE and img[off + 4] >> 4 == 0xE


class Index:
    """WINDOW-byte windows at every `step`-aligned offset, as sorted 64-bit hashes."""

    def __init__(self, img, step):
        self.img = img
        a = np.frombuffer(img, dtype=np.uint8)
        n = (len(a) - WINDOW) // step
        h = np.zeros(n, dtype=np.uint64)
        for k in range(0, WINDOW, 8):
            v = np.zeros(n, dtype=np.uint64)
            for j in range(8):
                v = (v << np.uint64(8)) | a[k + j:k + j + n * step:step][:n].astype(np.uint64)
            h = h * np.uint64(0x9E3779B97F4A7C15) ^ v
        order = np.argsort(h, kind='stable')
        self.h = h[order]
        self.off = order * step

    @staticmethod
    def hash(key):
        h = 0
        for k in range(0, WINDOW, 8):
            h = ((h * 0x9E3779B97F4A7C15) & (2**64 - 1)) ^ int.from_bytes(key[k:k + 8], 'big')
        return h

    def get(self, key, default=()):
        h = np.uint64(self.hash(key))
        lo, hi = np.searchsorted(self.h, h, 'left'), np.searchsorted(self.h, h, 'right')
        return [int(o) for o in self.off[lo:hi] if self.img[o:o + WINDOW] == key] or default


def calls_at(img_raw, off, size, thumb):
    """[(call offset, (target offset, target is Thumb))] inside a function, in order."""
    out = []
    if thumb:
        i = off
        while i + 4 <= off + size:
            h1 = int.from_bytes(img_raw[i:i + 2], 'big')
            h2 = int.from_bytes(img_raw[i + 2:i + 4], 'big')
            if (h1 & 0xF800) == 0xF000 and (h2 & 0xE800) == 0xE800:
                hi = h1 & 0x7FF
                if hi & 0x400:
                    hi -= 0x800
                tgt = i + 4 + (hi << 12) + ((h2 & 0x7FF) << 1)
                out.append((i, (tgt, bool(h2 & 0x1000))))   # Thumb BL, or BLX to ARM
                i += 4
            else:
                i += 2
    else:
        for i in range(off, off + size - 3, 4):
            w = int.from_bytes(img_raw[i:i + 4], 'big')
            if (w & 0x0F000000) == 0x0B000000:
                o = w & 0xFFFFFF
                if o & 0x800000:
                    o -= 0x1000000
                out.append((i, (i + 8 + 4 * o, False)))
    return out


def main():
    ours = load_ours()
    base = 0x10080000
    our_raw = ours[base]
    our_t, our_a = mask(our_raw)
    e1 = load_e1()
    e1_base = 0x10081ED0
    e_raw = e1[e1_base]
    e_t, e_a = mask(e_raw)

    names = json.load(open(ROOT / 'out/e1_scan.json'))['names']
    all_addrs = sorted(int(k, 16) for k in names)
    named = {int(k, 16): v for k, v in names.items() if not AUTO.match(v)}

    def extent(a):
        i = bisect.bisect_right(all_addrs, a)
        nxt = all_addrs[i] if i < len(all_addrs) else a + 0x100
        return min(nxt - a, 0x4000)

    print('indexing', file=sys.stderr)
    idx_t, idx_a = Index(our_t, 2), Index(our_a, 4)
    e_idx_t, e_idx_a = Index(e_t, 2), Index(e_a, 4)

    pairs = {}                                      # e1 offset -> (our offset, thumb)
    for ea, name in named.items():
        off = ea - e1_base
        if not 0 <= off < len(e_raw) - WINDOW:
            continue
        arm = is_arm(e_raw, off)
        img, idx, eidx = (e_a, idx_a, e_idx_a) if arm else (e_t, idx_t, e_idx_t)
        key = img[off:off + WINDOW]
        if len(set(key)) < 6:                       # padding / tables of zeros
            continue
        if len(eidx.get(key, ())) != 1:
            continue
        hits = idx.get(key, ())
        if len(hits) == 1:
            pairs[off] = (hits[0], not arm)
    print('seeds', len(pairs), file=sys.stderr)

    # grow along calls of pairs whose masked bodies are identical
    work = list(pairs.items())
    while work:
        off, (ooff, thumb) = work.pop()
        size = extent(e1_base + off)
        img_e, img_o = (e_t, our_t) if thumb else (e_a, our_a)
        a, b = img_e[off:off + size], img_o[ooff:ooff + size + 64]
        if a == b[:size]:
            blocks = [(0, 0, size)]
        else:
            sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
            blocks = [blk for blk in sm.get_matching_blocks() if blk[2] >= 8]
            if sum(blk[2] for blk in blocks) < 0.8 * size:
                continue
        co = {o - ooff: t for o, t in calls_at(our_raw, ooff, size + 64, thumb)}
        for pos, (te, ke) in calls_at(e_raw, off, size, thumb):
            p = pos - off
            for i, j, n in blocks:
                if i <= p and p + 4 <= i + n:
                    hit = co.get(j + p - i)
                    if hit and hit[1] == ke and te not in pairs \
                            and 0 <= te < len(e_raw) and 0 <= hit[0] < len(our_raw):
                        pairs[te] = (hit[0], ke if thumb else False)
                        work.append((te, pairs[te]))
                    break
    print('after growth', len(pairs), file=sys.stderr)

    out = {}
    for off, (ooff, thumb) in pairs.items():
        name = named.get(e1_base + off)
        if name:
            out[name] = base + ooff + (1 if thumb else 0)
    print('named', len(out), file=sys.stderr)
    json.dump(dict(sorted(out.items())), open(ROOT / 'out/symbols_e1_exact.json', 'w'), indent=0)


if __name__ == '__main__':
    main()
