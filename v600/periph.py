"""Peripheral models. Each has read(off, size) -> int and write(off, size, value)."""


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
    """The power-management IC behind the QSPI (32 x 25-bit registers, PCAP2-style).

    SPI word: bit31 = write, bits 30..26 = register, bits 24..0 = data. A read returns the
    register value in the reply word. Register 0 is the interrupt status: write 1 to clear."""

    ISR = 0

    def __init__(self, log):
        self.log = log
        self.regs = [0] * 32
        self.seen = set()

    def transfer(self, data):
        word = int.from_bytes(data[:4], 'big')
        return self.transfer_word(word).to_bytes(4, 'big')

    def transfer_word(self, word):
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


class DSPHostPort:
    """MCU side of the DSP interface, registers at 0x2485BFF2.. (offsets from 0x2485BFF0).

    Boot download (0x10AD5B90): +4 control (0x80 = hold DSP in reset), +6 status (bit 7 = DSP
    busy, bit 14 = DSP firmware running, bit 15 = buffer taken), +0xA next-buffer command,
    +0xE reply (0x8001 = block OK). There is no DSP: it always looks ready and running."""

    def __init__(self, log):
        self.r = Regs(0x10, fixed={0x6: 0xC000, 0xE: 0x8001})
        self.log = log

    def read(self, off, size):
        return self.r.read(off, size)

    def write(self, off, size, value):
        self.r.write(off, size, value)


class FreeRunCounter:
    """64-bit free-running counter at 0x2484A000: +0 high word, +4 low word.

    Driven by the machine's virtual clock (instructions executed). Assumes a 13 MHz counter
    and a 52 MHz CPU at ~1 instruction per cycle (to be calibrated)."""

    def __init__(self, machine, ticks_per_insn=0.25):
        self.m = machine
        self.k = ticks_per_insn

    def value(self):
        return int(self.m.insns * self.k)

    def read(self, off, size):
        v = self.value()
        return (v >> 32) & 0xFFFFFFFF if off == 0 else v & 0xFFFFFFFF

    def write(self, off, size, value):
        pass


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


class ATIChip:
    """ATI Imageon behind the serial host interface (QSPI chip select 6) and the DMA write
    port. Addresses >= 0x10000 are the Imageon register space (32-bit, little-endian byte
    order on the serial link); lower addresses are serial-interface registers (8-bit)."""

    def __init__(self, log, chip_id=0x57451002):
        self.log = log
        self.iface = {4: 0x01, 5: 0x11}          # interface ID checked by 0x10920A4C
        self.regs = {0x10000: chip_id}           # CHIP_ID
        self.mem = bytearray(self.MEM_MASK + 1)  # internal / external video memory
        self.mem_writes = 0
        self.cp = []                             # pending PM4 words
        self.cp_notes = {}
        self.blits = 0
        self.fills = 0
        self.writes = 0
        self.reads = 0
        self.trace = []                          # (kind, addr, value) for the first N accesses

    def _note(self, kind, addr, value):
        if len(self.trace) < 2000:
            self.trace.append((kind, addr, value))

    MEM_MASK = 0x3FFFFF                          # 4 MB address window for chip memory (little-endian)

    def screen(self):
        """The displayed frame as an RGB image (graphics base 0x418, pitch 0x41C bytes,
        size from the active display registers), RGB565."""
        import numpy as np
        from PIL import Image
        base = self.reg(0x418)
        pitch = self.reg(0x41C) or 0x160
        w = (self.reg(0x42C) >> 16) or 176
        h = (self.reg(0x430) >> 16) or 220
        rows = [bytes(self.mem[base + y * pitch: base + y * pitch + 2 * w]) for y in range(h)]
        a = np.frombuffer(b''.join(rows), dtype='<u2').reshape(h, w)
        rgb = np.dstack([((a >> 11) & 31) * 255 // 31, ((a >> 5) & 63) * 255 // 63,
                         (a & 31) * 255 // 31]).astype(np.uint8)
        return Image.fromarray(rgb)

    # Status bits the hardware clears once an operation completes (operations are instant here).
    BUSY_BITS = {0x104D8: 0x2}
    # Status bits that always read as set: 0x204 bit 11 is waited for after display updates
    # (0x1091EE84; most likely vertical blank / frame done - there is no scan timing here).
    READY_BITS = {0x10204: 0x800}

    def read32(self, addr):
        self.reads += 1
        v = (self.regs.get(addr & ~3, 0) & ~self.BUSY_BITS.get(addr & ~3, 0)) \
            | self.READY_BITS.get(addr & ~3, 0)
        self._note('R', addr, v)
        return v

    def write32(self, addr, value):
        self.writes += 1
        self.regs[addr & ~3] = value & 0xFFFFFFFF
        self._note('W', addr, value)

    # Command ring (PM4) in chip memory; host writes wrap inside it.
    RING_BASE, RING_SIZE = 0x80000, 0x4000

    def write_block(self, addr, words):
        """Host-port burst: registers if in 0x10000-0x1FFFF, otherwise chip memory. Words
        written into the command ring are also fed to the command processor in order."""
        if 0x10000 <= addr < 0x20000:
            for i, w in enumerate(words):
                self.write32(addr + 4 * i, w)
            return
        ring = self.RING_BASE <= addr < self.RING_BASE + self.RING_SIZE
        for i, w in enumerate(words):
            a = addr + 4 * i
            if ring:
                a = self.RING_BASE + (a - self.RING_BASE) % self.RING_SIZE
            a &= self.MEM_MASK
            self.mem[a:a + 4] = w.to_bytes(4, 'little')     # the Imageon is little-endian
        self.mem_writes += len(words)
        if ring:
            self.cp.extend(words)
            self.run_cp()

    # -- command processor (PM4) -----------------------------------------------------------
    def reg(self, off):
        return self.regs.get(0x10000 + off, 0)

    def run_cp(self):
        cp = self.cp
        i = 0
        while i < len(cp):
            h = cp[i]
            t = h >> 30
            if t == 0:                                  # type 0: n registers from index
                n = ((h >> 16) & 0x3FFF) + 1
                if i + 1 + n > len(cp):
                    break
                base = (h & 0xFFFF) * 4
                for k in range(n):
                    self.write32(0x10000 + base + 4 * k, cp[i + 1 + k])
                i += 1 + n
            elif t == 1:                                # type 1: two registers
                r0, r1 = (h & 0x7FF) * 4, ((h >> 11) & 0x7FF) * 4
                if i + 3 > len(cp):
                    break
                self.write32(0x10000 + r0, cp[i + 1])
                self.write32(0x10000 + r1, cp[i + 2])
                i += 3
            elif t == 2:                                # type 2: filler
                i += 1
            else:                                       # type 3: opcode + payload
                n = ((h >> 16) & 0x3FFF) + 1
                if i + 1 + n > len(cp):
                    break
                self.packet3((h >> 8) & 0xFF, cp[i + 1:i + 1 + n])
                i += 1 + n
        del cp[:i]

    def cp_note(self, text):
        if self.cp_notes.get(text, 0) < 3:
            self.log(f'  ati cp: {text}')
        self.cp_notes[text] = self.cp_notes.get(text, 0) + 1

    def packet3(self, op, p):
        if op == 0x19 and len(p) >= 2:                  # host-data blit into DST surface
            y, x = p[0] >> 16, p[0] & 0xFFFF
            h, w = p[1] >> 16, p[1] & 0xFFFF
            base, pitch = self.reg(0x1004), self.reg(0x1008)     # DST_OFFSET, DST_PITCH (px)
            px = b''.join(v.to_bytes(4, 'little') for v in p[2:])
            for row in range(h):
                src = px[row * w * 2:(row + 1) * w * 2]
                a = (base + ((y + row) * pitch + x) * 2) & self.MEM_MASK
                self.mem[a:a + len(src)] = src
            self.blits += 1
        elif op == 0x1A and len(p) >= 2:                # paint rectangle with the brush colour
            x, y = p[0] >> 16, p[0] & 0xFFFF
            w, h = p[1] >> 16, p[1] & 0xFFFF
            base, pitch = self.reg(0x1004), self.reg(0x1008)
            colour = (self.reg(0x107C) & 0xFFFF).to_bytes(2, 'little')   # DP_BRUSH_FRGD_CLR
            for row in range(h):
                a = (base + ((y + row) * pitch + x) * 2) & self.MEM_MASK
                self.mem[a:a + 2 * w] = colour * w
            self.fills += 1
        else:
            self.cp_note(f'type-3 opcode {op:#x} ({len(p)} words) not implemented')

    def read8(self, addr):
        if addr < 0x10000:
            return self.iface.get(addr, 0)
        return (self.regs.get(addr & ~3, 0) >> (8 * (addr & 3))) & 0xFF

    def write8(self, addr, value):
        if addr < 0x10000:
            self.iface[addr] = value
            return
        w = addr & ~3
        sh = 8 * (addr & 3)
        self.regs[w] = (self.regs.get(w, 0) & ~(0xFF << sh)) | (value << sh)


class ATISerialPort:
    """The ATI chip as seen on the QSPI (chip select 6).

    Frame (inside a 32-byte queue buffer): byte 0 = flags | (length - 1), flag 0x20 = write;
    bytes 1-3 = address. Reads: dummy bytes up to offset 12, then data (little-endian for
    32-bit registers). Writes: data from offset 8."""

    READ_DATA, WRITE_DATA = 12, 8

    def __init__(self, chip):
        self.chip = chip

    def transfer(self, data):
        out = bytearray(len(data))
        if len(data) < 4:
            return bytes(out)
        n = (data[0] & 0x1F) + 1
        write = bool(data[0] & 0x20)
        addr = (data[1] << 16) | int.from_bytes(data[2:4], 'big')
        if write:
            vals = data[self.WRITE_DATA:n]
            if len(vals) == 4 and addr >= 0x10000:
                self.chip.write32(addr, int.from_bytes(vals, 'little'))
            else:
                for i, v in enumerate(vals):
                    self.chip.write8(addr + i, v)
        else:
            k = max(0, n - self.READ_DATA)
            if k == 4 and addr >= 0x10000:
                vals = self.chip.read32(addr).to_bytes(4, 'little')
            else:
                vals = bytes(self.chip.read8(addr + i) for i in range(k))
            out[self.READ_DATA:self.READ_DATA + k] = vals
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
        data = bytes(self.m.uc.mem_read(src, n & ~3))
        words = [int.from_bytes(data[i:i + 4], 'big') for i in range(0, len(data), 4)]
        if self.r.get16(0x1C) & 0x100 and words:
            self.addr = words.pop(0)
            self.r.set16(0x1C, self.r.get16(0x1C) & ~0x100)
        if words:
            key = self.addr >> 16
            self.ranges[key] = self.ranges.get(key, 0) + len(words)
            self.chip.write_block(self.addr, words)
            self.addr += 4 * len(words)
        self.count += 1
        self.r.set16(0x18, (self.r.get16(0x18) & ~0x3) | 0x40)   # idle + done
