"""Peripheral models. Each has read(off, size) -> int and write(off, size, value)."""


class Regs:
    """Plain register file: reads return what was written (with optional fixed overrides)."""

    def __init__(self, size, fixed=None):
        self.mem = bytearray(size)
        self.fixed = fixed or {}

    def read(self, off, size):
        if off in self.fixed:
            return self.fixed[off]
        return int.from_bytes(self.mem[off:off + size], 'big')

    def write(self, off, size, value):
        self.mem[off:off + size] = value.to_bytes(size, 'big')

    def get16(self, off):
        return int.from_bytes(self.mem[off:off + 2], 'big')

    def set16(self, off, v):
        self.mem[off:off + 2] = (v & 0xFFFF).to_bytes(2, 'big')


class PowerIC:
    """The power-management IC behind the QSPI (32 x 25-bit registers, PCAP2-style).

    SPI word: bit31 = write, bits 30..26 = register, bits 24..0 = data. A read returns the
    register value in the reply word. Register 0 is the interrupt status: write 1 to clear."""

    ISR = 0

    def __init__(self, log):
        self.log = log
        self.regs = [0] * 32
        self.seen = set()

    def transfer(self, word):
        write = bool(word & 0x80000000)
        reg = (word >> 26) & 0x1F
        data = word & 0x1FFFFFF
        if write:
            if reg == self.ISR:
                self.regs[reg] &= ~data
            else:
                self.regs[reg] = data
            reply = 0
        else:
            reply = self.regs[reg]
        key = (write, reg)
        if key not in self.seen:
            self.seen.add(key)
            self.log(f'  pmic {"W" if write else "R"} reg{reg:<2} {"<-" if write else "->"} '
                     f'{data if write else reply:#08x}')
        return reply


class QSPI:
    """Queued SPI controller at 0x24851000 (minimal).

    +0x006: start; value = 0x20 | channel; hardware clears 0x20 once the transfer is taken. +0x012 / +0x014: done bits for channels 0-15 /
    16-31 (bit set = transfer complete, write 1 to clear). +0x400 + 4*q: queue config.
    +0x480: TX word, +0x484: RX word (as used so far)."""

    def __init__(self, devices, log):
        self.r = Regs(0x1000)
        self.devices = devices          # chip-select -> device with transfer(word)
        self.log = log
        self.done = 0xFFFFFFFF          # 32 channel-done bits; idle (1) after reset
        self.channels = set()

    def read(self, off, size):
        if off == 0x012:
            return self.done & 0xFFFF
        if off == 0x014:
            return (self.done >> 16) & 0xFFFF
        return self.r.read(off, size)

    def write(self, off, size, value):
        if off == 0x012:
            self.done &= ~value
            return
        if off == 0x014:
            self.done &= ~(value << 16)
            return
        self.r.write(off, size, value)
        if off == 0x006 and value & 0x20:
            ch = value & 0x1F
            if ch not in self.channels:
                self.channels.add(ch)
                self.log(f'  qspi channel {ch} first start (TX {self.r.mem[0x480:0x488].hex()})')
            word = int.from_bytes(self.r.mem[0x480:0x484], 'big')
            dev = self.devices.get(0)
            reply = dev.transfer(word) if dev else 0
            self.r.mem[0x484:0x488] = reply.to_bytes(4, 'big')
            self.done |= 1 << ch
            self.r.set16(0x006, value & ~0x20)      # hardware clears the start bit
