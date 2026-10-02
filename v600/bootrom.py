"""High-level stand-ins for the boot ROM / bootloader routines in low memory.

The firmware calls ~9 routines below 0x20000 that live in the chip's boot ROM (not in the
superfile). Each gets a Thumb `bx lr` planted at its address plus a hook that logs the call
and sets r0. Return values are refined as we learn what each routine does.
"""
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_LR

# address -> (name, return value or callable(machine) -> int)
ROUTINES = {
    0x2188: ('rom_2188_mode', 0),        # result compared with 2 at 0x1087102E / 0x108710C8
    0x37CE: ('rom_37ce_sec', 0),         # (0x11, buf, 0, r3, [sp]) security service
    0xFCDC: ('rom_fcdc_init', 0),        # first call of the boot sequence, no args
    0xFFD0: ('rom_ffd0', 0),             # (0) -> u16
    0xFFF4: ('rom_fff4_get', 0),         # (id, buf) -> 1 on success
    0x100C2: ('rom_100c2_get', 0),       # (id, buf) -> 1 on success
    0x101CC: ('rom_101cc', 0),           # (type, buf, len, flag)
    0x102AA: ('rom_102aa', 0),           # (1)
    0x102C0: ('rom_102c0', 0),
}

# Exception vectors (bootloader-owned at 0). Point IRQ/FIQ at the firmware's veneers that the
# C runtime copies into internal RAM.
IRQ_VENEER = 0x03FC3604
FIQ_VENEER = 0x03FC3694


def install(m):
    uc = m.uc
    # vectors: ldr pc, [pc, #0x18] for each of the 8 slots, then a table of targets
    for slot in range(8):
        uc.mem_write(slot * 4, bytes.fromhex('e59ff018'))
    targets = [0, 0, 0, 0, 0, 0, IRQ_VENEER, FIQ_VENEER]
    for slot, t in enumerate(targets):
        uc.mem_write(0x20 + slot * 4, t.to_bytes(4, 'big'))
    m.calls = {}
    for addr, (name, ret) in ROUTINES.items():
        uc.mem_write(addr, bytes.fromhex('4770'))            # bx lr
        m.hook(addr, _make_stub(name, ret))


def _make_stub(name, ret):
    def stub(m):
        uc = m.uc
        args = [uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        n = m.calls[name] = m.calls.get(name, 0) + 1
        value = ret(m) if callable(ret) else ret
        if n <= 3:
            m.log(f'  rom {name}({", ".join(f"{a:#x}" for a in args)}) -> {value:#x}  '
                  f'lr={uc.reg_read(UC_ARM_REG_LR):08x}')
        uc.reg_write(UC_ARM_REG_R0, value)
    return stub
