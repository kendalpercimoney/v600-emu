"""Taking interrupts between instruction slices. The interrupt controller (AITC) and the OS
timer are in C: core/irq.c."""
from unicorn.arm_const import UC_ARM_REG_CPSR, UC_ARM_REG_SPSR, UC_ARM_REG_LR, UC_ARM_REG_PC

CPSR_E = 0x200


def deliver(m):
    """Take an IRQ or FIQ if one is pending and unmasked (called between instruction slices)."""
    uc = m.uc
    cpsr = uc.reg_read(UC_ARM_REG_CPSR)
    if m.aitc.fiq_line() and not cpsr & 0x40:
        mode, vector, mask = 0x11, 0x1C, 0xC0
    elif m.aitc.irq_line() and not cpsr & 0x80:
        mode, vector, mask = 0x12, 0x18, 0x80
    else:
        return False
    ret = uc.reg_read(UC_ARM_REG_PC)           # next instruction to execute
    uc.reg_write(UC_ARM_REG_CPSR, (cpsr & ~0x3F) | CPSR_E | mask | mode)   # ARM state, masked
    uc.reg_write(UC_ARM_REG_SPSR, cpsr)
    uc.reg_write(UC_ARM_REG_LR, ret + 4)
    uc.reg_write(UC_ARM_REG_PC, vector)
    m.irq_count += 1
    return True
