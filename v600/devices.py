"""Python handles on the chip models that run in C (core/*.c), for board.py and the tools.

Each object's `cdev` is the C device pointer that Machine.attach() puts on the bus."""
import ctypes

import numpy as np
from PIL import Image

from .core import Core

p, u32, u64, i, dbl = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_int, ctypes.c_double
NONE = 0xFFFFFFFFFFFFFFFF


def _lib():
    if Core.lib is None:
        Core()
    lib = Core.lib
    if not getattr(lib, '_devices_declared', False):
        for name, res, args in [
                ('machine_new', p, [p, dbl]), ('machine_free', None, [p]),
                ('machine_set_host', None, [p, HOST_DEVICE]),
                ('machine_attach', None, [p, u32, u32, p]),
                ('machine_attach_host', None, [p, u32, u32, i]),
                ('machine_plain', None, [p, u32, u32]),
                ('machine_counter', u64, [p]), ('machine_insns', u64, [p]),
                ('machine_set_insns', None, [p, u64]), ('machine_set_ticks', None, [p, dbl]),
                ('machine_mmio_reads', u64, [p]), ('machine_mmio_writes', u64, [p]),
                ('machine_trace', i, [p, p]),
                ('aitc_new', p, [p]), ('aitc_set_line', None, [p, i, i]),
                ('aitc_irq_line', i, [p]), ('aitc_fiq_line', i, [p]),
                ('ostimer_new', p, [p]), ('ostimer_update', None, [p]),
                ('ostimer_next_event', u64, [p]),
                ('counter_new', p, [p]), ('regs_new', p, [p, u32, u32, u32]),
                ('keypad_new', p, [p]), ('keypad_press', None, [p, i, i]),
                ('edgeport_new', p, [p]), ('edgeport_set_pin', None, [p, i, i]),
                ('ds2502_new', p, [ctypes.c_char_p, ctypes.c_char_p]),
                ('onewire_new', p, [p, p]),
                ('flash_new', p, [p]), ('flash_flush', None, [p]), ('flash_idle', i, [p]),
                ('flash_writes', u64, [p]),
                ('sim_new', p, [p, i, dbl]), ('sim_update', None, [p]),
                ('sim_next_event', dbl, [p]),
                ('ati_new', p, []), ('ati_mem', p, [p]), ('ati_reg', u32, [p, u32]),
                ('ati_draws', u64, [p]), ('ati_outer', p, [p]),
                ('dma_new', p, [p, p]), ('pmic_new', p, []),
                ('pmic_set_reg', None, [p, i, u32]), ('pmic_connect', None, [p, p]),
                ('pmic_int_level', i, [p]), ('pmic_power_key', None, [p, i]),
                ('qspi_new', p, [p, p, p])]:
            f = getattr(lib, name)
            f.restype, f.argtypes = res, args
        lib._devices_declared = True
    return lib


HOST_DEVICE = ctypes.CFUNCTYPE(u32, i, i, u32, i, u32)


class Trace(ctypes.Structure):
    _fields_ = [('pc', u32), ('kind', ctypes.c_char), ('addr', u32), ('size', i), ('value', u32)]


class AITC:
    """Interrupt controller (irq.c)."""
    def __init__(self, m):
        self.lib = _lib()
        self.cdev = self.lib.aitc_new(m.c)

    def set_line(self, src, level):
        self.lib.aitc_set_line(self.cdev, src, 1 if level else 0)

    def irq_line(self):
        return bool(self.lib.aitc_irq_line(self.cdev))

    def fiq_line(self):
        return bool(self.lib.aitc_fiq_line(self.cdev))


class OSTimer:
    """OS wake-up timer (irq.c); a machine timer (update / next_event)."""
    def __init__(self, m):
        self.lib = _lib()
        self.cdev = self.lib.ostimer_new(m.c)

    def update(self):
        self.lib.ostimer_update(self.cdev)

    def next_event(self):
        v = self.lib.ostimer_next_event(self.cdev)
        return None if v == NONE else v


class FreeRunCounter:
    """RTR free-running counter (periph.c), driven by the virtual clock."""
    def __init__(self, m, ticks_per_insn):
        self.lib = _lib()
        self.m = m
        self.k = ticks_per_insn
        self.lib.machine_set_ticks(m.c, ticks_per_insn)
        self.cdev = self.lib.counter_new(m.c)

    def value(self):
        return self.lib.machine_counter(self.m.c)


class Regs:
    """Register file with one self-clearing word (periph.c)."""
    def __init__(self, m, size, clear_word=0, clear_mask=0):
        self.cdev = _lib().regs_new(m.c, size, clear_word, clear_mask)


class Keypad:
    """Keypad port (periph.c). CODES: the raw KPDR value per key (P2K-ELF-SDK hw_keypad.h);
    the order here is the C table's. The red key is the power key on the PCAP."""
    CODES = {
        '1': 0xFE7F, '2': 0xFEF7, '3': 0xFD7F, '4': 0xFDF7, '5': 0xFF7E, '6': 0xFFF6,
        '7': 0xFFF3, '8': 0xFFF5, '9': 0xFFEB, '*': 0xFF7D, '0': 0xFEBF, '#': 0xFDBF,
        'green': 0xFEDF, 'soft_right': 0xFFBB, 'soft_left': 0xFFBD, 'menu': 0xFF7B,
        'vol_up': 0xFDDF, 'vol_down': 0xFFDE, 'smart': 0xFFDD, 'voice': 0xFFDB,
        'right': 0xFFEE, 'left': 0xFDEF, 'up': 0xFEEF, 'down': 0xFFED, 'ok': 0xFFBE,
    }
    INDEX = {name: n for n, name in enumerate(CODES)}

    def __init__(self, m):
        self.lib = _lib()
        self.cdev = self.lib.keypad_new(m.c)

    def press(self, name, down=True):
        self.lib.keypad_press(self.cdev, self.INDEX[name], 1 if down else 0)


class EdgePort:
    """External interrupt pins (periph.c): INT0 = flip / headset, INT1 = power IC."""
    def __init__(self, m):
        self.lib = _lib()
        self.cdev = self.lib.edgeport_new(m.c)

    def set_pin(self, n, level):
        self.lib.edgeport_set_pin(self.cdev, n, 1 if level else 0)


class OneWire:
    """One-wire master (periph.c) with the battery's DS2502 ID EPROM (ds2502.c)."""
    def __init__(self, m, memory, serial=b'\x01\x02\x03\x04\x00\x50'):
        lib = _lib()
        self.battery = lib.ds2502_new(bytes(serial), bytes(memory))
        self.cdev = lib.onewire_new(m.c, self.battery)


class SIMInterface:
    """SIM card interface (SCIM) with a virtual GSM SIM (sim.c); a machine timer."""
    def __init__(self, m, card=True, ips=52_000_000):
        self.lib = _lib()
        self.cdev = self.lib.sim_new(m.c, 1 if card else 0, float(ips))

    def update(self):
        self.lib.sim_update(self.cdev)

    def next_event(self):
        v = self.lib.sim_next_event(self.cdev)
        return None if v < 0 else v


# -- graphics: chip (ati.c), DMA (dma.c), QSPI and the power IC (qspi.c) -------------------
MEM_SIZE = 0x400000
GRAPHIC_OFFSET, GRAPHIC_PITCH, GRAPHIC_H_DISP, GRAPHIC_V_DISP = 0x418, 0x41C, 0x42C, 0x430


class _OuterState(ctypes.Structure):
    _fields_ = [('pixels', ctypes.c_uint8 * (32 * 96)), ('page', i), ('column', i),
                ('mirrored', i), ('updates', u64)]


class OuterDisplay:
    """The 96 x 32 display on the outside of the flip (fed by the graphics chip)."""
    def __init__(self, ptr):
        self.state = ctypes.cast(ptr, ctypes.POINTER(_OuterState)).contents
        self.pixels = np.ctypeslib.as_array(self.state.pixels).reshape(32, 96)

    @property
    def updates(self):
        return self.state.updates

    def image(self, on=(20, 30, 60), off=(150, 190, 215)):
        rgb = np.where(self.pixels[:, :, None] != 0, np.array(on, np.uint8), np.array(off, np.uint8))
        return Image.fromarray(rgb.astype(np.uint8))


class ATIChip:
    """The ATI Imageon graphics chip (with its camera I2C bus and the outer display)."""
    def __init__(self):
        self.lib = _lib()
        self.cdev_chip = self.lib.ati_new()
        ptr = ctypes.cast(self.lib.ati_mem(self.cdev_chip), ctypes.POINTER(ctypes.c_uint8))
        self.mem = np.ctypeslib.as_array(ptr, shape=(MEM_SIZE,))     # live view of chip memory
        self.outer = OuterDisplay(self.lib.ati_outer(self.cdev_chip))

    @property
    def draws(self):
        """Drawing operations so far, to detect new frames."""
        return self.lib.ati_draws(self.cdev_chip)

    def reg(self, r):
        return self.lib.ati_reg(self.cdev_chip, r)

    def screen(self):
        """The displayed frame (GRAPHIC_OFFSET, GRAPHIC_PITCH bytes per row) as RGB."""
        base = self.reg(GRAPHIC_OFFSET)
        pitch = self.reg(GRAPHIC_PITCH) or 0x160
        w = (self.reg(GRAPHIC_H_DISP) >> 16) or 176
        h = (self.reg(GRAPHIC_V_DISP) >> 16) or 220
        rows = [bytes(self.mem[base + y * pitch: base + y * pitch + 2 * w]) for y in range(h)]
        a = np.frombuffer(b''.join(rows), dtype='<u2').reshape(h, w)
        rgb = np.dstack([((a >> 11) & 31) * 255 // 31, ((a >> 5) & 63) * 255 // 63,
                         (a & 31) * 255 // 31]).astype(np.uint8)
        return Image.fromarray(rgb)


class DMA:
    def __init__(self, m, chip):
        self.cdev = _lib().dma_new(m.c, chip.cdev_chip)


class PowerIC:
    """PCAP2 power IC on the QSPI: registers, ADC, the power (red) key."""
    def __init__(self):
        self.lib = _lib()
        self.cpmic = self.lib.pmic_new()

    def set_reg(self, reg, value):
        self.lib.pmic_set_reg(self.cpmic, reg, value)

    def connect(self, edgeport):
        """Its interrupt pin drives edge port INT1."""
        self.lib.pmic_connect(self.cpmic, edgeport.cdev)

    def int_level(self):
        return bool(self.lib.pmic_int_level(self.cpmic))

    def power_key(self, down):
        self.lib.pmic_power_key(self.cpmic, 1 if down else 0)


class QSPI:
    """Queued SPI controller: the power IC and the graphics chip's serial interface."""
    def __init__(self, m, pmic, chip):
        self.cdev = _lib().qspi_new(m.c, pmic.cpmic, chip.cdev_chip)
