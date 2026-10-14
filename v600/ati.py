"""ATI Imageon W2250: the V600's graphics and display chip.

The CPU reaches it two ways: a serial host interface on QSPI chip select 6 (registers, small
writes) and a DMA write port (bursts into registers or chip memory). The chip is
little-endian. Address space as seen by the host:
    0x00000-0x0FFFF  serial-interface registers (8-bit)
    0x10000-0x1FFFF  Imageon registers (32-bit); register R is at 0x10000 + R
    other            chip memory (4 MB window), including the PM4 command ring

Register names and fields come from the W100, which shares the register map: Linux
w100fb.h and the Zaurus X server's imageon_regs.h / imageon_const.h (docs/ref). Where the
firmware relies on something those do not document, the model says so and why.
"""
import numpy as np
from PIL import Image

CHIP_ID = 0x57451002
MEM_MASK = 0x3FFFFF
WHITE = b'\xff\xff'

# registers (offsets without the 0x10000 window base)
CIF_CNTL = 0x0018           # host interface: swap_fbuf_1 (bits 3:2) byte lanes for memory
RBBM_STATUS = 0x0140
GEN_INT_STATUS = 0x0204
RB_RPTR, RB_WPTR = 0x021C, 0x0220
GRAPHIC_CTRL, GRAPHIC_OFFSET, GRAPHIC_PITCH = 0x0414, 0x0418, 0x041C
GRAPHIC_H_DISP, GRAPHIC_V_DISP = 0x042C, 0x0430
DISP_DB_BUF_CNTL = 0x04D8
DST_OFFSET, DST_PITCH = 0x1004, 0x1008
DP_GUI_MASTER_CNTL = 0x106C
DP_BRUSH_FRGD_CLR = 0x107C
SRC_X_Y, DST_X_Y, DST_WIDTH_HEIGHT = 0x1190, 0x1194, 0x1198
SRC_OFFSET, SRC_PITCH = 0x11AC, 0x11B0
SC_TOP_LEFT, SC_BOTTOM_RIGHT = 0x11BC, 0x11C0
GLOBAL_ALPHA = 0x1210
DP_SRC_FRGD_CLR, DP_SRC_BKGD_CLR = 0x1240, 0x1244
DP_MIX = 0x12C8
I2C_CNTL_0, I2C_CNTL_1, I2C_DATA = 0x0988, 0x098C, 0x0990


class ATIChip:
    def __init__(self, log):
        self.log = log
        self.iface = {4: 0x01, 5: 0x11}          # interface ID checked by 0x10920A4C
        self.regs = {0x10000: CHIP_ID}
        self.mem = bytearray(MEM_MASK + 1)
        self.cp = []                             # PM4 words not yet executed
        self.notes = {}
        self.draws = 0                           # drawing operations, to detect new frames
        # veil memory, see "2D engine" below
        self.veiled = bytearray((MEM_MASK + 1) // 2)
        self.under = bytearray(MEM_MASK + 1)
        self.i2c = I2CEngine()                   # the camera sensor's bus

    def reg(self, r):
        return self.regs.get(0x10000 + r, 0)

    def note(self, text):
        if self.notes.get(text, 0) < 3:
            self.log(f'  ati: {text}')
        self.notes[text] = self.notes.get(text, 0) + 1

    # == host access ==========================================================================
    # Operations complete instantly, so status bits always read as done/idle:
    #   RBBM_STATUS: command FIFO free (6:0), nothing busy; DISP_DB_BUF_CNTL bit 1 (update
    #   pending) clear; GEN_INT_STATUS bit 11 set (waited for after display updates,
    #   0x1091EE84); GRAPHIC_CTRL pclk/sclk_running (25/26) follow lcd_pclk/sclk_on (23/24).
    # CHIP_ID is read-only: the driver writes test patterns there, and the re-probe after a
    # panel reset must still see the real ID.
    def read32(self, addr):
        r = (addr & ~3) - 0x10000
        if r in I2CEngine.REGS:
            return self.i2c.read(r)
        v = self.regs.get(addr & ~3, 0)
        if r == RBBM_STATUS:
            return 0x40
        if r == GRAPHIC_CTRL:
            return (v & ~(3 << 25)) | (((v >> 23) & 3) << 25)
        if r == DISP_DB_BUF_CNTL:
            return v & ~0x2
        if r == GEN_INT_STATUS:
            return v | 0x800
        return v

    def write32(self, addr, value):
        a = addr & ~3
        if a - 0x10000 in I2CEngine.REGS:
            self.i2c.write(a - 0x10000, value & 0xFFFFFFFF)
            return
        if a != 0x10000:
            self.regs[a] = value & 0xFFFFFFFF
        r = a - 0x10000
        if r == RB_WPTR:                         # ring (re)start: read pointer follows
            self.regs[0x10000 + RB_RPTR] = value & 0xFFFFFFFF
            self.cp.clear()
        elif r == DST_WIDTH_HEIGHT:
            self.blit_registers()

    def read8(self, addr):
        if addr < 0x10000:
            return self.iface.get(addr, 0)
        if addr < 0x20000:
            return (self.read32(addr & ~3) >> (8 * (addr & 3))) & 0xFF
        return self.mem[addr & MEM_MASK]

    def write8(self, addr, value):
        if addr < 0x10000:
            self.iface[addr] = value
        elif addr < 0x20000:
            sh = 8 * (addr & 3)
            self.write32(addr, (self.regs.get(addr & ~3, 0) & ~(0xFF << sh)) | (value << sh))
        else:
            self.write_tail(addr, bytes([value]))

    # CIF_CNTL swap_fbuf_1: byte lanes of host writes to memory. Icons are uploaded with 3
    # (the two 16-bit halves of each word swapped); ignoring it mirrored every pixel pair.
    @staticmethod
    def _swap(w, mode):
        if mode == 1:                                   # bytes within each 16-bit half
            return ((w & 0x00FF00FF) << 8) | ((w >> 8) & 0x00FF00FF)
        if mode == 2:                                   # all four bytes
            return int.from_bytes(w.to_bytes(4, 'little'), 'big')
        if mode == 3:                                   # 16-bit halves
            return ((w & 0xFFFF) << 16) | (w >> 16)
        return w

    def write_block(self, addr, words):
        """A host burst: registers if in the register window, otherwise chip memory. Words
        written into the command ring are executed in order."""
        if 0x10000 <= addr < 0x20000:
            for i, w in enumerate(words):
                self.write32(addr + 4 * i, w)
            return
        ring = self.RING_BASE <= addr < self.RING_BASE + self.RING_SIZE
        swap = 0 if ring else (self.reg(CIF_CNTL) >> 2) & 3
        for i, w in enumerate(words):
            a = addr + 4 * i
            if ring:
                a = self.RING_BASE + (a - self.RING_BASE) % self.RING_SIZE
            a &= MEM_MASK
            self.mem[a:a + 4] = self._swap(w, swap).to_bytes(4, 'little')
        if ring:
            self.cp.extend(words)
            self.run_cp()
            end = (addr + 4 * len(words) - self.RING_BASE) % self.RING_SIZE
            self.regs[0x10000 + RB_WPTR] = end // 4
            self.regs[0x10000 + RB_RPTR] = ((end - 4 * len(self.cp)) % self.RING_SIZE) // 4

    def write_tail(self, addr, data):
        """A memory write shorter than a word (an odd-width image row ends in one): the bytes
        land where they would in a full word written at addr with the current swap mode."""
        swap = (self.reg(CIF_CNTL) >> 2) & 3
        lanes = self._swap(0x01020304, swap).to_bytes(4, 'little')    # memory byte <- source
        for k, src in enumerate(lanes):
            if src - 1 < len(data):
                self.mem[(addr + k) & MEM_MASK] = data[src - 1]

    # == command processor ====================================================================
    # PM4 ring in chip memory; 0x21C / 0x220 are its read / write pointers in dwords. The
    # driver waits for read == write == its own count (0x10B87A60); a wait that never ends
    # trips the task-progress watchdog and the display is torn down. Type-3 opcodes are
    # Radeon's with bit 7 clear.
    RING_BASE, RING_SIZE = 0x80000, 0x4000

    def run_cp(self):
        cp, i = self.cp, 0
        while i < len(cp):
            h = cp[i]
            kind = h >> 30
            if kind == 0:                               # n registers from an index
                n = ((h >> 16) & 0x3FFF) + 1
                if i + 1 + n > len(cp):
                    break
                for k in range(n):
                    self.write32(0x10000 + (h & 0xFFFF) * 4 + 4 * k, cp[i + 1 + k])
                i += 1 + n
            elif kind == 1:                             # two registers
                if i + 3 > len(cp):
                    break
                self.write32(0x10000 + (h & 0x7FF) * 4, cp[i + 1])
                self.write32(0x10000 + ((h >> 11) & 0x7FF) * 4, cp[i + 2])
                i += 3
            elif kind == 2:                             # filler
                i += 1
            else:                                       # opcode + payload
                n = ((h >> 16) & 0x3FFF) + 1
                if i + 1 + n > len(cp):
                    break
                self.packet3((h >> 8) & 0xFF, cp[i + 1:i + 1 + n])
                i += 1 + n
        del cp[:i]

    def packet3(self, op, p):
        if op == 0x19 and len(p) >= 2:                  # host data: (y, x), (h, w), pixels
            self.host_data(p[0] & 0xFFFF, p[0] >> 16, p[1] & 0xFFFF, p[1] >> 16, p[2:])
        elif op == 0x1A:                                # PAINT_MULTI: (x, y), (w, h)
            for k in range(0, len(p) - 1, 2):
                self.paint(p[k] >> 16, p[k] & 0xFFFF, p[k + 1] >> 16, p[k + 1] & 0xFFFF)
        elif op == 0x15:                                # POLYLINE: points (y, x)
            self.polyline([(w & 0xFFFF, w >> 16) for w in p])
        elif op == 0x1B:                                # BITBLT_MULTI: (src)(dst)(w, h)
            for k in range(0, len(p) - 2, 3):
                self.copy(self.reg(SRC_OFFSET), self.reg(SRC_PITCH), p[k] >> 16, p[k] & 0xFFFF,
                          p[k + 1] >> 16, p[k + 1] & 0xFFFF, p[k + 2] >> 16, p[k + 2] & 0xFFFF)
        else:
            self.note(f'type-3 opcode {op:#x} ({len(p)} words) not implemented')
            return
        self.draws += 1

    def blit_registers(self):
        """Writing DST_WIDTH_HEIGHT (w << 16 | h) starts a blit from SRC_X_Y to DST_X_Y (both
        x << 16 | y); the firmware draws icons this way."""
        s, d, wh = self.reg(SRC_X_Y), self.reg(DST_X_Y), self.reg(DST_WIDTH_HEIGHT)
        self.copy(self.reg(SRC_OFFSET), self.reg(SRC_PITCH), s >> 16, s & 0xFFFF,
                  d >> 16, d & 0xFFFF, wh >> 16, wh & 0xFFFF)
        self.draws += 1

    # == 2D engine ============================================================================
    # Destination: DST_OFFSET / DST_PITCH (pixels), RGB565. With GMC dst_clipping (bit 3)
    # writes are limited to SC_TOP_LEFT - SC_BOTTOM_RIGHT (y << 16 | x, bottom-right
    # exclusive). Mono source data (text) expands to DP_SRC_FRGD_CLR / DP_SRC_BKGD_CLR; fills
    # and lines use DP_BRUSH_FRGD_CLR.
    #
    # Modes, from DP_MIX (and DP_GUI_MASTER_CNTL, which writes the same fields):
    #   dp_op (DP_MIX bit 24, GMC bit 31): arithmetic op. The firmware sets E2_ARITHMETIC_CNTL
    #       to 4, DST_SADDBLEND_SRC2 with both factors = GLOBAL_ALPHA, see blend().
    #   bits 26-27, not documented anywhere we have. Two uses are understood from what the
    #       firmware does and what the phone shows:
    #     - copies with ROP pair AA/CC (DP_MIX 23:16 / 7:0) are icon blits: the icon's flat
    #       background must not show (no colour-compare register is set; the background is
    #       taken from the icon's top-left pixel);
    #     - white fills redraw translucent areas: a menu row the highlight leaves is filled
    #       white in this mode, and on the phone it is translucent again. So an alpha fill
    #       remembers what it veiled (veiled/under), and such a white fill re-veils it.
    # The UI composes each screen in a layer surface (wallpaper or a patch of it, veiled by
    # alpha fills, then dialogs) and copies it to the displayed surface.

    def _mode(self):
        """(alpha op, bits 26-27) for the current operation."""
        mix, gmc = self.reg(DP_MIX), self.reg(DP_GUI_MASTER_CNTL)
        return bool(mix >> 24 & 1 or gmc >> 31), bool(mix >> 26 & 3)

    def _clip(self):
        if not self.reg(DP_GUI_MASTER_CNTL) & 0x8:
            return 0, 0, 0x2000, 0x2000
        tl, br = self.reg(SC_TOP_LEFT), self.reg(SC_BOTTOM_RIGHT)
        return tl & 0xFFFF, tl >> 16, br & 0xFFFF, br >> 16

    def _rows(self, x, y, w, h):
        """(row, address of the first visible pixel, first, end) for each visible row run of
        a w x h rectangle at (x, y) on the destination; first/end index into the row."""
        base, pitch = self.reg(DST_OFFSET), self.reg(DST_PITCH)
        x0, y0, x1, y1 = self._clip()
        c0, c1 = max(0, x0 - x), max(0, min(w, x1 - x, pitch - x))
        if c0 >= c1:
            return
        for r in range(max(0, y0 - y), max(0, min(h, y1 - y))):
            yield r, (base + ((y + r) * pitch + x + c0) * 2) & MEM_MASK, c0, c1

    def _put(self, a, data, keep_veil):
        """Plain write of pixels; content that replaces the picture also forgets its veil."""
        self.mem[a:a + len(data)] = data
        if not keep_veil:
            self.veiled[a >> 1:(a + len(data)) >> 1] = bytes(len(data) // 2)

    def blend(self, src, dst):
        """DST_SADDBLEND_SRC2: src * alpha + dst * (1 - alpha) per RGB565 channel, with
        alpha_r/g/b from GLOBAL_ALPHA bytes 0/1/2 in sixteenths (the UI uses 8 and 11). The
        destination weight is taken as 1 - alpha: read literally (both factors = alpha,
        added, saturated), every alpha-11 layer the UI draws comes out white."""
        ga = self.reg(GLOBAL_ALPHA)
        out = 0
        for shift, mask, k in ((11, 31, ga & 0xFF), (5, 63, ga >> 8 & 0xFF), (0, 31, ga >> 16 & 0xFF)):
            a = min(k, 16) / 16
            v = ((src >> shift) & mask) * a + ((dst >> shift) & mask) * (1 - a)
            out |= min(mask, int(v + 0.5)) << shift
        return out

    def _fill(self, a, colour, n):
        """n pixels of a brush colour at address a, in the current mode."""
        alpha_op, special = self._mode()
        if not alpha_op and not (special and colour == WHITE):
            self._put(a, colour * n, special)
            return
        fg = int.from_bytes(colour, 'little')
        cache = {}
        for c in range(a, a + 2 * n, 2):
            if alpha_op:                                # veil: remember what is under it
                self.under[c:c + 2] = self.mem[c:c + 2]
                self.veiled[c >> 1] = 1
            elif not self.veiled[c >> 1]:
                self.mem[c:c + 2] = colour
                continue
            under = bytes(self.under[c:c + 2])
            if under not in cache:
                cache[under] = self.blend(fg, int.from_bytes(under, 'little')).to_bytes(2, 'little')
            self.mem[c:c + 2] = cache[under]

    def paint(self, x, y, w, h):
        colour = (self.reg(DP_BRUSH_FRGD_CLR) & 0xFFFF).to_bytes(2, 'little')
        for _, a, c0, c1 in self._rows(x, y, w, h):
            self._fill(a, colour, c1 - c0)

    def polyline(self, pts):
        colour = (self.reg(DP_BRUSH_FRGD_CLR) & 0xFFFF).to_bytes(2, 'little')
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            for x, y in self._line(x0, y0, x1, y1):
                for _, a, _, _ in self._rows(x, y, 1, 1):
                    self._fill(a, colour, 1)

    def host_data(self, x, y, w, h, words):
        px = b''.join(v.to_bytes(4, 'little') for v in words)
        src_type = (self.reg(DP_GUI_MASTER_CNTL) >> 12) & 7
        if src_type in (0, 1):                          # 1 bpp, leftmost pixel = bit 7
            fg = (self.reg(DP_SRC_FRGD_CLR) & 0xFFFF).to_bytes(2, 'little')
            bg = (self.reg(DP_SRC_BKGD_CLR) & 0xFFFF).to_bytes(2, 'little')
            for r, a, c0, c1 in self._rows(x, y, w, h):
                for c in range(c0, c1):
                    i = r * w + c
                    on = (px[i >> 3] if (i >> 3) < len(px) else 0) >> (7 - (i & 7)) & 1
                    if on or src_type == 0:             # type 1: background left alone
                        self.mem[a + 2 * (c - c0):a + 2 * (c - c0) + 2] = fg if on else bg
            return
        for r, a, c0, c1 in self._rows(x, y, w, h):     # 16 bpp image
            self._put(a, px[(r * w + c0) * 2:(r * w + c1) * 2], False)

    def copy(self, sbase, spitch, sx, sy, dx, dy, w, h):
        mix = self.reg(DP_MIX)
        icon = mix >> 26 & 3 and (mix >> 16 & 0xFF, mix & 0xFF) == (0xAA, 0xCC)
        key = bytes(self.mem[(sbase + (sy * spitch + sx) * 2) & MEM_MASK:][:2])
        w = min(w, spitch - sx)
        rows = [bytes(self.mem[(sbase + ((sy + r) * spitch + sx) * 2) & MEM_MASK:][:2 * w])
                for r in range(h)]                      # read first: rectangles may overlap
        for r, a, c0, c1 in self._rows(dx, dy, w, h):
            row = rows[r][2 * c0:2 * c1]
            if not icon:
                self._put(a, row, False)
                continue
            for c in range(0, len(row), 2):
                if row[c:c + 2] != key:
                    self.mem[a + c:a + c + 2] = row[c:c + 2]

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

    # == display ==============================================================================
    def screen(self):
        """The displayed frame (GRAPHIC_OFFSET, GRAPHIC_PITCH bytes per row) as RGB."""
        base = self.reg(GRAPHIC_OFFSET)
        pitch = self.reg(GRAPHIC_PITCH) or 0x160
        w = (self.reg(GRAPHIC_H_DISP) >> 16) or 176
        h = (self.reg(GRAPHIC_V_DISP) >> 16) or 220
        rows = [bytes(self.mem[base + y * pitch: base + y * pitch + 2 * w]) for y in range(h)]
        a = np.frombuffer(b''.join(rows), dtype='<u2').reshape(h, w)
        rgb = np.dstack([((a >> 11) & 31) * 255 // 31, ((a >> 5) & 63) * 255 // 63,
                         (a & 31) * 255 // 31]).astype(np.uint8)
        return Image.fromarray(rgb)


class ATISerialPort:
    """The chip on the QSPI (chip select 6). Frame (inside a 32-byte queue buffer): byte 0 =
    flags | (length - 1), flag 0x20 = write; bytes 1-3 = address. Reads: data from offset 12
    (little-endian for registers). Writes: data from offset 8."""

    READ_DATA, WRITE_DATA = 12, 8

    def __init__(self, chip):
        self.chip = chip

    def transfer(self, data):
        out = bytearray(len(data))
        if len(data) < 4:
            return bytes(out)
        n = (data[0] & 0x1F) + 1
        addr = (data[1] << 16) | int.from_bytes(data[2:4], 'big')
        if data[0] & 0x20:
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


class I2CEngine:
    """The chip's hardware-assisted I2C master (the camera sensor's control bus).

    Not in the W2250 manual; the layout is the Radeon multimedia I2C block (radeon_reg.h,
    I2C_CNTL_0/1 and I2C_DATA at 0x90/0x94/0x98 there), which matches how the V600 driver
    (0x10B7EB0C read, 0x10B7EE48 write) uses 0x988/0x98C/0x990:
      CNTL_0: done/nack/halt (bits 0-2, write 1 to clear), soft_rst (5), start/stop (8/9),
              receive (10), go (12; the driver waits for it to clear), prescale (16-31)
      CNTL_1: data byte count (4:0), address byte count (10:8)
      DATA:   byte FIFO; the address bytes (device address first), then data to send, and
              after a receive the bytes read.
    Transfers complete at once."""
    REGS = (I2C_CNTL_0, I2C_CNTL_1, I2C_DATA)
    DONE, NACK, HALT, SOFT_RST, RECEIVE, GO = 1, 2, 4, 1 << 5, 1 << 10, 1 << 12

    def __init__(self):
        self.devices = {}                        # 7-bit address -> device(write(bytes), read(n))
        self.cntl0 = self.cntl1 = 0
        self.fifo = []

    def read(self, r):
        if r == I2C_DATA:
            return self.fifo.pop(0) if self.fifo else 0
        return self.cntl0 if r == I2C_CNTL_0 else self.cntl1

    def write(self, r, value):
        if r == I2C_DATA:
            self.fifo.append(value & 0xFF)
        elif r == I2C_CNTL_1:
            self.cntl1 = value
        else:
            status = self.cntl0 & ~value & (self.DONE | self.NACK | self.HALT)
            self.cntl0 = (value & ~(self.DONE | self.NACK | self.HALT | self.SOFT_RST)) | status
            if value & self.SOFT_RST:
                self.fifo.clear()
            if value & self.GO:
                self._transfer()

    def _transfer(self):
        n_addr, n_data = (self.cntl1 >> 8) & 7, self.cntl1 & 0x1F
        addr, out = self.fifo[:n_addr], self.fifo[n_addr:]
        self.fifo = []
        dev = self.devices.get(addr[0] >> 1) if addr else None
        self.cntl0 &= ~self.GO
        self.cntl0 |= self.DONE
        if dev is None:
            self.cntl0 |= self.NACK
        elif self.cntl0 & self.RECEIVE or addr[0] & 1:
            if addr[1:]:
                dev.write(bytes(addr[1:]))
            self.fifo = list(dev.read(n_data))
        else:
            dev.write(bytes(addr[1:] + out[:n_data]))
