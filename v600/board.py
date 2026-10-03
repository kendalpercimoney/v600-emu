"""Wires the peripheral models onto a Machine."""
from . import bootrom, overrides
from .dsp import DSPMessages
from .ds2502 import DS2502
from .flash import Flash
from .irq import AITC, OSTimer
from .periph import (ATIChip, ATISerialPort, DMA, DSPHostPort, FreeRunCounter, EdgePort, Keypad, OneWire,
                     PowerIC, QSPI, Regs)

# Power-IC register 23 is VENDOR_REV (PCAP2, Linux ezx-pcap.h), read as signal 0xE7 by the
# board-variant probe (0x10628B1C). Real value unknown. Non-zero low bits select a real phone profile (IDs 1/2) instead of the
# "unsupported hardware -> reset" profile (ID 0x11).
PMIC_VENDOR_REV = 0x1FFFFFF

# Power-IC register 0 (interrupt status) latches the power-on cause. The power-up decision
# (0x106363F0) reads signal 0x51 = bit 8 (power key), 0x16 = bit 7, 0x19 = bit 5.
# Power key pressed and nothing else -> reason 1 (normal power-up).
PMIC_ISR_POWER_KEY = 1 << 8


# Persistent Data Storage (per-phone calibration/settings) lives at 0x10010000-0x1001FFFF:
# two 32 KB blocks, each starting with the signature "PDS_2006" followed by a 16-bit state
# word ("barker") at +8. It is not part of the superfile, so we create an empty one. The PDS
# finder (0x108735BC) accepts barker 0xFFFD or 0xFFF8-0xFFFB; the header check
# (0x10874598) wants the signature.
PDS_BASE, PDS_BLOCK = 0x10010000, 0x8000
PDS_BARKER = 0xFFF8


# Virtual time. The CPU is assumed to run ~52 M instructions/s. The RTOS measures time in
# units of (RTR counter >> 19). The RTR counts 32.768 kHz in bits 63:17 (Neptune LTE spec,
# ch. 56: MSB = seconds, LSB[31:17] = fraction), so one unit is 4/32768 s = 122.07 us.
CPU_IPS = 52_000_000
TIME_UNIT_US = 1e6 / 8192

# Idle loop: the RTOS idle task calls this to enter low-power wait (0x2484502C mode bits).
IDLE_LOW_POWER = 0x03FF0692


def set_flip(m, open_):
    """Flip sensor on INT0 (HS_INT, pulled up): high = open (main display), low = closed
    (the UI moves to the outer CLI display)."""
    m.edgeport.set_pin(0, 1 if open_ else 0)


def press(m, name, down=True):
    """A key by name (periph.Keypad.CODES, or 'red': the power key on the PCAP)."""
    if name == 'red':
        m.pmic.power_key(down)
    else:
        m.keypad.press(name, down)


def make_pds(m, barker=PDS_BARKER):
    m.uc.mem_write(PDS_BASE, b'PDS_2006' + barker.to_bytes(2, 'big'))


def build(m, pds_barker=PDS_BARKER, flip_open=True):
    bootrom.install(m)
    m.dspmsg = DSPMessages(m)
    m.flash = Flash(m)
    m.pmic = PowerIC(m.log)
    m.pmic.regs[23] = PMIC_VENDOR_REV
    m.pmic.regs[0] = PMIC_ISR_POWER_KEY
    # QSPI devices, keyed by the driver's device descriptor (see QSPI.queue_for)
    # cs6: the ATI Imageon's serial host interface (CHIP_ID: device 0x5745, ATI vendor 0x1002;
    # the driver accepts 0x5641-7 / 0x5741-7 but its frame-buffer setup needs 0x5645 / 0x5745).
    m.ati = ATIChip(m.log)
    m.qspi = QSPI(m, {0x10800562: m.pmic, 0x1080058C: ATISerialPort(m.ati)}, m.log)
    m.attach(0x24851000, 0x1000, m.qspi)
    m.dsp = DSPHostPort(m.log)
    m.attach(0x2485BFF0, 0x10, m.dsp)
    ticks_per_insn = (1 << 19) / (TIME_UNIT_US * 1e-6 * CPU_IPS)
    m.counter = FreeRunCounter(m, ticks_per_insn)
    m.attach(0x2484A000, 0x8, m.counter)
    m.aitc = AITC(m)
    m.attach(0x29890000, 0x100, m.aitc)
    m.dma = DMA(m, m.aitc, m.ati, m.log)
    m.attach(0x24854000, 0x40, m.dma)
    ostimer = OSTimer(m, m.aitc, m.counter)
    m.attach(0x24848000, 0x40, ostimer)
    m.timers.append(ostimer)
    m.hook(IDLE_LOW_POWER, lambda m: setattr(m, 'idle_hint', True))
    # Unidentified unit at 0x24845000: +0x18 data, +0x10 bit 8 = start/busy (self-clearing).
    m.attach(0x24845000, 0x1000, Regs(0x1000, selfclear={0x10: 0x100}))
    # SIM card interface (SCIM) at 0x2484F000 (driver strings p/SCIM_EXT_PORT, p/dl_sim_mgr).
    # No card is fitted: card reset/deactivate are overridden (overrides.py).
    m.attach(0x2484F000, 0x100, Regs(0x100))
    # Keypad port. Unmodelled it read as all rows low = every key held, which the boot-time
    # 'user initiated panic' key check (0x10AE52B0) took as a request and reset the display.
    m.keypad = Keypad(m.aitc)
    m.attach(0x2484E000, 0x10, m.keypad)
    # External interrupt pins, idle high (pulled up). INT0 = HS_INT (flip / headset detect).
    m.edgeport = EdgePort(m.aitc)
    set_flip(m, flip_open)
    m.pmic.on_int = lambda level: m.edgeport.set_pin(1, level)
    m.edgeport.set_pin(1, m.pmic.int_level())
    m.attach(0x2484C000, 0x8, m.edgeport)
    # One-wire bus to the battery's ID EPROM (DS2502).
    # Battery EPROM (128 bytes). The reader (0x10B1D38E) checks each page's CRC and wants
    # byte 0x2F = 0; the validator (0x1008DAA0 / 0x1008DB8A) wants "COPR" at 0x60,
    # "MOTOROLA E.P CHARGE ONLY" at 0x68 and zero byte sums over 0x20-0x3F and 0x40-0x5F.
    # Battery parameters (0x00-0x5F) are not decoded yet: all zero.
    battery_data = bytearray(128)
    battery_data[0x60:0x64] = b'COPR'
    battery_data[0x68:0x80] = b'MOTOROLA E.P CHARGE ONLY'
    m.battery_id = DS2502(memory=battery_data, log=m.log)
    m.onewire = OneWire(m.battery_id)
    m.attach(0x24856000, 0x8, m.onewire)
    make_pds(m, pds_barker)
    overrides.install(m)
    return m
