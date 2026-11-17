/* Running the machine: instruction slices, interrupts between them, timers and the idle
   fast-forward, breakpoint hooks, and calling firmware functions.

   Ported from v600/machine.py and irq.py (tag py-reference), keeping the order of every
   step, so the instruction counts at which things happen are the same.

   Breakpoints. A hook replaces the instruction at its address with BKPT; when it is hit
   the handlers run, then the replaced instruction is carried out here (one of the simple
   forms found at hooked addresses: push, bx, ldr from a literal, ARM `b .`). A handler that
   moves PC skips it. Code the firmware has yet to copy into internal RAM gets its
   breakpoint once it is there. Hooks marked once per slice put the original instruction
   back after the first hit and return at the end of the slice (for the idle loop, entered
   constantly). Addresses with another instruction fall back to a code hook. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "machine.h"

#define IRAM_BASE 0x03FC0000u
#define IRAM_SIZE 0x40000u
#define FLASH_BASE 0x10000000u
#define FLASH_SIZE 0x2000000u
#define CPSR_E 0x200u
#define CALL_RETURN 0x1F000u
static const uint8_t THUMB_BKPT[2] = {0xBE, 0x00};
static const uint8_t ARM_BKPT[4] = {0xE1, 0x20, 0x00, 0x70};
static const uint8_t ARM_B_SELF[4] = {0xEA, 0xFF, 0xFF, 0xFE};

enum { EMU_NONE, EMU_B_SELF, EMU_PUSH, EMU_BX, EMU_LDR_LITERAL };

static int in_iram(uint32_t a) { return a - IRAM_BASE < IRAM_SIZE; }

void machine_log(machine_t *m, int kind, const char *fmt, ...) {
    if (!m->log) return;
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    m->log(kind, buf);
}

static uint32_t reg(machine_t *m, int r) { return arm_reg_read(m->cpu, r); }
static void set_reg(machine_t *m, int r, uint32_t v) { arm_reg_write(m->cpu, r, v); }

uint32_t machine_read32(machine_t *m, uint32_t a) {
    uint8_t b[4] = {0};
    arm_mem_read(m->cpu, a, b, 4);
    return (uint32_t)b[0] << 24 | b[1] << 16 | b[2] << 8 | b[3];
}

/* -- breakpoints -------------------------------------------------------------------------- */
static int thumb_emulator(const uint8_t *ins) {
    uint32_t op = ins[0] << 8 | ins[1];
    if ((op & 0xFE00) == 0xB400) return EMU_PUSH;
    if ((op & 0xFF87) == 0x4700) return EMU_BX;
    if ((op & 0xF800) == 0x4800) return EMU_LDR_LITERAL;
    return EMU_NONE;
}

static void emulate(machine_t *m, bp_t *b) {
    uint32_t pc = b->addr, op = b->orig[0] << 8 | b->orig[1];
    switch (b->emulate) {
    case EMU_B_SELF:                                      /* `b .`: stays put */
        break;
    case EMU_PUSH: {
        uint32_t regs[9], n = 0;
        for (int r = 0; r < 8; r++) if (op >> r & 1) regs[n++] = reg(m, r);
        if (op & 0x100) regs[n++] = reg(m, 14);
        uint32_t sp = reg(m, 13) - 4 * n;
        uint8_t buf[36];
        for (uint32_t i = 0; i < n; i++)
            for (int k = 0; k < 4; k++) buf[4 * i + k] = regs[i] >> (8 * (3 - k));
        arm_mem_write(m->cpu, sp, buf, 4 * n);
        set_reg(m, 13, sp);
        set_reg(m, 15, (pc + 2) | 1);
        break;
    }
    case EMU_BX: {
        int rm = op >> 3 & 0xF;
        set_reg(m, 15, rm == 15 ? (pc + 4) & ~3u : reg(m, rm));   /* bx pc: to ARM */
        break;
    }
    case EMU_LDR_LITERAL:
        set_reg(m, op >> 8 & 7, machine_read32(m, ((pc + 4) & ~3u) + (op & 0xFF) * 4));
        set_reg(m, 15, (pc + 2) | 1);
        break;
    }
}

static bp_t *find_bp(machine_t *m, uint32_t addr) {
    for (int i = 0; i < m->nbps; i++) if (m->bps[i].addr == addr) return &m->bps[i];
    return 0;
}

static void plant(machine_t *m, bp_t *b, const uint8_t *orig, int n, int how) {
    memcpy(b->orig, orig, n);
    b->norig = n;
    b->emulate = how;
    b->planted = 1;
    arm_mem_write(m->cpu, b->addr, n == 4 ? ARM_BKPT : THUMB_BKPT, n);
    if (in_iram(b->addr)) m->iram_bps = 1;
}

static void run_handlers(machine_t *m, const handler_t *h, int n) {
    for (int i = 0; i < n; i++) {
        if (h[i].fn) h[i].fn(m, h[i].ctx);
        else if (m->host_hook) m->host_hook(h[i].host_id);
    }
}

static int on_code(void *ctx, int access, uint32_t addr, int size, uint32_t value) {
    code_hook_t *c = ctx;
    run_handlers(c->m, &c->h, 1);
    return 0;
}

static void add_hook(machine_t *m, uint32_t addr, handler_t h, int once) {
    addr &= ~1u;
    if (once && m->nonce < MAX_ONCE) m->once[m->nonce++] = addr;
    bp_t *b = find_bp(m, addr);
    if (b) {
        if (b->nh < MAX_HANDLERS) b->h[b->nh++] = h;
        return;
    }
    uint8_t old[4] = {0};
    arm_mem_read(m->cpu, addr, old, 4);
    int how = !memcmp(old, ARM_B_SELF, 4) ? EMU_B_SELF : thumb_emulator(old);
    if (how != EMU_NONE || in_iram(addr)) {
        if (m->nbps == m->cbps) {
            m->cbps = m->cbps ? 2 * m->cbps : 64;
            m->bps = realloc(m->bps, m->cbps * sizeof *m->bps);
        }
        b = &m->bps[m->nbps++];
        memset(b, 0, sizeof *b);
        b->addr = addr;
        b->h[b->nh++] = h;
        if (how == EMU_B_SELF) plant(m, b, old, 4, how);
        else if (how != EMU_NONE) plant(m, b, old, 2, how);
        else m->iram_bps = 1;                             /* not copied there yet */
        return;
    }
    code_hook_t *c = calloc(1, sizeof *c);                /* any other instruction */
    c->m = m;
    c->h = h;
    arm_hook_add_native(m->cpu, HK_CODE, addr, addr, on_code, c);
}

void machine_hook(machine_t *m, uint32_t addr, hook_fn fn, void *ctx, int once) {
    add_hook(m, addr, (handler_t){fn, ctx, -1}, once);
}

void machine_hook_host(machine_t *m, uint32_t addr, int host_id, int once) {
    add_hook(m, addr, (handler_t){0, 0, host_id}, once);
}

static int is_once(machine_t *m, uint32_t a) {
    for (int i = 0; i < m->nonce; i++) if (m->once[i] == a) return 1;
    return 0;
}

static int on_interrupt(void *ctx, int access, uint32_t intno, int size, uint32_t value) {
    machine_t *m = ctx;
    if (intno != 7) return 0;                             /* 7: BKPT */
    uint32_t pc = reg(m, 15);
    bp_t *b = find_bp(m, pc);
    if (!b) return 0;
    uint32_t at = b->addr;
    run_handlers(m, b->h, b->nh);
    b = find_bp(m, at);                                   /* (handlers may add hooks) */
    if (is_once(m, at)) {                                 /* run the real instruction now */
        arm_mem_write(m->cpu, at, b->orig, b->norig);
        if (m->nlifted < MAX_ONCE) m->lifted[m->nlifted++] = at;
    } else if (reg(m, 15) == at) {                        /* nothing jumped elsewhere */
        emulate(m, b);
    }
    return 0;
}

/* Plant breakpoints in internal RAM once the firmware has copied code there, and again if
   it copies over them, keeping what it wrote as the instruction to emulate. */
static void replant(machine_t *m) {
    for (int i = 0; i < m->nbps; i++) {
        bp_t *b = &m->bps[i];
        if (!in_iram(b->addr)) continue;
        uint8_t now[2];
        arm_mem_read(m->cpu, b->addr, now, 2);
        int how = thumb_emulator(now);
        if (memcmp(now, THUMB_BKPT, 2) && how != EMU_NONE) plant(m, b, now, 2, how);
    }
}

/* Planted breakpoints in flash (code the emulator changed, not phone data): for the state
   file. Returns how many, filling addr / 4-byte original-length bytes. */
int machine_flash_patches(machine_t *m, uint32_t *addr, uint8_t *bytes, int *len, int max) {
    int n = 0;
    for (int i = 0; i < m->nbps && n < max; i++) {
        bp_t *b = &m->bps[i];
        if (!b->planted || b->addr - FLASH_BASE >= FLASH_SIZE) continue;
        addr[n] = b->addr;
        len[n] = b->norig;
        memcpy(bytes + 4 * n, b->norig == 4 ? ARM_BKPT : THUMB_BKPT, b->norig);
        n++;
    }
    return n;
}

/* -- timers ---------------------------------------------------------------------------------- */
void machine_add_timer(machine_t *m, void (*update)(void *), double (*next)(void *), void *ctx) {
    if (m->ntimers < MAX_TIMERS) m->timers[m->ntimers++] = (timer_entry_t){update, next, ctx, -1};
}

void machine_add_host_timer(machine_t *m, int host_id) {
    if (m->ntimers < MAX_TIMERS) m->timers[m->ntimers++] = (timer_entry_t){0, 0, 0, host_id};
}

void machine_remove_host_timer(machine_t *m, int host_id) {
    for (int i = 0; i < m->ntimers; i++)
        if (!m->timers[i].update && m->timers[i].host_id == host_id) {
            memmove(&m->timers[i], &m->timers[i + 1], (m->ntimers - i - 1) * sizeof m->timers[0]);
            m->ntimers--;
            return;
        }
}

static void timer_update(machine_t *m, timer_entry_t *t) {
    if (t->update) t->update(t->ctx);
    else if (m->host_timer) m->host_timer(t->host_id, 0);
}

static double timer_next(machine_t *m, timer_entry_t *t) {
    if (t->update) return t->next ? t->next(t->ctx) : -1;          /* a timer in C */
    return m->host_timer ? m->host_timer(t->host_id, 1) : -1;
}

/* -- interrupts ------------------------------------------------------------------------------ */
/* Take an IRQ or FIQ if one is pending and unmasked (between instruction slices). */
static int deliver(machine_t *m) {
    uint32_t cpsr = reg(m, REG_CPSR), mode, vector, mask;
    if (aitc_fiq_line(m->aitc) && !(cpsr & 0x40)) { mode = 0x11; vector = 0x1C; mask = 0xC0; }
    else if (aitc_irq_line(m->aitc) && !(cpsr & 0x80)) { mode = 0x12; vector = 0x18; mask = 0x80; }
    else return 0;
    uint32_t ret = reg(m, 15);                            /* next instruction to execute */
    set_reg(m, REG_CPSR, (cpsr & ~0x3Fu) | CPSR_E | mask | mode);   /* ARM state, masked */
    set_reg(m, REG_SPSR, cpsr);
    set_reg(m, 14, ret + 4);
    set_reg(m, 15, vector);
    m->irq_count++;
    return 1;
}

static void service(machine_t *m) {
    for (int i = 0; i < m->nlifted; i++) {
        bp_t *b = find_bp(m, m->lifted[i]);
        if (b) arm_mem_write(m->cpu, b->addr, b->norig == 2 ? THUMB_BKPT : ARM_BKPT, b->norig);
    }
    m->nlifted = 0;
    if (m->iram_bps) replant(m);
    for (int i = 0; i < m->ntimers; i++) timer_update(m, &m->timers[i]);
    if (m->idle_hint && !aitc_irq_line(m->aitc)) {
        /* The OS is idling: jump the virtual clock to the next timer event. */
        double best = -1;
        for (int i = 0; i < m->ntimers; i++) {
            double e = timer_next(m, &m->timers[i]);
            if (e >= 0 && (best < 0 || e < best)) best = e;
        }
        if (best >= 0 && m->ticks_per_insn > 0) {
            uint64_t target = (uint64_t)(best / m->ticks_per_insn) + 1;
            if (target > m->insns) m->insns = target;
            for (int i = 0; i < m->ntimers; i++) timer_update(m, &m->timers[i]);
        }
    }
    m->idle_hint = 0;
    deliver(m);
}

/* -- calling firmware functions ------------------------------------------------------------
   Have the firmware run fn(args) (Thumb, up to 4 arguments) from its idle task the next
   time the OS idles; then(m, ctx, r0) gets the result and may queue more calls, which run
   before the idle task continues. For posting OS messages to the phone's tasks
   (firmware.c): idle is a safe point, outside the kernel and interrupts. */
void machine_call(machine_t *m, uint32_t fn, const uint32_t *args, int nargs, call_then_fn then, void *ctx) {
    if (m->ncalls == MAX_CALLS) return;
    call_t *c = &m->calls[(m->call_head + m->ncalls++) % MAX_CALLS];
    c->fn = fn;
    c->nargs = nargs;
    memcpy(c->args, args, nargs * sizeof *args);
    c->then = then;
    c->ctx = ctx;
}

static void start_call(machine_t *m) {
    call_t c = m->calls[m->call_head];
    m->call_head = (m->call_head + 1) % MAX_CALLS;
    m->ncalls--;
    m->current = c;
    for (int i = 0; i < c.nargs; i++) set_reg(m, i, c.args[i]);
    set_reg(m, 14, CALL_RETURN | 1);
    set_reg(m, 15, c.fn | 1);
}

/* The idle loop is about to enter low-power wait (board.c hooks it). */
void machine_at_idle(machine_t *m, void *unused) {
    m->idle_hint = 1;
    if (m->ncalls && !m->in_call) {
        for (int r = 0; r < 16; r++) m->call_context[r] = reg(m, r);
        m->call_context[16] = reg(m, REG_CPSR);
        m->in_call = 1;
        start_call(m);
    }
}

static void call_returned(machine_t *m, void *unused) {
    if (m->current.then) m->current.then(m, m->current.ctx, reg(m, 0));
    if (m->ncalls) { start_call(m); return; }
    uint32_t cpsr = m->call_context[16];
    m->in_call = 0;
    set_reg(m, REG_CPSR, cpsr);
    for (int r = 0; r < 15; r++) set_reg(m, r, m->call_context[r]);
    set_reg(m, 15, m->call_context[15] | (cpsr & 0x20 ? 1 : 0));
}

/* -- running ------------------------------------------------------------------------------------ */
static int on_unmapped(void *ctx, int access, uint32_t addr, int size, uint32_t value) {
    machine_t *m = ctx;
    m->fault_access = access; m->fault_addr = addr; m->fault_size = size;
    m->fault_value = value; m->fault_pc = reg(m, 15);
    m->fault = 1;
    return 0;
}

void machine_stop(machine_t *m, const char *why) {
    snprintf(m->stopped, sizeof m->stopped, "%s", why);
    arm_stop(m->cpu);
}

/* Run in slices; stop on a fault, a hook's stop, a stall or after max_insns. A stall is the
   PC staying inside a small window: after poll_slices if every slice read MMIO (hardware
   polling), or after spin_slices otherwise (memory clears are normal, so that limit is
   long). Limits above STALL_WINDOW are off. */
int machine_run(machine_t *m, uint64_t max_insns, uint32_t slice, uint32_t poll_slices, uint32_t spin_slices) {
    uint32_t pcs[STALL_WINDOW];
    uint8_t polled[STALL_WINDOW];
    int npcs = 0, head = 0;
    uint64_t done = 0;
    uint32_t start = m->start;
    while (done < max_insns) {
        m->fault = 0;
        uint64_t reads0 = m->mmio_reads, writes0 = m->mmio_writes;
        int err = arm_run(m->cpu, start, 0xFFFFFFFF, slice);
        if (err) {
            machine_log(m, 0, "!! CPU error %d at pc=%08x thumb=%d", err, reg(m, 15), (reg(m, REG_CPSR) >> 5) & 1);
            if (m->fault)
                machine_log(m, 0, "   unmapped access type=%d addr=%08x size=%d value=%x",
                            m->fault_access, m->fault_addr, m->fault_size, m->fault_value);
            return RUN_ERROR;
        }
        done += slice;
        m->insns += slice;
        if (m->aitc) {
            service(m);
            start = reg(m, 15) | ((reg(m, REG_CPSR) >> 5) & 1);
        }
        if (m->stopped[0]) {
            machine_log(m, 0, "!! stopped by hook: %s", m->stopped);
            m->start = reg(m, 15) | ((reg(m, REG_CPSR) >> 5) & 1);
            return RUN_HOOK;
        }
        uint32_t pc = reg(m, 15);
        start = pc | ((reg(m, REG_CPSR) >> 5) & 1);
        pcs[head] = pc;
        polled[head] = m->mmio_reads > reads0 && m->mmio_writes == writes0;
        head = (head + 1) % STALL_WINDOW;
        if (npcs < STALL_WINDOW) npcs++;
        if (poll_slices <= STALL_WINDOW && (uint32_t)npcs >= poll_slices) {
            int all = 1, nwin = 0;
            uint32_t win[4];
            for (uint32_t k = 1; k <= poll_slices && all; k++) {
                int i = (head - (int)k + STALL_WINDOW) % STALL_WINDOW;
                all = polled[i];
                uint32_t w = pcs[i] >> 6;
                int seen = 0;
                for (int j = 0; j < nwin; j++) seen |= win[j] == w;
                if (!seen) { if (nwin < 4) win[nwin] = w; nwin++; }
            }
            if (all && nwin <= 3) {
                machine_log(m, 0, "!! poll stall: pc at %08x after %llu insns", pc, (unsigned long long)done);
                m->start = start;
                return RUN_STALL;
            }
        }
        if (spin_slices <= STALL_WINDOW && (uint32_t)npcs >= spin_slices) {
            uint32_t lo = 0xFFFFFFFF, hi = 0;
            for (uint32_t k = 1; k <= spin_slices; k++) {
                uint32_t p = pcs[(head - (int)k + STALL_WINDOW) % STALL_WINDOW];
                if (p < lo) lo = p;
                if (p > hi) hi = p;
            }
            if (hi - lo < 0x100) {
                machine_log(m, 0, "!! spin stall: pc in %08x-%08x after %llu insns", lo, hi, (unsigned long long)done);
                m->start = start;
                return RUN_STALL;
            }
        }
    }
    m->start = start;
    return RUN_BUDGET;
}

void machine_init_run(machine_t *m) {
    arm_hook_add_native(m->cpu, HK_UNMAPPED, 1, 0, on_unmapped, m);
    arm_hook_add_native(m->cpu, HK_INTR, 1, 0, on_interrupt, m);
    static const uint8_t bx_lr[2] = {0x47, 0x70};
    arm_mem_write(m->cpu, CALL_RETURN, bx_lr, 2);
    machine_hook(m, CALL_RETURN, call_returned, 0, 0);
}

/* -- for the host ------------------------------------------------------------------------- */
void machine_set_log(machine_t *m, log_fn fn) { m->log = fn; }
void machine_set_host_hook(machine_t *m, host_hook_fn fn) { m->host_hook = fn; }
void machine_set_host_timer(machine_t *m, host_timer_fn fn) { m->host_timer = fn; }
uint32_t machine_start(machine_t *m) { return m->start; }
void machine_set_start(machine_t *m, uint32_t v) { m->start = v; }
const char *machine_stopped(machine_t *m) { return m->stopped; }
double machine_ticks(machine_t *m) { return m->ticks_per_insn; }
uint64_t machine_irq_count(machine_t *m) { return m->irq_count; }
