"""Name functions in our V600 0B.09.38R build from the ROKR E1 R373 build database.

The E1 was built with a different compiler release (register allocation differs), so bytes
rarely match. Instead functions are matched by structure:

  * functions:  call targets (BL / BLX / ARM BL) that start like a function, plus E1's names
  * features:   strings referenced from literal pools; ordered list of callees
  * anchors:    exact masked-byte matches (port_e1.py) and unique string-reference sets
  * spreading:  for matched pairs, callees are paired in order when the call lists align
                (difflib over the callee sequences, with already-matched callees as anchors);
                callers with a unique, already-matched callee set are paired too.

Output (git-ignored): out/symbols_e1.json {name: address | 1 if Thumb}.
"""
import bisect
import collections
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from v600 import superfile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
AUTO = re.compile(r'^(a[A-Z0-9_]|sub_|loc_|off_|unk_|byte_|word_|dword_|stru_|asc_|nullsub|j_|def_|'
                  r'jpt_|locret_|flt_|dbl_|var_|arg_|case_|\$|\.|_stru_|jstr_|Bytecode_|Method_|'
                  r'StackMap_|Handlers_|Consts_|Class_)')
NOT_FUNC = re.compile(r'^(loc_|locret_|off_|unk_|byte_|word_|dword_|stru_|asc_|a[A-Z0-9_]|def_|jpt_|'
                      r'flt_|dbl_|case_|\.|jstr_|Bytecode_|StackMap_|Handlers_|Consts_)')


class Image:
    def __init__(self, base, data, extra=()):
        self.base, self.data = base, data
        self.extra = list(extra)                # other (base, data) regions for string lookups

    def u16(self, a):
        o = a - self.base
        return int.from_bytes(self.data[o:o + 2], 'big')

    def u32(self, a):
        for base, data in [(self.base, self.data)] + self.extra:
            o = a - base
            if 0 <= o <= len(data) - 4:
                return int.from_bytes(data[o:o + 4], 'big')
        return None

    def cstr(self, a):
        for base, data in [(self.base, self.data)] + self.extra:
            o = a - base
            if 0 <= o < len(data):
                end = data.find(b'\0', o, o + 200)
                s = data[o:end] if end > o else b''
                if len(s) >= 4 and all(32 <= c < 127 for c in s):
                    return s.decode()
                return None
        return None

    def contains(self, a):
        return self.base <= a < self.base + len(self.data)

    def call_targets(self):
        """{target address: thumb?} for every BL/BLX (Thumb, 2-aligned) and BL (ARM, 4-aligned)
        whose target looks like a function prologue."""
        d, base = self.data, self.base
        out = {}
        for o in range(0, len(d) - 4, 2):
            h1 = (d[o] << 8) | d[o + 1]
            if (h1 & 0xF800) != 0xF000:
                continue
            h2 = (d[o + 2] << 8) | d[o + 3]
            if (h2 & 0xE800) != 0xE800:
                continue
            hi = h1 & 0x7FF
            if hi & 0x400:
                hi -= 0x800
            t = base + o + 4 + (hi << 12) + ((h2 & 0x7FF) << 1)
            thumb = bool(h2 & 0x1000)
            if not thumb:
                t &= ~3
            if self.contains(t) and self.prologue(t, thumb):
                out[t] = thumb
        for o in range(0, len(d) - 4, 4):
            w = int.from_bytes(d[o:o + 4], 'big')
            if (w & 0xFF000000) == 0xEB000000:
                off = w & 0xFFFFFF
                if off & 0x800000:
                    off -= 0x1000000
                t = base + o + 8 + 4 * off
                if self.contains(t) and self.prologue(t, False):
                    out.setdefault(t, False)
        return out

    def prologue(self, a, thumb):
        if thumb:
            h = self.u16(a)
            return (h & 0xFE00) == 0xB400 or (h & 0xF800) in (0x4800, 0x2000, 0x1C00, 0x6800) \
                or (h & 0xFF00) in (0xB000, 0x4700) or h in (0x4770,)
        w = self.u32(a)
        return w is not None and (w >> 28) == 0xE

    def _literal(self, lit, out):
        """A literal-pool word is a feature if it points at a string, or is a constant that
        does not move between builds (not a code, flash or RAM address)."""
        v = self.u32(lit)
        if v is None:
            return
        s = self.cstr(v)
        if s:
            out.append(s)
        elif v > 0xFFFF and not (0x03F00000 <= v < 0x04000000 or 0x10000000 <= v < 0x13000000) \
                and v not in (0xFFFFFFFF, 0x80000000, 0x7FFFFFFF, 0xFFFF0000):
            out.append(f'#{v:08x}')

    def features(self, a, size, thumb):
        """(ordered callees [(addr, thumb)], ordered strings)"""
        d, base = self.data, self.base
        calls, strings = [], []
        o, end = a - base, min(a - base + size, len(d) - 4)
        if thumb:
            while o < end:
                h1 = (d[o] << 8) | d[o + 1]
                if (h1 & 0xF800) == 0xF000:
                    h2 = (d[o + 2] << 8) | d[o + 3]
                    if (h2 & 0xE800) == 0xE800:
                        hi = h1 & 0x7FF
                        if hi & 0x400:
                            hi -= 0x800
                        t = base + o + 4 + (hi << 12) + ((h2 & 0x7FF) << 1)
                        th = bool(h2 & 0x1000)
                        calls.append((t if th else t & ~3, th))
                        o += 4
                        continue
                if (h1 & 0xF800) == 0x4800:
                    lit = ((base + o + 4) & ~3) + (h1 & 0xFF) * 4
                    self._literal(lit, strings)
                o += 2
        else:
            o &= ~3
            while o < end:
                w = int.from_bytes(d[o:o + 4], 'big')
                if (w & 0x0F000000) == 0x0B000000:
                    off = w & 0xFFFFFF
                    if off & 0x800000:
                        off -= 0x1000000
                    calls.append((base + o + 8 + 4 * off, False))
                elif (w & 0x0F7F0000) == 0x051F0000:
                    lit = base + o + 8 + ((w & 0xFFF) if w & 0x00800000 else -(w & 0xFFF))
                    self._literal(lit, strings)
                o += 4
        return calls, strings


def functions(img, starts):
    """{start: (size, thumb)} with each function extending to the next start."""
    keys = sorted(starts)
    out = {}
    for i, a in enumerate(keys):
        nxt = keys[i + 1] if i + 1 < len(keys) else a + 0x200
        out[a] = (min(nxt - a, 0x2000), starts[a])
    return out


def main():
    regions = {a: bytes(b) for a, b in superfile.load(ROOT / 'firmware/v600.shx')}
    ours = Image(0x10080000, regions[0x10080000],
                 [(a, b) for a, b in regions.items() if a != 0x10080000])
    e1_parts = {int(f.stem[3:], 16): f.read_bytes() for f in (ROOT / 'docs/re').glob('e1_*.bin')}
    e1 = Image(0x10081ED0, e1_parts[0x10081ED0],
               [(a, b) for a, b in e1_parts.items() if a != 0x10081ED0])

    names = json.load(open(ROOT / 'out/e1_scan.json'))['names']
    e1_names = {int(k, 16): v for k, v in names.items()}

    print('finding functions', file=sys.stderr)
    o_starts = ours.call_targets()
    e_starts = e1.call_targets()
    for a, n in e1_names.items():
        if e1.contains(a) and not NOT_FUNC.match(n) and a not in e_starts:
            e_starts[a] = not (a % 4 == 0 and e1.prologue(a, False) and
                               (e1.u32(a + 4) or 0) >> 28 == 0xE)
    of, ef = functions(ours, o_starts), functions(e1, e_starts)
    print(f'functions: ours {len(of)}, e1 {len(ef)}', file=sys.stderr)

    print('features', file=sys.stderr)
    ofeat = {a: ours.features(a, s, t) for a, (s, t) in of.items()}
    efeat = {a: e1.features(a, s, t) for a, (s, t) in ef.items()}

    pairs, rev = {}, {}                     # e1 addr -> our addr, and back

    def pair(e, o):
        if e in pairs or o in rev or e not in ef or o not in of or ef[e][1] != of[o][1]:
            return False
        pairs[e], rev[o] = o, e
        return True

    # anchors 1: exact byte matches from port_e1 (if run before)
    try:
        exact = json.load(open(ROOT / 'out/symbols_e1_exact.json'))
        inv = {v: k for k, v in e1_names.items()}
        for n, a in exact.items():
            if n in inv:
                pair(inv[n], a & ~1)
    except FileNotFoundError:
        pass
    print('anchors from bytes', len(pairs), file=sys.stderr)

    # anchors 2: unique string-reference sets
    def sigs(feat):
        by = collections.defaultdict(list)
        for a, (_, strs) in feat.items():
            if strs:
                by[tuple(sorted(set(strs)))].append(a)
        return {k: v[0] for k, v in by.items() if len(v) == 1}
    es, os_ = sigs(efeat), sigs(ofeat)
    n0 = len(pairs)
    for k, e in es.items():
        if k in os_:
            pair(e, os_[k])
    # anchors 3: a string referenced by exactly one function on each side
    def owners(feat):
        by = collections.defaultdict(set)
        for a, (_, strs) in feat.items():
            for x in strs:
                by[x].add(a)
        return {x: next(iter(v)) for x, v in by.items() if len(v) == 1}
    eo, oo = owners(efeat), owners(ofeat)
    votes = collections.defaultdict(collections.Counter)
    for x, e in eo.items():
        if x in oo:
            votes[e][oo[x]] += 1
    for e, c in votes.items():
        (o, n), *rest = c.most_common()
        if not rest or n > 2 * rest[0][1]:
            pair(e, o)
    print('anchors from strings', len(pairs) - n0, file=sys.stderr)

    # spreading through the call graph
    ocallers, ecallers = collections.defaultdict(set), collections.defaultdict(set)
    for a, (c, _) in ofeat.items():
        for t, _ in c:
            ocallers[t].add(a)
    for a, (c, _) in efeat.items():
        for t, _ in c:
            ecallers[t].add(a)

    changed = True
    rounds = 0
    while changed:
        changed = False
        rounds += 1
        for e, o in list(pairs.items()):
            ce, co = efeat[e][0], ofeat[o][0]
            if not ce or not co:
                continue
            # anchors: positions where both call an already-matched pair
            me = [(i, pairs[t]) for i, (t, _) in enumerate(ce) if t in pairs]
            mo = [(j, t) for j, (t, _) in enumerate(co) if t in rev]
            # a matched callee of e that o never calls (or vice versa) means e/o is doubtful
            if {x for _, x in me} != {x for _, x in mo}:
                continue
            if not me:
                continue
            sm = difflib.SequenceMatcher(None, [x for _, x in me], [x for _, x in mo],
                                         autojunk=False)
            anchors = [(-1, -1)]
            for i, j, n in sm.get_matching_blocks():
                anchors += [(me[i + k][0], mo[j + k][0]) for k in range(n)]
            anchors.append((len(ce), len(co)))
            for (i0, j0), (i1, j1) in zip(anchors, anchors[1:]):
                if i1 - i0 != j1 - j0:
                    continue
                for k in range(1, i1 - i0):
                    (a_, ta), (b_, tb) = ce[i0 + k], co[j0 + k]
                    if ta == tb and pair(a_, b_):
                        changed = True
        # callers: an unmatched E1 function whose matched callees all map into exactly one
        # unmatched function of ours that calls them all
        for e in list(ef):
            if e in pairs:
                continue
            known = [pairs[t] for t, _ in efeat[e][0] if t in pairs]
            if len(set(known)) < 3:
                continue
            cands = set.intersection(*(ocallers[t] for t in set(known))) - set(rev)
            if len(cands) == 1:
                c = cands.pop()
                if len(ofeat[c][0]) == len(efeat[e][0]) and pair(e, c):
                    changed = True
        print(f'round {rounds}: {len(pairs)} pairs', file=sys.stderr)

    out = {}
    for e, o in pairs.items():
        n = e1_names.get(e)
        if n and not AUTO.match(n):
            out[n] = o | (1 if of[o][1] else 0)
    print('named', len(out), file=sys.stderr)
    json.dump(dict(sorted(out.items())), open(ROOT / 'out/symbols_e1.json', 'w'), indent=0)


if __name__ == '__main__':
    main()
