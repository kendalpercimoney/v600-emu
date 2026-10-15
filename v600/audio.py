"""The DSP's audio side, at the level of the commands the ARM sends it: what it is asked to
play, rendered as PCM, and the replies the firmware waits for.

Commands are type 8 messages (dsp.py): word 0 opcode, word 1 = id << 8 | payload length.
Media data comes as type 0 messages. Replies set bit 15 of the opcode and go on queue 0xB;
the receiver 0x106939B0 takes 0x9980, 0x9A80, 0x9B80, 0x9C80/1, 0x9D80-3, 0x9E80-2,
0x9F80-4 and 0xA182.

Tones (the audio manager)
  0x1B80, tone sequence (built at 0x10694640 from a melody or a table such as
  USA_DIAL_TONE 0x10A31A58):
    w2      the tone's id (DL_AudPlayTone's argument, e.g. 0x12 for a key click)
    w5      number of tones << 8 | number of cadence pairs
    flags   one byte per tone, padded to a word; 1 = starts after the previous tones end,
            0 = sounds together with the previous one (inferred: dial tones pair up)
    tones   (coefficient, duration, 0): coefficient = cos(2 pi f / 8000) in Q15, the
            recursive-oscillator form (USA_DIAL_TONE has 0x7B31 / 0x786E = 350 / 440 Hz);
            duration in 8 kHz samples, 0x7FFF = until stopped; 0x7FFF as coefficient = rest
    cadence (on, off) pairs, 8 kHz samples: the tones loop during "on", silence during
            "off", and the cadence repeats until stopped. Ring tones use it, e.g. Continental:
            a 10/20 ms warble, 0.4 s on, 0.2 s off, 0.4 s on, then off.
  Without a cadence the sequence plays once and the DSP replies 0x9B80 [id << 8 | 2, w2, 0]
  when it ends, the form the audio manager fabricates itself at 0x10693980; until then it
  holds back further tones. 0x1B83, just before, gives one gain per tone (0x0800 for key
  clicks); 0x1B81 [w2, 1] stops. Around each tone the audio manager also sends 0x1A93,
  0x1980 [0x0901, 0x00CC or 0xFFFF, 0] (path on / off), 0x1A8E and once 0x1A98 (mute).

MIDI ringtones (the MIDI output filter, MME_MOF_*)
  The filter fakes its own session-start replies (0x9F80, 0x9E7F, via
  MME_MOF_ONLY_fake_ss_se_msg), streams the file as type 0 packets [0x5E80, words, bytes]
  and sends 0x1E80 [0x0401, 0x0018, 0] to play (then [0x0405, ...] while playing; ext
  control, MME_MOF_MDI_midi_audio_ext_ctrl). The DSP asks for more by putting 0x8002 in MRR0 (dsp.MDI); the
  filter sends more only while ROM 0xFFD0 says fewer than 0x82 words are queued. When the
  tune ends the DSP sends 0x9E80 [1]: the completion handler (0x10636B6C) reports the end
  to the application for status 1 or 2 (0 or 3 make the filter send 0x1E81 and wait).
  0x1E81 [reason] stops it early; the DSP answers with the same 0x9E80 [1], without which
  the filter never starts another tune.

Sampled sounds (the audio/video output filter, MME_AVOF_*): 0x1F82 [handle, codec,
  0, sample rate] opens a session, answered by 0x9F80 [handle] (MME_FW_MDI_avio_callback
  finds the filter by handle); frames follow as [0x5F80, words, handle, 0x0080, flags,
  time ms, frame] (MP3: one 120-byte MPEG-2 layer III frame each), 0x1F84 [handle, 1] plays,
  0x1F83 [handle, 1] closes, answered by 0x9F81 [handle]. The frames are passed on as a
  whole file (media_events) for the listener to decode: browsers decode MP3, Python can't
  without extra packages.
"""
import numpy as np

from .midi import MidiStream, RATE

TONE_RATE = 8000                    # the DSP's tone generator: coefficients and durations
SCALE = RATE // TONE_RATE
CONTINUOUS = 0x7FFF
UNITY = 0x2000                      # tone gain that plays at full scale (0x0800 -> 1/4)
MIDI_MORE = 0x8002                  # MRR0 word: the MIDI filter's data callback
MIDI_AHEAD = 2.0                    # seconds of MIDI to have parsed ahead of playback
MIDI_ENDED = [0x9E80, 0x0001, 0x0001]


class Tones:
    """One 0x1B80 sequence from `start` (in samples) until `end` (None: until stopped)."""

    def __init__(self, w, gains, start):
        count, npairs = w[5] >> 8, w[5] & 0xFF
        nflags = (count + 1) // 2
        flags = b''.join(x.to_bytes(2, 'big') for x in w[6:6 + nflags])
        tones = w[6 + nflags:6 + nflags + 3 * count]
        cad = [x * SCALE for x in w[6 + nflags + 3 * count:][:2 * npairs]]
        self.notes = []                 # (offset, duration, freq, level)
        t = prev = 0
        for i in range(count):
            coef, dur = tones[3 * i], tones[3 * i + 1]
            at = t if (i == 0 or flags[i]) else prev
            dur = np.inf if dur == CONTINUOUS else dur * SCALE
            if coef != CONTINUOUS:
                c = (coef - 0x10000 if coef & 0x8000 else coef) / 32768
                level = min(1.0, (gains[i] if i < len(gains) else UNITY // 4) / UNITY)
                f = np.arccos(np.clip(c, -1, 1)) * TONE_RATE / (2 * np.pi)
                self.notes.append((at, dur, f, level))
            prev, t = at, max(t, at + dur)
        self.length = max(t, 1)
        self.cadence = list(zip(cad[0::2], cad[1::2]))
        self.start = start
        self.end = start + self.length if not self.cadence and self.length < np.inf else None

    def render(self, idx):
        rel = idx - self.start
        if self.cadence:
            period = sum(on + off for on, off in self.cadence)
            pos = rel % period
            ppos = np.full(len(idx), -1.0)
            at = 0
            for on, off in self.cadence:
                inside = (pos >= at) & (pos < at + on)
                ppos[inside] = (pos[inside] - at) % self.length
                at += on + off
        else:
            ppos = rel.astype(float)
        out = np.zeros(len(idx))
        for off, dur, f, level in self.notes:
            on = (ppos >= off) & (ppos < off + dur)
            out[on] += level * np.sin(2 * np.pi * f * (ppos[on] - off) / RATE)
        return out


class Clip:
    """Ready-made samples from `start`; `end` can be brought forward to cut it short."""

    def __init__(self, start, samples):
        self.start, self.samples = start, samples
        self.end = start + len(samples)

    def render(self, idx):
        return self.samples[idx - self.start]


class AudioDSP:
    def __init__(self, m, ips):
        self.m = m
        self.ips = ips
        self.playing = []               # Tones and Clips
        self.gains = []
        self.replies = []               # (due sample, words)
        self.pulled = None              # next sample index a listener will get
        self.commands = []              # (sample, words) for debugging
        self.midi = None                # MidiStream of the current MIDI session
        self.midi_start = None          # sample where it started playing
        self.midi_clips = []
        self.midi_asked = 0             # sample of the last data request
        self.sessions = {}              # sampled-sound handle -> bytearray of frames
        self.media = {}                 # handle -> id of the clip playing
        self.events = []                # ('start', id, sample, mime, bytes) / ('stop', id, sample)
        self.next_id = 1

    def now(self):
        return self.m.insns * RATE // self.ips

    # -- commands -----------------------------------------------------------------------
    def command(self, w):
        now = self.now()
        self.commands.append((now, w))
        op = w[0]
        if op == 0x1B80:
            tones = Tones(w, self.gains, now)
            self.playing.append(tones)
            if tones.end is not None:
                self.replies.append((tones.end, [0x9B80, (w[1] & 0xFF00) | 2, w[2], 0]))
        elif op == 0x1B83:
            self.gains = w[2:]
        elif op == 0x1B81:
            self._cut([t for t in self.playing if isinstance(t, Tones)], now)
        elif op == 0x1E80 and self.midi_start is None:    # later ones: [0x0405, ...] updates
            self.midi_start = now
            self.midi = self.midi or MidiStream()
        elif op == 0x1E81:                                  # [reason]: 1 done, 5 deleted
            self._cut(self.midi_clips, now)
            if self.midi_start is not None:
                self.replies.append((now + 16, MIDI_ENDED))
            self.midi = self.midi_start = None
        elif op == 0x1F82:
            self.sessions[w[2] << 16 | w[3]] = bytearray()
            self.replies.append((now + 16, [0x9F80, 0x0002, w[2], w[3]]))
        elif op == 0x1F84 and w[4] == 1:
            handle = w[2] << 16 | w[3]
            frames = self.sessions.get(handle)
            if frames and handle not in self.media:
                self.media[handle] = self.next_id
                self._event('start', self.next_id, now, 'audio/mpeg', bytes(frames))
                self.next_id += 1
        elif op == 0x1F83:
            handle = w[2] << 16 | w[3]
            if handle in self.media:
                self._event('stop', self.media.pop(handle), now)
            self.sessions.pop(handle, None)
            self.replies.append((now + 16, [0x9F81, 0x0002, w[2], w[3]]))

    def data(self, w):
        """Type 0 messages: packets of [opcode, words, payload]."""
        raw = b''.join(x.to_bytes(2, 'big') for x in w)
        i = 0
        while i + 4 <= len(raw):
            op, n = int.from_bytes(raw[i:i + 2], 'big'), int.from_bytes(raw[i + 2:i + 4], 'big')
            body = raw[i + 4:i + 4 + 2 * n]
            if op == 0x5E80:
                self.midi = self.midi or MidiStream()
                self.midi.feed(body)
            elif op == 0x5F80:
                frames = self.sessions.get(int.from_bytes(body[:4], 'big'))
                if frames is not None:
                    frames += body[12:]
            i += 4 + 2 * n

    def space(self):
        """Words waiting in the DSP's data queue (ROM 0xFFD0; capacity 0x400): MP3 frames go
        while there is room (0x10AECBF0), MIDI data only below 0x82 words. Data is taken
        as it arrives, so the queue always reads empty."""
        return 0

    def _event(self, *e):
        self.events = self.events[-20:] + [e]

    def media_events(self):
        """Clips started or stopped since the last call (for a listener: live mode)."""
        events, self.events = self.events, []
        return events

    def _cut(self, items, at):
        for t in items:
            if t.end is None or t.end > at:
                t.end = at

    # -- time ---------------------------------------------------------------------------
    def update(self):
        now = self.now()
        due = [r for r in self.replies if r[0] <= now]
        if due:
            self.replies = [r for r in self.replies if r[0] > now]
            for _, words in due:
                self.m.dspmsg.post(0xB, words)
        if self.midi_start is not None:
            self._midi(now)
        listening = self.pulled is not None and self.pulled > now - RATE
        keep = self.pulled if listening else now - RATE
        self.playing = [t for t in self.playing if t.end is None or t.end > keep]
        self.midi_clips = [c for c in self.midi_clips if c.end > keep]

    def _midi(self, now):
        s = self.midi
        for at, samples in s.take():
            clip = Clip(self.midi_start + int(at * RATE), samples)
            self.playing.append(clip)
            self.midi_clips.append(clip)
        played = (now - self.midi_start) / RATE
        if not s.ended and s.ticks_seconds < played + MIDI_AHEAD and now - self.midi_asked > RATE // 10:
            self.midi_asked = now
            self.m.mdi.receive(MIDI_MORE)
        if s.ended and played >= s.end_seconds:
            self.m.dspmsg.post(0xB, MIDI_ENDED)
            self.midi = self.midi_start = None

    def next_event(self):
        times = [r[0] for r in self.replies]
        if self.midi_start is not None:
            times.append(self.now() + RATE // 10)
        if not times:
            return None
        return (min(times) * self.ips // RATE + 1) * self.m.counter.k

    # -- output -------------------------------------------------------------------------
    def pull(self):
        """(first sample index, 16-bit little-endian mono PCM at RATE) from the last pull up
        to now. Safe to call from another thread (reads a snapshot of what is playing)."""
        now = self.now()
        start = now if self.pulled is None else max(self.pulled, now - RATE)
        self.pulled = now
        out = np.zeros(now - start)
        for t in list(self.playing):
            a, b = max(t.start, start), now if t.end is None else min(t.end, now)
            if a < b:
                out[a - start:b - start] += t.render(np.arange(a, b))
        return start, (np.clip(out, -1, 1) * 32767).astype('<i2').tobytes()
