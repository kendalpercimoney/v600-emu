/* The MMIO bus. MMIO is plain memory plus a read and a write hook (as on Unicorn): a read
   puts the model's value in memory just before the load, unmodelled registers read back
   what was last written. "Plain" words are left out (no model, not counted). */
#include <stdlib.h>
#include "machine.h"

static void note(machine_t *m, char kind, uint32_t addr, int size, uint32_t value) {
    trace_t *t = &m->trace[m->trace_pos++ % TRACE_LEN];
    t->pc = arm_reg_read(m->cpu, 15);
    t->kind = kind; t->addr = addr; t->size = size; t->value = value;
}

static int is_plain(machine_t *m, uint32_t a) {
    for (int i = 0; i < m->nplain; i++)
        if (a >= m->plain[i].begin && a < m->plain[i].end) return 1;
    return 0;
}

static int route(machine_t *m, uint32_t a) {
    uint32_t w = a & ~3u;
    for (int i = 0; i < m->ndevices; i++)
        if (w - m->devices[i].base < m->devices[i].size) return i;
    return -1;
}

static int on_read(void *ctx, int access, uint32_t a, int size, uint32_t unused) {
    machine_t *m = ctx;
    if (is_plain(m, a)) return 0;
    uint32_t mask = size == 4 ? 0xFFFFFFFFu : (1u << (8 * size)) - 1, v;
    int i = route(m, a);
    if (i >= 0) {
        uint32_t off = a - m->devices[i].base;
        device_t *d = m->devices[i].dev;
        v = (d ? d->read(d, off, size) : m->host(m->devices[i].host_id, 0, off, size, 0)) & mask;
        uint8_t b[4];
        for (int k = 0; k < size; k++) b[k] = v >> (8 * (size - 1 - k));
        arm_mem_write(m->cpu, a, b, size);
    } else {
        uint8_t b[4] = {0};
        arm_mem_read(m->cpu, a, b, size);
        v = 0;
        for (int k = 0; k < size; k++) v = v << 8 | b[k];
    }
    m->mmio_reads++;
    note(m, 'R', a, size, v);
    return 0;
}

static int on_write(void *ctx, int access, uint32_t a, int size, uint32_t value) {
    machine_t *m = ctx;
    if (is_plain(m, a)) return 0;
    if (size < 4) value &= (1u << (8 * size)) - 1;
    if (a != 0x24849002) m->mmio_writes++;               /* watchdog service */
    int i = route(m, a);
    if (i >= 0) {
        uint32_t off = a - m->devices[i].base;
        device_t *d = m->devices[i].dev;
        if (d) d->write(d, off, size, value);
        else m->host(m->devices[i].host_id, 1, off, size, value);
    }
    note(m, 'W', a, size, value);
    return 0;
}

machine_t *machine_new(arm_t *cpu, double ticks_per_insn) {
    machine_t *m = calloc(1, sizeof *m);
    m->cpu = cpu;
    m->ticks_per_insn = ticks_per_insn;
    arm_hook_add_native(cpu, HK_READ, MMIO_BASE, MMIO_BASE + MMIO_SIZE - 1, on_read, m);
    arm_hook_add_native(cpu, HK_WRITE, MMIO_BASE, MMIO_BASE + MMIO_SIZE - 1, on_write, m);
    return m;
}

void machine_free(machine_t *m) { free(m); }
void machine_set_host(machine_t *m, host_device_fn fn) { m->host = fn; }

void machine_attach(machine_t *m, uint32_t base, uint32_t size, device_t *dev) {
    if (m->ndevices == MAX_DEVICES) return;
    m->devices[m->ndevices].base = base;
    m->devices[m->ndevices].size = size;
    m->devices[m->ndevices].dev = dev;
    m->devices[m->ndevices].host_id = -1;
    m->ndevices++;
}

void machine_attach_host(machine_t *m, uint32_t base, uint32_t size, int host_id) {
    machine_attach(m, base, size, NULL);
    if (m->ndevices) m->devices[m->ndevices - 1].host_id = host_id;
}

void machine_plain(machine_t *m, uint32_t addr, uint32_t size) {
    if (m->nplain == MAX_PLAIN) return;
    m->plain[m->nplain].begin = addr;
    m->plain[m->nplain].end = addr + size;
    m->nplain++;
}

uint64_t machine_counter(machine_t *m) {
    return (uint64_t)((double)m->insns * m->ticks_per_insn);
}

/* -- for the host ---------------------------------------------------------------------- */
uint64_t machine_insns(machine_t *m) { return m->insns; }
void     machine_set_insns(machine_t *m, uint64_t v) { m->insns = v; }
void     machine_set_ticks(machine_t *m, double k) { m->ticks_per_insn = k; }
uint64_t machine_mmio_reads(machine_t *m) { return m->mmio_reads; }
uint64_t machine_mmio_writes(machine_t *m) { return m->mmio_writes; }

/* The last MMIO accesses, oldest first; returns how many. */
int machine_trace(machine_t *m, trace_t *out) {
    int n = m->trace_pos < TRACE_LEN ? (int)m->trace_pos : TRACE_LEN;
    for (int i = 0; i < n; i++) out[i] = m->trace[(m->trace_pos - n + i) % TRACE_LEN];
    return n;
}
