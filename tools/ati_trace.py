"""Boot and print the ATI register access trace."""
import sys; sys.path.insert(0, 'tools'); sys.path.insert(0, '.')
from v600.machine import Machine
from v600 import board, fwlog
m = Machine(log=lambda *a: None); m.verbose_mmio = False
board.build(m); m.reset(); fwlog.install(m, sink=lambda s: None)
r = m.run(int(sys.argv[1]) if len(sys.argv) > 1 else 300_000_000)
print(r, f'ati reads={m.ati.reads} writes={m.ati.writes} dma={m.dma.count}')
for k, a, v in m.ati.trace[:int(sys.argv[2]) if len(sys.argv) > 2 else 120]:
    print(f'  {k} {a:#07x} {v:#010x}')
print('\n'.join(m.fw_messages[-12:]))
