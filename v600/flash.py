"""The phone's NOR flash (32 MB at 0x10000000): the command state machine is in C
(core/flash.c); this keeps the state file. The phone's settings, contacts and messages
live in the flash file system, so the blocks that differ from the firmware image are saved
and restored: 4-byte block address then the 128 KB block, repeated."""
from pathlib import Path

from .devices import _lib

BASE, SIZE = 0x10000000, 0x2000000
BLOCK = 0x20000


class Flash:
    def __init__(self, m):
        self.lib = _lib()
        self.uc = m.uc
        self.cflash = self.lib.flash_new(m.c)
        self.original = bytearray(self.uc.mem_read(BASE, SIZE))  # as loaded from the firmware

    @property
    def writes(self):
        """Program / erase operations so far, to tell when to save."""
        return self.lib.flash_writes(self.cflash)

    def idle(self):
        """No command in progress and every partition reading the array: safe to snapshot."""
        return bool(self.lib.flash_idle(self.cflash))

    def patched(self, addr, data):
        """The emulator changed code here (a breakpoint, Machine.hook): not phone data."""
        self.original[addr - BASE:addr - BASE + len(data)] = data

    def save(self, path):
        self.lib.flash_flush(self.cflash)
        cur = bytes(self.uc.mem_read(BASE, SIZE))
        out = bytearray()
        for off in range(0, SIZE, BLOCK):
            blk = cur[off:off + BLOCK]
            if blk != self.original[off:off + BLOCK]:
                out += (BASE + off).to_bytes(4, 'big') + blk
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_bytes(bytes(out))
        tmp.replace(path)
        return len(out) // (BLOCK + 4)

    def load(self, path):
        data = Path(path).read_bytes()
        n = 0
        for i in range(0, len(data) - 3, BLOCK + 4):
            addr = int.from_bytes(data[i:i + 4], 'big')
            if BASE <= addr < BASE + SIZE and len(data) >= i + 4 + BLOCK:
                self.uc.mem_write(addr, data[i + 4:i + 4 + BLOCK])
                n += 1
        return n
