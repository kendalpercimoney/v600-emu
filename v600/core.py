"""The C library (core/, built by `make`), and its CPU behind the subset of Unicorn's
Python API the tools use (registers, memory, hooks)."""
import ctypes
from pathlib import Path

from unicorn import (UcError, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_HOOK_CODE, UC_HOOK_INTR,
                     UC_HOOK_MEM_UNMAPPED)
from unicorn import arm_const

LIB = Path(__file__).resolve().parent.parent / 'build' / 'libv600core.dylib'

# hook kinds in core/arm.h
HK_READ, HK_WRITE, HK_CODE, HK_INTR, HK_UNMAPPED = 1, 2, 4, 8, 16
KINDS = {UC_HOOK_MEM_READ: HK_READ, UC_HOOK_MEM_WRITE: HK_WRITE, UC_HOOK_CODE: HK_CODE,
         UC_HOOK_INTR: HK_INTR, UC_HOOK_MEM_UNMAPPED: HK_UNMAPPED}
REG_CPSR, REG_SPSR = 16, 17
REGS = {getattr(arm_const, f'UC_ARM_REG_R{i}'): i for i in range(16)}
REGS.update({arm_const.UC_ARM_REG_SP: 13, arm_const.UC_ARM_REG_LR: 14, arm_const.UC_ARM_REG_PC: 15,
             arm_const.UC_ARM_REG_CPSR: REG_CPSR, arm_const.UC_ARM_REG_SPSR: REG_SPSR})

CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                            ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)


def _load():
    if not LIB.exists():
        raise RuntimeError(f'{LIB} is missing: run `make` first')
    lib = ctypes.CDLL(str(LIB))
    p, u32, i = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    for name, res, args in [
            ('arm_new', p, []), ('arm_free', None, [p]),
            ('arm_set_callback', None, [p, CALLBACK, p]),
            ('arm_map', i, [p, u32, u32]),
            ('arm_mem_read', i, [p, u32, ctypes.c_char_p, u32]),
            ('arm_mem_write', i, [p, u32, ctypes.c_char_p, u32]),
            ('arm_reg_read', u32, [p, i]), ('arm_reg_write', None, [p, i, u32]),
            ('arm_hook_add', i, [p, i, u32, u32]), ('arm_hook_del', None, [p, i]),
            ('arm_run', i, [p, u32, u32, ctypes.c_uint64]), ('arm_stop', None, [p]),
            ('arm_executed', ctypes.c_uint64, [p])]:
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = res, args
    return lib


class Core:
    """Unicorn-compatible CPU object (the methods Machine and the models call)."""
    lib = None

    @classmethod
    def load(cls):
        if cls.lib is None:
            cls.lib = _load()
        return cls.lib

    def __init__(self, ptr=None):
        Core.load()
        self.owner = ptr is None                     # else the machine owns the CPU
        self.s = ptr or Core.lib.arm_new()
        self.hooks = {}                              # id -> (kind, fn, user_data)
        self._cb = CALLBACK(self._dispatch)          # keep a reference: C holds the pointer
        Core.lib.arm_set_callback(self.s, self._cb, None)

    def __del__(self):
        if self.owner and self.s and Core.lib:
            Core.lib.arm_free(self.s)
            self.s = None

    def _dispatch(self, _host, hid, access, addr, size, value):
        kind, fn, user = self.hooks[hid]
        if kind == HK_INTR:
            fn(self, addr, user)
        elif kind == HK_CODE:
            fn(self, addr, size, user)
        else:
            return 1 if fn(self, access, addr, size, value, user) else 0
        return 0

    # -- memory ---------------------------------------------------------------------------
    def mem_map(self, addr, size, perms=7):
        Core.lib.arm_map(self.s, addr, size)

    def mem_read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        err = Core.lib.arm_mem_read(self.s, addr, buf, size)
        if err:
            raise UcError(err)
        return bytearray(buf.raw)

    def mem_write(self, addr, data):
        data = bytes(data)
        err = Core.lib.arm_mem_write(self.s, addr, data, len(data))
        if err:
            raise UcError(err)

    # -- registers ------------------------------------------------------------------------
    def reg_read(self, reg):
        return Core.lib.arm_reg_read(self.s, REGS[reg])

    def reg_write(self, reg, value):
        Core.lib.arm_reg_write(self.s, REGS[reg], value & 0xFFFFFFFF)

    # -- hooks and running ----------------------------------------------------------------
    def hook_add(self, htype, callback, user_data=None, begin=1, end=0, *_):
        kind = KINDS[htype]
        hid = Core.lib.arm_hook_add(self.s, kind, begin & 0xFFFFFFFF, end & 0xFFFFFFFF)
        if hid < 0:
            raise RuntimeError('too many hooks')
        self.hooks[hid] = (kind, callback, user_data)
        return hid

    def hook_del(self, hid):
        Core.lib.arm_hook_del(self.s, hid)
        self.hooks.pop(hid, None)

    def emu_start(self, begin, until, timeout=0, count=0):
        err = Core.lib.arm_run(self.s, begin, until & 0xFFFFFFFF, count)
        if err:
            raise UcError(err)

    def emu_stop(self):
        Core.lib.arm_stop(self.s)

    def ctl_remove_cache(self, begin, end=None):
        pass                                         # no translation cache to flush

    def ctl_set_cpu_model(self, model):
        pass
