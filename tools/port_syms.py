"""Port symbols from the V600 0B.09.72R build (SDK .sym files) to our 0B.09.38R build.

For each code symbol, take the first bytes of the function in the 72R image, mask the parts
that move between builds (Thumb BL/BLX offsets, ARM BL offsets, literal-pool words) and look
for a unique match in our image. Data symbols are not ported.

    .venv/bin/python tools/port_syms.py > out/symbols_ported.json
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from v600 import superfile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SDK = Path('/tmp/p2ksdk/res/V600_TRIPLETS_G_0B.09.72R')
NEW = ROOT / 'docs/re/V600_TRIPLETS_G_0B.09.72R.smg'
BASE = 0x10080000
LENGTHS = (64, 48, 32, 24)


def read_syms():
    syms = {}
    for f in ('elfloader.sym', 'library.sym'):
        for line in (SDK / f).read_text(errors='replace').splitlines():
            p = line.split()
            if len(p) == 3 and p[0].startswith('0x') and p[1] in 'AT':
                syms[p[2]] = (int(p[0], 16), p[1])
    return syms


def thumb_pattern(code, n):
    """Regex for n bytes of big-endian Thumb code, with BL offsets and LDR literals masked."""
    out, i = [], 0
    while i < n:
        hw = code[i] << 8 | code[i + 1]
        if (hw & 0xF800) == 0xF000 and i + 3 < n:          # BL/BLX prefix + suffix
            out.append(rb'[\xf0-\xf7].[\xe8-\xff].')
            i += 4
            continue
        if (hw & 0xF800) == 0x4800:                         # LDR rX,[pc,#imm]: keep, literal masked below
            pass
        out.append(re.escape(code[i:i + 2]))
        i += 2
    return b''.join(out)


def arm_pattern(code, n):
    out = []
    for i in range(0, n, 4):
        w = code[i:i + 4]
        if (w[0] & 0x0E) == 0x0A:                            # B/BL: keep cond+opcode, mask offset
            out.append(re.escape(w[:1]) + b'...')
        else:
            out.append(re.escape(w))
    return b''.join(out)


def main():
    new = NEW.read_bytes()
    old_base, old = next((a, bytes(b)) for a, b in superfile.load(ROOT / 'firmware/v600.shx')
                         if a == BASE)
    found, stats = {}, {'unique': 0, 'ambiguous': 0, 'missing': 0}
    for name, (addr, kind) in sorted(read_syms().items()):
        thumb = kind == 'T'
        off = (addr & ~1) - BASE
        if not 0 <= off < len(new):
            continue
        result = 'missing'
        for n in LENGTHS:
            code = new[off:off + n]
            pat = thumb_pattern(code, n) if thumb else arm_pattern(code, n)
            hits = [m.start() for m in re.finditer(pat, old, re.S)
                    if m.start() % (2 if thumb else 4) == 0][:3]
            if len(hits) == 1:
                found[name] = old_base + hits[0] + (1 if thumb else 0)
                result = 'unique'
                break
            if len(hits) > 1:
                result = 'ambiguous'
                break                                       # shorter patterns only get worse
        stats[result] += 1
    print(stats, file=sys.stderr)
    json.dump(dict(sorted(found.items())), sys.stdout, indent=0)


if __name__ == '__main__':
    main()
