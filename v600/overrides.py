"""Targeted high-level overrides of firmware functions.

Used where a real model would need per-phone data we don't have. Each override runs at the
function's entry; it either returns early (with r0 set) or lets the function run.
"""
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_LR, UC_ARM_REG_R0


def return_from(m, value):
    """Skip the rest of the current function: r0 = value, jump to lr."""
    m.uc.reg_write(UC_ARM_REG_R0, value)
    m.uc.reg_write(UC_ARM_REG_PC, m.uc.reg_read(UC_ARM_REG_LR))


# Product type expected by the reflash check (0x10AB9304) = header byte 0x10080019.
PRODUCT_TYPE = 3


def read_secure_item(m):
    """0x10647372(item, buf) -> 0 on success. Item 0x293 is the product type."""
    item, buf = m.r(0), m.r(1)
    if item == 0x293:
        m.uc.mem_write(buf, bytes([PRODUCT_TYPE]))
        return_from(m, 0)


def skip_boot_battery_check(m):
    """0x10AE4784: powers off if the ADC battery reading (table 0x121BFF14) is below 0x68.
    The reading arrives via the power IC's ADC-done interrupt, which is not modelled yet.
    TEMPORARY: remove once interrupts and the ADC are in."""
    return_from(m, 0)


# No SIM card. The SIM reset state machine (0x101B83C8) calls card reset 0x103A0624 and treats
# 1 as "answer-to-reset received"; anything else retries via deactivate 0x103A08C8 until the
# retry count runs out, then reports 5 = no card. Both functions busy-wait on the SIM
# interface, so they are replaced: reset never gets an answer, deactivate completes at once.
def sim_reset_no_answer(m):
    return_from(m, 0)


def sim_deactivate(m):
    return_from(m, 0)


def install(m, sim_card=True):
    if not sim_card:
        m.hook(0x103A0624, sim_reset_no_answer)
        m.hook(0x103A08C8, sim_deactivate)
    m.hook(0x10647372, read_secure_item)
    m.hook(0x10AE4784, skip_boot_battery_check)
