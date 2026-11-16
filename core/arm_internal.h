/* CPU state and the memory fast paths shared by cpu.c and mem.c. */
#ifndef V600_ARM_INTERNAL_H
#define V600_ARM_INTERNAL_H
#include "arm.h"

#define PAGE_BITS 12
#define PAGE_SIZE (1u << PAGE_BITS)
#define NPAGES (1u << (32 - PAGE_BITS))
#define MAX_HOOKS 256
#define REGION_BITS 20                  /* read / write hooks are tracked per 1 MB region */
#define NREGIONS (1u << (32 - REGION_BITS))

enum { PF_MAPPED = 1, PF_HREAD = 2, PF_HWRITE = 4, PF_HCODE = 8 };

#define FLAG_N 0x80000000u
#define FLAG_Z 0x40000000u
#define FLAG_C 0x20000000u
#define FLAG_V 0x10000000u
#define T_BIT 0x20u

typedef struct { int kind, live; uint32_t begin, end; arm_native_fn fn; void *ctx; } hook_t;

struct arm {
    uint32_t r[16];                 /* r15 = address of the current / next instruction */
    uint32_t cpsr, spsr;            /* spsr of the current mode */
    uint32_t r13b[6], r14b[6], spsrb[6], fiq8[5], usr8[5];   /* banks (see mode_index) */
    uint32_t cp15_control;
    uint8_t **page;                 /* host memory per 4 KB page, allocated on first use */
    uint8_t *pflags;                /* PF_MAPPED, PF_HCODE per page */
    uint8_t rflags[NREGIONS];       /* PF_HREAD, PF_HWRITE per region */
    hook_t hooks[MAX_HOOKS];
    int nhooks, has_intr, has_unmapped;
    arm_cb cb;
    void *host;
    int stop, err, intr;            /* intr: interrupt raised by the instruction, or -1 */
    uint32_t next_pc;
    uint64_t executed;
};

uint32_t mem_load_slow(arm_t *s, uint32_t a, int n);
void     mem_store_slow(arm_t *s, uint32_t a, int n, uint32_t v);
int      mem_fetch_slow(arm_t *s, uint32_t a, int n, uint32_t *out);

static inline uint32_t be_get(const uint8_t *p, int n) {
    return n == 4 ? (uint32_t)p[0] << 24 | p[1] << 16 | p[2] << 8 | p[3]
         : n == 2 ? (uint32_t)(p[0] << 8 | p[1]) : p[0];
}

static inline void be_put(uint8_t *p, int n, uint32_t v) {
    if (n == 4) { p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v; }
    else if (n == 2) { p[0] = v >> 8; p[1] = v; }
    else p[0] = v;
}

/* fast path: allocated page, no read / write hooks in its region, access within the page */
static inline uint32_t mem_load(arm_t *s, uint32_t a, int n) {
    uint32_t pg = a >> PAGE_BITS;
    uint8_t *p = s->page[pg];
    if (__builtin_expect(p && !s->rflags[a >> REGION_BITS] && (a & (PAGE_SIZE - 1)) + n <= PAGE_SIZE, 1))
        return be_get(p + (a & (PAGE_SIZE - 1)), n);
    return mem_load_slow(s, a, n);
}

static inline void mem_store(arm_t *s, uint32_t a, int n, uint32_t v) {
    uint32_t pg = a >> PAGE_BITS;
    uint8_t *p = s->page[pg];
    if (__builtin_expect(p && !s->rflags[a >> REGION_BITS] && (a & (PAGE_SIZE - 1)) + n <= PAGE_SIZE, 1))
        be_put(p + (a & (PAGE_SIZE - 1)), n, v);
    else
        mem_store_slow(s, a, n, v);
}

static inline int mode_index(uint32_t cpsr) {
    switch (cpsr & 0x1F) {
    case 0x11: return 1;            /* FIQ */
    case 0x12: return 2;            /* IRQ */
    case 0x13: return 3;            /* SVC */
    case 0x17: return 4;            /* abort */
    case 0x1B: return 5;            /* undefined */
    default:   return 0;            /* user, system */
    }
}

void write_cpsr(arm_t *s, uint32_t v);   /* switches banks when the mode changes */

#endif
