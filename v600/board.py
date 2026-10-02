"""Wires the peripheral models onto a Machine."""
from . import bootrom
from .periph import PowerIC, QSPI

# Power-IC register 23 holds the board strap bits read as signal 0xE7 by the board-variant
# probe (0x10628B1C). Non-zero low bits select a real phone profile (IDs 1/2) instead of the
# "unsupported hardware -> reset" profile (ID 0x11).
PMIC_BOARD_STRAPS = 0x1FFFFFF

# Power-IC register 0 (interrupt status) latches the power-on cause. The power-up decision
# (0x106363F0) reads signal 0x51 = bit 8 (power key), 0x16 = bit 7, 0x19 = bit 5.
# Power key pressed and nothing else -> reason 1 (normal power-up).
PMIC_ISR_POWER_KEY = 1 << 8


def build(m):
    bootrom.install(m)
    m.pmic = PowerIC(m.log)
    m.pmic.regs[23] = PMIC_BOARD_STRAPS
    m.pmic.regs[0] = PMIC_ISR_POWER_KEY
    m.qspi = QSPI({0: m.pmic}, m.log)
    m.attach(0x24851000, 0x1000, m.qspi)
    return m
