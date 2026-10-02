"""Big-endian ARM/Thumb disassembler over the firmware image, with literal-pool annotation.
usage: armdis.py ADDR COUNT [t]"""
import sys, capstone
sys.path.insert(0, __file__.rsplit('/tools/', 1)[0])
from v600 import superfile

_regions = None
def regions():
    global _regions
    if _regions is None:
        _regions = superfile.load(__file__.rsplit('/tools/', 1)[0] + '/firmware/v600.shx')
    return _regions

def rd(addr, n):
    for b, d in regions():
        if b <= addr < b + len(d):
            return bytes(d[addr - b:addr - b + n])
    return b''

def dis(addr, n=40, thumb=False, out=print):
    md = capstone.Cs(capstone.CS_ARCH_ARM, (capstone.CS_MODE_THUMB if thumb else capstone.CS_MODE_ARM) | capstone.CS_MODE_BIG_ENDIAN)
    a = addr
    for _ in range(n):
        code = rd(a, 4)
        ins = next(md.disasm(code, a), None)
        if ins is None:
            sz = 2 if thumb else 4
            out(f"{a:08x}: {code[:sz].hex():10s} .word"); a += sz; continue
        extra = ''
        if ins.mnemonic.startswith('ldr') and '[pc, #' in ins.op_str:
            off = int(ins.op_str.split('#')[-1].rstrip(']'), 16)
            lit = ((a + 4) & ~3) + off if thumb else a + 8 + off
            extra = f'   ; ={rd(lit, 4).hex()}'
        out(f"{a:08x}: {ins.bytes.hex():10s} {ins.mnemonic} {ins.op_str}{extra}")
        a += ins.size

if __name__ == '__main__':
    dis(int(sys.argv[1], 16), int(sys.argv[2]), sys.argv[3:] == ['t'])
