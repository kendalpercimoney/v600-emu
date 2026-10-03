"""High-level stand-in for the DSP's side of the MCU<->DSP message interface.

The firmware talks to the DSP through boot-ROM routines (see bootrom.py):
  0xFFF4  receive(queue, buf) -> 1 if a message was copied into buf (16-bit words)
  0x101CC send(type, buf, nwords, flag) -> 1 on success

Boot handshake (0x1064E8BC): after the DSP code download the DSP posts 0xA080 ("alive") on
queue 0xD; the MCU sends type 0xC [0x2080, 0]; the DSP answers 0xA081 with its ROM version in
word 2. The MCU picks DSP patches by version from the table at 0x10040010 (only 0x62D5 there).
"""
import collections

DSP_ROM_VERSION = 0x62D5


class DSPMessages:
    def __init__(self, m):
        self.m = m
        self.queues = collections.defaultdict(collections.deque)
        self.queues[0xD].append([0xA080])          # alive, once the DSP code is running
        self.sent = 0

    def receive(self, m):
        q, buf = m.r(0), m.r(1)
        if not self.queues[q]:
            return 0
        msg = self.queues[q].popleft()
        m.uc.mem_write(buf, b''.join(w.to_bytes(2, 'big') for w in msg))
        m.log(f'  dsp -> mcu q{q:#x}: {" ".join(f"{w:04x}" for w in msg)}')
        return 1

    def send(self, m):
        typ, buf, n = m.r(0), m.r(1), m.r(2)
        words = [int.from_bytes(m.uc.mem_read(buf + 2 * i, 2), 'big') for i in range(min(n, 16))]
        self.sent += 1
        if self.sent <= 40:
            m.log(f'  mcu -> dsp type {typ:#x}: {" ".join(f"{w:04x}" for w in words)}')
        if typ == 0xC and words and words[0] == 0x2080:
            self.queues[0xD].append([0xA081, 0x0000, DSP_ROM_VERSION])
        return 1
