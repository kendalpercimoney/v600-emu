/* DMA unit at 0x24854000 feeding the graphics chip's host write port.

   +0x00/+0x02 source address (high / low), +0x04/+0x06 byte count (bit 0 of +0x04 = bit 16),
   +0x18 control / status: low bits = command (1 = start), bit 2 busy, bit 6 done (w1c,
   raises AITC source 26). +0x1C transaction: bit 8 = address phase (the first word of the
   next transfer is the target address in the chip's address space); without it, data
   continues from the current address. Words are big-endian in ARM memory. Transfers
   complete at once. */
#include <stdlib.h>
#include <string.h>
#include "machine.h"

#define DMA_SOURCE 26

struct dma {
    device_t d;
    uint8_t r[0x40];
    uint32_t addr;
    ati_t *ati;
};

static uint32_t get16(const uint8_t *r, uint32_t off) { return (uint32_t)r[off] << 8 | r[off + 1]; }
static void set16(uint8_t *r, uint32_t off, uint32_t v) { r[off] = v >> 8; r[off + 1] = v; }

static void run(dma_t *t) {
    uint32_t src = get16(t->r, 0x00) << 16 | get16(t->r, 0x02);
    uint32_t n = (get16(t->r, 0x04) & 1) << 16 | get16(t->r, 0x06);
    uint8_t *data = malloc(n + 4);
    if (arm_mem_read(t->d.m->cpu, src, data, n)) n = 0;
    uint8_t *p = data;
    if ((get16(t->r, 0x1C) & 0x100) && n >= 4) {
        t->addr = (uint32_t)p[0] << 24 | p[1] << 16 | p[2] << 8 | p[3];
        p += 4; n -= 4;
        set16(t->r, 0x1C, get16(t->r, 0x1C) & ~0x100u);
    }
    /* Image rows are often an odd number of 16-bit pixels: the trailing halfword must
       arrive too, and the next transfer continues right after it (dropping it lost the last
       pixel of every row and shifted the rows that followed). */
    uint32_t whole = n & ~3u;
    if (whole) {
        uint32_t *words = malloc(whole);
        for (uint32_t i = 0; i < whole / 4; i++)
            words[i] = (uint32_t)p[4 * i] << 24 | p[4 * i + 1] << 16 | p[4 * i + 2] << 8 | p[4 * i + 3];
        ati_write_block(t->ati, t->addr, words, whole / 4);
        free(words);
    }
    if (n > whole) ati_write_tail(t->ati, t->addr + whole, p + whole, n - whole);
    t->addr += n;
    set16(t->r, 0x18, (get16(t->r, 0x18) & ~0x3u) | 0x40);   /* idle + done */
    free(data);
}

static uint32_t dma_read(device_t *d, uint32_t off, int size) {
    dma_t *t = (dma_t *)d;
    uint32_t v = 0;
    for (int i = 0; i < size && off + i < sizeof t->r; i++) v = v << 8 | t->r[off + i];
    return v;
}

static void dma_write(device_t *d, uint32_t off, int size, uint32_t v) {
    dma_t *t = (dma_t *)d;
    if (off == 0x18) {
        uint32_t old = get16(t->r, 0x18);
        if (v & 0x40) { v &= ~0x40u; old &= ~0x40u; }  /* done flag: write 1 to clear */
        uint32_t now = (v & ~0x40u) | (old & 0x40);
        set16(t->r, 0x18, now);
        if ((now & 3) == 1) run(t);                      /* start */
        aitc_set_line(d->m->aitc, DMA_SOURCE, (get16(t->r, 0x18) & 0x40) != 0);
        return;
    }
    for (int i = 0; i < size && off + i < sizeof t->r; i++) t->r[off + i] = v >> (8 * (size - 1 - i));
}

dma_t *dma_new(machine_t *m, ati_t *ati) {
    dma_t *t = calloc(1, sizeof *t);
    t->d = (device_t){dma_read, dma_write, m};
    t->ati = ati;
    return t;
}
