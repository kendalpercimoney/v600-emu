"""Input events at fixed points of virtual time (seconds since reset), e.g. key presses.

    s = Script(m); s.at(3.0, lambda m: m.keypad.press('1')); m.timers.append(s)

It takes part in idle fast-forward, so the clock stops at each event."""
from .board import CPU_IPS


class Script:
    def __init__(self, m):
        self.m = m
        self.events = []                       # (insns, fn), sorted

    def at(self, seconds, fn):
        self.events.append((int(seconds * CPU_IPS), fn))
        self.events.sort(key=lambda e: e[0])
        return self

    def key(self, seconds, name, hold=0.15):
        self.at(seconds, lambda m: m.keypad.press(name))
        return self.at(seconds + hold, lambda m: m.keypad.press(name, False))

    def update(self):
        while self.events and self.m.insns >= self.events[0][0]:
            _, fn = self.events.pop(0)
            fn(self.m)

    def next_event(self):
        if not self.events:
            return None
        return self.events[0][0] * self.m.counter.k
