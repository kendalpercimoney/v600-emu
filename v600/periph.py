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
    """The power-management IC behind the QSPI (PCAP2: 32 x 25-bit registers).

    SPI word: bit31 = write, bits 30..26 = register, bits 24..0 = data. A read returns the
    register value in the reply word. Register 0 is the interrupt status: write 1 to clear.

    ADC (Linux ezx-pcap.h): writing ASC (bit 22) to reg 10 (ADR) converts the 7 channels of
    the bank selected by reg 9 (ADC) bit 2; ADA1 (reg 9 bits 6:4) / ADA2 (9:7) pick which
    results appear in ADR ADD1 (9:0) / ADD2 (19:10). Completion sets ISR bit 0 (ADCDONE)."""

    ISR, MSR, ADC, ADR = 0, 1, 9, 10
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
    """RTR real-time reference at 0x2484A000: +0 MSB (seconds), +4 LSB (bits 31:17 = 15-bit
    fraction of a second). Effectively a 32.768 kHz count in bits 63:17.

    Driven by the machine's virtual clock (instructions executed, 52 MHz assumed)."""

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
        self.copies = 0
        self.glyphs = 0
        self.lines = 0
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

    # Register names from the Imageon W100 (Linux w100fb.h), which shares this register map.
    # Operations are instant here, so busy/pending bits always read as done:
    #   0x4D8 DISP_DB_BUF_CNTL bit 1: double-buffered display update pending (self-clearing)
    BUSY_BITS = {0x104D8: 0x2}
    #   0x204 GEN_INT_STATUS bit 11: waited for after display updates (0x1091EE84)
    READY_BITS = {0x10204: 0x800}
    #   0x140 RBBM_STATUS: command FIFO entries free (bits 6:0), nothing busy, GUI idle
    RBBM_STATUS, RBBM_IDLE = 0x10140, 0x40
    #   0x414 GRAPHIC_CTRL: pclk_running (25) / sclk_running (26) follow lcd_pclk_on (23) /
    #   lcd_sclk_on (24)
    GRAPHIC_CTRL = 0x10414

    def read32(self, addr):
        self.reads += 1
        a = addr & ~3
        if a == self.RBBM_STATUS:
            v = self.RBBM_IDLE
        elif a == self.GRAPHIC_CTRL:
            v = self.regs.get(a, 0)
            v = (v & ~(3 << 25)) | (((v >> 23) & 3) << 25)
        else:
            v = (self.regs.get(a, 0) & ~self.BUSY_BITS.get(a, 0)) | self.READY_BITS.get(a, 0)
        self._note('R', addr, v)
        return v

    READ_ONLY = {0x10000}                        # CHIP_ID (the driver writes test patterns
                                                 # there; the re-probe after a panel reset
                                                 # must still see the real ID)

    def write32(self, addr, value):
        self.writes += 1
        if addr & ~3 not in self.READ_ONLY:
            self.regs[addr & ~3] = value & 0xFFFFFFFF
        if addr & ~3 == self.RB_WPTR:            # ring (re)start: read pointer follows
            self.regs[self.RB_RPTR] = value & 0xFFFFFFFF
            self.cp.clear()
        self._note('W', addr, value)

    # Command ring (PM4) in chip memory; host writes wrap inside it. 0x21C / 0x220 are the
    # ring read / write pointers in dwords. The driver waits on the read pointer for free
    # space (0x10B88298) and for idle (0x10B87A60: read == write pointer == its own count);
    # a wait that never ends is flagged by the task-progress watchdog (0x10AE56C4, slot 6)
    # and the display is torn down and reinitialised.
    RING_BASE, RING_SIZE = 0x80000, 0x4000
    RB_RPTR = 0x1021C
    RB_WPTR = 0x10220     # advanced by ring writes; the driver writes it on ring (re)start

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
            # write pointer (dwords) follows the data pushed into the ring; read pointer:
            # everything up to the unconsumed tail has been executed
            end = (addr + 4 * len(words) - self.RING_BASE) % self.RING_SIZE
            self.regs[self.RB_WPTR] = end // 4
            self.regs[self.RB_RPTR] = ((end - 4 * len(self.cp)) % self.RING_SIZE) // 4

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

    # Monochrome host-data colours (text): candidates from the register dump at glyph time.
    MONO_FRGD, MONO_BKGD = 0x107C, 0x1078   # DP_BRUSH_FRGD_CLR / _BKGD_CLR

    def packet3(self, op, p):
        if op == 0x19 and len(p) >= 2:                  # host-data blit into DST surface
            y, x = p[0] >> 16, p[0] & 0xFFFF
            h, w = p[1] >> 16, p[1] & 0xFFFF
            base, pitch = self.reg(0x1004), self.reg(0x1008)     # DST_OFFSET, DST_PITCH (px)
            px = b''.join(v.to_bytes(4, 'little') for v in p[2:])
            src_type = (self.reg(0x106C) >> 12) & 7               # GMC_SRC_DATATYPE
            if src_type in (0, 1):                                # monochrome: 1 bit per pixel
                fg = (self.reg(self.MONO_FRGD) & 0xFFFF).to_bytes(2, 'little')
                bg = (self.reg(self.MONO_BKGD) & 0xFFFF).to_bytes(2, 'little')
                for i in range(w * h):
                    byte = px[i >> 3] if (i >> 3) < len(px) else 0
                    on = byte >> (7 - (i & 7)) & 1                # leftmost pixel = bit 7
                    if on or src_type == 0:                       # type 1: background left alone
                        r, c = divmod(i, w)
                        a = (base + ((y + r) * pitch + x + c) * 2) & self.MEM_MASK
                        self.mem[a:a + 2] = fg if on else bg
                self.glyphs += 1
                return
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
        elif op == 0x15 and len(p) >= 2:                # POLYLINE: points (y << 16 | x)
            base, pitch = self.reg(0x1004), self.reg(0x1008)
            colour = (self.reg(0x107C) & 0xFFFF).to_bytes(2, 'little')   # DP_BRUSH_FRGD_CLR
            pts = [(w & 0xFFFF, w >> 16) for w in p]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                for x, y in self._line(x0, y0, x1, y1):
                    if 0 <= x < pitch:
                        a = (base + (y * pitch + x) * 2) & self.MEM_MASK
                        self.mem[a:a + 2] = colour
            self.lines += 1
        elif op == 0x1B and len(p) >= 3:                # screen-to-screen blit, rectangles of
            sbase, spitch = self.reg(0x11AC), self.reg(0x11B0)   # (src x,y)(dst x,y)(w,h)
            dbase, dpitch = self.reg(0x1004), self.reg(0x1008)   # SRC/DST_OFFSET, _PITCH (px)
            for k in range(0, len(p) - 2, 3):
                sx, sy = p[k] >> 16, p[k] & 0xFFFF
                dx, dy = p[k + 1] >> 16, p[k + 1] & 0xFFFF
                w, h = p[k + 2] >> 16, p[k + 2] & 0xFFFF
                w = min(w, spitch - sx, dpitch - dx)        # clip to the surfaces' width
                if w <= 0:
                    continue
                rows = [bytes(self.mem[(sbase + ((sy + r) * spitch + sx) * 2) & self.MEM_MASK:]
                              [:2 * w]) for r in range(h)]
                for r, row in enumerate(rows):
                    a = (dbase + ((dy + r) * dpitch + dx) * 2) & self.MEM_MASK
                    self.mem[a:a + len(row)] = row
            self.copies += 1
        else:
            self.cp_note(f'type-3 opcode {op:#x} ({len(p)} words) not implemented')

    @staticmethod
    def _line(x0, y0, x1, y1):
        """Bresenham, both end points included."""
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            yield x0, y0
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

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


class Keypad:
    """KPP keypad port at 0x2484E000 (Neptune LTE spec ch. 58), 16-bit registers:
    +0 KPCR (KCO open-drain 15:8, KRE row enable 7:0), +2 KPSR (bit 0 KPKD key detect, w1c),
    +4 KDDR (KCDD column direction 15:8, 1 = output), +6 KPDR (KCD columns 15:8, KRD rows 7:0).
    All lines are pulled-up inputs (the firmware never drives a column: KDDR = 0). On the
    V600 a key pulls its row line low, and for most keys also one of the two column lines
    (KBC0-1), so KPDR reads back exactly the SDK key code. `pressed` holds key names.
    KPKD (any enabled row low after none was) raises AITC source 9."""

    GROUND = None
    SOURCE = 9

    # Raw KPDR value per key (P2K-ELF-SDK sdk/hw_keypad.h, mask 0xFC00): a cleared column bit
    # (8-9) names the column, both set = ground; the cleared row bit names the row.
    CODES = {
        '1': 0xFE7F, '2': 0xFEF7, '3': 0xFD7F, '4': 0xFDF7, '5': 0xFF7E, '6': 0xFFF6,
        '7': 0xFFF3, '8': 0xFFF5, '9': 0xFFEB, '*': 0xFF7D, '0': 0xFEBF, '#': 0xFDBF,
        'green': 0xFEDF, 'soft_right': 0xFFBB, 'soft_left': 0xFFBD, 'menu': 0xFF7B,
        'vol_up': 0xFDDF, 'vol_down': 0xFFDE, 'smart': 0xFFDD, 'camera': 0xFFDB,
        'right': 0xFFEE, 'left': 0xFDEF, 'k17': 0xFEEF, 'k19': 0xFFED, 'k29': 0xFFBE,
        'k31': 0xFF3F, 'k32': 0xFF9F,
    }

    @classmethod
    def position(cls, name):
        code = cls.CODES[name]
        cols = (code >> 8) & 3
        col = cls.GROUND if cols == 3 else (0 if not cols & 1 else 1)
        row = next(i for i in range(8) if not code >> i & 1)
        return col, row

    def __init__(self, aitc=None):
        self.kpcr = self.kpsr = self.kddr = 0
        self.cols = 0xFF                        # column output latch (KPDR 15:8)
        self.pressed = set()
        self.aitc = aitc
        self.was_down = False

    def press(self, name, down=True):
        (self.pressed.add if down else self.pressed.discard)(name)
        self.update()

    def lines(self):
        """KPDR pin levels: AND of the pressed keys' codes; output columns show the latch."""
        v = 0xFFFF
        for name in self.pressed:
            v &= self.CODES[name] | 0xFC00
        out = self.kddr & 0xFF00
        return (v & ~out) | ((self.cols << 8) & out)

    def rows(self):
        return self.lines() & 0xFF

    def update(self):
        down = self.rows() & self.kpcr & 0xFF != self.kpcr & 0xFF
        if down and not self.was_down:          # latched on a new press only
            self.kpsr |= 1
        self.was_down = down
        if self.aitc is not None:
            self.aitc.set_line(self.SOURCE, bool(self.kpsr & 1))

    def read(self, off, size):
        self.update()
        v = {0: self.kpcr, 2: self.kpsr, 4: self.kddr,
             6: self.lines()}.get(off & ~1, 0)
        if size == 1:
            return (v >> 8) & 0xFF if off % 2 == 0 else v & 0xFF
        if size == 4:
            nxt = {0: self.kpsr, 4: self.lines()}.get(off, 0)
            return (v << 16) | nxt
        return v

    def write(self, off, size, value):
        if size == 4:
            self.write(off, 2, value >> 16)
            self.write(off + 2, 2, value & 0xFFFF)
            return
        if size == 1:                           # byte access: merge into the 16-bit register
            cur = self.read(off & ~1, 2)
            value = (cur & 0x00FF) | (value << 8) if off % 2 == 0 else (cur & 0xFF00) | value
            off &= ~1
        if off == 0:
            self.kpcr = value
        elif off == 2:
            self.kpsr &= ~value & 1
        elif off == 4:
            self.kddr = value & 0xFF00
        elif off == 6:
            self.cols = value >> 8


class EdgePort:
    """INT edge port at 0x2484C000 (external interrupt pins INT0-7; spec ch. 69), 16-bit:
    +0 EPPAR (2 bits per pin: 00 level, 01 rising, 10 falling, 11 both edges), +2 EPDDR
    (1 = read back EPDR instead of the pin), +4 EPDR, +6 EPFR (edge flags, write 1 to clear).
    Level-sensitive pins request while low; edge flags are latched. Each pin has its own AITC
    source (spec table 6-14). Pins: INT0 = HS_INT (headset / flip detect, pulled up),
    INT1 = power IC interrupt (PCAP INT, driven by the PowerIC)."""

    SOURCES = (60, 54, 39, 35, 30, 22, 13, 5)

    def __init__(self, aitc=None, pins=0xFF):
        self.eppar = self.epddr = self.epdr = self.epfr = 0
        self.pins = pins
        self.aitc = aitc

    def set_pin(self, n, level):
        old = self.pins >> n & 1
        self.pins = (self.pins & ~(1 << n)) | (int(bool(level)) << n)
        mode = self.eppar >> (2 * n) & 3
        if old != level and (mode == 3 or (mode == 1 and level) or (mode == 2 and not level)):
            self.epfr |= 1 << n
        self.update()

    def update(self):
        if self.aitc is None:
            return
        for n, src in enumerate(self.SOURCES):
            mode = self.eppar >> (2 * n) & 3
            req = not (self.pins >> n & 1) if mode == 0 else bool(self.epfr >> n & 1)
            self.aitc.set_line(src, req)

    def read(self, off, size):
        if off == 0:
            return self.eppar
        if off == 2:
            return self.epddr
        if off == 4:
            return (self.epdr & self.epddr) | (self.pins & ~self.epddr & 0xFF)
        if off == 6:
            return self.epfr
        return 0

    def write(self, off, size, value):
        if off == 0:
            self.eppar = value & 0xFFFF
        elif off == 2:
            self.epddr = value & 0xFF
        elif off == 4:
            self.epdr = value & 0xFF
        elif off == 6:
            self.epfr &= ~value
        self.update()


class OneWire:
    """One-wire master at 0x24856000 (spec ch. 66), 16-bit registers: +0 CONTROL (bit 7 RPP
    reset + presence, bit 6 PST presence status, bit 5 WR0, bit 4 WR1/RD, bit 3 RDST),
    +2 TIME_DIVIDER, +4 RESET (bit 0 soft reset). Operations complete instantly. `device`
    is the slave (the battery's DS2502 EPROM) with reset() -> present and bit(value) -> bit
    read back; None means no battery ID chip."""

    def __init__(self, device=None):
        self.device = device
        self.ctrl = 0
        self.div = 0

    def read(self, off, size):
        return {0: self.ctrl, 2: self.div}.get(off, 0)

    def write(self, off, size, value):
        if off == 2:
            self.div = value
        elif off == 4 and value & 1:
            self.ctrl = 0
        elif off == 0:
            ctrl = self.ctrl & 0x48
            if value & 0x80:
                present = bool(self.device and self.device.reset())
                ctrl = (ctrl & ~0x40) | (0x40 if present else 0)
            if value & 0x20 and self.device:
                self.device.bit(0)
            if value & 0x10:
                b = self.device.bit(1) if self.device else 1
                ctrl = (ctrl & ~0x08) | (0x08 if b else 0)
            self.ctrl = ctrl
