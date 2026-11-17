"""Python handles on the phone's parts, which run in C (core/), for the tools.

The C library is declared here once (_lib); board.build() makes the handles."""
import ctypes

import numpy as np
from PIL import Image

from .core import Core

p, u32, u64, i, dbl = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_int, ctypes.c_double
i64 = ctypes.c_int64
LOG = ctypes.CFUNCTYPE(None, i, ctypes.c_char_p)
HOST_HOOK = ctypes.CFUNCTYPE(None, i)
HOST_TIMER = ctypes.CFUNCTYPE(dbl, i, i)
HOST_DEVICE = ctypes.CFUNCTYPE(u32, i, i, u32, i, u32)
PART_CPU, PART_KEYPAD, PART_EDGEPORT, PART_PMIC, PART_ATI, PART_AUDIO, PART_FLASH, PART_NETWORK = range(8)


def _lib():
    lib = Core.load()
    if not getattr(lib, '_declared', False):
        for name, res, args in [
                ('v600_machine_new', p, [ctypes.c_char_p, i64]), ('v600_reset', None, [p]),
                ('v600_build', None, [p, i, i]), ('v600_press', None, [p, i, i]),
                ('v600_flip', None, [p, i]), ('v600_part', p, [p, i]),
                ('machine_set_log', None, [p, LOG]), ('machine_set_host_hook', None, [p, HOST_HOOK]),
                ('machine_set_host_timer', None, [p, HOST_TIMER]),
                ('machine_set_host', None, [p, HOST_DEVICE]),
                ('machine_attach_host', None, [p, u32, u32, i]),
                ('machine_plain', None, [p, u32, u32]),
                ('machine_hook_host', None, [p, u32, i, i]),
                ('machine_add_host_timer', None, [p, i]), ('machine_remove_host_timer', None, [p, i]),
                ('machine_run', i, [p, u64, u32, u32, u32]), ('machine_stop', None, [p, ctypes.c_char_p]),
                ('machine_start', u32, [p]), ('machine_set_start', None, [p, u32]),
                ('machine_stopped', ctypes.c_char_p, [p]), ('machine_ticks', dbl, [p]),
                ('machine_irq_count', u64, [p]),
                ('machine_counter', u64, [p]), ('machine_insns', u64, [p]),
                ('machine_set_insns', None, [p, u64]),
                ('machine_mmio_reads', u64, [p]), ('machine_mmio_writes', u64, [p]),
                ('machine_trace', i, [p, p]), ('fwlog_install', None, [p]),
                ('machine_flash_patches', i, [p, p, p, p, i]),
                ('flash_flush', None, [p]), ('flash_idle', i, [p]), ('flash_writes', u64, [p]),
                ('ati_mem', p, [p]), ('ati_reg', u32, [p, u32]), ('ati_draws', u64, [p]),
                ('ati_outer', p, [p]),
                ('audio_pull', i, [p, ctypes.POINTER(i64), p, i]), ('audio_events', i, [p]),
                ('audio_event', i64, [p, i, ctypes.POINTER(i), ctypes.POINTER(i),
                                      ctypes.POINTER(i64), p, i64]),
                ('audio_events_clear', None, [p]),
                ('network_registered', i, [p]), ('network_from_phone', i, [p, p, i])]:
            f = getattr(lib, name)
            f.restype, f.argtypes = res, args
        lib._declared = True
    return lib


class Trace(ctypes.Structure):
    _fields_ = [('pc', u32), ('kind', ctypes.c_char), ('addr', u32), ('size', i), ('value', u32)]


class Counter:
    """The free-running counter's rate (ticks per instruction) and value, for timers."""
    def __init__(self, m):
        self.m = m
        self.k = _lib().machine_ticks(m.c)

    def value(self):
        return _lib().machine_counter(self.m.c)


class Keypad:
    """CODES: the raw KPDR value per key (P2K-ELF-SDK hw_keypad.h), in the order of the C
    table (core/periph.c). The red key is the power key on the PCAP: board.press()."""
    CODES = {
        '1': 0xFE7F, '2': 0xFEF7, '3': 0xFD7F, '4': 0xFDF7, '5': 0xFF7E, '6': 0xFFF6,
        '7': 0xFFF3, '8': 0xFFF5, '9': 0xFFEB, '*': 0xFF7D, '0': 0xFEBF, '#': 0xFDBF,
        'green': 0xFEDF, 'soft_right': 0xFFBB, 'soft_left': 0xFFBD, 'menu': 0xFF7B,
        'vol_up': 0xFDDF, 'vol_down': 0xFFDE, 'smart': 0xFFDD, 'voice': 0xFFDB,
        'right': 0xFFEE, 'left': 0xFDEF, 'up': 0xFEEF, 'down': 0xFFED, 'ok': 0xFFBE,
    }
    INDEX = {name: n for n, name in enumerate(CODES)}


# -- graphics: the chip's memory, display registers and the outer display ----------------
MEM_SIZE = 0x400000
GRAPHIC_OFFSET, GRAPHIC_PITCH, GRAPHIC_H_DISP, GRAPHIC_V_DISP = 0x418, 0x41C, 0x42C, 0x430


class _OuterState(ctypes.Structure):
    _fields_ = [('pixels', ctypes.c_uint8 * (32 * 96)), ('page', i), ('column', i),
                ('mirrored', i), ('updates', u64)]


class OuterDisplay:
    """The 96 x 32 display on the outside of the flip."""
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
    """The ATI Imageon graphics chip (core/ati.c)."""
    def __init__(self, ptr):
        self.lib = _lib()
        self.ptr = ptr
        mem = ctypes.cast(self.lib.ati_mem(ptr), ctypes.POINTER(ctypes.c_uint8))
        self.mem = np.ctypeslib.as_array(mem, shape=(MEM_SIZE,))     # live view
        self.outer = OuterDisplay(self.lib.ati_outer(ptr))

    @property
    def draws(self):
        """Drawing operations so far, to detect new frames."""
        return self.lib.ati_draws(self.ptr)

    def reg(self, r):
        return self.lib.ati_reg(self.ptr, r)

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


# -- sound ------------------------------------------------------------------------------------
RATE = 16000                            # PCM sample rate of AudioDSP.pull()


class AudioDSP:
    """What the DSP plays (core/audio.c). pull() and media_events() may be called from
    another thread (the C side locks)."""
    def __init__(self, ptr):
        self.lib = _lib()
        self.ptr = ptr
        self._buf = (ctypes.c_int16 * (RATE + 16))()

    def pull(self):
        """(first sample index, 16-bit little-endian mono PCM at RATE) since the last pull."""
        start = i64()
        n = self.lib.audio_pull(self.ptr, ctypes.byref(start), self._buf, RATE + 16)
        return start.value, bytes(np.frombuffer(self._buf, '<i2', n))

    def media_events(self):
        """Clips started or stopped since the last call: ('start', id, sample, mime, bytes)
        or ('stop', id, sample)."""
        out = []
        kind, ident, at = i(), i(), i64()
        for k in range(self.lib.audio_events(self.ptr)):
            n = self.lib.audio_event(self.ptr, k, kind, ident, at, None, 0)
            if kind.value:
                data = ctypes.create_string_buffer(max(n, 1))
                self.lib.audio_event(self.ptr, k, kind, ident, at, data, n)
                out.append(('start', ident.value, at.value, 'audio/mpeg', data.raw[:n]))
            else:
                out.append(('stop', ident.value, at.value))
        self.lib.audio_events_clear(self.ptr)
        return out
