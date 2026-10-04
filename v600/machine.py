"""The emulated V600: CPU, memory map and the MMIO dispatcher."""
import collections
from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_ARM, UC_MODE_BIG_ENDIAN,
                     UC_HOOK_MEM_UNMAPPED, UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE)
from unicorn.arm_const import *

from . import superfile

IRAM = (0x03FC0000, 0x40000)
RAM = (0x12000000, 0x400000)
FLASH = (0x10000000, 0x2000000)
MMIO = (0x20000000, 0x10000000)
CPSR_E = 0x200              # Unicorn does big-endian data via CPSR.E: keep it set on every write
ENTRY = 0x106289BE          # Thumb; from header word 0 of the main code group

REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4,
        UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9,
        UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR,
        UC_ARM_REG_PC]


class Machine:
    def __init__(self, shx='firmware/v600.shx', log=print):
        self.log = log
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_BIG_ENDIAN)
        uc.ctl_set_cpu_model(UC_CPU_ARM_926)         # ARMv5: closest to the ARM7TDMI (v4T)
        uc.mem_map(0, 0x20000)                       # vectors + boot ROM routines (stubbed, see bootrom.py)
        uc.mem_map(*IRAM)
        uc.mem_map(*RAM)
        uc.mem_map(*FLASH)
        uc.mem_write(FLASH[0], b'\xff' * FLASH[1])  # erased flash, incl. the missing bootloader
        for base, data in superfile.load(shx):
            uc.mem_write(base, bytes(data))
        self.periph = {}                             # address -> model object (see attach)
        self.mmio_log = collections.Counter()        # (kind, addr, size) -> count
        self.mmio_trace = collections.deque(maxlen=64)
        self.mmio_reads = 0
        self.mmio_writes = 0                         # excluding watchdog service writes
        self.verbose_mmio = True
        # Unicorn's mmio_map mangles big-endian accesses, so MMIO is plain memory plus hooks:
        # the read hook stores the model's value just before the load, unmodelled registers
        # read back the last value written.
        uc.mem_map(*MMIO)
        uc.hook_add(UC_HOOK_MEM_READ, self._mmio_read, begin=MMIO[0], end=MMIO[0] + MMIO[1] - 1)
        uc.hook_add(UC_HOOK_MEM_WRITE, self._mmio_write, begin=MMIO[0], end=MMIO[0] + MMIO[1] - 1)
        uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._unmapped)
        self.code_hooks = {}
        self.stopped = None                          # set by hooks to end run()
        self.insns = 0                               # virtual clock: instructions executed
        self.aitc = None                             # interrupt controller (board.py)
        self.timers = []                             # objects with update() / next_event()
        self.irq_count = 0
        self.idle_hint = False                       # set by the idle-loop hook
        self.counter = None                          # FreeRunCounter, for fast-forward
        self.fault = None

    # ---- MMIO -------------------------------------------------------------
    def attach(self, base, size, model):
        """Route [base, base+size) to model.read(off, size) / model.write(off, size, value)."""
        for a in range(base, base + size, 4):
            self.periph[a] = (base, model)

    def _route(self, addr):
        return self.periph.get(addr & ~3)

    def _mmio_read(self, uc, access, addr, size, _value, _):
        r = self._route(addr)
        if r:
            val = r[1].read(addr - r[0], size) & ((1 << (size * 8)) - 1)
            uc.mem_write(addr, val.to_bytes(size, 'big'))
        else:
            val = int.from_bytes(uc.mem_read(addr, size), 'big')
        self.mmio_reads += 1
        self._note('R', addr, size, val)

    def _mmio_write(self, uc, access, addr, size, value, _):
        value &= (1 << (size * 8)) - 1
        if addr != 0x24849002:
            self.mmio_writes += 1
        r = self._route(addr)
        if r:
            r[1].write(addr - r[0], size, value)
        self._note('W', addr, size, value)

    def _note(self, kind, addr, size, val):
        key = (kind, addr, size)
        self.mmio_log[key] += 1
        self.mmio_trace.append((self.pc(), kind, addr, size, val))
        if self.verbose_mmio and self.mmio_log[key] <= 2:
            self.log(f'  mmio {kind}{size * 8:<2} {addr:08x} = {val:08x}   pc={self.pc():08x}')

    def _unmapped(self, uc, access, addr, size, value, _):
        self.fault = (access, addr, size, value, self.pc())
        return False

    # ---- CPU helpers --------------------------------------------------------
    def pc(self):
        return self.uc.reg_read(UC_ARM_REG_PC)

    def thumb(self):
        return bool(self.uc.reg_read(UC_ARM_REG_CPSR) & 0x20)

    def r(self, n):
        return self.uc.reg_read(REGS[n])

    def dump_regs(self):
        v = [self.r(i) for i in range(16)]
        cpsr = self.uc.reg_read(UC_ARM_REG_CPSR)
        lines = []
        for i in range(0, 16, 4):
            lines.append('  '.join(f'r{j:<2}={v[j]:08x}' for j in range(i, i + 4)))
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

    def _service_interrupts(self):
        from .irq import deliver
        for t in self.timers:
            t.update()
        if self.idle_hint and not self.aitc.irq_line():
            # The OS is idling: jump the virtual clock to the next timer event.
            events = [e for e in (t.next_event() for t in self.timers) if e is not None]
            if events and self.counter is not None:
                target_insns = int(min(events) / self.counter.k) + 1
                if target_insns > self.insns:
                    self.insns = target_insns
                for t in self.timers:
                    t.update()
        self.idle_hint = False
        deliver(self)

    def stop(self, why):
        self.stopped = why
        self.uc.emu_stop()

    def hook(self, addr, fn):
        """Call fn(machine) whenever execution reaches addr (Thumb or ARM)."""
        self.code_hooks[addr & ~1] = fn
        self.uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, _: fn(self), begin=addr & ~1, end=addr & ~1)

    def read32(self, addr):
        return int.from_bytes(self.uc.mem_read(addr, 4), 'big')

    # ---- boot ---------------------------------------------------------------
    def reset(self):
        """CPU state the (missing) bootloader leaves: SVC mode, IRQ/FIQ off, a stack in IRAM."""
        uc = self.uc
        # The firmware never sets the exception-mode stacks. SVC keeps the top of IRAM: it
        # becomes the OS's interrupt/idle context and reaches below 0x03FFFE60. The others
        # sit in 0x03FFB85C-0x03FFEEEB, which nothing else touches; an FIQ stack inside the
        # SVC area corrupted that context once the SIM (FIQ source 51) was active.
        stacks = {0x11: 0x03FFE800, 0x12: 0x03FFE000, 0x17: 0x03FFD000, 0x1B: 0x03FFC800,
                  0x1F: 0x03FFC000, 0x13: 0x03FFFF80}
        for mode, sp in stacks.items():
            uc.reg_write(UC_ARM_REG_CPSR, CPSR_E | 0xC0 | mode)
            uc.reg_write(UC_ARM_REG_SP, sp)
        uc.reg_write(UC_ARM_REG_CPSR, CPSR_E | 0xD3)
        # Clock-speed code the bootloader leaves in IRAM; the delay loop (0x107FBD04) resets the
        # phone unless it is 1..8 (13/16.8/19.5/26/39/40/48/52 MHz). 8 = 52 MHz.
        uc.mem_write(0x03FC3600, bytes([8]))
        self.start = ENTRY | 1

    def run(self, max_insns=10_000_000, slice_=20_000, poll_slices=10, spin_slices=500):
        """Run in slices; stop on fault, on a detected stall loop or after max_insns.

        A stall is the PC staying inside a small window: after poll_slices if every slice
        read MMIO (hardware polling), or after spin_slices otherwise (pure CPU loops such as
        memory clears are normal, so that limit is long)."""
        pcs = collections.deque(maxlen=spin_slices)
        polled = collections.deque(maxlen=spin_slices)
        done = 0
        start = self.start
        while done < max_insns:
            self.fault = None
            reads_before, writes_before = self.mmio_reads, self.mmio_writes
            try:
                self.uc.emu_start(start, 0xFFFFFFFF, count=slice_)
            except UcError as e:
                self.log(f'!! CPU error: {e} at pc={self.pc():08x} thumb={self.thumb()}')
                if self.fault:
                    acc, a, s, v, pc = self.fault
                    self.log(f'   unmapped access type={acc} addr={a:08x} size={s} value={v:x}')
                return 'error'
            done += slice_
            self.insns += slice_
            if self.aitc is not None:
                self._service_interrupts()
                start = self.pc() | (1 if self.thumb() else 0)
            if self.stopped:
                self.log(f'!! stopped by hook: {self.stopped}')
                self.start = self.pc() | (1 if self.thumb() else 0)
                return 'hook'
            pc = self.pc()
            start = pc | (1 if self.thumb() else 0)
            pcs.append(pc)
            polled.append(self.mmio_reads > reads_before and self.mmio_writes == writes_before)
            recent = list(pcs)[-poll_slices:]
            if (len(recent) == poll_slices and all(list(polled)[-poll_slices:])
                    and len({p >> 6 for p in recent}) <= 3):
                where = ' '.join(f'{p:08x}' for p in sorted(set(recent)))
                self.log(f'!! poll stall: pc at {where} after {done} insns')
                self.start = start
                return 'stall'
            if len(pcs) == spin_slices and max(pcs) - min(pcs) < 0x100:
                self.log(f'!! spin stall: pc in {min(pcs):08x}-{max(pcs):08x} after {done} insns')
                self.start = start
                return 'stall'
        self.start = start
        return 'budget'
