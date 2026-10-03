"""Boot and render the ATI chip's video memory as 176x220 RGB565 images."""
import sys; sys.path.insert(0, 'tools'); sys.path.insert(0, '.')
import numpy as np
from PIL import Image
from v600.machine import Machine
from v600 import board, fwlog

W, H = 176, 220

def rgb565(buf, little):
    a = np.frombuffer(buf, dtype='<u2' if little else '>u2').reshape(H, W)
    r = ((a >> 11) & 0x1F) * 255 // 31
    g = ((a >> 5) & 0x3F) * 255 // 63
    b = (a & 0x1F) * 255 // 31
    return Image.fromarray(np.dstack([r, g, b]).astype(np.uint8))

if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300_000_000
    m = Machine(log=lambda *a: None); m.verbose_mmio = False
    board.build(m); m.reset(); fwlog.install(m, sink=lambda s: None)
    m.log = print
    m.run(n)
    mem = bytes(m.ati.mem)
    print('blits', m.ati.blits, 'frame base', hex(m.ati.reg(0x418)))
    for base in (m.ati.reg(0x418) or 0x103D10,):
        for little in (False, True):
            img = rgb565(mem[base:base + W * H * 2], little)
            name = f'out/ati_{base:06x}_{"le" if little else "be"}.png'
            img.resize((W * 2, H * 2), Image.NEAREST).save(name)
            print(name)
