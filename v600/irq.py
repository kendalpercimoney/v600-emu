"""Interrupts: the AITC interrupt controller at 0x29890000 and the OS wake-up timer.

AITC register layout (Freescale ARM Interrupt Controller, as used by the firmware):
  +00 INTCNTL   +04 NIMASK    +08 INTENNUM  +0C INTDISNUM
  +10 INTENABLEH (sources 63..32)  +14 INTENABLEL (31..0)
  +18 INTTYPEH  +1C INTTYPEL  (1 = FIQ)
  +20..+3C NIPRIORITY7..0  (4 bits per source; +20 holds sources 63..56, source 63 in bits 31:28)
  +40 NIVECSR   (bits 31:16 = highest pending normal source, 0xFFFF if none; 15:0 = its priority)
  +44 FIVECSR   (highest pending fast source, 0xFFFFFFFF if none)
  +48/+4C INTSRCH/L (raw)  +50/+54 INTFRCH/L (software force)  +58/+5C NIPNDH/L  +60/+64 FIPNDH/L
"""
from unicorn.arm_const import UC_ARM_REG_CPSR, UC_ARM_REG_SPSR, UC_ARM_REG_LR, UC_ARM_REG_PC

CPSR_E = 0x200


class AITC:
    def __init__(self, m):
        self.m = m
        self.intcntl = 0
        self.nimask = 0x1F
        self.enable = 0
        self.type = 0
        self.prio = [0] * 64
        self.raw = 0                    # level inputs from peripherals
        self.force = 0
        self.raised = {}                # source -> count, for logging

    # -- peripheral side --------------------------------------------------------------
    def set_line(self, src, level):
        if level:
            self.raw |= 1 << src
        else:
            self.raw &= ~(1 << src)

    # -- arbitration ------------------------------------------------------------------
    def pending(self):
        return (self.raw | self.force) & self.enable

    def normal_vector(self):
        p = self.pending() & ~self.type
        best, best_prio = None, -1
        for s in range(64):
            if p >> s & 1:
                pr = self.prio[s]
                if self.nimask != 0x1F and pr <= self.nimask:
                    continue
                if pr >= best_prio:          # same priority: higher source number wins
                    best, best_prio = s, pr
        return best, best_prio

    def fast_vector(self):
        p = self.pending() & self.type
        for s in range(63, -1, -1):
            if p >> s & 1:
                return s
        return None

    def irq_line(self):
        return self.normal_vector()[0] is not None

    def fiq_line(self):
        return self.fast_vector() is not None

    # -- bus side ---------------------------------------------------------------------
    def read(self, off, size):
        off &= ~3
        if off == 0x00: return self.intcntl
        if off == 0x04: return self.nimask
        if off == 0x10: return self.enable >> 32
        if off == 0x14: return self.enable & 0xFFFFFFFF
        if off == 0x18: return self.type >> 32
        if off == 0x1C: return self.type & 0xFFFFFFFF
        if 0x20 <= off <= 0x3C:
            base = (7 - (off - 0x20) // 4) * 8
            return sum(self.prio[base + i] << (4 * i) for i in range(8))
        if off == 0x40:
            s, p = self.normal_vector()
            return 0xFFFFFFFF if s is None else (s << 16) | p
        if off == 0x44:
            s = self.fast_vector()
            return 0xFFFFFFFF if s is None else s
        if off == 0x48: return self.raw >> 32
        if off == 0x4C: return self.raw & 0xFFFFFFFF
        if off == 0x50: return self.force >> 32
        if off == 0x54: return self.force & 0xFFFFFFFF
        if off == 0x58: return (self.pending() & ~self.type) >> 32
        if off == 0x5C: return (self.pending() & ~self.type) & 0xFFFFFFFF
        if off == 0x60: return (self.pending() & self.type) >> 32
        if off == 0x64: return (self.pending() & self.type) & 0xFFFFFFFF
        return 0

    def write(self, off, size, value):
        off &= ~3
        if off == 0x00: self.intcntl = value
        elif off == 0x04: self.nimask = value & 0x1F
        elif off == 0x08: self.enable |= 1 << (value & 0x3F)
        elif off == 0x0C: self.enable &= ~(1 << (value & 0x3F))
        elif off == 0x10: self.enable = (self.enable & 0xFFFFFFFF) | (value << 32)
        elif off == 0x14: self.enable = (self.enable & ~0xFFFFFFFF) | value
        elif off == 0x18: self.type = (self.type & 0xFFFFFFFF) | (value << 32)
        elif off == 0x1C: self.type = (self.type & ~0xFFFFFFFF) | value
        elif 0x20 <= off <= 0x3C:
            base = (7 - (off - 0x20) // 4) * 8
            for i in range(8):
                self.prio[base + i] = (value >> (4 * i)) & 0xF
        elif off == 0x50: self.force = (self.force & 0xFFFFFFFF) | (value << 32)
        elif off == 0x54: self.force = (self.force & ~0xFFFFFFFF) | value


class OSTimer:
    """Timer block at 0x24848000 (16-bit registers).

    Channel used by the RTOS for wake-ups: +0x20/+0x22 = 32-bit countdown in time() units
    (time() = free-running counter >> 19, see 0x1089B6A4), latched when the low half is
    written; it fires that many units later. +0x06 bit 8 = interrupt enable;
    +0x04 bit 8 = match status (write 1 to clear). Raises AITC source 14.
    +0x12/+0x16/+0x18 form the watchdog pre-warning channel (source 15, handler = reset);
    it is not modelled, so it never fires."""

    SOURCE = 14

    def __init__(self, m, aitc, counter):
        self.m = m
        self.aitc = aitc
        self.counter = counter
        self.r = bytearray(0x40)
        self.armed = False              # fires once per programmed countdown
        self.fire_at = 0                # free-running counter value when it fires

    def _get(self, off):
        return int.from_bytes(self.r[off:off + 2], 'big')

    def _set(self, off, v):
        self.r[off:off + 2] = (v & 0xFFFF).to_bytes(2, 'big')

    def now(self):
        return (self.counter.value() >> 19) & 0xFFFFFFFF

    def target(self):
        return (self._get(0x20) << 16) | self._get(0x22)

    def update(self):
        """Latch the match and drive the interrupt line."""
        if self.armed and self.counter.value() >= self.fire_at:
            self.armed = False
            self._set(0x04, self._get(0x04) | 0x100)
        self.aitc.set_line(self.SOURCE, bool(self._get(0x04) & 0x100 and self._get(0x06) & 0x100))

    def next_event(self):
        """Counter value at which the armed match fires, or None."""
        if not (self.armed and self._get(0x06) & 0x100):
            return None
        return self.fire_at

    def read(self, off, size):
        self.update()
        if off == 0x18:                                # watchdog-channel counter
            return (self.counter.value() >> 8) & 0xFFFF
        return int.from_bytes(self.r[off:off + size], 'big')

    def write(self, off, size, value):
        if off == 0x04:
            self._set(0x04, self._get(0x04) & ~value)      # write 1 to clear
        else:
            self.r[off:off + size] = value.to_bytes(size, 'big')
            if off == 0x22:                     # countdown latched by the low-half write
                self.armed = True
                self.fire_at = self.counter.value() + (self.target() << 19)
        self.update()


def deliver(m):
    """Take an IRQ or FIQ if one is pending and unmasked (called between instruction slices)."""
    uc = m.uc
    cpsr = uc.reg_read(UC_ARM_REG_CPSR)
    if m.aitc.fiq_line() and not cpsr & 0x40:
        mode, vector, mask = 0x11, 0x1C, 0xC0
    elif m.aitc.irq_line() and not cpsr & 0x80:
        mode, vector, mask = 0x12, 0x18, 0x80
    else:
        return False
    ret = uc.reg_read(UC_ARM_REG_PC)           # next instruction to execute
    uc.reg_write(UC_ARM_REG_CPSR, (cpsr & ~0x3F) | CPSR_E | mask | mode)   # ARM state, masked
    uc.reg_write(UC_ARM_REG_SPSR, cpsr)
    uc.reg_write(UC_ARM_REG_LR, ret + 4)
    uc.reg_write(UC_ARM_REG_PC, vector)
    m.irq_count += 1
    return True
