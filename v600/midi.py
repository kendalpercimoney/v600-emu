"""MIDI ringtones: a Standard MIDI File parser that accepts the file in pieces, and a small
General MIDI synthesiser. The phone's own synthesiser is DSP code in mask ROM; this one
only aims to be recognisable: additive voices per instrument family, simple drums.

The phone's MIDI filter streams ringtones as format 0 files whose track length is
0x7FFFFFFF (open-ended), so events are parsed as they arrive. Other files (format 1, real
track lengths) are parsed once all their tracks are in.
"""
import numpy as np

RATE = 16000
MASTER = 0.3

# (harmonic amplitudes, attack s, decay time constant s or None = sustained, release s)
FAMILIES = {
    0: ([1, .5, .25, .12], .004, .7, .08),            # piano
    1: ([1, 0, .3, 0, .1], .002, .5, .1),             # chromatic percussion
    2: ([1, .6, .4, .3, .2], .01, None, .05),         # organ
    3: ([1, .6, .35, .2, .1], .003, .45, .08),        # guitar
    4: ([1, .45, .15], .005, .8, .06),                # bass
    5: ([1, .5, .33, .25, .2, .16], .06, None, .15),  # strings
    6: ([1, .5, .33, .25, .2], .08, None, .2),        # ensemble
    7: ([1, .7, .5, .35, .25, .18, .12], .03, None, .08),  # brass
    8: ([1, 0, .5, 0, .3, 0, .2], .02, None, .06),    # reed
    9: ([1, .15, .05], .03, None, .06),               # pipe
    10: ([1, 0, .33, 0, .2, 0, .14, 0, .11], .005, None, .05),  # synth lead
    11: ([1, .5, .33, .25], .15, None, .3),           # synth pad
}
DEFAULT = ([1, .5, .33, .25], .01, .6, .1)


def voice(key, seconds, program, level, bend=0.0):
    """One note: `seconds` held, then its release."""
    harmonics, attack, decay, release = FAMILIES.get(program // 8, DEFAULT)
    f = 440.0 * 2 ** ((key - 69 + bend) / 12)
    n = int((seconds + release) * RATE)
    t = np.arange(n) / RATE
    wave = np.zeros(n)
    for k, a in enumerate(harmonics, 1):
        if a and k * f < RATE / 2:
            wave += a * np.sin(2 * np.pi * k * f * t)
    env = np.minimum(1.0, t / attack)
    if decay:
        env *= np.exp(-t / (decay * (440 / f) ** .3))
    off = t > seconds
    env[off] *= np.exp(-(t[off] - seconds) / (release / 3))
    return level * env * wave / sum(harmonics)


def drum(key, level, rng=np.random.default_rng(1)):
    """General MIDI percussion (channel 10), roughly."""
    def noise(sec, tau, bright=True):
        n = int(sec * RATE)
        x = rng.standard_normal(n)
        if bright:
            x = np.diff(x, prepend=0)
        return x * np.exp(-np.arange(n) / RATE / tau)

    def sweep(f0, f1, sec, tau):
        t = np.arange(int(sec * RATE)) / RATE
        f = f1 + (f0 - f1) * np.exp(-t / .03)
        return np.sin(2 * np.pi * np.cumsum(f) / RATE) * np.exp(-t / tau)

    if key in (35, 36):
        x = sweep(150, 50, .3, .12)
    elif key in (38, 40):
        x = .6 * noise(.25, .06) + .5 * sweep(300, 180, .25, .05)
    elif key in (42, 44):
        x = .5 * noise(.08, .02)
    elif key == 46:
        x = .5 * noise(.35, .12)
    elif key in (49, 52, 55, 57):
        x = .5 * noise(1.2, .4)
    elif key in (51, 53, 59):
        x = .4 * noise(.6, .25)
    elif 41 <= key <= 50:
        x = sweep(80 + (key - 41) * 25, 60 + (key - 41) * 18, .35, .14)
    else:
        x = .4 * noise(.15, .05, bright=False)
    return level * x


class MidiStream:
    """Feed bytes as they arrive; take finished notes as (start s, samples)."""

    OPEN = 0x7FFFFFFF

    def __init__(self):
        self.data = bytearray()
        self.header = None              # (format, tracks, division)
        self.pos = None                 # next event (open-ended single track)
        self.ticks_seconds = 0.0
        self.tempo = 500000
        self.running = 0
        self.ended = False
        self.end_seconds = 0.0          # when the last note finishes
        self.program = [0] * 16
        self.volume = [100] * 16
        self.expression = [127] * 16
        self.bend = [0.0] * 16
        self.held = {}                  # (channel, key) -> (start s, velocity)
        self.notes = []                 # (start s, samples), not yet taken

    def feed(self, chunk):
        self.data += chunk
        if self.header is None and len(self.data) >= 22:
            fmt, ntrk, div = (int.from_bytes(self.data[i:i + 2], 'big') for i in (8, 10, 12))
            self.header = (fmt, ntrk, div)
            if int.from_bytes(self.data[18:22], 'big') == self.OPEN:
                self.pos = 22
        if self.header is None or self.ended:
            return
        if self.pos is not None:
            self._run(self.data, self.pos, open_ended=True)
        else:
            self._complete_file()

    def take(self):
        notes, self.notes = self.notes, []
        return notes

    # -- parsing -------------------------------------------------------------------------
    @staticmethod
    def _vlq(d, p):
        v = 0
        while True:
            b = d[p]
            p += 1
            v = v << 7 | b & 0x7F
            if not b & 0x80:
                return v, p

    def _event(self, d, p):
        """(delta ticks, event bytes, next position); IndexError if incomplete."""
        dt, p = self._vlq(d, p)
        st = d[p]
        if st == 0xFF:
            n, q = self._vlq(d, p + 2)
            if q + n > len(d):
                raise IndexError
            return dt, bytes(d[p:q + n]), q + n
        if st in (0xF0, 0xF7):
            n, q = self._vlq(d, p + 1)
            if q + n > len(d):
                raise IndexError
            return dt, bytes(d[p:q + n]), q + n
        if st & 0x80:
            self.running = st
            p += 1
        n = 1 if self.running >> 4 in (0xC, 0xD) else 2
        if p + n > len(d):
            raise IndexError
        return dt, bytes([self.running]) + bytes(d[p:p + n]), p + n

    def _run(self, d, p, open_ended):
        div = self.header[2]
        while not self.ended:
            try:
                dt, ev, q = self._event(d, p)
            except IndexError:
                break
            p = q
            self.ticks_seconds += dt * self.tempo / 1e6 / div
            self._apply(ev)
        if open_ended:
            self.pos = p

    def _complete_file(self):
        """Format 1: merge the tracks once every one has arrived."""
        d, p, events = self.data, 14, []
        for track in range(self.header[1]):
            if d[p:p + 4] != b'MTrk' or p + 8 > len(d):
                return
            n = int.from_bytes(d[p + 4:p + 8], 'big')
            if p + 8 + n > len(d):
                return
            q, end, tick, self.running = p + 8, p + 8 + n, 0, 0
            try:
                while q < end:
                    dt, ev, q = self._event(d[:end], q)
                    tick += dt
                    events.append((tick, track, len(events), ev))
            except IndexError:                      # a damaged track: keep what parsed
                pass
            p = end
        last = 0
        for tick, _, _, ev in sorted(events):
            self.ticks_seconds += (tick - last) * self.tempo / 1e6 / self.header[2]
            last = tick
            self._apply(ev)
        self._apply(b'\xff\x2f\x00')

    def _apply(self, ev):
        now = self.ticks_seconds
        st = ev[0]
        if st == 0xFF:
            if ev[1] == 0x51:
                self.tempo = int.from_bytes(ev[-3:], 'big')
            elif ev[1] == 0x2F:
                for (ch, key) in list(self.held):
                    self._release(ch, key, now)
                self.ended = True
            return
        kind, ch = st >> 4, st & 0xF
        if kind == 0x9 and ev[2]:
            self._release(ch, ev[1], now)
            self.held[(ch, ev[1])] = (now, ev[2])
        elif kind in (0x8, 0x9):
            self._release(ch, ev[1], now)
        elif kind == 0xC:
            self.program[ch] = ev[1]
        elif kind == 0xB and ev[1] in (7, 11):
            (self.volume if ev[1] == 7 else self.expression)[ch] = ev[2]
        elif kind == 0xE:
            self.bend[ch] = ((ev[2] << 7 | ev[1]) - 8192) / 8192 * 2

    def _release(self, ch, key, now):
        if (ch, key) not in self.held:
            return
        start, vel = self.held.pop((ch, key))
        level = MASTER * vel / 127 * self.volume[ch] / 127 * self.expression[ch] / 127
        if ch == 9:
            samples = drum(key, level)
        else:
            held = min(max(now - start, .03), 8.0)    # damaged data can hold a note for hours
            samples = voice(key, held, self.program[ch], level, self.bend[ch])
        self.notes.append((start, samples))
        self.end_seconds = max(self.end_seconds, start + len(samples) / RATE)
