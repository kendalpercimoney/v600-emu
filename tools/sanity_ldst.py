"""Check every load/store form in big-endian ARM and Thumb under Unicorn."""
from unicorn import *
from unicorn.arm_const import *
import capstone

def run(code, thumb, cpu=None):
    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_BIG_ENDIAN)
    if cpu is not None: uc.ctl_set_cpu_model(cpu)
    uc.mem_map(0, 0x10000)
    uc.mem_write(0, bytes.fromhex(code))
    uc.mem_write(0x1000, bytes.fromhex('11223344 55667788'.replace(' ', '')))
    uc.reg_write(UC_ARM_REG_R0, 0x1000); uc.reg_write(UC_ARM_REG_R7, 0xA1B2C3D4)
    uc.reg_write(UC_ARM_REG_SP, 0x2000)
    uc.emu_start(1 if thumb else 0, len(bytes.fromhex(code)), count=1)
    return uc

tests = [  # (name, code, thumb, check)
 ('arm ldr',  'e5901000', 0, lambda u: u.reg_read(UC_ARM_REG_R1) == 0x11223344),
 ('arm ldm',  'e8900006', 0, lambda u: u.reg_read(UC_ARM_REG_R1) == 0x11223344 and u.reg_read(UC_ARM_REG_R2) == 0x55667788),
 ('arm ldm!', 'e8b00006', 0, lambda u: u.reg_read(UC_ARM_REG_R1) == 0x11223344),
 ('arm ldrh', 'e1d010b0', 0, lambda u: u.reg_read(UC_ARM_REG_R1) == 0x1122),
 ('arm ldrb', 'e5d01001', 0, lambda u: u.reg_read(UC_ARM_REG_R1) == 0x22),
 ('arm str',  'e5807000', 0, lambda u: u.mem_read(0x1000, 4).hex() == 'a1b2c3d4'),
 ('arm stm',  'e8800080', 0, lambda u: u.mem_read(0x1000, 4).hex() == 'a1b2c3d4'),
 ('arm strh', 'e1c070b0', 0, lambda u: u.mem_read(0x1000, 2).hex() == 'c3d4'),
 ('thumb ldr', '6801', 1, lambda u: u.reg_read(UC_ARM_REG_R1) == 0x11223344),
 ('thumb ldm', 'c806', 1, lambda u: u.reg_read(UC_ARM_REG_R1) == 0x11223344 and u.reg_read(UC_ARM_REG_R2) == 0x55667788),
 ('thumb ldrh', '8801', 1, lambda u: u.reg_read(UC_ARM_REG_R1) == 0x1122),
 ('thumb stm', 'c080', 1, lambda u: u.mem_read(0x1000, 4).hex() == 'a1b2c3d4'),
 ('thumb push', 'b480', 1, lambda u: u.mem_read(0x1ffc, 4).hex() == 'a1b2c3d4'),
]
for cpu_name in ('default', 'ARM926', 'ARM946', 'ARM1136'):
    cpu = None if cpu_name == 'default' else getattr(unicorn.arm_const, f'UC_CPU_ARM_{cpu_name[3:]}', None)
    if cpu_name != 'default' and cpu is None: print(cpu_name, 'not available'); continue
    res = []
    for name, code, thumb, ok in tests:
        try: res.append((name, ok(run(code, thumb, cpu))))
        except Exception as e: res.append((name, f'ERR {e}'))
    print(f'{cpu_name:8s}', '  '.join(f'{n}:{"ok" if r is True else "BAD" if r is False else r}' for n, r in res))
