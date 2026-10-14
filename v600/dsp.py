"""The DSP as the ARM sees it: the MCU-DSP interface (MDI) registers, and a high-level
stand-in for the DSP's side of their message queues. There is no DSP core: its program is
in the Neptune's mask ROM, which we do not have.

Messages go through boot-ROM routines (see bootrom.py):
  0xFFF4  receive(queue, buf) -> 1 if a message was copied into buf (16-bit words)
  0x100C2 the same (used by the audio reply receiver 0x106939B0)
  0x101CC send(type, buf, nwords, flag) -> 1 on success

Boot handshake (0x1064E8BC): after the DSP code download the DSP posts 0xA080 ("alive") on
queue 0xD; the MCU sends type 0xC [0x2080, 0]; the DSP answers 0xA081 with its ROM version in
word 2. The MCU picks DSP patches by version from the table at 0x10040010 (only 0x62D5 there).
Type 8 messages are audio commands, handled by audio.py; their replies go on queue 0xB.

Replies on queue 0xB reach the audio manager through MDI general interrupt 0: its handler
(0x10AD3364, AITC source 17) clears MSR bit 11 and, if the queue's two indices in shared
memory (0x2485D934 / 0x2485D936) differ, calls the reply receiver 0x106939B0, which reads
queue 0xB until it is empty.
"""
import collections

DSP_ROM_VERSION = 0x62D5
AUDIO_QUEUE = 0xB
QUEUE_INDICES = 0x2485D934          # queue 0xB write / read index, then another queue's pair
GINT0_SOURCE = 17


class MDI:
    """Registers at 0x2485BFF2-0x2485BFFF (Neptune spec ch. 37; offsets from 0x2485BFF0):
    +2 MCVR, +4 MCR (bit 7: hold the DSP in reset during the download, 0x10AD5B90), +6 MSR,
    +8/+A MTR1/0 (to the DSP), +C/+E MRR1/0 (from the DSP).
    MSR reads with both receive registers full (bits 15/14) and MRR0 = 0x8001 ("block OK"):
    what the code download waits for. MSR bit 11 is general interrupt 0 (write 1 to clear)."""
    MSR, MRR0 = 0x6, 0xE
    MGIP0 = 1 << 11

    def __init__(self, m):
        self.m = m
        self.regs = bytearray(0x10)
        self.msr = 0xC000

    def read(self, off, size):
        if size == 4:
            return self.read(off, 2) << 16 | self.read(off + 2, 2)
        if off == self.MSR:
            return self.msr
        if off == self.MRR0:
            return 0x8001
        return int.from_bytes(self.regs[off:off + 2], 'big')

    def write(self, off, size, value):
        if size == 4:
            self.write(off, 2, value >> 16)
            self.write(off + 2, 2, value & 0xFFFF)
        elif off == self.MSR:
            self.msr &= ~(value & self.MGIP0)
            self._line()
        else:
            self.regs[off:off + size] = value.to_bytes(size, 'big')

    def general_interrupt(self):
        self.msr |= self.MGIP0
        self._line()

    def _line(self):
        self.m.aitc.set_line(GINT0_SOURCE, bool(self.msr & self.MGIP0))


class DSPMessages:
    def __init__(self, m):
        self.m = m
        self.queues = collections.defaultdict(collections.deque)
        self.queues[0xD].append([0xA080])          # alive, once the DSP code is running
        self.sent = 0

    def post(self, q, words):
        """A message from the DSP."""
        self.queues[q].append(list(words))
        if q == AUDIO_QUEUE:
            self._indices()
            self.m.mdi.general_interrupt()

    def _indices(self):
        pending = len(self.queues[AUDIO_QUEUE])
        self.m.uc.mem_write(QUEUE_INDICES, bytes([0, pending & 0xFF, 0, 0, 0, 0, 0, 0]))

    def receive(self, m):
        q, buf = m.r(0), m.r(1)
        if not self.queues[q]:
            return 0
        msg = self.queues[q].popleft()
        m.uc.mem_write(buf, b''.join(w.to_bytes(2, 'big') for w in msg))
        if q == AUDIO_QUEUE:
            self._indices()
        else:
            m.log(f'  dsp -> mcu q{q:#x}: {" ".join(f"{w:04x}" for w in msg)}')
        return 1

    def send(self, m):
        typ, buf, n = m.r(0), m.r(1), m.r(2)
        words = [int.from_bytes(m.uc.mem_read(buf + 2 * i, 2), 'big') for i in range(n)]
        self.sent += 1
        if typ == 8:
            m.audio.command(words)
            return 1
        if self.sent <= 40:
            m.log(f'  mcu -> dsp type {typ:#x}: {" ".join(f"{w:04x}" for w in words[:16])}')
        if typ == 0xC and words and words[0] == 0x2080:
            self.queues[0xD].append([0xA081, 0x0000, DSP_ROM_VERSION])
        return 1
