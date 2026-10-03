"""Function names for our build, merged from the symbol files in out/ (git-ignored; made by
tools/port_syms.py, tools/port_e1.py and tools/match_e1.py). Earlier files win."""
import bisect
import json
from pathlib import Path

FILES = ('symbols_from_pts.json', 'symbols_ported.json',        # MotoFanRu SDK (exact)
         'symbols_e1_exact.json', 'symbols_e1.json')            # E1 build: bytes, structure
OUT = Path(__file__).resolve().parent.parent / 'out'


class Symbols:
    def __init__(self):
        self.by_name, self.by_addr = {}, {}
        for f in FILES:
            try:
                d = json.load(open(OUT / f))
            except FileNotFoundError:
                continue
            for name, addr in d.items():
                a = addr & ~1
                if name not in self.by_name and a not in self.by_addr:
                    self.by_name[name], self.by_addr[a] = addr, name
        self.sorted = sorted(self.by_addr)

    def __len__(self):
        return len(self.by_name)

    def __getitem__(self, name):
        return self.by_name[name]

    def get(self, name, default=None):
        return self.by_name.get(name, default)

    def describe(self, addr, limit=0x1000):
        """'name+0x12' for the nearest named function at or below addr, or ''."""
        i = bisect.bisect_right(self.sorted, addr & ~1) - 1
        if i < 0 or addr - self.sorted[i] > limit:
            return ''
        a = self.sorted[i]
        return self.by_addr[a] + (f'+{addr - a:#x}' if addr != a else '')


_cache = None


def load():
    global _cache
    if _cache is None:
        _cache = Symbols()
    return _cache
