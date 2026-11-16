"""Python handles on the chip models that run in C (core/*.c), for board.py and the tools.

Each object's `cdev` is the C device pointer that Machine.attach() puts on the bus."""
import ctypes

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
                ('flash_writes', u64, [p])]:
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
