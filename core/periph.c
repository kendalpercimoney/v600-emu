/* Simple peripherals: free-running counter, register file, keypad, edge port, one-wire. */
#include <stdlib.h>
#include <string.h>
#include "machine.h"

/* -- RTR free-running counter at 0x2484A000 ------------------------------------------------
   +0 MSB (seconds), +4 LSB (bits 31:17 = 15-bit fraction): a 32.768 kHz count in bits 63:17,
   driven by the virtual clock (instructions executed, 52 MHz assumed). */
static uint32_t counter_read(device_t *d, uint32_t off, int size) {
    uint64_t v = machine_counter(d->m);
    return off == 0 ? (uint32_t)(v >> 32) : (uint32_t)v;
}

static void ignore_write(device_t *d, uint32_t off, int size, uint32_t v) {}

counter_t *counter_new(machine_t *m) {
    counter_t *c = calloc(1, sizeof *c);
    c->d = (device_t){counter_read, ignore_write, m};
    return c;
}

/* -- register file: reads return what was written; one word may have self-clearing bits
   (hardware that completes an operation instantly, e.g. a start / busy bit) -------------- */
static uint32_t regs_read(device_t *d, uint32_t off, int size) {
    regs_t *r = (regs_t *)d;
    uint32_t v = 0;
    for (int i = 0; i < size && off + i < r->size; i++) v = v << 8 | r->mem[off + i];
    return v;
}

static void regs_write(device_t *d, uint32_t off, int size, uint32_t v) {
    regs_t *r = (regs_t *)d;
    for (int i = 0; i < size && off + i < r->size; i++) r->mem[off + i] = v >> (8 * (size - 1 - i));
    if (r->clear_mask && off >= r->clear_word && off < r->clear_word + 4) {
        uint8_t *p = r->mem + r->clear_word;
        uint32_t w = ((uint32_t)p[0] << 24 | p[1] << 16 | p[2] << 8 | p[3]) & ~r->clear_mask;
        p[0] = w >> 24; p[1] = w >> 16; p[2] = w >> 8; p[3] = w;
    }
}

regs_t *regs_new(machine_t *m, uint32_t size, uint32_t clear_word, uint32_t clear_mask) {
    regs_t *r = calloc(1, sizeof *r);
    r->d = (device_t){regs_read, regs_write, m};
    r->mem = calloc(1, size);
    r->size = size;
    r->clear_word = clear_word;
    r->clear_mask = clear_mask;
    return r;
}

/* -- Watchdog at 0x24849000, 16-bit registers: +0 WCR, +2 WSR (the service sequence) --------
   WCR bit 2 WDE (enabled), bit 5 WDA, active low: drives the watchdog pin, which on this
   board cuts the power. The firmware's power-down ends by writing 0x7E56 (WDA low); at
   boot it writes 0x7E76. Kept as plain storage otherwise (the timeout is not modelled). */
#define WDE (1u << 2)
#define WDA (1u << 5)
typedef struct { device_t d; uint8_t r[4]; } wdog_t;

static uint32_t wdog_read(device_t *d, uint32_t off, int size) {
    wdog_t *w = (wdog_t *)d;
    uint32_t v = 0;
    for (int i = 0; i < size; i++) v = v << 8 | (off + i < 4 ? w->r[off + i] : 0);
    return v;
}

static void wdog_write(device_t *d, uint32_t off, int size, uint32_t v) {
    wdog_t *w = (wdog_t *)d;
    for (int i = 0; i < size; i++) if (off + i < 4) w->r[off + i] = v >> (8 * (size - 1 - i));
    uint32_t wcr = (uint32_t)w->r[0] << 8 | w->r[1];
    if (off < 2 && (wcr & WDE) && !(wcr & WDA)) {
        d->m->powered_off = 1;
        machine_stop(d->m, "power off");
    }
}

device_t *wdog_new(machine_t *m) {
    wdog_t *w = calloc(1, sizeof *w);
    w->d = (device_t){wdog_read, wdog_write, m};
    return &w->d;
}

/* -- KPP keypad port at 0x2484E000 (Neptune LTE spec ch. 58), 16-bit registers -------------
   +0 KPCR (KCO 15:8, KRE row enable 7:0), +2 KPSR (bit 0 KPKD key detect, w1c), +4 KDDR
   (column direction 15:8), +6 KPDR (columns 15:8, rows 7:0). Lines are pulled-up inputs; a
   key pulls its row low and for most keys one of the two column lines, so KPDR reads back
   the SDK key code (P2K-ELF-SDK hw_keypad.h). KPKD (a new press) raises AITC source 9.
   Key order: v600/periph.py Keypad.CODES. The red key is the power key on the PCAP. */
#define KEYPAD_SOURCE 9
static const uint16_t KEY_CODES[] = {
    0xFE7F, 0xFEF7, 0xFD7F, 0xFDF7, 0xFF7E, 0xFFF6, 0xFFF3, 0xFFF5, 0xFFEB,     /* 1-9 */
    0xFF7D, 0xFEBF, 0xFDBF,                                                     /* * 0 # */
    0xFEDF, 0xFFBB, 0xFFBD, 0xFF7B,               /* green soft_right soft_left menu */
    0xFDDF, 0xFFDE, 0xFFDD, 0xFFDB,               /* vol_up vol_down smart voice */
    0xFFEE, 0xFDEF, 0xFEEF, 0xFFED, 0xFFBE,       /* right left up down ok */
};
#define NKEYS (int)(sizeof KEY_CODES / sizeof KEY_CODES[0])

static uint32_t keypad_lines(keypad_t *k) {
    uint32_t v = 0xFFFF;
    for (int i = 0; i < NKEYS; i++)
        if (k->pressed >> i & 1) v &= KEY_CODES[i] | 0xFC00;
    uint32_t out = k->kddr & 0xFF00;
    return (v & ~out) | ((k->cols << 8) & out);
}

static void keypad_update(keypad_t *k) {
    int down = (keypad_lines(k) & 0xFF & k->kpcr & 0xFF) != (k->kpcr & 0xFF);
    if (down && !k->was_down) k->kpsr |= 1;              /* latched on a new press only */
    k->was_down = down;
    aitc_set_line(k->d.m->aitc, KEYPAD_SOURCE, k->kpsr & 1);
}

void keypad_press(keypad_t *k, int key, int down) {
    if (key < 0 || key >= NKEYS) return;
    if (down) k->pressed |= 1u << key;
    else k->pressed &= ~(1u << key);
    keypad_update(k);
}

static uint32_t keypad_reg(keypad_t *k, uint32_t off) {
    switch (off & ~1u) {
    case 0: return k->kpcr;
    case 2: return k->kpsr;
    case 4: return k->kddr;
    case 6: return keypad_lines(k);
    }
    return 0;
}

static uint32_t keypad_read(device_t *d, uint32_t off, int size) {
    keypad_t *k = (keypad_t *)d;
    keypad_update(k);
    uint32_t v = keypad_reg(k, off);
    if (size == 1) return off % 2 == 0 ? (v >> 8) & 0xFF : v & 0xFF;
    if (size == 4) {
        uint32_t next = off == 0 ? k->kpsr : off == 4 ? keypad_lines(k) : 0;
        return v << 16 | next;
    }
    return v;
}

static void keypad_write(device_t *d, uint32_t off, int size, uint32_t v) {
    keypad_t *k = (keypad_t *)d;
    if (size == 4) {
        keypad_write(d, off, 2, v >> 16);
        keypad_write(d, off + 2, 2, v & 0xFFFF);
        return;
    }
    if (size == 1) {                         /* byte access: merge into the 16-bit register */
        uint32_t cur = keypad_read(d, off & ~1u, 2);
        v = off % 2 == 0 ? (cur & 0x00FF) | v << 8 : (cur & 0xFF00) | v;
        off &= ~1u;
    }
    if (off == 0) k->kpcr = v;
    else if (off == 2) k->kpsr &= ~v & 1;
    else if (off == 4) k->kddr = v & 0xFF00;
    else if (off == 6) k->cols = v >> 8;
}

keypad_t *keypad_new(machine_t *m) {
    keypad_t *k = calloc(1, sizeof *k);
    k->d = (device_t){keypad_read, keypad_write, m};
    k->cols = 0xFF;
    return k;
}

/* -- INT edge port at 0x2484C000 (pins INT0-7; spec ch. 69), 16-bit -------------------------
   +0 EPPAR (2 bits per pin: 00 level, 01 rising, 10 falling, 11 both), +2 EPDDR (1 = read
   EPDR instead of the pin), +4 EPDR, +6 EPFR (edge flags, w1c). Level pins request while
   low. INT0 = HS_INT (flip / headset detect), INT1 = the power IC's interrupt. */
static const int EDGE_SOURCES[8] = {60, 54, 39, 35, 30, 22, 13, 5};

static void edgeport_update(edgeport_t *e) {
    for (int n = 0; n < 8; n++) {
        int mode = e->eppar >> (2 * n) & 3;
        int req = mode == 0 ? !(e->pins >> n & 1) : (int)(e->epfr >> n & 1);
        aitc_set_line(e->d.m->aitc, EDGE_SOURCES[n], req);
    }
}

void edgeport_set_pin(edgeport_t *e, int n, int level) {
    level = !!level;
    int old = e->pins >> n & 1;
    e->pins = (e->pins & ~(1u << n)) | (uint32_t)level << n;
    int mode = e->eppar >> (2 * n) & 3;
    if (old != level && (mode == 3 || (mode == 1 && level) || (mode == 2 && !level)))
        e->epfr |= 1u << n;
    edgeport_update(e);
}

static uint32_t edgeport_read(device_t *d, uint32_t off, int size) {
    edgeport_t *e = (edgeport_t *)d;
    switch (off) {
    case 0: return e->eppar;
    case 2: return e->epddr;
    case 4: return (e->epdr & e->epddr) | (e->pins & ~e->epddr & 0xFF);
    case 6: return e->epfr;
    }
    return 0;
}

static void edgeport_write(device_t *d, uint32_t off, int size, uint32_t v) {
    edgeport_t *e = (edgeport_t *)d;
    if (off == 0) e->eppar = v & 0xFFFF;
    else if (off == 2) e->epddr = v & 0xFF;
    else if (off == 4) e->epdr = v & 0xFF;
    else if (off == 6) e->epfr &= ~v;
    edgeport_update(e);
}

edgeport_t *edgeport_new(machine_t *m) {
    edgeport_t *e = calloc(1, sizeof *e);
    e->d = (device_t){edgeport_read, edgeport_write, m};
    e->pins = 0xFF;
    return e;
}

/* -- one-wire master at 0x24856000 (spec ch. 66), 16-bit --------------------------------------
   +0 CONTROL (bit 7 RPP reset + presence, 6 PST presence status, 5 WR0, 4 WR1/RD, 3 RDST),
   +2 TIME_DIVIDER, +4 RESET (bit 0). Operations complete instantly. The slave is the
   battery's DS2502 (ds2502.c), or none. */
static uint32_t onewire_read(device_t *d, uint32_t off, int size) {
    onewire_t *o = (onewire_t *)d;
    return off == 0 ? o->ctrl : off == 2 ? o->div : 0;
}

static void onewire_write(device_t *d, uint32_t off, int size, uint32_t v) {
    onewire_t *o = (onewire_t *)d;
    if (off == 2) {
        o->div = v;
    } else if (off == 4 && (v & 1)) {
        o->ctrl = 0;
    } else if (off == 0) {
        uint32_t ctrl = o->ctrl & 0x48;
        if (v & 0x80) {
            int present = o->device && ds2502_reset(o->device);
            ctrl = (ctrl & ~0x40u) | (present ? 0x40 : 0);
        }
        if ((v & 0x20) && o->device) ds2502_bit(o->device, 0);
        if (v & 0x10) {
            int b = o->device ? ds2502_bit(o->device, 1) : 1;
            ctrl = (ctrl & ~0x08u) | (b ? 0x08 : 0);
        }
        o->ctrl = ctrl;
    }
}

onewire_t *onewire_new(machine_t *m, ds2502_t *dev) {
    onewire_t *o = calloc(1, sizeof *o);
    o->d = (device_t){onewire_read, onewire_write, m};
    o->device = dev;
    return o;
}
