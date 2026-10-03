"""Dallas DS2502 1-Kbit add-only memory, the battery ID chip on the one-wire bus.

Bit-level slave: the OneWire master calls reset() and bit(v) (v = 1 also reads a bit).
ROM commands: 0x33 read ROM, 0xCC skip ROM, 0x55 match ROM, 0xF0 search ROM.
Memory commands: 0xF0 read memory (addr lo, addr hi, then CRC8, then data to the end),
0xC3 read data/generate 8-bit CRC (CRC of cmd+addr, then each 32-byte page + CRC8), 0xAA read status (CRC + data).
CRC: Dallas/Maxim CRC8 (poly 0x8C reflected)."""


def crc8(data, crc=0):
    for b in data:
        for _ in range(8):
            mix = (crc ^ b) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            b >>= 1
    return crc


class DS2502:
    """Motorola batteries carry the UniqueWare part (family 0x89); the battery check
    (0x10B1D572) also wants ROM byte 6 = 0x50 and byte 5 bits 7:4 = 0 (customer code)."""
    FAMILY = 0x89

    def __init__(self, serial=b'\x01\x02\x03\x04\x00\x50', memory=None, status=None, log=None):
        rom = bytes([self.FAMILY]) + serial
        self.rom = rom + bytes([crc8(rom)])
        self.mem = bytearray(memory if memory is not None else b'\xFF' * 128)
        self.status = bytearray(status if status is not None else b'\xFF' * 8)
        self.log = log
        self.reset()

    # --- bus ---------------------------------------------------------------
    def reset(self):
        self.state = 'rom'
        self.inbits = []
        self.out = []                  # bits queued to send to the master
        self.cmd = None
        self.args = b''
        return True

    def bit(self, v):
        if self.out:                   # read slot: the master writes 1 and samples
            return self.out.pop(0) & v
        if self.state == 'search':     # master's direction bit after our bit/complement pair
            if v != self._rombit(self.idx):
                self.state = 'idle'
                return v
            self.idx += 1
            if self.idx == 64:
                self.state = 'mem'
            else:
                self._search_pair()
            return v
        self.inbits.append(v)
        if len(self.inbits) == 8:
            byte = sum(b << i for i, b in enumerate(self.inbits))
            self.inbits = []
            self.on_byte(byte)
        return v

    def _rombit(self, i):
        return (self.rom[i // 8] >> (i % 8)) & 1

    def _search_pair(self):
        b = self._rombit(self.idx)
        self.out += [b, b ^ 1]

    def send(self, data):
        for byte in data:
            self.out += [(byte >> i) & 1 for i in range(8)]

    # --- protocol ----------------------------------------------------------
    def on_byte(self, b):
        if self.log:
            self.log(f'  ds2502 <- {b:#04x} ({self.state})')
        if self.state == 'rom':
            if b == 0x33:
                self.send(self.rom)
                self.state = 'mem'
            elif b == 0xCC:
                self.state = 'mem'
            elif b == 0xF0:
                self.state, self.idx = 'search', 0
                self._search_pair()
            elif b == 0x55:
                self.state, self.args = 'match', b''
            else:
                self.state = 'idle'
        elif self.state == 'match':
            self.args += bytes([b])
            if len(self.args) == 8:
                self.state = 'mem' if self.args == self.rom else 'idle'
        elif self.state == 'mem':
            self.cmd, self.args, self.state = b, b'', 'addr'
        elif self.state == 'addr':
            self.args += bytes([b])
            if len(self.args) == 2:
                addr = self.args[0] | self.args[1] << 8
                hdr = bytes([self.cmd]) + self.args
                if self.cmd == 0xF0:                         # read memory
                    self.send(bytes([crc8(hdr)]) + bytes(self.mem[addr:]) + b'\xFF' * 16)
                elif self.cmd == 0xAA:                       # read status
                    data = bytes(self.status[addr:])
                    self.send(bytes([crc8(hdr)]) + data + bytes([crc8(data)]))
                elif self.cmd == 0xC3:                       # read data + page CRC
                    out = bytes([crc8(hdr)])                 # CRC of command + address,
                    a = addr                                 # then each page + its CRC
                    while a < len(self.mem):
                        end = (a | 31) + 1
                        page = bytes(self.mem[a:end])
                        out += page + bytes([crc8(page)])
                        a = end
                    self.send(out)
                self.state = 'data'
        # 'data' / 'idle': ignore further writes until reset
