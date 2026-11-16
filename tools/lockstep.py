"""Check the C core against Unicorn: boot the phone on both and compare their state.

    .venv/bin/python tools/lockstep.py [SECONDS] [STEP]      (default 20.0 s, every 0.25 s)
    .venv/bin/python tools/lockstep.py --from=T SECONDS       single-step from T to find
                                                              the first different instruction

Both machines run the same slices (the virtual clock counts instructions, so the runs are
deterministic); at every STEP of phone time all registers of every mode, internal RAM and
RAM must match. At the first difference it prints where and which registers / addresses
differ. See PORTING.md, phase 1.
"""
import os
import sys
import zlib

import capstone

from drive import boot, run_until
from v600.board import CPU_IPS
from v600.machine import IRAM, RAM, REGS

from unicorn.arm_const import UC_ARM_REG_CPSR, UC_ARM_REG_SPSR, UC_ARM_REG_SP, UC_ARM_REG_LR

MODES = (0x10, 0x11, 0x12, 0x13, 0x17, 0x1B)


def state(m):
    """Registers (each mode's banked ones too) and the RAM contents."""
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


def make(cpu):
    os.environ['V600_CPU'] = cpu
    m = boot()
    m.log = lambda *a: None
    return m


def disasm(m, pc, thumb):
    md = capstone.Cs(capstone.CS_ARCH_ARM, (capstone.CS_MODE_THUMB if thumb else capstone.CS_MODE_ARM)
                     | capstone.CS_MODE_BIG_ENDIAN)
    code = bytes(m.uc.mem_read(pc, 4))
    ins = next(md.disasm(code, pc), None)
    return f'{pc:08x}: {ins.mnemonic} {ins.op_str}' if ins else f'{pc:08x}: {code.hex()} ?'


def regs(m):
    return [m.uc.reg_read(r) for r in REGS] + [m.uc.reg_read(UC_ARM_REG_CPSR)]


def step(start, end):
    """Run both to `start`, then one instruction at a time (servicing interrupts every
    slice, as Machine.run does) until their registers differ."""
    ref, core = make('unicorn'), make('c')
    run_until(ref, start)
    run_until(core, start)
    for m in (ref, core):
        m.uc.reg_write(REGS[15], m.start)        # the next instruction (PC alone may be stale)
    if regs(ref) != regs(core):
        print(f'already different at {start}s: pick an earlier --from')
        return 1
    names = [f'r{i}' for i in range(16)] + ['cpsr']
    history = []
    for n in range(int((end - start) * CPU_IPS)):
        if n and n % 20_000 == 0:
            for m in (ref, core):
                m.insns += 20_000
                m._service_interrupts()
        pc, thumb = ref.pc(), ref.thumb()
        history.append(disasm(ref, pc, thumb))
        for m in (ref, core):
            m.uc.emu_start(m.pc() | (1 if m.thumb() else 0), 0xFFFFFFFF, count=1)
            m.start = m.pc() | (1 if m.thumb() else 0)
        a, b = regs(ref), regs(core)
        if a != b:
            print(f'first difference after {n + 1} instructions from {start}s:')
            for line in history[-8:-1]:
                print('       ', line)
            print('  -->  ', history[-1])
            for k, x, y in zip(names, a, b):
                if x != y:
                    print(f'  {k}: unicorn {x:08x}  c {y:08x}')
            return 1
        if len(history) > 64:
            del history[:32]
    print(f'no difference in {end - start}s of single steps')
    return 0


def main(argv):
    frm = next((float(a[7:]) for a in argv if a.startswith('--from=')), None)
    argv = [a for a in argv if not a.startswith('--')]
    if frm is not None:
        return step(frm, frm + float(argv[0]) if argv else frm + 0.05)
    end = float(argv[0]) if argv else 20.0
    every = float(argv[1]) if len(argv) > 1 else 0.25
    ref, core = make('unicorn'), make('c')
    t = 0.0
    while t < end:
        t = min(t + every, end)
        ok = run_until(ref, t), run_until(core, t)
        (ra, ma), (rb, mb) = state(ref), state(core)
        bad = [k for k in ra if ra[k] != rb[k]]
        for name in ma:
            if ma[name] != mb[name]:
                base = IRAM[0] if name == 'iram' else RAM[0]
                diff = [i for i in range(0, len(ma[name]), 4) if ma[name][i:i + 4] != mb[name][i:i + 4]]
                bad.append(f'{name}: {len(diff)} words, first at {base + diff[0]:08x}')
        if bad or ok[0] != ok[1]:
            print(f'DIFFERENT at {t:.2f}s (both ran the same instructions up to {t - every:.2f}s)')
            for k in bad[:20]:
                print(f'  {k}: unicorn {ra[k]:08x}  c {rb[k]:08x}' if k in ra else f'  {k}')
            print(f'  cpu ok: unicorn {ok[0]}, c {ok[1]}')
            return 1
        print(f'{t:6.2f}s  same  (ram crc {zlib.crc32(ma["ram"]):08x})', flush=True)
    print(f'MATCH: identical state every {every}s for {end}s of phone time '
          f'({int(end * CPU_IPS):,} instructions)')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
