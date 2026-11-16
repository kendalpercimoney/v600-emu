/* ARM7TDMI-class CPU (ARMv5T: ARM + Thumb, big-endian) with a paged memory map and hooks.

   The interface mirrors the parts of Unicorn the emulator uses, with the same semantics,
   so the Python side can swap one for the other (v600/core.py):
   - read / write hooks run before the access (a read hook may change memory to supply
     the value; the store follows a write hook);
   - writing PC sets Thumb state from bit 0; writing CPSR switches register banks;
   - BKPT / SWI call the interrupt hook (7 / 2) with PC on that instruction, then
     execution goes on from whatever PC is then;
   - memory hooks see PC = the instruction doing the access. */
#ifndef V600_ARM_H
#define V600_ARM_H
#include <stdint.h>

typedef struct arm arm_t;

/* hook kinds (bit flags) */
enum { HK_READ = 1, HK_WRITE = 2, HK_CODE = 4, HK_INTR = 8, HK_UNMAPPED = 16 };
/* access codes passed for HK_UNMAPPED / HK_READ / HK_WRITE (Unicorn's values) */
enum { ACC_READ = 16, ACC_WRITE = 17, ACC_READ_UNMAPPED = 19, ACC_WRITE_UNMAPPED = 20,
       ACC_FETCH_UNMAPPED = 21 };
/* errors (Unicorn's codes) */
enum { ERR_OK = 0, ERR_READ_UNMAPPED = 6, ERR_WRITE_UNMAPPED = 7, ERR_FETCH_UNMAPPED = 8,
       ERR_INSN_INVALID = 10, ERR_EXCEPTION = 21 };
/* registers: 0-15 = r0-r15 of the current mode, then: */
enum { REG_CPSR = 16, REG_SPSR = 17 };

/* One callback for every hook: id from arm_hook_add, kind HK_*, access ACC_* (memory),
   intno (HK_INTR, in addr). Returns nonzero from HK_UNMAPPED to retry the access. */
typedef int (*arm_cb)(void *host, int id, int access, uint32_t addr, int size, uint32_t value);

/* A hook handled in C (the machine's own devices): same arguments, no id. */
typedef int (*arm_native_fn)(void *ctx, int access, uint32_t addr, int size, uint32_t value);

arm_t   *arm_new(void);
void     arm_free(arm_t *);
void     arm_set_callback(arm_t *, arm_cb, void *host);
int      arm_map(arm_t *, uint32_t addr, uint32_t size);
int      arm_mem_read(arm_t *, uint32_t addr, uint8_t *out, uint32_t n);
int      arm_mem_write(arm_t *, uint32_t addr, const uint8_t *in, uint32_t n);
uint32_t arm_reg_read(arm_t *, int reg);
void     arm_reg_write(arm_t *, int reg, uint32_t value);
int      arm_hook_add(arm_t *, int kind, uint32_t begin, uint32_t end);  /* begin > end: all */
int      arm_hook_add_native(arm_t *, int kind, uint32_t begin, uint32_t end,
                            arm_native_fn fn, void *ctx);
void     arm_hook_del(arm_t *, int id);
/* Run from begin (bit 0 = Thumb) until PC == until, count instructions (0 = no limit),
   arm_stop() or an error; returns ERR_*. */
int      arm_run(arm_t *, uint32_t begin, uint32_t until, uint64_t count);
void     arm_stop(arm_t *);
uint64_t arm_executed(arm_t *);

#endif
