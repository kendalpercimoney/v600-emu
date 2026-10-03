"""Boot and save a PNG every time the firmware copies a frame to the visible surface.

    .venv/bin/python tools/frames.py [insns]   -> out/frame_NN.png
"""
import sys; sys.path.insert(0, 'tools'); sys.path.insert(0, '.')
from PIL import Image
from v600.machine import Machine
from v600 import board, fwlog

m = Machine(log=lambda *a: None); m.verbose_mmio = False
board.build(m); m.reset(); fwlog.install(m, sink=lambda s: None)
orig = m.ati.packet3
n = [0]

def packet3(op, p):
    orig(op, p)
    if op == 0x1B and m.ati.reg(0x1004) == m.ati.reg(0x418):
        img = m.ati.screen()
        img.resize((img.width * 2, img.height * 2), Image.NEAREST).save(f'out/frame_{n[0]:02d}.png')
        print(f'frame {n[0]:02d} at {m.insns:,}')
        n[0] += 1

m.ati.packet3 = packet3
m.run(int(sys.argv[1]) if len(sys.argv) > 1 else 600_000_000)
