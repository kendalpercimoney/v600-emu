"""Boot, then summarise MMIO usage per 4 KB page and save the machine state for inspection."""
import sys, collections, pickle
sys.path.insert(0, 'tools'); sys.path.insert(0, '.')
from v600.machine import Machine
from v600 import board, fwlog
m = Machine(log=lambda *a: None); m.verbose_mmio = False
board.build(m); m.reset(); fwlog.install(m, sink=lambda s: None)
m.run(int(sys.argv[1]) if len(sys.argv) > 1 else 300_000_000)
pages = collections.defaultdict(collections.Counter)
for (kind, addr, size), n in m.mmio_log.items():
    pages[addr & ~0xFFF][kind] += n
for p, c in sorted(pages.items()):
    print(f'{p:08x}  R={c["R"]:<8} W={c["W"]:<8}')
open('out/ram.bin', 'wb').write(bytes(m.uc.mem_read(0x12000000, 0x400000)))
open('out/iram.bin', 'wb').write(bytes(m.uc.mem_read(0x03FC0000, 0x40000)))
