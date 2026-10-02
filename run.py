"""Boot the V600 firmware as far as the current models allow."""
import sys
sys.path.insert(0, 'tools')
from v600.machine import Machine
from v600 import board
from armdis import dis


def trace_boot(m):
    from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_LR
    progress = []

    def checkpoint(m):
        code = m.uc.reg_read(UC_ARM_REG_R0)
        progress.append(code)
        m.log(f'  [boot] checkpoint {code:#04x}  from {m.uc.reg_read(UC_ARM_REG_LR) - 5:08x}')

    def shutdown(m):
        m.log(f'!! shutdown/reset requested: reason={m.uc.reg_read(UC_ARM_REG_R0):#x} '
              f'from {m.uc.reg_read(UC_ARM_REG_LR) - 5:08x}')
        m.log('   backtrace: ' + ' '.join(f'{a:08x}' for a in m.backtrace()))
    def reset_hook(name):
        def f(m):
            m.log(f'!! {name}: lr={m.uc.reg_read(UC_ARM_REG_LR):08x} r0={m.r(0):#x}')
            m.log('   backtrace: ' + ' '.join(f'{a:08x}' for a in m.backtrace()))
        return f
    m.hook(0x10AB7F6A, reset_hook('watchdog soft reset'))
    m.hook(0x10AB7FA8, reset_hook('watchdog hard reset'))
    m.hook(0x106330B8, checkpoint)
    m.hook(0x10AB8218, shutdown)
    m.progress = progress


def main():
    m = Machine()
    board.build(m)
    m.reset()
    trace_boot(m)
    result = m.run(max_insns=int(sys.argv[1]) if len(sys.argv) > 1 else 50_000_000)
    print(f'\n== stopped: {result}')
    print(m.dump_regs())
    print('backtrace:', ' '.join(f'{a:08x}' for a in m.backtrace()))
    pc = m.pc()
    print('\n-- code at pc')
    dis(max(pc - 16, 0), 16, m.thumb())
    print('\n-- last MMIO accesses')
    for pc_, kind, addr, size, val in list(m.mmio_trace)[-16:]:
        print(f'  pc={pc_:08x} {kind}{size * 8:<2} {addr:08x} = {val:08x}')


if __name__ == '__main__':
    main()
