"""Loader for Motorola P2K superfiles (.shx): a text header followed by S-record groups."""
import re
from pathlib import Path

_S3 = re.compile(rb'S3([0-9A-F]{2})([0-9A-F]{8})([0-9A-F]*?)([0-9A-F]{2})\r?\n')


def load(path):
    """Return a list of (base_address, bytearray) for each contiguous region in the file."""
    data = Path(path).read_bytes()
    regions = []
    cur = None
    for _, addr, payload, _ in _S3.findall(data):
        a = int(addr, 16)
        b = bytes.fromhex(payload.decode())
        if cur and cur[0] + len(cur[1]) == a:
            cur[1].extend(b)
        else:
            cur = [a, bytearray(b)]
            regions.append(cur)
    return [(a, b) for a, b in regions]


if __name__ == '__main__':
    import sys
    for a, b in load(sys.argv[1] if len(sys.argv) > 1 else 'firmware/v600.shx'):
        print(f'{a:08x}-{a + len(b) - 1:08x}  {len(b):>9}')
