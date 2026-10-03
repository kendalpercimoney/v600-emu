"""Wires the peripheral models onto a Machine."""
from . import bootrom, overrides
from .dsp import DSPMessages
from .flash import Flash
from .irq import AITC, OSTimer
from .periph import (ATIChip, ATISerialPort, DMA, DSPHostPort, FreeRunCounter, PowerIC,
                     QSPI, Regs)

# Power-IC register 23 holds the board strap bits read as signal 0xE7 by the board-variant
# probe (0x10628B1C). Non-zero low bits select a real phone profile (IDs 1/2) instead of the
# "unsupported hardware -> reset" profile (ID 0x11).
PMIC_BOARD_STRAPS = 0x1FFFFFF

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
# units of (free-running counter >> 19); the length of one unit is not known yet, so it is a
# setting (calibrate against something visible such as animation frame rates).
CPU_IPS = 52_000_000
TIME_UNIT_US = 1000

# Idle loop: the RTOS idle task calls this to enter low-power wait (0x2484502C mode bits).
IDLE_LOW_POWER = 0x03FF0692


def make_pds(m, barker=PDS_BARKER):
    m.uc.mem_write(PDS_BASE, b'PDS_2006' + barker.to_bytes(2, 'big'))


def build(m, pds_barker=PDS_BARKER):
    bootrom.install(m)
    m.dspmsg = DSPMessages(m)
    m.flash = Flash(m)
    m.pmic = PowerIC(m.log)
    m.pmic.regs[23] = PMIC_BOARD_STRAPS
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
    make_pds(m, pds_barker)
    overrides.install(m)
    return m
