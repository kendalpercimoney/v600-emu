"""Golden traces: digests of the whole machine every 0.25 s through scripted sessions, to
check that a change (porting a chip model to C, PORTING.md phase 2) leaves the emulated
phone exactly as it was.

    .venv/bin/python tools/golden.py check [SESSION...]       compare with tests/golden/
    .venv/bin/python tools/golden.py reference [SESSION...]   record from the reference
    .venv/bin/python tools/golden.py record [SESSION...]      record from this tree

The reference is the commit tagged `py-reference` (every model in Python, on Unicorn):
`reference` checks it out in build/reference (a git worktree) and records there.
Sessions (SESSIONS below): `call` (boot, menu, dial 555, a call, the flip) and
`ringtones` (the ring volume slider and the ringtone list: tones, MP3 and MIDI).
Each line: phone time, then CRC-32s of the registers (every mode), RAM, internal RAM,
graphics memory and outer display, the sound made since the last line as [samples, RMS],
and a CRC-32 of the media clips (MP3 bytes included). All must match exactly except the
RMS (within SOUND_TOLERANCE): the synthesis uses numpy's sin / exp and random numbers in
the reference, C's in the port, so samples may differ in the last bits; everything the
CPU can observe (replies, their timing) is exact.

Deliberate changes since the reference (2026-11-17):
- The power IC's headset-detect line (PSTAT bit 12) reads "no headset"; before, the phone
  thought a headset was in (it answered incoming calls by itself). Fixed in the reference
  too (build/reference/v600/periph.py, PSTAT_IDLE), and the traces recorded from it.
- The SIM has a text message centre (EF_SMSP), as in sim.c; same change in the reference.
- call.jsonl digests 200-202 (dialling 555) carry sound, the keypad's DTMF tones, silent in
  the reference; every other field there is the reference's.
"""
import json
import os
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np

from drive import ROOT, boot, run_until, steps
from v600.board import CPU_IPS

GOLDEN = ROOT / 'tests' / 'golden'
EVERY = 0.25
TO_SETTINGS = ['45.0', 'soft_right', '+2.0', 'soft_right', '+2.0', 'red', '+2.0', 'menu', '+2.5',
               'down', 'down', 'right', 'right', '+1.0', 'ok', '+2.0']
SESSIONS = {
    'call': ['45.0', 'menu', '+2.0', 'red', '+2.0', '5', '5', '5', '+0.5', 'green', '+8.0',
             'red', '+3.0', 'flip:0', '+3.0', 'flip:1', '+2.0'],
    # Settings > Ring Styles > detail: the volume slider beeps, then the Line 1 list previews
    # each ringtone as it is highlighted (tones, an MP3, then MIDI tunes)
    'ringtones': TO_SETTINGS + ['down', '+1.0', 'ok', '+2.0',          # Ring Styles
                                'down', '+1.0', 'ok', '+2.0',          # its detail list
                                'ok', '+1.0', 'left', '+1.0', 'right', '+1.0',   # volume
                                'soft_left', '+1.0',
                                'down', '+1.0', 'ok', '+2.0']          # Line 1: the list
                 + ['down', '+1.5'] * 22,
}
FIELDS = ['regs', 'ram', 'iram', 'ati', 'outer', 'sound', 'media']
SOUND_TOLERANCE = 0.15                  # relative RMS difference (and 64 absolute)
MODES = (0x10, 0x11, 0x12, 0x13, 0x17, 0x1B)


def state(m):
    """Registers (each mode's banked ones too) and the RAM contents."""
    from unicorn.arm_const import UC_ARM_REG_CPSR, UC_ARM_REG_SPSR, UC_ARM_REG_SP, UC_ARM_REG_LR
    from v600.machine import IRAM, RAM, REGS
    uc = m.uc
    cpsr = uc.reg_read(UC_ARM_REG_CPSR)
    regs = {f'r{i}': uc.reg_read(r) for i, r in enumerate(REGS)}
    regs['cpsr'] = cpsr
    for mode in MODES:
        uc.reg_write(UC_ARM_REG_CPSR, (cpsr & ~0x1F) | mode)
        regs[f'sp_{mode:x}'] = uc.reg_read(UC_ARM_REG_SP)
        regs[f'lr_{mode:x}'] = uc.reg_read(UC_ARM_REG_LR)
        if mode != 0x10:
            regs[f'spsr_{mode:x}'] = uc.reg_read(UC_ARM_REG_SPSR)
    uc.reg_write(UC_ARM_REG_CPSR, cpsr)
    mem = {name: bytes(uc.mem_read(*region)) for name, region in (('iram', IRAM), ('ram', RAM))}
    return regs, mem


def digest(m):
    regs, mem = state(m)
    crc = lambda b: zlib.crc32(b) & 0xFFFFFFFF
    media = [[e[0], e[1], e[2]] + [crc(x) if isinstance(x, (bytes, bytearray)) else x for x in e[3:]]
             for e in m.audio.media_events()]
    return [round(m.insns / CPU_IPS, 3),
            crc(json.dumps(regs, sort_keys=True).encode()), crc(mem['ram']), crc(mem['iram']),
            crc(bytes(m.ati.mem)), crc(np.packbits(m.ati.outer.pixels != 0).tobytes()),
            loudness(m.audio.pull()[1]), crc(json.dumps(media).encode())]


def loudness(pcm):
    x = np.frombuffer(pcm, '<i2').astype(float)
    return [len(x), int(np.sqrt(np.mean(x * x))) if len(x) else 0]


def same(a, b):
    """Digests a (this run) and b (reference) agree; the fields that differ otherwise."""
    bad = []
    for f, x, y in zip(FIELDS, a[1:], b[1:]):
        if f == 'sound':
            if x[0] != y[0] or abs(x[1] - y[1]) > max(64, SOUND_TOLERANCE * y[1]):
                bad.append(f'sound {x} vs {y}')
        elif x != y:
            bad.append(f)
    if a[0] != b[0]:
        bad.append('time')
    return bad


def run(script):
    """Yield a digest every EVERY seconds of phone time and after each scripted action."""
    m = boot()
    m.log = lambda *a: None
    for step in script:
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


def check(name):
    ref = [json.loads(line) for line in open(GOLDEN / f'{name}.jsonl')]
    for i, d in enumerate(run(SESSIONS[name])):
        bad = same(d, ref[i])
        if bad:
            print(f'{name}: DIFFERENT at {d[0]}s (digest {i}): {", ".join(bad)}; '
                  f'last match at {ref[i - 1][0] if i else 0}s')
            return False
    print(f'{name}: MATCH, {len(ref)} digests through {ref[-1][0]}s of phone time', flush=True)
    return True


def record(name, out):
    with open(out, 'w') as f:
        for d in run(SESSIONS[name]):
            f.write(json.dumps(d) + '\n')
    print(f'{name}: recorded {sum(1 for _ in open(out))} digests to {out}', flush=True)


def reference(names):
    """Record from the py-reference tag, checked out in build/reference."""
    tree = ROOT / 'build' / 'reference'
    if not tree.exists():
        subprocess.run(['git', 'worktree', 'add', '--detach', str(tree), 'py-reference'],
                       cwd=ROOT, check=True)
    for tool in ('golden.py', 'drive.py'):           # this version of the tools
        (tree / 'tools' / tool).write_text((ROOT / 'tools' / tool).read_text())
    if not (tree / 'firmware').exists():
        os.symlink(ROOT / 'firmware', tree / 'firmware')
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name in names:
        subprocess.run([sys.executable, 'tools/golden.py', 'record', f'--out={GOLDEN / name}.jsonl',
                        name], cwd=tree, check=True)


def main(argv):
    mode = argv[0] if argv else 'check'
    out = next((a[6:] for a in argv if a.startswith('--out=')), None)
    names = [a for a in argv[1:] if not a.startswith('--')] or list(SESSIONS)
    if mode == 'reference':
        reference(names)
        return 0
    if mode == 'record':
        GOLDEN.mkdir(parents=True, exist_ok=True)
        for name in names:
            record(name, out or GOLDEN / f'{name}.jsonl')
        return 0
    return 0 if all([check(name) for name in names]) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
