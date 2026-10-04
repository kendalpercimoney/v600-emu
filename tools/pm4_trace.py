"""Boot and log every PM4 type-3 packet with the 2D engine registers in effect."""
import sys; sys.path.insert(0, 'tools'); sys.path.insert(0, '.')
from v600.machine import Machine
from v600 import board, fwlog

m = Machine(log=lambda *a: None); m.verbose_mmio = False
board.build(m); m.reset(); fwlog.install(m, sink=lambda s: None)
orig = m.ati.packet3
seen = {}

def packet3(op, p):
    eng = {k - 0x10000: v for k, v in sorted(m.ati.regs.items()) if 0x11000 <= k < 0x11200}
    key = (op, tuple(sorted(eng.items())))
    if seen.get(op, 0) < int(sys.argv[2] if len(sys.argv) > 2 else 6):
        seen[op] = seen.get(op, 0) + 1
        print(f'op {op:#04x} n={len(p)} insns={m.insns:,} disp={m.ati.reg(0x418):#x}')
        print('   payload', ' '.join(f'{w:08x}' for w in p[:12]))
        print('   regs   ', ' '.join(f'{k:#x}={v:#x}' for k, v in eng.items()))
    orig(op, p)

m.ati.packet3 = packet3
r = m.run(int(sys.argv[1]) if len(sys.argv) > 1 else 600_000_000)
print(r, m.ati.draws)
