"""The DSP's audio side, at the level of the audio manager's commands: what it is asked to
play, rendered as 8 kHz PCM, and the replies the audio manager waits for.

Commands are type 8 messages (dsp.py): word 0 opcode, word 1 = id << 8 | payload length.
Replies set bit 15 of the opcode and go on queue 0xB; the receiver 0x106939B0 dispatches
0x9980, 0x9A80, 0x9B80, 0x9C80/1, 0x9D80-3, 0x9E80-2, 0x9F80-4 and 0xA182.

0x1B80, tone sequence (built at 0x10694640 from a melody or a tone table such as
USA_DIAL_TONE 0x10A31A58):
  w2-w5   header; w5 >> 8 = number of tones
  flags   one byte per tone (padded to a word); 1 = starts after the previous tones end,
          0 = sounds together with the previous one (inferred: dial tones pair up)
  tones   (coefficient, duration, 0): coefficient = cos(2 pi f / 8000) in Q15, the
          recursive-oscillator form (USA_DIAL_TONE has 0x7B31 / 0x786E = 350 / 440 Hz);
          duration in 8 kHz samples, 0x7FFF = until stopped; 0x7FFF as coefficient = silence
When the sequence ends the DSP replies 0x9B80 [id << 8 | 2, w2, 0], the form the audio
manager fabricates itself at 0x10693980; until then it holds back further tones. w2 is the
tone's id (DL_AudPlayTone's argument, e.g. 0x12 for a key click).

0x1B83 comes just before: one gain per tone (0x0800 for key clicks and the power-up tune).
0x1B81 [w2 of the sequence, 1] stops it. Around each tone the audio manager also sends
0x1A93 and 0x1980 [0x0901, 0x00CC or 0xFFFF, 0] (path on / off), 0x1A8E and, once, 0x1A98
(mute); they need no reply.
"""
import math

import numpy as np

RATE = 8000
CONTINUOUS = 0x7FFF
UNITY = 0x2000                      # gain that plays at full scale (0x0800 -> 1/4)


class AudioDSP:
    def __init__(self, m, ips):
        self.m = m
        self.ips = ips
        self.voices = []                # [start, end or None, freq, level] in samples
        self.gains = []
        self.replies = []               # (due sample, words)
        self.pulled = None              # next sample index a listener will get
        self.commands = []              # (sample, words) for debugging

    def now(self):
        return self.m.insns * RATE // self.ips

    # -- commands -----------------------------------------------------------------------
    def command(self, w):
        self.commands.append((self.now(), w))
        if w[0] == 0x1B80:
            self._sequence(w)
        elif w[0] == 0x1B83:
            self.gains = w[2:]
        elif w[0] == 0x1B81:            # stop (sent by the tone code at 0x10694654)
            self._stop()

    def _sequence(self, w):
        count = w[5] >> 8
        nflags = len(w) - 6 - 3 * count
        flags = b''.join(x.to_bytes(2, 'big') for x in w[6:6 + nflags])
        tones = w[6 + nflags:]
        t = prev = self.now()
        end_all = t
        for i in range(count):
            coef, dur = tones[3 * i], tones[3 * i + 1]
            start = t if (i == 0 or flags[i]) else prev
            end = None if dur == CONTINUOUS else start + dur
            if coef != CONTINUOUS:
                c = (coef - 0x10000 if coef & 0x8000 else coef) / 32768
                f = math.acos(max(-1.0, min(1.0, c))) * RATE / (2 * math.pi)
                gain = self.gains[i] if i < len(self.gains) else UNITY // 4
                self.voices.append([start, end, f, min(1.0, gain / UNITY)])
            prev = start
            if end is None:
                end_all = None
            elif end_all is not None:
                end_all = max(end_all, end)
            t = max(t, end or t)
        if end_all is not None:
            self.replies.append((end_all, [0x9B80, (w[1] & 0xFF00) | 2, w[2], 0]))

    def _stop(self):
        now = self.now()
        for v in self.voices:
            if v[1] is None or v[1] > now:
                v[1] = now

    # -- time ---------------------------------------------------------------------------
    def update(self):
        now = self.now()
        due = [r for r in self.replies if r[0] <= now]
        if due:
            self.replies = [r for r in self.replies if r[0] > now]
            for _, words in due:
                self.m.dspmsg.post(0xB, words)
        listening = self.pulled is not None and self.pulled > now - RATE
        keep = self.pulled if listening else now - RATE
        self.voices = [v for v in self.voices if v[1] is None or v[1] > keep]

    def next_event(self):
        if not self.replies:
            return None
        due = min(r[0] for r in self.replies)
        return (due * self.ips // RATE + 1) * self.m.counter.k

    # -- output -------------------------------------------------------------------------
    def pull(self):
        """16-bit little-endian mono PCM at RATE from the last pull up to now. Safe to call
        from another thread (reads a snapshot of the voices)."""
        now = self.now()
        start = now if self.pulled is None else max(self.pulled, now - RATE)
        self.pulled = now
        n = now - start
        out = np.zeros(n)
        idx = np.arange(start, now)
        for v0, v1, f, level in list(self.voices):
            a, b = max(v0, start), now if v1 is None else min(v1, now)
            if a < b:
                t = idx[a - start:b - start] - v0
                out[a - start:b - start] += level * np.sin(2 * np.pi * f * t / RATE)
        return (np.clip(out, -1, 1) * 32767).astype('<i2').tobytes()
