"""Intel-command-set NOR flash (32 MB at 0x10000000, 16-bit bus, 2 MB partitions).

The flash is ordinary Unicorn memory so code and data reads run at full speed. Writes are
hooked: Unicorn stores the value after the hook runs, so each written word is recorded with
the content it should really end up holding (original data for a command, old AND new for
program data) and fixed up before the next flash access. While a partition is out of
read-array mode a read hook answers status / ID / CFI queries.

Commands seen in the firmware's driver: 0x90 read ID, 0x70 read status, 0x50 clear status,
0x60+0x03 set configuration, 0x60+0xD0/0x01 lock/unlock, 0x40/0x10 word program,
0xE8 buffered program, 0x20+0xD0 block erase, 0xB0/0xD0 suspend/resume, 0xFF read array.
"""
from pathlib import Path

from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE

BASE, SIZE = 0x10000000, 0x2000000
PARTITION = 0x200000
BLOCK = 0x20000
MANUFACTURER, DEVICE = 0x0089, 0x880F       # accepted by the driver's ID table (32 MB part)
READY = 0x80


class Flash:
    def __init__(self, m):
        self.m = m
        self.uc = m.uc
        self.mode = {}            # partition -> 'array' | 'status' | 'id' | 'cfi'
        self.pending = {}         # addr -> bytes that must be in memory after the store
        self.cmd = None           # multi-cycle command in progress
        self.buf_left = 0
        self.read_hook = None
        self.status = READY
        self.log_count = 0
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_write, begin=BASE, end=BASE + SIZE - 1)
        self.original = bytes(self.uc.mem_read(BASE, SIZE))    # as loaded from the firmware
        self.writes = 0           # program / erase operations, to tell when to save

    # -- persistence -----------------------------------------------------------------------
    # The phone's settings, contacts and messages live in the flash file system. A state file
    # keeps the blocks that differ from the firmware image: 4-byte block address then the
    # 128 KB block, repeated.
    def idle(self):
        """No command in progress and every partition reading the array: safe to snapshot."""
        return self.cmd is None and all(v == 'array' for v in self.mode.values())

    def save(self, path):
        self._flush()
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

    # -- memory fix-ups ------------------------------------------------------------------
    def _flush(self):
        for addr, data in self.pending.items():
            self.uc.mem_write(addr, data)
        self.pending.clear()

    def _arm_read_hook(self):
        if self.read_hook is None:
            self.read_hook = self.uc.hook_add(UC_HOOK_MEM_READ, self._on_read,
                                              begin=BASE, end=BASE + SIZE - 1)

    def _maybe_disarm(self):
        if self.read_hook is not None and not self.pending and \
                all(v == 'array' for v in self.mode.values()):
            self.uc.hook_del(self.read_hook)
            self.read_hook = None

    def _part(self, addr):
        return (addr - BASE) // PARTITION

    # -- bus -----------------------------------------------------------------------------
    def _on_read(self, uc, access, addr, size, value, _):
        self._flush()
        mode = self.mode.get(self._part(addr), 'array')
        if mode != 'array':
            off = (addr - (BASE + self._part(addr) * PARTITION)) & ~1
            if mode == 'status':
                v = self.status
            elif mode == 'id':
                v = {0: MANUFACTURER, 2: DEVICE}.get(off & 0xFF, 0)
            else:                                         # CFI: minimal "QRY" signature
                v = {0x20: ord('Q'), 0x22: ord('R'), 0x24: ord('Y')}.get(off & 0xFF, 0)
            a = addr & ~1
            self.pending[a] = bytes(uc.mem_read(a, 2))
            uc.mem_write(a, (v & 0xFFFF).to_bytes(2, 'big'))
            if size == 4:
                self.pending[a + 2] = bytes(uc.mem_read(a + 2, 2))
                uc.mem_write(a + 2, (v & 0xFFFF).to_bytes(2, 'big'))
        self._maybe_disarm()

    def _on_write(self, uc, access, addr, size, value, _):
        self._flush()
        value &= (1 << (size * 8)) - 1
        orig = bytes(uc.mem_read(addr, size))
        part = self._part(addr)
        final = orig                                      # commands leave the array untouched

        if self.cmd in ('program', 'buffer-confirm', 'erase'):
            self.writes += 1
        if self.cmd in ('program',):
            final = (int.from_bytes(orig, 'big') & value).to_bytes(size, 'big')
            self.cmd = None
            self.mode[part] = 'status'
        elif self.cmd == 'buffer-count':
            self.buf_left = (value & 0xFFFF) + 1
            self.cmd = 'buffer-data'
        elif self.cmd == 'buffer-data':
            final = (int.from_bytes(orig, 'big') & value).to_bytes(size, 'big')
            self.buf_left -= 1
            if self.buf_left <= 0:
                self.cmd = 'buffer-confirm'
        elif self.cmd == 'buffer-confirm':
            self.cmd = None                               # expects 0xD0
            self.mode[part] = 'status'
        elif self.cmd == 'erase':
            self.cmd = None
            if value & 0xFF == 0xD0:
                blk = addr & ~(BLOCK - 1)
                uc.mem_write(blk, b'\xff' * BLOCK)
                orig = bytes(uc.mem_read(addr, size))
                final = orig
                self._note(f'erase block {blk:08x}')
            self.mode[part] = 'status'
        elif self.cmd == 'config':
            self.cmd = None                               # lock/unlock/RCR: accepted, no effect
            self.mode[part] = 'array' if value & 0xFF == 0x03 else 'status'
        else:
            c = value & 0xFF
            if c == 0xFF:
                self.mode[part] = 'array'
            elif c == 0x70:
                self.mode[part] = 'status'
            elif c == 0x50:
                self.status = READY
            elif c == 0x90:
                self.mode[part] = 'id'
            elif c == 0x98:
                self.mode[part] = 'cfi'
            elif c in (0x40, 0x10):
                self.cmd = 'program'
            elif c == 0xE8:
                self.cmd = 'buffer-count'
                self.mode[part] = 'status'
            elif c == 0x20:
                self.cmd = 'erase'
            elif c == 0x60:
                self.cmd = 'config'
            elif c in (0xB0, 0xD0):
                self.mode[part] = 'status'
            else:
                self._note(f'unknown flash command {c:#x} at {addr:08x}')
        self.pending[addr] = final
        self._arm_read_hook()

    def _note(self, text):
        self.log_count += 1
        if self.log_count <= 50:
            self.m.log(f'  flash {text}  pc={self.m.pc():08x}')
