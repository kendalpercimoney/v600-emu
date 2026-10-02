"""M0: check Unicorn runs big-endian ARM and Thumb code (with interworking) correctly."""
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_MODE_BIG_ENDIAN
from unicorn.arm_const import *

uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_BIG_ENDIAN)
uc.mem_map(0, 0x10000)
arm = bytes.fromhex(
    'e3a00011'   # mov r0, #0x11
    'e28f1005'   # add r1, pc, #5      -> thumb code at +0x10, bit0 set
    'e12fff11'   # bx r1
    'e1a00000'   # nop (pad)
)
thumb = bytes.fromhex(
    '3001'       # adds r0, #1         r0 = 0x12
    '4a01'       # ldr r2, [pc, #4]    literal (word aligned)
    '6013'       # str r3, [r2]... (r3 = 0) placeholder
    'e7fe'       # b .  (stop here)
    '00001000'   # literal 0x1000
)
uc.mem_write(0, arm)
uc.mem_write(0x10, thumb)
uc.mem_write(0x1000, b'\x12\x34\x56\x78')
uc.reg_write(UC_ARM_REG_R3, 0xCAFEBABE)
uc.emu_start(0, 0x16 | 1, count=20)
r0, r2 = uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R2)
word = uc.mem_read(0x1000, 4).hex()
cpsr = uc.reg_read(UC_ARM_REG_CPSR)
print(f'r0={r0:#x} r2={r2:#x} mem[0x1000]={word} thumb={(cpsr >> 5) & 1}')
assert r0 == 0x12 and r2 == 0x1000, 'big-endian Thumb decode/literal load wrong'
assert word == 'cafebabe', 'big-endian store wrong'
print('OK: big-endian ARM + Thumb + interworking work')
