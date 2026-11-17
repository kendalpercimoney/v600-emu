"""Boot the phone headless and check the main features work; PASS / FAIL for each.

    .venv/bin/python tools/selftest.py        (or ./v600 test)

Starts from the firmware as shipped (never touches state/flash.bin) and saves screenshots
to out/selftest/. Takes about a minute.
"""
import sys
import time

import numpy as np

from drive import ROOT, boot, run_until, steps
from v600 import board
from v600.board import CPU_IPS
from v600.network import ALERTING, CONNECT, CONNECT_ACK, DISCONNECT, RELEASE, SETUP

OUT = ROOT / 'out' / 'selftest'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    m = boot()
    m.log = lambda *a: None
    results = []

    def check(name, ok, detail=''):
        results.append(ok)
        print(f'{"PASS" if ok else "FAIL"}  {name}' + (f'  ({detail})' if detail else ''), flush=True)

    def shot(name):
        img = m.ati.screen()
        img.resize((img.width * 2, img.height * 2)).save(OUT / f'{name}.png')

    def run(*todo):
        if not steps(m, list(todo)):
            check('CPU keeps running', False, f'faulted at {m.insns / CPU_IPS:.1f}s')
            summary(results)

    t0 = time.monotonic()
    run('45.0')
    colours = len(np.unique(np.asarray(m.ati.screen()).reshape(-1, 3), axis=0))
    shot('1-idle')
    check('boots to the idle screen', colours > 8, f'{colours} colours on screen')
    check('registers on the pretend network', m.network.registered)

    run('menu', '+2.0')
    shot('2-menu')
    check('menu opens', m.ati.draws > 0)

    run('red', '+2.0', '5', '5', '5', '+0.5')
    shot('3-dialling')
    m.audio.pull()                          # drop what came before (it keeps the last 1 s)
    run('green', '+2.0')
    peak = np.abs(np.frombuffer(m.audio.pull()[1], '<i2')).max(initial=0)
    check('ringing tone plays while the call is alerting', peak > 1000, f'peak {peak}')

    run('+6.0')
    shot('4-in-call')
    seen = m.network.from_phone
    check('call is set up and answered', SETUP in seen and CONNECT_ACK in seen, f'messages {seen}')
    run('red', '+3.0')
    check('call hangs up', DISCONNECT in m.network.from_phone)
    shot('5-call-ended')

    before = len(m.network.from_phone)
    check('someone calls the phone', m.network.incoming_call('07700900123'))
    run('+3.0')
    shot('6-incoming-call')
    seen = m.network.from_phone[before:]
    check('it rings (alerting, not answered by itself)', ALERTING in seen and CONNECT not in seen,
          f'messages {seen}')
    run('green', '+2.0')
    check('green answers it', CONNECT in m.network.from_phone[before:])
    check('they hang up', m.network.hang_up())
    run('+3.0')
    check('the phone releases the call', RELEASE in m.network.from_phone[before:])
    shot('7-they-hung-up')

    updates = m.ati.outer.updates
    board.set_flip(m, False)
    run('+3.0')
    m.ati.outer.image().resize((288, 96)).save(OUT / '8-outer.png')
    check('outer display shows the clock when closed',
          m.ati.outer.updates > updates and m.ati.outer.pixels.sum() > 50)
    board.set_flip(m, True)
    run('+2.0')

    print(f'\nphone time {m.insns / CPU_IPS:.0f}s in {time.monotonic() - t0:.0f}s; '
          f'screenshots in {OUT.relative_to(ROOT)}/')
    summary(results)


def summary(results):
    failed = results.count(False)
    print('ALL PASSED' if not failed else f'{failed} FAILED')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
