/* Memory map (4 KB pages, allocated on first touch), hooks, and the register API. */
#include <stdlib.h>
#include <string.h>
#include "arm_internal.h"

arm_t *arm_new(void) {
    arm_t *s = calloc(1, sizeof *s);
    s->page = calloc(NPAGES, sizeof *s->page);
    s->pflags = calloc(NPAGES, 1);
    s->intr = -1;
    s->cpsr = 0xD3;                                   /* SVC, interrupts off, ARM */
    return s;
}

void arm_free(arm_t *s) {
    for (uint32_t i = 0; i < NPAGES; i++)
        free(s->page[i]);
    free(s->page);
    free(s->pflags);
    free(s);
}

void arm_set_callback(arm_t *s, arm_cb cb, void *host) { s->cb = cb; s->host = host; }
void arm_stop(arm_t *s) { s->stop = 1; }
uint64_t arm_executed(arm_t *s) { return s->executed; }

int arm_map(arm_t *s, uint32_t addr, uint32_t size) {
    for (uint64_t a = addr; a < (uint64_t)addr + size; a += PAGE_SIZE)
        s->pflags[a >> PAGE_BITS] |= PF_MAPPED;
    return ERR_OK;
}

static uint8_t *page_of(arm_t *s, uint32_t a) {
    uint32_t pg = a >> PAGE_BITS;
    if (!(s->pflags[pg] & PF_MAPPED))
        return NULL;
    if (!s->page[pg])
        s->page[pg] = calloc(1, PAGE_SIZE);
    return s->page[pg];
}

/* -- hooks ----------------------------------------------------------------------------- */
static int in_range(const hook_t *h, uint32_t a) {
    return h->begin > h->end || (a >= h->begin && a <= h->end);
}

static int overlaps(const hook_t *h, uint32_t lo, uint32_t hi) {
    return h->begin > h->end || (h->begin <= hi && h->end >= lo);
}

/* Recompute the hook flags of the regions (read / write) or pages (code) a hook covers. */
static void refresh_flags(arm_t *s, int kind, uint32_t begin, uint32_t end) {
    if (begin > end) { begin = 0; end = 0xFFFFFFFF; }
    int code = kind == HK_CODE, bits = code ? PAGE_BITS : REGION_BITS;
    for (uint32_t u = begin >> bits; ; u++) {
        uint32_t lo = u << bits, hi = lo + ((1u << bits) - 1);
        uint8_t f = 0;
        for (int i = 0; i < s->nhooks; i++) {
            const hook_t *h = &s->hooks[i];
            if (!h->live || !overlaps(h, lo, hi)) continue;
            if (!code && h->kind == HK_READ) f |= PF_HREAD;
            if (!code && h->kind == HK_WRITE) f |= PF_HWRITE;
            if (code && h->kind == HK_CODE) f |= PF_HCODE;
        }
        if (code) s->pflags[u] = (s->pflags[u] & PF_MAPPED) | f;
        else s->rflags[u] = f;
        if (u == end >> bits)
            break;
    }
}

static void recount(arm_t *s) {
    s->has_intr = s->has_unmapped = 0;
    for (int i = 0; i < s->nhooks; i++) {
        if (!s->hooks[i].live) continue;
        s->has_intr |= s->hooks[i].kind == HK_INTR;
        s->has_unmapped |= s->hooks[i].kind == HK_UNMAPPED;
    }
}

int arm_hook_add(arm_t *s, int kind, uint32_t begin, uint32_t end) {
    int id = 0;
    while (id < s->nhooks && s->hooks[id].live)
        id++;
    if (id == MAX_HOOKS)
        return -1;
    if (id == s->nhooks)
        s->nhooks++;
    s->hooks[id] = (hook_t){kind, 1, begin, end};
    if (kind & (HK_READ | HK_WRITE | HK_CODE))
        refresh_flags(s, kind, begin, end);
    recount(s);
    return id;
}

void arm_hook_del(arm_t *s, int id) {
    if (id < 0 || id >= s->nhooks || !s->hooks[id].live)
        return;
    s->hooks[id].live = 0;
    if (s->hooks[id].kind & (HK_READ | HK_WRITE | HK_CODE))
        refresh_flags(s, s->hooks[id].kind, s->hooks[id].begin, s->hooks[id].end);
    recount(s);
}

static void call_hooks(arm_t *s, int kind, int access, uint32_t a, int n, uint32_t v) {
    for (int i = 0; i < s->nhooks; i++)
        if (s->hooks[i].live && s->hooks[i].kind == kind && in_range(&s->hooks[i], a))
            s->cb(s->host, i, access, a, n, v);
}

/* An access to unmapped memory: the hook may map it (nonzero: retry); else an error. */
static int unmapped(arm_t *s, int access, uint32_t a, int n, uint32_t v) {
    int retry = 0;
    for (int i = 0; i < s->nhooks; i++)
        if (s->hooks[i].live && s->hooks[i].kind == HK_UNMAPPED)
            retry |= s->cb(s->host, i, access, a, n, v);
    if (retry && page_of(s, a))
        return 1;
    if (!s->err)
        s->err = access == ACC_READ_UNMAPPED ? ERR_READ_UNMAPPED
               : access == ACC_WRITE_UNMAPPED ? ERR_WRITE_UNMAPPED : ERR_FETCH_UNMAPPED;
    s->stop = 1;
    return 0;
}

/* -- memory slow paths ------------------------------------------------------------------ */
uint32_t mem_load_slow(arm_t *s, uint32_t a, int n) {
    if (s->rflags[a >> REGION_BITS] & PF_HREAD)
        call_hooks(s, HK_READ, ACC_READ, a, n, 0);
    if ((a & (PAGE_SIZE - 1)) + n > PAGE_SIZE) {        /* unaligned, across pages */
        uint32_t v = 0;
        for (int i = 0; i < n; i++) {
            uint8_t *p = page_of(s, a + i);
            if (!p && !(unmapped(s, ACC_READ_UNMAPPED, a + i, 1, 0) && (p = page_of(s, a + i))))
                return 0;
            v = v << 8 | p[(a + i) & (PAGE_SIZE - 1)];
        }
        return v;
    }
    uint8_t *p = page_of(s, a);
    if (!p && !(unmapped(s, ACC_READ_UNMAPPED, a, n, 0) && (p = page_of(s, a))))
        return 0;
    return be_get(p + (a & (PAGE_SIZE - 1)), n);
}

void mem_store_slow(arm_t *s, uint32_t a, int n, uint32_t v) {
    if (s->rflags[a >> REGION_BITS] & PF_HWRITE)
        call_hooks(s, HK_WRITE, ACC_WRITE, a, n, v);
    for (int i = 0; i < n; i++) {
        uint32_t b = a + i;
        uint8_t *p = page_of(s, b);
        if (!p && !(unmapped(s, ACC_WRITE_UNMAPPED, a, n, v) && (p = page_of(s, b))))
            return;
        p[b & (PAGE_SIZE - 1)] = v >> (8 * (n - 1 - i));
    }
}

int mem_fetch_slow(arm_t *s, uint32_t a, int n, uint32_t *out) {
    uint8_t *p = page_of(s, a);
    if (!p && !(unmapped(s, ACC_FETCH_UNMAPPED, a, n, 0) && (p = page_of(s, a))))
        return 0;
    *out = be_get(p + (a & (PAGE_SIZE - 1)), n);
    return 1;
}

/* -- host access (no hooks, like uc_mem_read / uc_mem_write) ---------------------------- */
int arm_mem_read(arm_t *s, uint32_t addr, uint8_t *out, uint32_t n) {
    while (n) {
        uint32_t off = addr & (PAGE_SIZE - 1), k = PAGE_SIZE - off < n ? PAGE_SIZE - off : n;
        uint32_t pg = addr >> PAGE_BITS;
        if (!(s->pflags[pg] & PF_MAPPED))
            return ERR_READ_UNMAPPED;
        if (s->page[pg]) memcpy(out, s->page[pg] + off, k);
        else memset(out, 0, k);
        addr += k; out += k; n -= k;
    }
    return ERR_OK;
}

int arm_mem_write(arm_t *s, uint32_t addr, const uint8_t *in, uint32_t n) {
    while (n) {
        uint32_t off = addr & (PAGE_SIZE - 1), k = PAGE_SIZE - off < n ? PAGE_SIZE - off : n;
        uint8_t *p = page_of(s, addr);
        if (!p)
            return ERR_WRITE_UNMAPPED;
        memcpy(p + off, in, k);
        addr += k; in += k; n -= k;
    }
    return ERR_OK;
}

/* -- registers ------------------------------------------------------------------------- */
void write_cpsr(arm_t *s, uint32_t v) {
    int o = mode_index(s->cpsr), n = mode_index(v);
    if (o != n) {
        s->r13b[o] = s->r[13]; s->r14b[o] = s->r[14]; s->spsrb[o] = s->spsr;
        if (o == 1) { memcpy(s->fiq8, s->r + 8, 20); memcpy(s->r + 8, s->usr8, 20); }
        if (n == 1) { memcpy(s->usr8, s->r + 8, 20); memcpy(s->r + 8, s->fiq8, 20); }
        s->r[13] = s->r13b[n]; s->r[14] = s->r14b[n]; s->spsr = s->spsrb[n];
    }
    s->cpsr = v;
}

uint32_t arm_reg_read(arm_t *s, int reg) {
    if (reg < 16) return s->r[reg];
    return reg == REG_CPSR ? s->cpsr : s->spsr;
}

void arm_reg_write(arm_t *s, int reg, uint32_t v) {
    if (reg == 15) {                                  /* bit 0 picks Thumb, as in Unicorn */
        s->r[15] = v & ~1u;
        s->cpsr = (s->cpsr & ~T_BIT) | (v & 1 ? T_BIT : 0);
    } else if (reg < 16) {
        s->r[reg] = v;
    } else if (reg == REG_CPSR) {
        write_cpsr(s, v);
    } else {
        s->spsr = v;
    }
}
