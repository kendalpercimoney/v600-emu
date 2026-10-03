import sys; sys.path.insert(0, 'tools'); sys.path.insert(0, '.')
from v600.machine import Machine
from v600 import board, fwlog
import run as runmod
for v in (0xFFFD, 0xFFFB, 0xFFF8):
    lines = []
    m = Machine(log=lines.append); m.verbose_mmio = False
    board.build(m, pds_barker=v); m.reset(); runmod.trace_boot(m); fwlog.install(m)
    r = m.run(150_000_000)
    cps = [l for l in lines if 'checkpoint' in l]
    print(f'== barker {v:#x}: {r}; last checkpoints:', ' '.join(l.split()[2] for l in cps[-3:]))
    for l in lines:
        if l.startswith('!!') or '[fw]' in l: print('  ', l.strip()[:150])
