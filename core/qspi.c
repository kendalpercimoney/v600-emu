/* The queued SPI controller and the power-management IC behind it.

   QSPI at 0x24851000:
     +0x006 start: value = 0x20 | hw channel; the hardware clears 0x20 once it takes the job
     +0x010 bits 0 / 8 = queue active (self-clearing)
     +0x012 / +0x014 done bits for hw channels 0-15 / 16-31 (1 = idle / complete, w1c)
     +0x400 + 4*q command RAM, +0x480.. data RAM
   The command RAM encoding is not decoded; each transfer is resolved through the driver's
   own queue table (pointer at [[0x1232E5F8]], 20-byte entries: device descriptor, TX
   address, RX address, [hw channel, ...], end address). Devices, by descriptor: the power
   IC (0x10800562) and the graphics chip's serial interface (0x1080058C). */
#include <stdlib.h>
#include <string.h>
#include "machine.h"

#define QSPI_BASE 0x24851000u
#define TABLE_PTR 0x1232E5F8u
#define DEV_PMIC 0x10800562u
#define DEV_ATI 0x1080058Cu
#define DEV_LED 0x1080059Au

/* ---- power IC (PCAP2: 32 x 25-bit registers) ---------------------------------------------
   SPI word: bit 31 = write, 30..26 = register, 24..0 = data; a read returns the register.
   Register 0 is the interrupt status (w1c). ADC (Linux ezx-pcap.h): writing ASC (bit 22) to
   reg 10 (ADR) converts the 7 channels of the bank selected by reg 9 (ADC) bit 2; ADA1 (reg 9
   bits 6:4) / ADA2 (9:7) pick the results in ADR ADD1 (9:0) / ADD2 (19:10); completion sets
   ISR bit 0. The power key (the red End key) is ISR bit 7 (ONOFF), latched on each change;
   PSTAT holds the line levels, active low: bit 7 = power key, bit 8 = ONOFF2. Released =
   both set; the debouncer (0x1088EA3A) reads bit 7 clear and bit 8 set as "power key held",
   which turns the phone off after ~2 s. Bit 12 is the headset detect, low = a headset is
   plugged in (the phone then answers calls by itself and shows the headset icon): idle, no
   headset. The PCAP's interrupt pin is edge port INT1. */
enum { ISR = 0, MSR = 1, PSTAT = 2, AUX_VREG = 7, ADC = 9, ADR = 10, TOD = 14, TODA = 15, DAY = 16, DAYA = 17 };
#define VIBRATOR (1u << 19)             /* AUX_VREG: the vibrator motor's supply, on / off */
#define ONOFF (1u << 7)
#define PSTAT_IDLE ((1u << 7) | (1u << 8) | (1u << 12))
#define ASC (1u << 22)
#define ADCDONE 1u
/* 10-bit readings for bank 0: coin cell, battery, B+, charger (none), battery thermistor
   (room temperature), charger ID (none), AD6. Bank 1 reads mid-scale. Calibration unknown:
   values chosen for a healthy battery. */
static const uint32_t ADC_BANK0[7] = {0x300, 0x2C0, 0x2C0, 0x000, 0x200, 0x3FF, 0x200};
static const uint32_t ADC_BANK1[7] = {0x200, 0x200, 0x200, 0x200, 0x200, 0x200, 0x200};

/* Real-time clock (Linux rtc-pcap.c): TOD = seconds of the day (17 bits), DAY = days since
   1970-01-01 (15 bits); TODA / DAYA the alarm. ISR bit 2 every second, bit 5 at the alarm.
   It runs only once the front end sets the time (pmic_start_clock, again to re-sync; a time
   set on the phone is kept as an offset): until then it reads 0, which keeps headless runs
   (the golden traces) the same from run to run. */
#define TOD_MASK 0x1FFFFu
#define DAY_MASK 0x7FFFu
#define IRQ_1HZ (1u << 2)
#define IRQ_TODA (1u << 5)

struct pmic {
    uint32_t regs[32], sense, adc[7];
    edgeport_t *int_pin;
    machine_t *m;                       /* the clock: time = base + instructions / ips */
    double base, ips;
    double offset;                      /* time the user set on the phone, vs the front end's */
    int64_t last_second;
};

static int int_level(pmic_t *p) { return (p->regs[ISR] & ~p->regs[MSR] & 0x1FFFFFF) != 0; }

static void raise_int(pmic_t *p) { if (p->int_pin) edgeport_set_pin(p->int_pin, 1, int_level(p)); }

void pmic_power_key(pmic_t *p, int down) {
    uint32_t old = p->sense;
    p->sense = down ? p->sense & ~ONOFF : p->sense | ONOFF;
    if (p->sense != old) {
        p->regs[ISR] |= ONOFF;
        raise_int(p);
    }
}

static int64_t clock_now(pmic_t *p) { return (int64_t)(p->base + (double)p->m->insns / p->ips); }

/* Write the clock registers' time fields from the running clock. */
static void clock_regs(pmic_t *p) {
    int64_t t = clock_now(p);
    p->regs[TOD] = (p->regs[TOD] & ~TOD_MASK) | (uint32_t)(t % 86400);
    p->regs[DAY] = (p->regs[DAY] & ~DAY_MASK) | ((uint32_t)(t / 86400) & DAY_MASK);
}

/* The firmware set the time: carry on from there. */
static void clock_set(pmic_t *p, int64_t t) {
    double d = (double)(t - clock_now(p));
    p->base += d;
    p->offset += d;
}

/* Machine timer: the second ticks (and the alarm). */
static void clock_update(void *ctx) {
    pmic_t *p = ctx;
    int64_t t = clock_now(p);
    if (t == p->last_second) return;
    p->last_second = t;
    p->regs[ISR] |= IRQ_1HZ;
    if (t % 86400 == (p->regs[TODA] & TOD_MASK) && (t / 86400 & DAY_MASK) == (p->regs[DAYA] & DAY_MASK))
        p->regs[ISR] |= IRQ_TODA;
    raise_int(p);
}

static double clock_next(void *ctx) {
    pmic_t *p = ctx;
    double next_insn = ((double)(clock_now(p) + 1) - p->base) * p->ips;
    return (next_insn + 1) * p->m->ticks_per_insn;
}

void pmic_start_clock(pmic_t *p, machine_t *m, uint32_t ips, int64_t now) {
    int first = !p->m;
    p->m = m;
    p->ips = ips;
    p->base = (double)now + p->offset - (double)m->insns / ips;
    p->last_second = clock_now(p);
    if (first) machine_add_timer(m, clock_update, clock_next, p);
}

static uint32_t adr(pmic_t *p) {
    uint32_t ctl = p->regs[ADC], a1 = (ctl >> 4) & 7, a2 = (ctl >> 7) & 7;
    uint32_t v1 = a1 < 7 ? p->adc[a1] : 0, v2 = a2 < 7 ? p->adc[a2] : 0;
    return (p->regs[ADR] & ~0xFFFFFu & ~ASC) | v1 | v2 << 10;
}

static uint32_t pmic_word(pmic_t *p, uint32_t word) {
    uint32_t reg = (word >> 26) & 0x1F, data = word & 0x1FFFFFF;
    if (!(word & 0x80000000u)) {
        if (p->m && (reg == TOD || reg == DAY)) clock_regs(p);
        return p->regs[reg] | (reg == PSTAT ? p->sense : 0);
    }
    if (reg == ISR) p->regs[reg] &= ~data;
    else p->regs[reg] = data;
    if (p->m && (reg == TOD || reg == DAY)) {
        int64_t t = clock_now(p);
        if (reg == TOD) clock_set(p, t - t % 86400 + (data & TOD_MASK));
        else clock_set(p, (int64_t)(data & DAY_MASK) * 86400 + t % 86400);
    }
    if (reg == ADR && (data & ASC)) {                    /* start conversion: instant */
        memcpy(p->adc, p->regs[ADC] & 0x4 ? ADC_BANK1 : ADC_BANK0, sizeof p->adc);
        p->regs[ISR] |= ADCDONE;
    }
    if (reg == ADC || reg == ADR) p->regs[ADR] = adr(p);
    raise_int(p);
    return 0;
}

pmic_t *pmic_new(void) {
    pmic_t *p = calloc(1, sizeof *p);
    p->sense = PSTAT_IDLE;
    memcpy(p->adc, ADC_BANK0, sizeof p->adc);
    return p;
}

void pmic_set_reg(pmic_t *p, int reg, uint32_t v) { p->regs[reg & 31] = v; }
void pmic_connect(pmic_t *p, edgeport_t *int_pin) { p->int_pin = int_pin; }
int pmic_int_level(pmic_t *p) { return int_level(p); }
int pmic_vibrator(pmic_t *p) { return (p->regs[AUX_VREG] & VIBRATOR) != 0; }

/* The phone's clock (local time, s since 1970), or -1 while it is not running. */
int64_t pmic_time(pmic_t *p) { return p->m ? clock_now(p) : -1; }

/* ---- LED driver (QSPI channel 4): the ring and event lights ------------------------------
   Writes only: (register, value) byte pairs, a 0 register ends them. 0x09 / 0x0B / 0x0D are
   the red / green / blue levels (0-15; the patterns fade them), 0x01 bits 5 / 4 / 3 switch
   those channels on (the power-up light show steps through them; Solar uses red + green),
   0x17 = 0x60 while any is lit. 0x13 / 0x15 (100 / 150 at start) are not understood. */
static uint32_t lights(const uint8_t *r) {
    uint32_t red = r[0x01] & 0x20 ? (r[0x09] & 15) * 17u : 0, green = r[0x01] & 0x10 ? (r[0x0B] & 15) * 17u : 0;
    uint32_t blue = r[0x01] & 0x08 ? (r[0x0D] & 15) * 17u : 0;
    return red << 16 | green << 8 | blue;
}

/* ---- QSPI ---------------------------------------------------------------------------- */
struct qspi {
    device_t d;
    uint8_t mem[0x1000];
    uint32_t done;
    pmic_t *pmic;
    ati_t *ati;
    uint8_t led[128];
};

uint32_t qspi_lights(qspi_t *q) { return lights(q->led); }

static uint32_t read_word(machine_t *m, uint32_t a, int *ok) {
    uint8_t b[4];
    if (arm_mem_read(m->cpu, a, b, 4)) { *ok = 0; return 0; }
    return (uint32_t)b[0] << 24 | b[1] << 16 | b[2] << 8 | b[3];
}

static void transfer(qspi_t *q, int ch) {
    machine_t *m = q->d.m;
    int ok = 1;
    uint32_t table = read_word(m, read_word(m, TABLE_PTR, &ok), &ok);
    if (!ok) return;
    for (int i = 0; i < 32; i++) {
        uint32_t e[5];
        for (int j = 0; j < 5; j++) e[j] = read_word(m, table + 0x14 * i + 4 * j, &ok);
        if (!ok || (e[0] == 0 && e[1] == 0)) return;
        if ((int)(e[3] >> 24) != ch || e[1] < QSPI_BASE || e[1] >= QSPI_BASE + 0x1000) continue;
        int64_t tx = e[1] - QSPI_BASE, rx = (int64_t)e[2] - QSPI_BASE, end = (int64_t)e[4] - QSPI_BASE;
        int64_t n = rx > tx ? rx - tx : end - tx;
        if (n < 0) n = 0;
        if (tx + n > 0x1000) n = 0x1000 - tx;
        uint8_t data[0x1000], reply[0x1000 + 64];
        memcpy(data, q->mem + tx, n);
        memset(reply, 0, n + 64);
        if (e[0] == DEV_PMIC && q->pmic) {
            uint32_t w = 0;
            for (int k = 0; k < 4; k++) w = w << 8 | (k < n ? data[k] : 0);
            if (n == 0) w = 0;
            else if (n < 4) w >>= 8 * (4 - n);              /* fewer bytes: a shorter number */
            uint32_t r = pmic_word(q->pmic, w);
            for (int k = 0; k < 4; k++) reply[k] = r >> (8 * (3 - k));
        } else if (e[0] == DEV_ATI && q->ati) {
            ati_serial_transfer(q->ati, data, (int)n, reply);
        } else if (e[0] == DEV_LED) {
            for (int k = 0; k + 1 < n && data[k]; k += 2) q->led[data[k] & 0x7F] = data[k + 1];
        }
        int64_t at = rx != tx ? rx : tx;
        if (at >= 0 && at + n <= 0x1000) memcpy(q->mem + at, reply, n);
        return;
    }
}

static uint32_t qspi_read(device_t *d, uint32_t off, int size) {
    qspi_t *q = (qspi_t *)d;
    if (off == 0x012) return q->done & 0xFFFF;
    if (off == 0x014) return (q->done >> 16) & 0xFFFF;
    uint32_t v = 0;
    for (int i = 0; i < size && off + i < 0x1000; i++) v = v << 8 | q->mem[off + i];
    return v;
}

static void qspi_write(device_t *d, uint32_t off, int size, uint32_t v) {
    qspi_t *q = (qspi_t *)d;
    if (off == 0x012) { q->done &= ~v; return; }
    if (off == 0x014) { q->done &= ~(v << 16); return; }
    for (int i = 0; i < size && off + i < 0x1000; i++) q->mem[off + i] = v >> (8 * (size - 1 - i));
    if (off >= 0x10 && off < 0x14) {                     /* queue-active bits self-clear */
        q->mem[0x10] &= ~1; q->mem[0x11] &= ~1;
    }
    if (off == 0x006 && (v & 0x20)) {
        int ch = v & 0x1F;
        transfer(q, ch);
        q->done |= 1u << ch;
        uint32_t s = v & ~0x20u;                         /* the hardware clears the start bit */
        q->mem[6] = s >> 8; q->mem[7] = s;
    }
}

qspi_t *qspi_new(machine_t *m, pmic_t *pmic, ati_t *ati) {
    qspi_t *q = calloc(1, sizeof *q);
    q->d = (device_t){qspi_read, qspi_write, m};
    q->done = 0xFFFFFFFF;                                /* idle after reset */
    q->pmic = pmic;
    q->ati = ati;
    return q;
}
