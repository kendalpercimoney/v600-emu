"""The emulated V600: CPU, memory map and the MMIO dispatcher."""
import collections
from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_ARM, UC_MODE_BIG_ENDIAN, UC_HOOK_INTR,
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
        self.mmio_hooks = []
        self.plain_words = []                        # MMIO words left as plain memory (plain())
        self._hook_mmio()
        uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._unmapped)
        self.code_hooks = {}                         # addr -> (fn, Unicorn hook or None)
        self.breakpoints = {}                        # addr -> ([fn], original bytes, emulate)
        uc.hook_add(UC_HOOK_INTR, self._interrupt)
        self.iram_breakpoints = False                # some live in IRAM: check them (replant)
        self.once_per_slice = set()                  # hook(once_per_slice=True) addresses
        self.lifted = []                             # ... whose instruction is back for now
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

    def plain(self, addr, size=4):
        """Leave [addr, addr+size) of MMIO as plain memory: no Python call per access. For
        registers with no side effects that the firmware hits constantly (each access
        through the hooks costs several microseconds)."""
        self.plain_words.append((addr, addr + size))
        self._hook_mmio()

    def _hook_mmio(self):
        for h in self.mmio_hooks:
            self.uc.hook_del(h)
        self.mmio_hooks = []
        start = MMIO[0]
        for a, b in sorted(self.plain_words) + [(MMIO[0] + MMIO[1], None)]:
            if a > start:
                self.mmio_hooks += [
                    self.uc.hook_add(UC_HOOK_MEM_READ, self._mmio_read, begin=start, end=a - 1),
                    self.uc.hook_add(UC_HOOK_MEM_WRITE, self._mmio_write, begin=start, end=a - 1)]
            start = b

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

    def _report(self, e):
        self.log(f'!! CPU error: {e} at pc={self.pc():08x} thumb={self.thumb()}')
        if self.fault:
            acc, a, s, v, pc = self.fault
            self.log(f'   unmapped access type={acc} addr={a:08x} size={s} value={v:x}')

    def _service_interrupts(self):
        from .irq import deliver
        for addr in self.lifted:
            bkpt = THUMB_BKPT if len(self.breakpoints[addr][1]) == 2 else ARM_BKPT
            self.uc.mem_write(addr, bkpt)
            self.uc.ctl_remove_cache(addr, addr + len(bkpt))
        self.lifted.clear()
        if self.iram_breakpoints:
            self.replant()
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

    def hook(self, addr, fn, once_per_slice=False):
        """Call fn(machine) whenever execution reaches addr (with once_per_slice, at most
        once per run() slice: the original instruction goes back after the first hit and
        the breakpoint returns at the end of the slice; for the idle loop's hint).

        A breakpoint (BKPT) replaces the instruction there, and _interrupt calls the hooked
        functions and then carries out the replaced instruction (one of the simple forms in
        _thumb_emulator). Unicorn code hooks are only the fallback: while run() counts
        instructions, a single code hook makes every instruction several times slower
        (30 of them: 116 -> 11 MIPS). Code the firmware has yet to copy into internal RAM
        gets its breakpoint once it is there (replant)."""
        addr &= ~1
        if once_per_slice:
            self.once_per_slice.add(addr)
        if addr in self.breakpoints:
            self.breakpoints[addr][0].append(fn)
            return
        old = bytes(self.uc.mem_read(addr, 4))
        if old == ARM_B_SELF:                            # the boot-ROM exception traps
            self._plant(addr, [fn], old, _arm_branch_self)
        elif _thumb_emulator(old[:2]):
            self._plant(addr, [fn], old[:2], _thumb_emulator(old[:2]))
        elif IRAM[0] <= addr < IRAM[0] + IRAM[1]:
            self.breakpoints[addr] = ([fn], None, None)  # not copied there yet
            self.iram_breakpoints = True
        else:
            self.code_hooks[addr] = (fn, self.uc.hook_add(
                UC_HOOK_CODE, lambda uc, a, s, _: fn(self), begin=addr, end=addr))

    def _plant(self, addr, fns, original, emulate):
        bkpt = ARM_BKPT if len(original) == 4 else THUMB_BKPT
        self.breakpoints[addr] = (fns, original, emulate)
        self.uc.mem_write(addr, bkpt)
        if FLASH[0] <= addr < FLASH[0] + FLASH[1] and hasattr(self, 'flash'):
            self.flash.patched(addr, bkpt)               # not a change to save
        self.iram_breakpoints |= IRAM[0] <= addr < IRAM[0] + IRAM[1]

    def _interrupt(self, uc, intno, _):
        if intno != 7:                                   # 7: BKPT
            return
        pc = self.pc()
        fns, original, emulate = self.breakpoints[pc]
        for fn in fns:
            fn(self)
        if pc in self.once_per_slice:                    # run the real instruction now
            self.uc.mem_write(pc, original)
            self.uc.ctl_remove_cache(pc, pc + len(original))   # else the BKPT runs again
            self.lifted.append(pc)
        elif self.pc() == pc:                            # nothing jumped elsewhere
            emulate(self, pc, original)

    def replant(self):
        """Plant breakpoints in internal RAM once the firmware has copied code there, and
        again if it copies over them, keeping what it wrote as the instruction to emulate."""
        for addr, (fns, original, emulate) in list(self.breakpoints.items()):
            if not IRAM[0] <= addr < IRAM[0] + IRAM[1]:
                continue
            now = bytes(self.uc.mem_read(addr, 2))
            if now != THUMB_BKPT and _thumb_emulator(now):
                self._plant(addr, fns, now, _thumb_emulator(now))

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
                self._report(e)
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


# -- breakpoints (Machine.hook) ------------------------------------------------------------
THUMB_BKPT = bytes.fromhex('be00')
ARM_BKPT = bytes.fromhex('e1200070')
ARM_B_SELF = bytes.fromhex('eafffffe')


def _arm_branch_self(m, pc, original):
    pass                                                 # `b .`: stays put


def _thumb_emulator(ins):
    """How to carry out a replaced Thumb instruction, for the forms found at hooked
    addresses (function entries and veneers), or None."""
    op = int.from_bytes(ins, 'big')
    if op & 0xFE00 == 0xB400:
        return _push
    if op & 0xFF87 == 0x4700:
        return _bx
    if op & 0xF800 == 0x4800:
        return _ldr_literal
    return None


def _push(m, pc, original):
    op = int.from_bytes(original, 'big')
    regs = [r for r in range(8) if op >> r & 1] + ([14] if op & 0x100 else [])
    sp = m.uc.reg_read(UC_ARM_REG_SP) - 4 * len(regs)
    m.uc.mem_write(sp, b''.join(m.uc.reg_read(REGS[r]).to_bytes(4, 'big') for r in regs))
    m.uc.reg_write(UC_ARM_REG_SP, sp)
    m.uc.reg_write(UC_ARM_REG_PC, (pc + 2) | 1)


def _bx(m, pc, original):
    rm = int.from_bytes(original, 'big') >> 3 & 0xF
    target = (pc + 4) & ~3 if rm == 15 else m.uc.reg_read(REGS[rm])   # bx pc: to ARM
    m.uc.reg_write(UC_ARM_REG_PC, target)


def _ldr_literal(m, pc, original):
    op = int.from_bytes(original, 'big')
    addr = ((pc + 4) & ~3) + (op & 0xFF) * 4
    m.uc.reg_write(REGS[op >> 8 & 7], m.read32(addr))
    m.uc.reg_write(UC_ARM_REG_PC, (pc + 2) | 1)
