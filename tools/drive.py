"""Run the phone headless through a scripted session.

    .venv/bin/python tools/drive.py [--state] [--log] STEP...

Steps, in order (a bare "--" starts a new scenario: the steps before the first "--" run once,
then each scenario runs in a forked copy of the phone from that point):
    12.5          run until 12.5 s of phone time
    +3            run 3 s more
    up / ok / 5   press and release a key (Keypad.CODES names, or 'red')
    shot:NAME     save the screen to out/NAME.png

--state starts from state/flash.bin like live mode (default: the firmware as shipped).
--log prints the firmware's own log lines. Stops with registers, backtrace and the last
MMIO accesses if the CPU faults.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from PIL import Image  # noqa: E402

from v600 import board, fwlog  # noqa: E402
from v600.board import CPU_IPS  # noqa: E402
from v600.machine import Machine  # noqa: E402


def boot(state=False, log=False, verbose=False):
    m = Machine(log=print if verbose else (lambda *a: None))
    m.verbose_mmio = False
    board.build(m)
    m.reset()
    if state and (ROOT / 'state/flash.bin').exists():
        m.flash.load(ROOT / 'state/flash.bin')
    fwlog.install(m, sink=print if log else (lambda s: None))
    m.log = print
    return m


def run_until(m, seconds):
    """Run to `seconds` of phone time; False if the CPU faulted."""
    while m.insns < seconds * CPU_IPS:
        n = min(2_000_000, int(seconds * CPU_IPS) - m.insns)
        if m.run(max_insns=max(n, 20_000), poll_slices=10**9, spin_slices=10**9) == 'error':
            return False
    return True


def crash_report(m):
    from armdis import dis
    print(m.dump_regs())
    print('backtrace:', ' '.join(f'{a:08x}' for a in m.backtrace()))
    dis(max(m.pc() - 16, 0), 12, m.thumb())
    for pc, kind, addr, size, val in list(m.mmio_trace)[-12:]:
        print(f'  pc={pc:08x} {kind}{size * 8:<2} {addr:08x} = {val:08x}')


def shot(m, name):
    path = ROOT / 'out' / f'{name}.png'
    img = m.ati.screen()
    img.resize((img.width * 2, img.height * 2), Image.NEAREST).save(path)
    print(f'{m.insns / CPU_IPS:7.2f}s  shot {path.name}')


def main(argv):
    flags = {a for a in argv if a.startswith('--') and a != '--'}
    groups = [[]]
    for a in argv:
        if a == '--':
            groups.append([])
        elif a not in flags:
            groups[-1].append(a)
    m = boot('--state' in flags, '--log' in flags, '--verbose' in flags)
    if not steps(m, groups[0]):
        return m
    for i, scenario in enumerate(groups[1:]):
        sys.stdout.flush()
        pid = os.fork()
        if pid == 0:
            print(f'== scenario {i + 1}: {" ".join(scenario)}')
            steps(m, scenario)
            sys.stdout.flush()
            os._exit(0)
        os.waitpid(pid, 0)
    return m


def steps(m, todo):
    for step in todo:
        now = m.insns / CPU_IPS
        if step.startswith('shot:'):
            shot(m, step[5:])
            continue
        try:
            target = now + float(step[1:]) if step.startswith('+') else float(step)
        except ValueError:
            board.press(m, step)
            ok = run_until(m, now + 0.2)
            board.press(m, step, False)
            ok = ok and run_until(m, now + 0.6)
        else:
            ok = run_until(m, target)
        if not ok:
            print(f'{m.insns / CPU_IPS:7.2f}s  CPU error after step {step!r}')
            crash_report(m)
            shot(m, 'crash')
            return False
    return True


if __name__ == '__main__':
    main(sys.argv[1:])
