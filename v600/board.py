"""The phone's board: the chip models wired up as on the V600 (core/board.c), and the
handles the tools use. CPU_IPS: the CPU is taken to run 52 M instructions / s."""
from .devices import (ATIChip, AudioDSP, Counter, Keypad, PART_ATI, PART_AUDIO, PART_FLASH,
                      PART_NETWORK, _lib)
from .flash import Flash
from .network import Network

CPU_IPS = 52_000_000


def build(m, flip_open=True, sim_card=True):
    lib = _lib()
    lib.v600_build(m.c, 1 if flip_open else 0, 1 if sim_card else 0)
    m.counter = Counter(m)
    m.ati = ATIChip(lib.v600_part(m.c, PART_ATI))
    m.audio = AudioDSP(lib.v600_part(m.c, PART_AUDIO))
    m.flash = Flash(m, lib.v600_part(m.c, PART_FLASH))
    m.network = Network(lib.v600_part(m.c, PART_NETWORK))
    return m


def set_flip(m, open_):
    """Flip sensor: open (main display) or closed (the UI moves to the outer display)."""
    _lib().v600_flip(m.c, 1 if open_ else 0)


def press(m, name, down=True):
    """A key by name (devices.Keypad.CODES, or 'red': the power key on the PCAP)."""
    _lib().v600_press(m.c, -1 if name == 'red' else Keypad.INDEX[name], 1 if down else 0)
