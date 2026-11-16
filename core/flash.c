/* Intel-command-set NOR flash (32 MB at 0x10000000, 16-bit bus, 2 MB partitions).

   The flash is ordinary memory, so code and data reads run at full speed. Writes are
   hooked: the store follows the hook, so each written word is recorded with the content it
   should really end up holding (the old data for a command, old AND new for program data)
   and fixed up before the next flash access. While a partition is out of read-array mode a
   read hook answers status / ID / CFI queries.

   Commands seen in the firmware's driver: 0x90 read ID, 0x70 read status, 0x50 clear
   status, 0x60+0x03 set configuration, 0x60+0xD0/0x01 lock/unlock, 0x40/0x10 word program,
   0xE8 buffered program, 0x20+0xD0 block erase, 0xB0/0xD0 suspend/resume, 0xFF read array. */
#include <stdlib.h>
#include <string.h>
#include "machine.h"

#define FLASH_BASE 0x10000000u
#define FLASH_SIZE 0x2000000u
#define PARTITION 0x200000u
#define BLOCK 0x20000u
#define NPARTS (FLASH_SIZE / PARTITION)
#define MANUFACTURER 0x0089
#define DEVICE 0x880F                   /* accepted by the driver's ID table (32 MB part) */
#define READY 0x80

enum { MODE_ARRAY, MODE_STATUS, MODE_ID, MODE_CFI };
enum { CMD_NONE, CMD_PROGRAM, CMD_BUF_COUNT, CMD_BUF_DATA, CMD_BUF_CONFIRM, CMD_ERASE, CMD_CONFIG };

struct flash {
    machine_t *m;
    int mode[NPARTS], cmd, buf_left, read_hook;
    uint32_t status;
    uint64_t writes;                    /* program / erase operations, to tell when to save */
    struct { uint32_t addr; int n; uint8_t b[4]; } pend[4];   /* bytes due after the store */
    int npend;
};

static void pend(flash_t *f, uint32_t addr, const uint8_t *b, int n) {
    int i = 0;
    while (i < f->npend && f->pend[i].addr != addr) i++;
    if (i == f->npend) {
        if (f->npend == 4) return;
        f->npend++;
    }
    f->pend[i].addr = addr;
    f->pend[i].n = n;
    memcpy(f->pend[i].b, b, n);
}

void flash_flush(flash_t *f) {
    for (int i = 0; i < f->npend; i++)
        arm_mem_write(f->m->cpu, f->pend[i].addr, f->pend[i].b, f->pend[i].n);
    f->npend = 0;
}

static int all_array(flash_t *f) {
    for (unsigned i = 0; i < NPARTS; i++)
        if (f->mode[i] != MODE_ARRAY) return 0;
    return 1;
}

int flash_idle(flash_t *f) { return f->cmd == CMD_NONE && all_array(f); }
uint64_t flash_writes(flash_t *f) { return f->writes; }

static int on_read(void *ctx, int access, uint32_t addr, int size, uint32_t unused);

static void arm_read_hook(flash_t *f) {
    if (f->read_hook < 0)
        f->read_hook = arm_hook_add_native(f->m->cpu, HK_READ, FLASH_BASE, FLASH_BASE + FLASH_SIZE - 1, on_read, f);
}

static void maybe_disarm(flash_t *f) {
    if (f->read_hook >= 0 && !f->npend && all_array(f)) {
        arm_hook_del(f->m->cpu, f->read_hook);
        f->read_hook = -1;
    }
}

static unsigned part_of(uint32_t addr) { return (addr - FLASH_BASE) / PARTITION; }

static int on_read(void *ctx, int access, uint32_t addr, int size, uint32_t unused) {
    flash_t *f = ctx;
    flash_flush(f);
    unsigned part = part_of(addr);
    int mode = f->mode[part];
    if (mode != MODE_ARRAY) {
        uint32_t off = (addr - (FLASH_BASE + part * PARTITION)) & ~1u, v;
        if (mode == MODE_STATUS) v = f->status;
        else if (mode == MODE_ID) v = (off & 0xFF) == 0 ? MANUFACTURER : (off & 0xFF) == 2 ? DEVICE : 0;
        else v = (off & 0xFF) == 0x20 ? 'Q' : (off & 0xFF) == 0x22 ? 'R' : (off & 0xFF) == 0x24 ? 'Y' : 0;
        uint8_t now[2], val[2] = {(uint8_t)(v >> 8), (uint8_t)v};
        uint32_t a = addr & ~1u;
        arm_mem_read(f->m->cpu, a, now, 2);
        pend(f, a, now, 2);
        arm_mem_write(f->m->cpu, a, val, 2);
        if (size == 4) {
            arm_mem_read(f->m->cpu, a + 2, now, 2);
            pend(f, a + 2, now, 2);
            arm_mem_write(f->m->cpu, a + 2, val, 2);
        }
    }
    maybe_disarm(f);
    return 0;
}

static uint32_t get_be(const uint8_t *b, int n) {
    uint32_t v = 0;
    for (int i = 0; i < n; i++) v = v << 8 | b[i];
    return v;
}

static void put_be(uint8_t *b, int n, uint32_t v) {
    for (int i = 0; i < n; i++) b[i] = v >> (8 * (n - 1 - i));
}

static int on_write(void *ctx, int access, uint32_t addr, int size, uint32_t value) {
    flash_t *f = ctx;
    flash_flush(f);
    if (size < 4) value &= (1u << (8 * size)) - 1;
    uint8_t orig[4], final[4];
    arm_mem_read(f->m->cpu, addr, orig, size);
    memcpy(final, orig, size);                        /* commands leave the array untouched */
    unsigned part = part_of(addr);

    if (f->cmd == CMD_PROGRAM || f->cmd == CMD_BUF_CONFIRM || f->cmd == CMD_ERASE)
        f->writes++;
    switch (f->cmd) {
    case CMD_PROGRAM:
        put_be(final, size, get_be(orig, size) & value);
        f->cmd = CMD_NONE;
        f->mode[part] = MODE_STATUS;
        break;
    case CMD_BUF_COUNT:
        f->buf_left = (int)(value & 0xFFFF) + 1;
        f->cmd = CMD_BUF_DATA;
        break;
    case CMD_BUF_DATA:
        put_be(final, size, get_be(orig, size) & value);
        if (--f->buf_left <= 0) f->cmd = CMD_BUF_CONFIRM;
        break;
    case CMD_BUF_CONFIRM:                             /* expects 0xD0 */
        f->cmd = CMD_NONE;
        f->mode[part] = MODE_STATUS;
        break;
    case CMD_ERASE:
        f->cmd = CMD_NONE;
        if ((value & 0xFF) == 0xD0) {
            static uint8_t erased[BLOCK];
            if (!erased[0]) memset(erased, 0xFF, BLOCK);
            arm_mem_write(f->m->cpu, addr & ~(BLOCK - 1), erased, BLOCK);
            arm_mem_read(f->m->cpu, addr, final, size);
        }
        f->mode[part] = MODE_STATUS;
        break;
    case CMD_CONFIG:                                  /* lock / unlock / RCR: no effect */
        f->cmd = CMD_NONE;
        f->mode[part] = (value & 0xFF) == 0x03 ? MODE_ARRAY : MODE_STATUS;
        break;
    default:
        switch (value & 0xFF) {
        case 0xFF: f->mode[part] = MODE_ARRAY; break;
        case 0x70: f->mode[part] = MODE_STATUS; break;
        case 0x50: f->status = READY; break;
        case 0x90: f->mode[part] = MODE_ID; break;
        case 0x98: f->mode[part] = MODE_CFI; break;
        case 0x40: case 0x10: f->cmd = CMD_PROGRAM; break;
        case 0xE8: f->cmd = CMD_BUF_COUNT; f->mode[part] = MODE_STATUS; break;
        case 0x20: f->cmd = CMD_ERASE; break;
        case 0x60: f->cmd = CMD_CONFIG; break;
        case 0xB0: case 0xD0: f->mode[part] = MODE_STATUS; break;
        default: break;                               /* unknown command: ignored */
        }
    }
    pend(f, addr, final, size);
    arm_read_hook(f);
    return 0;
}

flash_t *flash_new(machine_t *m) {
    flash_t *f = calloc(1, sizeof *f);
    f->m = m;
    f->status = READY;
    f->read_hook = -1;
    arm_hook_add_native(m->cpu, HK_WRITE, FLASH_BASE, FLASH_BASE + FLASH_SIZE - 1, on_write, f);
    return f;
}
