"""The emulated V600, as the tools see it: a handle on the machine in C (core/), with
Unicorn-style register and memory access (self.uc) and helpers for debugging.

The machine runs in C: instruction slices, interrupts, timers, breakpoint hooks and the
firmware hooks (core/run.c, firmware.c, board.c). From Python, hook() and timers still
work, for experiments and for the tools' own clocks (drive.py, live.py)."""
from pathlib import Path

from unicorn.arm_const import (UC_ARM_REG_CPSR, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_R0,
                               UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4,
                               UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8,
                               UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12,
                               UC_ARM_REG_SP)

from .core import Core
from .devices import HOST_DEVICE, HOST_HOOK, HOST_TIMER, LOG, PART_CPU, Trace, _lib

IRAM = (0x03FC0000, 0x40000)
RAM = (0x12000000, 0x400000)
FLASH = (0x10000000, 0x2000000)
MMIO = (0x20000000, 0x10000000)
RUN_RESULTS = ('budget', 'error', 'hook', 'stall')
REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4,
        UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9,
        UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR,
        UC_ARM_REG_PC]


class _Timers(list):
    """Machine timers added from Python (objects with update() and next_event() -> counter
    value or None). They run after the machine's own, in the order added."""
    def __init__(self, m):
        super().__init__()
        self.m = m
        self.ids = {}

    def append(self, t):
        super().append(t)
        n = len(self.m.host_objects)
        self.m.host_objects.append(t)
        self.ids[id(t)] = n
        self.m.lib.machine_add_host_timer(self.m.c, n)

    def remove(self, t):
        super().remove(t)
        self.m.lib.machine_remove_host_timer(self.m.c, self.ids.pop(id(t)))


class Machine:
    def __init__(self, shx='firmware/v600.shx', log=print):
        self.lib = lib = _lib()
        data = Path(shx).read_bytes()
        self.c = lib.v600_machine_new(data, len(data))
        self.uc = Core(ptr=lib.v600_part(self.c, PART_CPU))
        self.log = log
        self.fw_sink = None                     # firmware log lines (fwlog.install)
        self.quiet = ()
        self.verbose_mmio = False
        self.host_objects = []                  # hook functions and timers, by id
        self.host_devices = []
        self.timers = _Timers(self)
        self._callbacks = (LOG(self._on_log), HOST_HOOK(self._on_hook),
                           HOST_TIMER(self._on_timer), HOST_DEVICE(self._host_device))
        lib.machine_set_log(self.c, self._callbacks[0])
        lib.machine_set_host_hook(self.c, self._callbacks[1])
        lib.machine_set_host_timer(self.c, self._callbacks[2])
        lib.machine_set_host(self.c, self._callbacks[3])

    # ---- callbacks from C ---------------------------------------------------------------
    def _on_log(self, kind, text):
        text = text.decode('latin1')
        if kind == 0:
            self.log(text)
        elif self.fw_sink and not any(q in text for q in self.quiet):
            self.fw_sink(f'  [fw] {text}')

    def _on_hook(self, n):
        self.host_objects[n](self)

    def _on_timer(self, n, what):
        t = self.host_objects[n]
        if what == 0:
            t.update()
            return 0.0
        v = t.next_event()
        return -1.0 if v is None else float(v)

    def _host_device(self, n, write, off, size, value):
        model = self.host_devices[n]
        if write:
            model.write(off, size, value)
            return 0
        return model.read(off, size) & 0xFFFFFFFF

    # ---- running ------------------------------------------------------------------------
    def reset(self):
        self.lib.v600_reset(self.c)

    def run(self, max_insns=10_000_000, slice_=20_000, poll_slices=10, spin_slices=500):
        """Run in slices; 'budget' after max_insns, or 'error', 'hook' (a hook stopped it),
        'stall' (the PC stuck in a small window)."""
        lim = lambda v: min(v, 0xFFFFFFFF)
        return RUN_RESULTS[self.lib.machine_run(self.c, max_insns, slice_, lim(poll_slices),
                                                lim(spin_slices))]

    @property
    def start(self):
        """Where the next run() continues (bit 0 = Thumb)."""
        return self.lib.machine_start(self.c)

    @start.setter
    def start(self, v):
        self.lib.machine_set_start(self.c, v)

    @property
    def stopped(self):
        return self.lib.machine_stopped(self.c).decode() or None

    def stop(self, why):
        self.lib.machine_stop(self.c, why.encode())

    def hook(self, addr, fn, once_per_slice=False):
        """Call fn(machine) whenever execution reaches addr (see core/run.c)."""
        self.host_objects.append(fn)
        self.lib.machine_hook_host(self.c, addr, len(self.host_objects) - 1, 1 if once_per_slice else 0)

    def attach(self, base, size, model):
        """Put a Python model (read(off, size) / write(off, size, value)) on the bus."""
        self.lib.machine_attach_host(self.c, base, size, len(self.host_devices))
        self.host_devices.append(model)

    def plain(self, addr, size=4):
        self.lib.machine_plain(self.c, addr, size)

    @property
    def insns(self):
        """Virtual clock: instructions executed."""
        return self.lib.machine_insns(self.c)

    @insns.setter
    def insns(self, v):
        self.lib.machine_set_insns(self.c, v)

    @property
    def irq_count(self):
        return self.lib.machine_irq_count(self.c)

    @property
    def mmio_reads(self):
        return self.lib.machine_mmio_reads(self.c)

    @property
    def mmio_writes(self):
        return self.lib.machine_mmio_writes(self.c)

    @property
    def mmio_trace(self):
        """The last MMIO accesses: (pc, kind, addr, size, value), oldest first."""
        out = (Trace * 64)()
        n = self.lib.machine_trace(self.c, out)
        return [(t.pc, t.kind.decode(), t.addr, t.size, t.value) for t in out[:n]]

    # ---- CPU helpers --------------------------------------------------------------------
    def pc(self):
        return self.uc.reg_read(UC_ARM_REG_PC)

    def thumb(self):
        return bool(self.uc.reg_read(UC_ARM_REG_CPSR) & 0x20)

    def r(self, n):
        return self.uc.reg_read(REGS[n])

    def read32(self, addr):
        return int.from_bytes(self.uc.mem_read(addr, 4), 'big')

    def dump_regs(self):
        v = [self.r(i) for i in range(16)]
        cpsr = self.uc.reg_read(UC_ARM_REG_CPSR)
        lines = ['  '.join(f'r{j:<2}={v[j]:08x}' for j in range(i, i + 4)) for i in range(0, 16, 4)]
        lines.append(f'cpsr={cpsr:08x} mode={cpsr & 0x1f:#x} T={(cpsr >> 5) & 1}')
        return '\n'.join(lines)

    def backtrace(self, depth=0x200, limit=12):
        """Heuristic: words on the stack that point just after a BL in code."""
        sp = self.uc.reg_read(UC_ARM_REG_SP)
        out = []
        for a in range(sp, sp + depth, 4):
            try:
                w = self.read32(a)
            except Exception:
                break
            if 0x10080000 <= w < 0x11180000 and w & 1:
                prev = int.from_bytes(self.uc.mem_read((w & ~1) - 4, 4), 'big')
                if (prev >> 27) == 0x1E and ((prev >> 11) & 0x1F) in (0x1F, 0x1D):
                    out.append((w & ~1) - 4)
            if len(out) >= limit:
                break
        return out
