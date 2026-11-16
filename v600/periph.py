"""Peripheral models still in Python. Each has read(off, size) -> int and
write(off, size, value). Ported to C so far: counter, keypad, edge port, one-wire
(core/periph.c), the AITC and OS timer (core/irq.c)."""
import numpy as np



class Regs:
    """Plain register file: reads return what was written, with optional fixed values and
    self-clearing bits (word offset -> mask of 32-bit-word bits that the hardware clears
    immediately, e.g. a start/busy bit for an operation that completes instantly)."""

    def __init__(self, size, fixed=None, selfclear=None):
        self.mem = bytearray(size)
        self.fixed = fixed or {}
        self.selfclear = selfclear or {}

    def read(self, off, size):
        if off in self.fixed:
            return self.fixed[off]
        return int.from_bytes(self.mem[off:off + size], 'big')

    def write(self, off, size, value):
        self.mem[off:off + size] = value.to_bytes(size, 'big')
        for word, mask in self.selfclear.items():
            if word <= off < word + 4:
                v = int.from_bytes(self.mem[word:word + 4], 'big') & ~mask
                self.mem[word:word + 4] = v.to_bytes(4, 'big')

    def get16(self, off):
        return int.from_bytes(self.mem[off:off + 2], 'big')

    def set16(self, off, v):
        self.mem[off:off + 2] = (v & 0xFFFF).to_bytes(2, 'big')


class PowerIC:
    """The power-management IC behind the QSPI (PCAP2: 32 x 25-bit registers).

    SPI word: bit31 = write, bits 30..26 = register, bits 24..0 = data. A read returns the
    register value in the reply word. Register 0 is the interrupt status: write 1 to clear.

    ADC (Linux ezx-pcap.h): writing ASC (bit 22) to reg 10 (ADR) converts the 7 channels of
    the bank selected by reg 9 (ADC) bit 2; ADA1 (reg 9 bits 6:4) / ADA2 (9:7) pick which
    results appear in ADR ADD1 (9:0) / ADD2 (19:10). Completion sets ISR bit 0 (ADCDONE)."""

    ISR, MSR, PSTAT, ADC, ADR = 0, 1, 2, 9, 10
    # Power key (the red End key): ISR bit 7 (ONOFF) latches each change. PSTAT holds the
    # current line levels, active low: bit 7 = power key, bit 8 = the second on/off input
    # (ONOFF2). Released = both set; the debouncer (0x1088EA3A, signals 0x21 / 0x54) reads
    # bit 7 clear and bit 8 set as "power key held", which turns the phone off after ~2 s.
    ONOFF = 1 << 7
    PSTAT_IDLE = (1 << 7) | (1 << 8)
    ASC = 1 << 22
    ADCDONE = 1 << 0
    # 10-bit readings for bank 0: coin cell, battery, B+, charger port (none), battery
    # thermistor (room temperature), charger ID (none), AD6. Bank 1 (touch screen, spare
    # inputs) reads mid-scale. Calibration is unknown: values chosen for a healthy battery.
    ADC_BANK0 = [0x300, 0x2C0, 0x2C0, 0x000, 0x200, 0x3FF, 0x200]
    ADC_BANK1 = [0x200] * 7

    def __init__(self, log):
        self.log = log
        self.regs = [0] * 32
        self.seen = set()
        self.adc = list(self.ADC_BANK0)
        self.on_int = None              # called with the INT pin level (ISR & ~MSR != 0)
        self.sense = self.PSTAT_IDLE    # PSTAT: current level of the interrupt sources

    def power_key(self, down):
        """The red key: PSTAT sense bit follows the key, ISR latches each change."""
        old = self.sense
        self.sense = (self.sense & ~self.ONOFF) if down else (self.sense | self.ONOFF)
        if self.sense != old:
            self.regs[self.ISR] |= self.ONOFF
            if self.on_int:
                self.on_int(self.int_level())

    def int_level(self):
        return bool(self.regs[self.ISR] & ~self.regs[self.MSR] & 0x1FFFFFF)

    def transfer(self, data):
        word = int.from_bytes(data[:4], 'big')
        return self.transfer_word(word).to_bytes(4, 'big')

    def _adr(self):
        ctl = self.regs[self.ADC]
        a1, a2 = (ctl >> 4) & 7, (ctl >> 7) & 7
        v1 = self.adc[a1] if a1 < 7 else 0
        v2 = self.adc[a2] if a2 < 7 else 0
        return (self.regs[self.ADR] & ~0xFFFFF & ~self.ASC) | v1 | (v2 << 10)

    def transfer_word(self, word):
        write = bool(word & 0x80000000)
        reg = (word >> 26) & 0x1F
        data = word & 0x1FFFFFF
        if write:
            if reg == self.ISR:
                self.regs[reg] &= ~data
            else:
                self.regs[reg] = data
            if reg == self.ADR and data & self.ASC:          # start conversion: instant
                bank1 = bool(self.regs[self.ADC] & 0x4)
                self.adc = list(self.ADC_BANK1 if bank1 else self.ADC_BANK0)
                self.regs[self.ISR] |= self.ADCDONE
            if reg in (self.ADC, self.ADR):
                self.regs[self.ADR] = self._adr()
            if self.on_int:
                self.on_int(self.int_level())
            reply = 0
        else:
            reply = self.regs[reg] | (self.sense if reg == self.PSTAT else 0)
        key = (write, reg)
        if key not in self.seen:
            self.seen.add(key)
            self.log(f'  pmic {"W" if write else "R"} reg{reg:<2} {"<-" if write else "->"} '
                     f'{data if write else reply:#08x}')
        return reply


class QSPI:
    """Queued SPI controller at 0x24851000.

    +0x006: start; value = 0x20 | hw channel; hardware clears 0x20 once the transfer is taken.
    +0x010: bits 0 / 8 = queue active (self-clearing). +0x012 / +0x014: done bits for hw
    channels 0-15 / 16-31 (1 = idle/complete, write 1 to clear). +0x400 + 4*q: command RAM.
    +0x480..: data RAM.

    The command RAM encoding is not decoded yet; instead each transfer is resolved through the
    driver's own queue table (pointer at [[0x1232E5F8]], 20-byte entries:
    device descriptor, TX address, RX address, [hw channel, ...], end address)."""

    TABLE_PTR = 0x1232E5F8

    def __init__(self, m, devices, log):
        # +0x010 (16-bit): bits 0 / 8 = queue active, set by software, cleared when done
        self.r = Regs(0x1000, selfclear={0x10: 0x01010000})
        self.m = m
        self.devices = devices          # device descriptor address -> device with transfer(bytes)
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
            self.transfer(ch)
            self.done |= 1 << ch
            self.r.set16(0x006, value & ~0x20)      # hardware clears the start bit

    def queue_for(self, ch):
        try:
            table = self.m.read32(self.m.read32(self.TABLE_PTR))
            for i in range(32):
                e = bytes(self.m.uc.mem_read(table + 0x14 * i, 0x14))
                dev, tx, rx, info, end = (int.from_bytes(e[j:j + 4], 'big') for j in range(0, 20, 4))
                if dev == 0 and tx == 0:
                    break
                if info >> 24 == ch and 0x24851000 <= tx < 0x24852000:
                    return dev, tx - 0x24851000, rx - 0x24851000, end - 0x24851000
        except Exception:
            pass
        return None

    def transfer(self, ch):
        q = self.queue_for(ch)
        if q is None:
            if ch not in self.channels:
                self.channels.add(ch)
                self.log(f'  qspi channel {ch}: no queue entry found')
            return
        dev_id, tx, rx, end = q
        n = (rx - tx) if rx > tx else (end - tx)
        data = bytes(self.r.mem[tx:tx + n])
        dev = self.devices.get(dev_id)
        if (ch, dev_id) not in self.channels:
            self.channels.add((ch, dev_id))
            self.log(f'  qspi channel {ch} -> device {dev_id:08x} '
                     f'({"model" if dev else "none"}), {n} bytes TX {data.hex()}')
        self.last_channel = ch
        reply = dev.transfer(data) if dev else bytes(n)
        reply = (reply + bytes(n))[:n]
        if rx != tx:
            self.r.mem[rx:rx + n] = reply
        else:
            self.r.mem[tx:tx + n] = reply


class SPIRegDevice:
    """SPI device with 8-bit registers and 24-bit addresses (chip select 6; probed by the ATI
    driver at 0x10920A4C: reg 5 must read 0x11 and reg 4 must read 1).

    Frame (inside a 32-byte queue buffer): byte 0 = flags | (length - 1), flag 0x20 = write;
    bytes 1-3 = address. Reads: dummy bytes up to offset 12, then data (one register per
    byte). Writes: data from offset 8."""

    READ_DATA, WRITE_DATA = 12, 8

    def __init__(self, name, log, regs=None):
        self.name = name
        self.log = log
        self.regs = dict(regs or {})
        self.count = 0

    def transfer(self, data):
        out = bytearray(len(data))
        if len(data) < 4:
            return bytes(out)
        n = (data[0] & 0x1F) + 1
        write = bool(data[0] & 0x20)
        addr = (data[1] << 16) | int.from_bytes(data[2:4], 'big')
        self.count += 1
        if write:
            vals = data[self.WRITE_DATA:n]
            for i, v in enumerate(vals):
                self.regs[addr + i] = v
            if self.count <= 300:
                self.log(f'  {self.name} W [{addr:#06x}] = {vals.hex()}')
        else:
            vals = bytes(self.regs.get(addr + i, 0) for i in range(max(0, n - self.READ_DATA)))
            out[self.READ_DATA:self.READ_DATA + len(vals)] = vals
            if self.count <= 300:
                self.log(f'  {self.name} R [{addr:#06x}] -> {vals.hex()}')
        return bytes(out)


class DMA:
    """DMA unit at 0x24854000 feeding the ATI chip's host write port.

    +0x00/+0x02 source address (high/low), +0x04/+0x06 byte count (bit 0 of +0x04 = bit 16),
    +0x18 control/status: low bits = command (1 = start), bit 2 busy, bit 6 done (write 1 to
    clear, raises AITC source 26). +0x1C transaction: bit 8 = address phase (the first word of
    the next transfer is the target address in the ATI address space); without it, data
    continues from the current address. Words are big-endian in ARM memory."""

    SOURCE = 26

    def __init__(self, m, aitc, chip, log):
        self.m = m
        self.aitc = aitc
        self.chip = chip
        self.log = log
        self.r = Regs(0x40)
        self.count = 0
        self.addr = 0
        self.ranges = {}

    def read(self, off, size):
        return self.r.read(off, size)

    def write(self, off, size, value):
        if off == 0x18:
            old = self.r.get16(0x18)
            if value & 0x40:                       # done flag: write 1 to clear
                value &= ~0x40
                old &= ~0x40
            new = (value & ~0x40) | (old & 0x40)
            self.r.set16(0x18, new)
            if (new & 0x3) == 1:                   # start
                self.run()
            self.aitc.set_line(self.SOURCE, bool(self.r.get16(0x18) & 0x40))
            return
        self.r.write(off, size, value)

    def run(self):
        src = (self.r.get16(0x00) << 16) | self.r.get16(0x02)
        n = ((self.r.get16(0x04) & 1) << 16) | self.r.get16(0x06)
        data = bytes(self.m.uc.mem_read(src, n))
        if self.r.get16(0x1C) & 0x100 and len(data) >= 4:
            self.addr = int.from_bytes(data[:4], 'big')
            data = data[4:]
            self.r.set16(0x1C, self.r.get16(0x1C) & ~0x100)
        # Image rows are often an odd number of 16-bit pixels: the trailing halfword must
        # arrive too, and the next transfer continues right after it (dropping it lost the
        # last pixel of every row and shifted the rows that followed).
        whole = len(data) & ~3
        words = np.frombuffer(data[:whole], '>u4')
        if len(words):
            key = self.addr >> 16
            self.ranges[key] = self.ranges.get(key, 0) + len(words)
            self.chip.write_block(self.addr, words)
        if len(data) > whole:
            self.chip.write_tail(self.addr + whole, data[whole:])
        self.addr += len(data)
        self.count += 1
        self.r.set16(0x18, (self.r.get16(0x18) & ~0x3) | 0x40)   # idle + done
