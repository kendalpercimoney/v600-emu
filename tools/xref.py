"""Find Thumb BL / ARM BL / literal references to a target address. usage: xref.py ADDR"""
import sys, struct
sys.path.insert(0, __file__.rsplit('/tools/', 1)[0] + '/tools')
from armdis import regions

def xrefs(target):
    out = []
    for base, d in regions():
        if base not in (0x10080000, 0x10040000):
            continue
        for i in range(0, len(d) - 4, 2):
            hi, lo = struct.unpack('>HH', d[i:i + 4])
            if hi >> 11 == 0x1e and lo >> 11 in (0x1f, 0x1d):
                off = ((hi & 0x7ff) << 12) | ((lo & 0x7ff) << 1)
                if off & (1 << 22): off -= 1 << 23
                dst = base + i + 4 + off
                if lo >> 11 == 0x1d: dst &= ~3
                if dst == target & ~1: out.append(('thumb-bl', base + i))
            if i % 4 == 0:
                w = struct.unpack('>I', d[i:i + 4])[0]
                if (w & 0x0e000000) == 0x0a000000 and (w >> 28) != 0xf:
                    off = (w & 0xffffff) << 2
                    if off & (1 << 25): off -= 1 << 26
                    if base + i + 8 + off == target: out.append(('arm-b/bl', base + i))
                if w in (target, target | 1): out.append(('literal', base + i))
    return out

if __name__ == '__main__':
    for kind, a in xrefs(int(sys.argv[1], 16)):
        print(f'{kind:10s} {a:08x}')
