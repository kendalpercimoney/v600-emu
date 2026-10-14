"""SIM card: the Neptune smart-card interface (SCIM, port 0) and a virtual GSM SIM.

Interface registers (Neptune LTE spec ch. 51, 16-bit, offsets from 0x2484F000):
  0x10 PORT0_CNTL  bit 1 sven (card power), 2 sten, 3 srst (1 = reset asserted), 4 scen (clock)
  0x12 CNTL        bit 1 icm (initial character mode, cleared on a valid TS), 5:4 clk_sel
                   (ref/4, /3, /5, ref), 8:6 baud_sel (372, 256, ... card clocks per etu),
                   10:9 gpcnt_clk_sel (off, card clock, receive clock, etu clock)
  0x14 RCV_THRESHOLD  4:0 rdt
  0x16 ENABLE      bit 0 rcv_en, bit 1 xmt_en
  0x18 XMT_STATUS  bit 8 gpcnt, 7 tdtf, 6 tfo, 5 tc, 4 etc, 3 tfe, 0 xte (w1c)
  0x1A RCV_STATUS  bit 5 rdrf (>= rdt bytes), 4 rfd (any byte), 0 oef (w1c)
  0x1C INT_MASK    1 = masked: 0 rim, 1 tcim, 2 oim, 3 etcim, 4 tfeim, 5 xtm, 6 tfom, 7 tdtfm,
                   8 gpcntm, 9 cwtm, 10 rtm
  0x1E PORT0_XMT_BUF, 0x20 PORT0_RCV_BUF (bit 8 pe, 9 fe, data 7:0)
  0x26 XMT_THRESHOLD 3:0 tdt;  0x2C RESET_CNTL bit 1 flush_rcv, 2 flush_xmt, 3 soft_reset
  0x30 GPCNT       compare value for the general purpose counter
Interrupts: data (tc, etc, tfe, rdrf, tdtf) = AITC source 51, general (xte, oef, gpcnt, ...)
= source 12.

The card answers in real (virtual) time: one character is 12 etu, an etu is 372 card clocks
at ref/4 = 3.25 MHz, so about 1.4 ms per byte.
"""
import collections

REF_HZ = 13_000_000


class SIMCard:
    """GSM 11.11 SIM, T=0, with a small file system. CHV1 (PIN) is disabled."""

    ATR = bytes([0x3B, 0x02, 0x14, 0x50])      # direct convention, T=0, defaults, 2 hist.

    IMSI = '001010123456789'                   # MCC 001 / MNC 01: the GSM test network

    def __init__(self):
        self.files = {}                        # fid -> dict
        self.log = []                          # (header, data, status, response) per command
        self._build()
        self.reset()

    # -- file system --------------------------------------------------------------------
    def _df(self, fid, parent):
        self.files[fid] = {'fid': fid, 'parent': parent, 'type': 'MF' if fid == 0x3F00 else 'DF'}

    def _ef(self, fid, parent, data=None, records=None, rec_len=0, cyclic=False):
        f = {'fid': fid, 'parent': parent, 'type': 'EF'}
        if records is not None:
            f.update(records=[bytearray(r) for r in records], rec_len=rec_len,
                     structure=3 if cyclic else 1, rec=0)
        else:
            f.update(data=bytearray(data), structure=0)
        self.files[(parent, fid)] = f

    @staticmethod
    def _bcd_swapped(digits):
        digits = digits + 'F' * (len(digits) % 2)
        return bytes(int(digits[i + 1], 16) << 4 | int(digits[i], 16)
                     for i in range(0, len(digits), 2))

    def _imsi(self):
        d = self.IMSI
        first = int(d[0]) << 4 | (9 if len(d) % 2 else 1)       # parity nibble: 9 = odd
        rest = self._bcd_swapped(d[1:])
        return bytes([1 + len(rest), first]) + rest + b'\xFF' * (8 - 1 - len(rest))

    def _build(self):
        MF, GSM, TEL = 0x3F00, 0x7F20, 0x7F10
        self._df(MF, None)
        self._df(GSM, MF)
        self._df(TEL, MF)
        ff = lambda n: b'\xFF' * n
        self._ef(0x2FE2, MF, self._bcd_swapped('8901010123456789012'))        # ICCID
        self._ef(0x2F05, MF, ff(4))                                           # ELP
        self._ef(0x6F05, GSM, ff(4))                                          # LP
        self._ef(0x6F07, GSM, self._imsi())                                   # IMSI
        self._ef(0x6F20, GSM, ff(8) + b'\x07')                                # Kc
        self._ef(0x6F30, GSM, ff(24))                                         # PLMNsel
        self._ef(0x6F31, GSM, b'\x0A')                                        # HPPLMN
        # SST: services 1 CHV1 disable, 2 ADN, 4 SMS, 7 PLMNsel, 9 MSISDN, 10 EXT1,
        # 12 SMSP, 13 LND, 17 SPN (2 bits each: allocated, activated)
        self._ef(0x6F38, GSM, bytes([0xCF, 0x30, 0xCF, 0x03, 0x03]))
        self._ef(0x6F46, GSM, b'\x01' + b'V600 EMU'.ljust(16, b'\xFF'))       # SPN
        self._ef(0x6F74, GSM, bytes(16))                                      # BCCH
        self._ef(0x6F78, GSM, b'\x00\x01')                                    # ACC
        self._ef(0x6F7B, GSM, ff(12))                                         # FPLMN
        self._ef(0x6F7E, GSM, ff(4) + b'\x00\xF1\x10\xFF\xFE' + b'\xFF\x01')  # LOCI
        self._ef(0x6FAD, GSM, b'\x00\x00\x00\x02')                            # AD
        self._ef(0x6FAE, GSM, b'\x02')                                        # Phase 2
        self._ef(0x6F37, GSM, b'\x00\x00\x00')                                # ACMmax
        self._ef(0x6F39, GSM, records=[bytes(3)], rec_len=3, cyclic=True)    # ACM
        self._ef(0x6F41, GSM, ff(5))                                          # PUCT
        self._ef(0x6F45, GSM, ff(2))                                          # CBMI
        rec = lambda n, k: [ff(n)] * k
        self._ef(0x6F3A, TEL, records=rec(28, 20), rec_len=28)                # ADN
        self._ef(0x6F3B, TEL, records=rec(28, 5), rec_len=28)                 # FDN
        self._ef(0x6F3C, TEL, records=[b'\x00' + ff(175)] * 10, rec_len=176)  # SMS
        self._ef(0x6F3D, TEL, records=rec(14, 2), rec_len=14)                 # CCP
        self._ef(0x6F40, TEL, records=rec(28, 2), rec_len=28)                 # MSISDN
        self._ef(0x6F42, TEL, records=rec(40, 1), rec_len=40)                 # SMSP
        self._ef(0x6F43, TEL, b'\xFF\xFF')                                    # SMSS
        self._ef(0x6F44, TEL, records=rec(28, 5), rec_len=28, cyclic=True)   # LND
        self._ef(0x6F4A, TEL, records=rec(13, 5), rec_len=13)                 # EXT1

    def _children(self, df):
        return [f for k, f in self.files.items() if isinstance(k, tuple) and k[0] == df] + \
               [f for k, f in self.files.items() if isinstance(k, int) and f['parent'] == df]

    def _response(self, f):
        fid = f['fid'].to_bytes(2, 'big')
        if f['type'] != 'EF':
            kids = self._children(f['fid'])
            n_df = sum(k['type'] == 'DF' for k in kids)
            return bytes([0, 0, 0x10, 0x00]) + fid + bytes([1 if f['type'] == 'MF' else 2,
                         0, 0, 0, 0, 0, 9,
                         0x91,                  # clock stop allowed, 3 V, CHV1 disabled
                         n_df, len(kids) - n_df, 4, 0,
                         0x83, 0x8A, 0x83, 0x8A])   # CHV1/2 initialised, 3 / 10 tries
        size = len(f['records']) * f['rec_len'] if 'records' in f else len(f['data'])
        return bytes([0, 0]) + size.to_bytes(2, 'big') + fid + bytes([
            4, 0x40 if f['structure'] == 3 else 0, 0x00, 0x00, 0x00, 0x01, 2,
            f['structure'], f.get('rec_len', 0)])

    def _select(self, fid):
        if fid == 0x3F00:
            return self.files[0x3F00]
        cur = self.df
        for df in (cur, self.files[cur]['parent'], 0x3F00):
            if df is None:
                continue
            f = self.files.get((df, fid))
            if f:
                return f
            f = self.files.get(fid)
            if f and (f['parent'] == df or fid == self.files[cur]['parent']):
                return f
        return None

    # -- protocol -----------------------------------------------------------------------
    def reset(self):
        self.df = 0x3F00
        self.ef = None
        self.last = b''
        self.state = 'header'
        self.buf = bytearray()
        self.need = 0
        self.log.append(('reset', '', '', ''))
        return self.ATR

    INCOMING = {0xA4, 0xD6, 0xDC, 0x20, 0x24, 0x26, 0x28, 0x2C, 0x88, 0xA2, 0x32, 0x10, 0x14,
                0xC2}
    OUTGOING = {0xB0, 0xB2, 0xC0, 0xF2, 0x12}

    def feed(self, byte):
        """One byte from the phone; returns the bytes the card sends back."""
        self.buf.append(byte)
        if self.state == 'header':
            if len(self.buf) < 5:
                return b''
            self.hdr = bytes(self.buf)
            self.buf.clear()
            cla, ins, p1, p2, p3 = self.hdr
            if cla != 0xA0:
                return b'\x6E\x00'
            if ins in self.INCOMING and p3:
                self.state, self.need = 'data', p3
                return bytes([ins])                 # procedure byte: send the data
            return self._execute(b'')
        if self.state == 'data' and len(self.buf) >= self.need:
            data = bytes(self.buf)
            self.buf.clear()
            self.state = 'header'
            return self._execute(data)
        return b''

    def _execute(self, data):
        cla, ins, p1, p2, p3 = self.hdr
        sw, out = self._command(ins, p1, p2, p3, data)
        self.log.append((self.hdr.hex(), data.hex(), sw.hex(), out.hex()))
        if ins in self.OUTGOING and sw[:1] == b'\x90':
            return bytes([ins]) + out + sw
        return sw

    def _command(self, ins, p1, p2, p3, data):
        OK = b'\x90\x00'
        if ins == 0xA4:                                         # SELECT
            f = self._select(int.from_bytes(data[:2], 'big'))
            if f is None:
                return b'\x94\x04', b''
            if f['type'] == 'EF':
                self.ef = f
            else:
                self.df, self.ef = f['fid'], None
            self.last = self._response(f)
            return bytes([0x9F, len(self.last)]), b''
        if ins == 0xC0:                                         # GET RESPONSE
            n = p3 or 256
            if n > len(self.last):
                return bytes([0x67, len(self.last)]), b''
            return OK, self.last[:n]
        if ins == 0xF2:                                         # STATUS
            r = self._response(self.files[self.df])
            n = p3 or 256
            return OK, (r + bytes(max(0, n - len(r))))[:n]
        f = self.ef
        if ins in (0xB0, 0xD6):                                 # READ / UPDATE BINARY
            if f is None:
                return b'\x94\x00', b''
            if f['structure'] != 0:
                return b'\x94\x08', b''
            off = p1 << 8 | p2
            n = p3 or 256
            if off + (len(data) if ins == 0xD6 else n) > len(f['data']):
                return b'\x67\x00', b''
            if ins == 0xD6:
                f['data'][off:off + len(data)] = data
                return OK, b''
            return OK, bytes(f['data'][off:off + n])
        if ins in (0xB2, 0xDC):                                 # READ / UPDATE RECORD
            if f is None:
                return b'\x94\x00', b''
            if 'records' not in f:
                return b'\x94\x08', b''
            recs, cur = f['records'], f['rec']
            if p2 == 0x02:
                cur = cur + 1 if cur < len(recs) else (1 if f['structure'] == 3 else 0)
            elif p2 == 0x03:
                cur = cur - 1 if cur > 1 else (len(recs) if f['structure'] == 3 else 0)
            elif p2 == 0x04:
                cur = p1 or cur
            if not 1 <= cur <= len(recs):
                return b'\x94\x02', b''
            if (p3 or 256) != f['rec_len']:
                return bytes([0x67, f['rec_len']]), b''
            f['rec'] = cur
            if ins == 0xDC:
                recs[cur - 1][:] = data
                return OK, b''
            return OK, bytes(recs[cur - 1])
        if ins == 0xA2:                                         # SEEK
            return b'\x94\x04', b''
        if ins == 0x32:                                         # INCREASE
            return b'\x98\x04', b''
        if ins == 0x88:                                         # RUN GSM ALGORITHM
            self.last = bytes(12)                               # SRES + Kc
            return b'\x9F\x0C', b''
        if ins in (0x20, 0x24, 0x26, 0x28, 0x2C):               # CHV commands
            return OK, b''
        if ins in (0xFA, 0x04, 0x44, 0x10, 0x14, 0xC2):         # SLEEP, (in)validate, SAT
            return OK, b''
        return b'\x6D\x00', b''


class SIMInterface:
    DATA_SOURCE, GENERAL_SOURCE = 51, 12

    P0CNTL, CNTL, RCVTH, ENABLE, XSTAT, RSTAT, IMASK = 0x10, 0x12, 0x14, 0x16, 0x18, 0x1A, 0x1C
    P0XMT, P0RCV, XTH, RESET_CNTL, GPCNT = 0x1E, 0x20, 0x26, 0x2C, 0x30
    W1C = {XSTAT: 0x01F9, RSTAT: 0x0F01}

    def __init__(self, m, card=None, ips=52_000_000):
        self.m = m
        self.card = card
        self.ips = ips
        self.r = collections.defaultdict(int)
        self.r[self.CNTL] = 0x0006
        self.r[self.RCVTH] = 0x0001
        self.r[self.XSTAT] = 0x0038                 # tc, etc, tfe set out of reset
        self.r[self.IMASK] = 0x1FFF                 # everything masked out of reset
        self.rx = collections.deque()
        self.tx = collections.deque()
        self.out = collections.deque()              # (due insns, byte) from the card
        self.tx_busy_until = None
        self.tx_byte = None
        self.gp_start = None
        self.gp_matches = 0                         # comparator matches since the count began
        self.awake = False                          # card has answered reset since power-up
        self.trace = []                             # (time, 'tx'/'rx', byte) for debugging

    # -- timing ---------------------------------------------------------------------------
    def card_hz(self):
        return REF_HZ / (4, 3, 5, 1)[(self.r[self.CNTL] >> 4) & 3]

    def etu_clocks(self):
        return (372, 256, 128, 64, 32, 32, 16, 372)[(self.r[self.CNTL] >> 6) & 7]

    def byte_insns(self):
        return int(12 * self.etu_clocks() / self.card_hz() * self.ips)

    def gp_count(self):
        sel = (self.r[self.CNTL] >> 9) & 3
        if not sel or self.gp_start is None:
            return None
        clocks = (self.m.insns - self.gp_start) * self.card_hz() / self.ips
        return clocks if sel == 1 else clocks / self.etu_clocks() * (16 if sel == 2 else 1)

    # -- card wiring ----------------------------------------------------------------------
    def _port(self, old, new):
        """A powered card answers (ATR) once it is clocked and out of reset: after power-up
        (cold) or after the reset line is released (warm). Stopping and restarting the clock
        (the phone does this when idle, the card allows it) does not reset it."""
        if not new & 0x02 or new & 0x08:                    # unpowered or held in reset
            self.awake = False
            self.out.clear()
            return
        if self.card and not self.awake and new & 0x10:
            self.awake = True
            atr = self.card.reset()
            self.tx.clear()
            self._send(atr, self.m.insns + int(1000 / self.card_hz() * self.ips))

    def _send(self, data, start):
        t = max(start, self.out[-1][0] if self.out else 0)
        for b in data:
            t += self.byte_insns()
            self.out.append((t, b))

    # -- registers ------------------------------------------------------------------------
    def _status(self):
        n = len(self.rx)
        rs = self.r[self.RSTAT] & ~0x30
        if n:
            rs |= 0x10
        if n >= max(1, self.r[self.RCVTH] & 0x1F):
            rs |= 0x20
        self.r[self.RSTAT] = rs

    def _irq(self):
        aitc = getattr(self.m, 'aitc', None)
        if aitc is None:
            return
        self._status()
        mask = self.r[self.IMASK]
        xs, rs = self.r[self.XSTAT], self.r[self.RSTAT]
        data = ((xs & 0x20 and not mask & 0x02) or (xs & 0x10 and not mask & 0x08) or
                (xs & 0x08 and not mask & 0x10) or (rs & 0x20 and not mask & 0x01) or
                (xs & 0x80 and not mask & 0x80))
        general = ((xs & 0x100 and not mask & 0x100) or (xs & 0x01 and not mask & 0x20) or
                   (xs & 0x40 and not mask & 0x40) or (rs & 0x01 and not mask & 0x04))
        aitc.set_line(self.DATA_SOURCE, bool(data))
        aitc.set_line(self.GENERAL_SOURCE, bool(general))

    def read(self, off, size):
        if size == 4:
            return self.read(off, 2) << 16 | self.read(off + 2, 2)
        if size == 1:
            v = self.read(off & ~1, 2)
            return v >> 8 if off % 2 == 0 else v & 0xFF
        self.update()
        if off == self.P0RCV:
            v = self.rx.popleft() if self.rx else 0
            self._irq()
            return v
        self._status()
        return self.r[off]

    def write(self, off, size, value):
        if size == 4:
            self.write(off, 2, value >> 16)
            self.write(off + 2, 2, value & 0xFFFF)
            return
        if size == 1:
            cur = self.r[off & ~1]
            value = (cur & 0x00FF) | (value << 8) if off % 2 == 0 else (cur & 0xFF00) | value
            off &= ~1
        value &= 0xFFFF
        if off in self.W1C:
            self.r[off] &= ~(value & self.W1C[off])
        elif off == self.P0XMT:
            self.tx.append(value & 0xFF)
            self.r[self.XSTAT] &= ~0x38                      # transmission pending
        elif off == self.RESET_CNTL:
            if value & 0x0A:                                  # flush_rcv / soft_reset
                self.rx.clear()
            if value & 0x0C:                                  # flush_xmt / soft_reset
                self.tx.clear()
            self.r[off] = value & ~0x0E
        else:
            old = self.r[off]
            self.r[off] = value
            if off == self.P0CNTL:
                self._port(old, value)
            if off == self.CNTL and (value >> 9) & 3 != (old >> 9) & 3:
                self.gp_start = self.m.insns if (value >> 9) & 3 else None
                self.gp_matches = 0
            if off == self.GPCNT:                             # next match of the running count
                n = self.gp_count() or 0
                self.gp_matches = max(0, int((n - (value & 0xFFFF)) // 0x10000) + 1) \
                    if n > (value & 0xFFFF) else 0
        self.update()

    # -- time -----------------------------------------------------------------------------
    def update(self):
        now = self.m.insns
        while self.out and self.out[0][0] <= now:            # card -> receive FIFO
            if self.r[self.ENABLE] & 1 and len(self.rx) >= 32:
                self.r[self.RSTAT] |= 0x01                    # overrun (oef)
                if self.r[self.CNTL] & 0x08:                  # onack: NACK it, the card resends
                    self._delay_out(now)
                    break
                self.out.popleft()                            # otherwise the byte is lost
                continue
            _, b = self.out.popleft()
            self.trace.append((now, 'rx', b))
            if self.r[self.ENABLE] & 1:
                if self.r[self.CNTL] & 0x02 and b in (0x3B, 0x3F):
                    self.r[self.CNTL] &= ~0x02                # valid TS ends initial char mode
                self.rx.append(b)
        if self.tx_busy_until is not None and now >= self.tx_busy_until:
            b, self.tx_byte, self.tx_busy_until = self.tx_byte, None, None
            self.trace.append((now, 'tx', b))
            if self.card and self.active_card():
                reply = self.card.feed(b)
                if reply:
                    self._send(reply, now + self.byte_insns())
            if not self.tx:
                self.r[self.XSTAT] |= 0x30                    # etc, tc
        if self.tx_busy_until is None and self.tx and self.r[self.ENABLE] & 2:
            self.tx_byte = self.tx.popleft()
            self.tx_busy_until = now + self.byte_insns()
            if len(self.tx) <= (self.r[self.XTH] & 0xF):
                self.r[self.XSTAT] |= 0x80                    # tdtf (latched, w1c)
            if not self.tx:
                self.r[self.XSTAT] |= 0x08                    # tfe
        n = self.gp_count()
        if n is not None and n >= self._gp_target():
            self.gp_matches = int((n - self.r[self.GPCNT]) // 0x10000) + 1
            self.r[self.XSTAT] |= 0x100
        self._irq()

    def _delay_out(self, now):
        """Retransmission after a NACK: the pending bytes move back by one character time
        (plus the error signal's ~2 etu, ignored)."""
        shift = now + self.byte_insns() - self.out[0][0]
        self.out = collections.deque((t + shift, b) for t, b in self.out)

    def _gp_target(self):
        """Count at which the 16-bit counter next equals GPCNT (it wraps and keeps going)."""
        return self.r[self.GPCNT] + 0x10000 * self.gp_matches

    def active_card(self):
        v = self.r[self.P0CNTL]
        return v & 0x02 and v & 0x10 and not v & 0x08

    def next_event(self):
        k = self.m.counter.k
        times = []
        if self.out:
            times.append(self.out[0][0])
        if self.tx_busy_until is not None:
            times.append(self.tx_busy_until)
        elif self.tx and self.r[self.ENABLE] & 2:
            times.append(self.m.insns + 1)
        n = self.gp_count()
        if n is not None:
            left = self._gp_target() - n
            sel = (self.r[self.CNTL] >> 9) & 3
            per_clock = self.ips / self.card_hz() * (1 if sel == 1 else
                                                     self.etu_clocks() / (16 if sel == 2 else 1))
            times.append(self.m.insns + max(1, int(left * per_clock) + 1))
        return min(times) * k if times else None
