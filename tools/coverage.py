"""Boot and record which basic blocks executed inside an address range. usage: coverage.py LO HI [insns]"""
import sys, collections; sys.path.insert(0, 'tools'); sys.path.insert(0, '.')
from unicorn import UC_HOOK_BLOCK
from v600.machine import Machine
from v600 import board, fwlog
lo, hi = int(sys.argv[1], 16), int(sys.argv[2], 16)
n = int(sys.argv[3]) if len(sys.argv) > 3 else 300_000_000
m = Machine(log=lambda *a: None); m.verbose_mmio = False
board.build(m); m.reset()
blocks = collections.Counter()
m.uc.hook_add(UC_HOOK_BLOCK, lambda uc, a, s, _: blocks.update([a]), begin=lo, end=hi)
m.run(n)
print(f'{len(blocks)} blocks executed in {lo:08x}-{hi:08x}')
for a, c in sorted(blocks.items()):
    print(f'  {a:08x} x{c}')
