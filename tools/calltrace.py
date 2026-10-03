"""Boot with every known symbol hooked; print call counts and the first calls of each.

    .venv/bin/python tools/calltrace.py [insns] [name-prefix ...]
"""
import json
import sys
sys.path.insert(0, 'tools'); sys.path.insert(0, '.')
from v600.machine import Machine
from v600 import board, fwlog

n = int(sys.argv[1]) if len(sys.argv) > 1 else 600_000_000
prefixes = tuple(sys.argv[2:]) or ('',)
syms = {}
for f in ('out/symbols_from_pts.json', 'out/symbols_ported.json', 'out/symbols_e1.json'):
    try:
        syms.update(json.load(open(f)))
    except FileNotFoundError:
        pass
m = Machine(log=lambda *a: None); m.verbose_mmio = False
board.build(m); m.reset(); fwlog.install(m, sink=lambda s: None)
import os
START = int(os.environ.get('START', 0))       # only record calls after this many insns
calls = {}
events = []

def tracer(name):
    def f(m):
        if m.insns < START:
            return
        c = calls[name] = calls.get(name, 0) + 1
        if c <= 3 and len(events) < 5000:
            events.append(f'{m.insns:>14,}  {name}({m.r(0):#x}, {m.r(1):#x}, {m.r(2):#x}, {m.r(3):#x})'
                          f'  lr={m.r(14):08x}')
    return f

for name, addr in syms.items():
    if name.startswith(prefixes) and 0x10080000 <= addr < 0x11180000 and (addr & ~1) not in m.code_hooks:
        m.hook(addr, tracer(name))
r = m.run(n)
print(r)
print('\n'.join(events))
print('\n-- counts')
for k, v in sorted(calls.items(), key=lambda x: -x[1]):
    print(f'{v:>8}  {k}')
