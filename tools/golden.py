"""Golden trace: digests of the whole machine every 0.25 s through a scripted session, to
check that a change (porting a chip model to C, PORTING.md phase 2) leaves the emulated
phone exactly as it was.

    .venv/bin/python tools/golden.py record     write tests/golden.jsonl (the reference)
    .venv/bin/python tools/golden.py check      run again and compare, stop at the first
                                                difference

The session: boot, menu, dial 555, a call answered and hung up, flip closed and opened.
Each line: phone time, then CRC-32s of the registers (every mode), RAM, internal RAM, the
graphics chip's memory, the outer display and the sound made since the last line.
The reference is the commit tagged `py-reference` (all models in Python, on Unicorn).
"""
import json
import sys
import zlib
from pathlib import Path

import numpy as np

from drive import ROOT, boot, run_until, steps
from lockstep import state
from v600.board import CPU_IPS

GOLDEN = ROOT / 'tests' / 'golden.jsonl'
EVERY = 0.25
SESSION = ['45.0', 'menu', '+2.0', 'red', '+2.0', '5', '5', '5', '+0.5', 'green', '+8.0',
           'red', '+3.0', 'flip:0', '+3.0', 'flip:1', '+2.0']
FIELDS = ['regs', 'ram', 'iram', 'ati', 'outer', 'sound']


def digest(m):
    regs, mem = state(m)
    crc = lambda b: zlib.crc32(b) & 0xFFFFFFFF
    return [round(m.insns / CPU_IPS, 3),
            crc(json.dumps(regs, sort_keys=True).encode()), crc(mem['ram']), crc(mem['iram']),
            crc(bytes(m.ati.mem)), crc(np.packbits(m.ati.outer.pixels).tobytes()),
            crc(m.audio.pull()[1])]


def session(m):
    """Yield a digest every EVERY seconds of phone time and after each scripted action."""
    for step in SESSION:
        now = m.insns / CPU_IPS
        if step[0] in '+0123456789' and '.' in step:
            end = now + float(step[1:]) if step[0] == '+' else float(step)
            t = now
            while t < end - 1e-9:
                t = min(end, (int(t / EVERY + 1e-6) + 1) * EVERY)
                if not run_until(m, t):
                    raise SystemExit(f'CPU error at {m.insns / CPU_IPS:.2f}s')
                yield digest(m)
        else:
            if not steps(m, [step]):
                raise SystemExit(f'CPU error after {step}')
            yield digest(m)


def main(argv):
    mode = argv[0] if argv else 'check'
    m = boot()
    m.log = lambda *a: None
    if mode == 'record':
        GOLDEN.parent.mkdir(exist_ok=True)
        with open(GOLDEN, 'w') as f:
            for d in session(m):
                f.write(json.dumps(d) + '\n')
        print(f'recorded {sum(1 for _ in open(GOLDEN))} digests to {GOLDEN.relative_to(ROOT)}')
        return 0
    ref = [json.loads(line) for line in open(GOLDEN)]
    for i, d in enumerate(session(m)):
        if d != ref[i]:
            bad = [name for name, a, b in zip(FIELDS, d[1:], ref[i][1:]) if a != b]
            print(f'DIFFERENT at {d[0]}s (digest {i}, reference time {ref[i][0]}s): {", ".join(bad)}')
            print(f'  last match at {ref[i - 1][0] if i else 0}s')
            return 1
        if i % 40 == 0:
            print(f'{d[0]:7.2f}s  same', flush=True)
    print(f'MATCH: {len(ref)} digests through {ref[-1][0]}s of phone time')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
