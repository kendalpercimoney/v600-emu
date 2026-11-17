/* Interrupts: the AITC interrupt controller at 0x29890000 and the OS wake-up timer.

   AITC register layout (Freescale ARM Interrupt Controller, as used by the firmware):
     +00 INTCNTL   +04 NIMASK    +08 INTENNUM  +0C INTDISNUM
     +10 INTENABLEH (sources 63..32)  +14 INTENABLEL (31..0)
     +18 INTTYPEH  +1C INTTYPEL  (1 = FIQ)
     +20..+3C NIPRIORITY7..0  (4 bits per source; +20 holds sources 63..56, 63 in bits 31:28)
     +40 NIVECSR   (bits 31:16 = highest pending normal source, 0xFFFF if none; 15:0 its priority)
     +44 FIVECSR   (highest pending fast source, 0xFFFFFFFF if none)
     +48/+4C INTSRCH/L (raw)  +50/+54 INTFRCH/L (software force)  +58/+5C NIPNDH/L
     +60/+64 FIPNDH/L */
#include <stdlib.h>
#include "machine.h"

#define LO(v) ((uint32_t)(v))
#define HI(v) ((uint32_t)((v) >> 32))

void aitc_set_line(aitc_t *a, int src, int level) {
    if (level) a->raw |= 1ull << src;
    else a->raw &= ~(1ull << src);
}

static uint64_t pending(aitc_t *a) { return (a->raw | a->force) & a->enable; }

/* highest-priority pending normal source (same priority: higher number wins), or -1 */
static int normal_vector(aitc_t *a, int *prio) {
    uint64_t p = pending(a) & ~a->type;
    int best = -1, best_prio = -1;
    for (int s = 0; s < 64; s++) {
        if (!(p >> s & 1)) continue;
        int pr = a->prio[s];
        if (a->nimask != 0x1F && pr <= (int)a->nimask) continue;
        if (pr >= best_prio) { best = s; best_prio = pr; }
    }
    if (prio) *prio = best_prio;
    return best;
}

static int fast_vector(aitc_t *a) {
    uint64_t p = pending(a) & a->type;
    for (int s = 63; s >= 0; s--)
        if (p >> s & 1) return s;
    return -1;
}

int aitc_irq_line(aitc_t *a) { return normal_vector(a, 0) >= 0; }
int aitc_fiq_line(aitc_t *a) { return fast_vector(a) >= 0; }

static uint32_t aitc_read(device_t *d, uint32_t off, int size) {
    aitc_t *a = (aitc_t *)d;
    off &= ~3u;
    switch (off) {
    case 0x00: return a->intcntl;
    case 0x04: return a->nimask;
    case 0x10: return HI(a->enable);
    case 0x14: return LO(a->enable);
    case 0x18: return HI(a->type);
    case 0x1C: return LO(a->type);
    case 0x40: { int p, s = normal_vector(a, &p); return s < 0 ? 0xFFFFFFFF : (uint32_t)s << 16 | (uint32_t)p; }
    case 0x44: { int s = fast_vector(a); return s < 0 ? 0xFFFFFFFF : (uint32_t)s; }
    case 0x48: return HI(a->raw);
    case 0x4C: return LO(a->raw);
    case 0x50: return HI(a->force);
    case 0x54: return LO(a->force);
    case 0x58: return HI(pending(a) & ~a->type);
    case 0x5C: return LO(pending(a) & ~a->type);
    case 0x60: return HI(pending(a) & a->type);
    case 0x64: return LO(pending(a) & a->type);
    }
    if (off >= 0x20 && off <= 0x3C) {
        int base = (7 - (off - 0x20) / 4) * 8;
        uint32_t v = 0;
        for (int i = 0; i < 8; i++) v |= (uint32_t)a->prio[base + i] << (4 * i);
        return v;
    }
    return 0;
}

static void aitc_write(device_t *d, uint32_t off, int size, uint32_t v) {
    aitc_t *a = (aitc_t *)d;
    off &= ~3u;
    switch (off) {
    case 0x00: a->intcntl = v; return;
    case 0x04: a->nimask = v & 0x1F; return;
    case 0x08: a->enable |= 1ull << (v & 0x3F); return;
    case 0x0C: a->enable &= ~(1ull << (v & 0x3F)); return;
    case 0x10: a->enable = (a->enable & 0xFFFFFFFFull) | (uint64_t)v << 32; return;
    case 0x14: a->enable = (a->enable & ~0xFFFFFFFFull) | v; return;
    case 0x18: a->type = (a->type & 0xFFFFFFFFull) | (uint64_t)v << 32; return;
    case 0x1C: a->type = (a->type & ~0xFFFFFFFFull) | v; return;
    case 0x50: a->force = (a->force & 0xFFFFFFFFull) | (uint64_t)v << 32; return;
    case 0x54: a->force = (a->force & ~0xFFFFFFFFull) | v; return;
    }
    if (off >= 0x20 && off <= 0x3C) {
        int base = (7 - (off - 0x20) / 4) * 8;
        for (int i = 0; i < 8; i++) a->prio[base + i] = v >> (4 * i) & 0xF;
    }
}

aitc_t *aitc_new(machine_t *m) {
    aitc_t *a = calloc(1, sizeof *a);
    a->d = (device_t){aitc_read, aitc_write, m};
    a->nimask = 0x1F;
    m->aitc = a;
    return a;
}

/* -- OS timer -----------------------------------------------------------------------------
   Timer block at 0x24848000 (16-bit registers). Channel used by the RTOS for wake-ups:
   +0x20/+0x22 = 32-bit countdown in time() units (free-running counter >> 19), latched when
   the low half is written; it fires that many units later. +0x06 bit 8 = interrupt enable;
   +0x04 bit 8 = match status (write 1 to clear). Raises AITC source 14. +0x12/+0x16/+0x18
   are the watchdog pre-warning channel (source 15): not modelled, it never fires. */
#define OST_SOURCE 14

static uint32_t get16(const uint8_t *r, uint32_t off) { return (uint32_t)r[off] << 8 | r[off + 1]; }
static void set16(uint8_t *r, uint32_t off, uint32_t v) { r[off] = v >> 8; r[off + 1] = v; }

void ostimer_update(ostimer_t *t) {
    machine_t *m = t->d.m;
    if (t->armed && machine_counter(m) >= t->fire_at) {
        t->armed = 0;
        set16(t->r, 0x04, get16(t->r, 0x04) | 0x100);
    }
    aitc_set_line(m->aitc, OST_SOURCE, (get16(t->r, 0x04) & 0x100) && (get16(t->r, 0x06) & 0x100));
}

uint64_t ostimer_next_event(ostimer_t *t) {
    if (!(t->armed && (get16(t->r, 0x06) & 0x100))) return UINT64_MAX;
    return t->fire_at;
}

static uint32_t ostimer_read(device_t *d, uint32_t off, int size) {
    ostimer_t *t = (ostimer_t *)d;
    ostimer_update(t);
    if (off == 0x18) return (uint32_t)(machine_counter(d->m) >> 8) & 0xFFFF;
    uint32_t v = 0;
    for (int i = 0; i < size && off + i < sizeof t->r; i++) v = v << 8 | t->r[off + i];
    return v;
}

static void ostimer_write(device_t *d, uint32_t off, int size, uint32_t v) {
    ostimer_t *t = (ostimer_t *)d;
    if (off == 0x04) {
        set16(t->r, 0x04, get16(t->r, 0x04) & ~v);               /* write 1 to clear */
    } else {
        for (int i = 0; i < size && off + i < sizeof t->r; i++)
            t->r[off + i] = v >> (8 * (size - 1 - i));
        if (off == 0x22) {                                       /* latched by the low half */
            t->armed = 1;
            uint64_t target = (uint64_t)get16(t->r, 0x20) << 16 | get16(t->r, 0x22);
            t->fire_at = machine_counter(d->m) + (target << 19);
        }
    }
    ostimer_update(t);
}

ostimer_t *ostimer_new(machine_t *m) {
    ostimer_t *t = calloc(1, sizeof *t);
    t->d = (device_t){ostimer_read, ostimer_write, m};
    return t;
}

/* The same in counter units as a double (machine timers), or -1. */
double ostimer_next(void *t) {
    uint64_t v = ostimer_next_event(t);
    return v == UINT64_MAX ? -1 : (double)v;
}
