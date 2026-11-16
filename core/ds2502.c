/* Dallas DS2502 1-Kbit add-only memory: the battery ID chip on the one-wire bus.

   Bit-level slave: the master calls ds2502_reset() and ds2502_bit(v) (v = 1 also reads).
   ROM commands: 0x33 read ROM, 0xCC skip ROM, 0x55 match ROM, 0xF0 search ROM. Memory
   commands: 0xF0 read memory (address lo, hi; then CRC8, then data to the end), 0xC3 read
   data + 8-bit CRC per 32-byte page, 0xAA read status. CRC: Dallas/Maxim CRC8.
   Motorola batteries carry the UniqueWare part (family 0x89); the battery check
   (0x10B1D572) also wants ROM byte 6 = 0x50 and byte 5 bits 7:4 = 0. */
#include <stdlib.h>
#include <string.h>
#include "machine.h"

enum { ST_ROM, ST_SEARCH, ST_MATCH, ST_MEM, ST_ADDR, ST_DATA, ST_IDLE };

static uint8_t crc8(const uint8_t *p, int n) {
    uint8_t crc = 0;
    for (int i = 0; i < n; i++) {
        uint8_t b = p[i];
        for (int k = 0; k < 8; k++) {
            int mix = (crc ^ b) & 1;
            crc >>= 1;
            if (mix) crc ^= 0x8C;
            b >>= 1;
        }
    }
    return crc;
}

static void push_bit(ds2502_t *d, int b) {
    if (d->out_head + d->out_len < (int)sizeof d->out) d->out[d->out_head + d->out_len++] = b;
}

static void send(ds2502_t *d, const uint8_t *p, int n) {
    for (int i = 0; i < n; i++)
        for (int k = 0; k < 8; k++) push_bit(d, p[i] >> k & 1);
}

static int rom_bit(ds2502_t *d, int i) { return d->rom[i / 8] >> (i % 8) & 1; }

static void search_pair(ds2502_t *d) {
    int b = rom_bit(d, d->idx);
    push_bit(d, b);
    push_bit(d, b ^ 1);
}

int ds2502_reset(ds2502_t *d) {
    d->state = ST_ROM;
    d->ninbits = d->inbits = 0;
    d->out_head = d->out_len = 0;
    d->cmd = -1;
    d->nargs = 0;
    return 1;
}

static void on_byte(ds2502_t *d, int b) {
    switch (d->state) {
    case ST_ROM:
        if (b == 0x33) { send(d, d->rom, 8); d->state = ST_MEM; }
        else if (b == 0xCC) d->state = ST_MEM;
        else if (b == 0xF0) { d->state = ST_SEARCH; d->idx = 0; search_pair(d); }
        else if (b == 0x55) { d->state = ST_MATCH; d->nargs = 0; }
        else d->state = ST_IDLE;
        break;
    case ST_MATCH:
        d->args[d->nargs++] = b;
        if (d->nargs == 8) d->state = memcmp(d->args, d->rom, 8) == 0 ? ST_MEM : ST_IDLE;
        break;
    case ST_MEM:
        d->cmd = b; d->nargs = 0; d->state = ST_ADDR;
        break;
    case ST_ADDR:
        d->args[d->nargs++] = b;
        if (d->nargs == 2) {
            int addr = d->args[0] | d->args[1] << 8;
            uint8_t hdr[3] = {(uint8_t)d->cmd, d->args[0], d->args[1]}, c = crc8(hdr, 3);
            if (d->cmd == 0xF0) {                                   /* read memory */
                static const uint8_t ff[16] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
                                               0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
                send(d, &c, 1);
                if (addr < 128) send(d, d->mem + addr, 128 - addr);
                send(d, ff, 16);
            } else if (d->cmd == 0xAA) {                            /* read status */
                int n = addr < 8 ? 8 - addr : 0;
                uint8_t c2 = crc8(d->status + (addr < 8 ? addr : 8), n);
                send(d, &c, 1);
                send(d, d->status + (addr < 8 ? addr : 8), n);
                send(d, &c2, 1);
            } else if (d->cmd == 0xC3) {                            /* data + page CRCs */
                send(d, &c, 1);
                for (int a = addr; a < 128; ) {
                    int end = (a | 31) + 1;
                    uint8_t pc = crc8(d->mem + a, end - a);
                    send(d, d->mem + a, end - a);
                    send(d, &pc, 1);
                    a = end;
                }
            }
            d->state = ST_DATA;
        }
        break;
    default:                                                        /* data, idle */
        break;
    }
}

int ds2502_bit(ds2502_t *d, int v) {
    if (d->out_len) {                       /* read slot: the master writes 1 and samples */
        int b = d->out[d->out_head++];
        if (--d->out_len == 0) d->out_head = 0;
        return b & v;
    }
    if (d->state == ST_SEARCH) {            /* master's direction bit after our pair */
        if (v != rom_bit(d, d->idx)) { d->state = ST_IDLE; return v; }
        if (++d->idx == 64) d->state = ST_MEM;
        else search_pair(d);
        return v;
    }
    d->inbits |= v << d->ninbits;
    if (++d->ninbits == 8) {
        int byte = d->inbits;
        d->ninbits = d->inbits = 0;
        on_byte(d, byte);
    }
    return v;
}

ds2502_t *ds2502_new(const uint8_t serial[6], const uint8_t memory[128]) {
    ds2502_t *d = calloc(1, sizeof *d);
    d->rom[0] = 0x89;
    memcpy(d->rom + 1, serial, 6);
    d->rom[7] = crc8(d->rom, 7);
    memcpy(d->mem, memory, 128);
    memset(d->status, 0xFF, 8);
    ds2502_reset(d);
    return d;
}
