"""The V600's camera sensor, as far as its control bus goes.

The camera driver probes three sensor types (functions ATI_agilent_/micron_/philips_*) over
the graphics chip's I2C bus. The Micron probe (0x1066392A) selects the sensor-core register
page (R1 = 4), reads the chip version from R255 and accepts 0x8229 or 0x823A; 0x823A is the
Micron MT9V111 VGA system-on-chip camera, at I2C address 0xB8.

MT9V111 register access: a write sends the register number then 16-bit values (big-endian,
the register number advancing after each); a read returns 16-bit values from the last
register number written. R1 selects the page (1 = image flow processor, 4 = sensor core).
Registers keep what is written; no image is produced yet.
"""

class MT9V111:
    ADDRESS = 0xB8 >> 1
    CHIP_VERSION = 0x823A

    def __init__(self):
        self.page = 4
        self.regs = {1: {}, 4: {0xFF: self.CHIP_VERSION}}
        self.ptr = 0

    def write(self, data):
        if not data:
            return
        self.ptr = data[0]
        for i in range(1, len(data) - 1, 2):
            self._set(self.ptr, data[i] << 8 | data[i + 1])
            self.ptr = (self.ptr + 1) & 0xFF

    def _set(self, reg, value):
        if reg == 1:
            self.page = value & 0xFF
        elif reg != 0xFF:
            self.regs.setdefault(self.page, {})[reg] = value

    def read(self, n):
        out = bytearray()
        while len(out) < n:
            v = self.page if self.ptr == 1 else self.regs.get(self.page, {}).get(self.ptr, 0)
            out += v.to_bytes(2, 'big')
            self.ptr = (self.ptr + 1) & 0xFF
        return bytes(out[:n])
