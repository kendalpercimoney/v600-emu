/* ATI Imageon W2250: the V600's graphics and display chip, its I2C master (camera bus),
   the outer display it feeds, and the camera sensor's control registers.

   The CPU reaches the chip two ways: a serial host interface on QSPI chip select 6
   (registers, small writes; ati_serial_transfer) and a DMA write port (bursts into
   registers or chip memory; ati_write_block). The chip is little-endian. Address space:
       0x00000-0x0FFFF  serial-interface registers (8-bit)
       0x10000-0x1FFFF  Imageon registers (32-bit); register R is at 0x10000 + R
       other            chip memory (4 MB window), including the PM4 command ring
   Register names and fields come from the W100, which shares the register map: Linux
   w100fb.h and the Zaurus X server's imageon_regs.h / imageon_const.h. Where the firmware
   relies on something those do not document, the comments say so and why.

   Ported from v600/ati.py and camera.py (tag py-reference), keeping its exact behaviour,
   floating-point blending included. */
#include <stdlib.h>
#include <string.h>
#include "machine.h"

#define CHIP_ID 0x57451002u
#define MEM_SIZE 0x400000u
#define MEM_MASK (MEM_SIZE - 1)
#define RING_BASE 0x80000u
#define RING_SIZE 0x4000u

/* registers (offsets without the 0x10000 window base) */
#define CIF_CNTL 0x0018            /* host interface: swap_fbuf_1 (3:2) byte lanes for memory */
#define RBBM_STATUS 0x0140
#define GEN_INT_STATUS 0x0204
#define RB_RPTR 0x021C
#define RB_WPTR 0x0220
#define GRAPHIC_CTRL 0x0414
#define DISP_DB_BUF_CNTL 0x04D8
#define DST_OFFSET 0x1004
#define DST_PITCH 0x1008
#define DP_GUI_MASTER_CNTL 0x106C
#define DP_BRUSH_FRGD_CLR 0x107C
#define SRC_X_Y 0x1190
#define DST_X_Y 0x1194
#define DST_WIDTH_HEIGHT 0x1198
#define SRC_OFFSET 0x11AC
#define SRC_PITCH 0x11B0
#define SC_TOP_LEFT 0x11BC
#define SC_BOTTOM_RIGHT 0x11C0
#define GLOBAL_ALPHA 0x1210
#define DP_SRC_FRGD_CLR 0x1240
#define DP_SRC_BKGD_CLR 0x1244
#define DP_MIX 0x12C8
#define I2C_CNTL_0 0x0988
#define I2C_CNTL_1 0x098C
#define I2C_DATA 0x0990
#define SUB_ADDR 0x05AC
#define SUB_START 0x05B0
#define SUB_LENGTH 0x05B4

/* ---- camera sensor: Micron MT9V111 on I2C address 0xB8 ---------------------------------
   The camera driver probes three sensor types over the chip's I2C bus. The Micron probe
   (0x1066392A) selects the sensor-core page (R1 = 4), reads the chip version from R255 and
   accepts 0x8229 or 0x823A (the MT9V111). A write sends the register number, then 16-bit
   big-endian values (the number advancing after each); a read returns 16-bit values from
   the last register number. R1 selects the page (1 = image flow processor, 4 = sensor
   core). Registers keep what is written; no image is produced yet. */
#define CAMERA_ADDRESS (0xB8 >> 1)
typedef struct { int page, ptr; uint16_t regs[256][256]; } camera_t;

static void camera_write(camera_t *c, const uint8_t *d, int n) {
    if (n <= 0) return;
    c->ptr = d[0];
    for (int i = 1; i < n - 1; i += 2) {
        int v = d[i] << 8 | d[i + 1];
        if (c->ptr == 1) c->page = v & 0xFF;
        else if (c->ptr != 0xFF) c->regs[c->page][c->ptr] = (uint16_t)v;
        c->ptr = (c->ptr + 1) & 0xFF;
    }
}

static int camera_read(camera_t *c, uint8_t *out, int n) {
    int k = 0;
    while (k < n) {
        int v = c->ptr == 1 ? c->page : c->regs[c->page][c->ptr];
        out[k++] = v >> 8;
        if (k < n) out[k++] = v;
        c->ptr = (c->ptr + 1) & 0xFF;
    }
    return n;
}

/* ---- I2C master ----------------------------------------------------------------------
   Not in the W2250 manual; the layout is the Radeon multimedia I2C block (radeon_reg.h),
   which matches how the V600 driver (0x10B7EB0C read, 0x10B7EE48 write) uses it:
     CNTL_0: done/nack/halt (0-2, w1c), soft_rst (5), start/stop (8/9), receive (10),
             go (12; the driver waits for it to clear), prescale (16-31)
     CNTL_1: data byte count (4:0), address byte count (10:8)
     DATA:   byte FIFO: the address bytes (device address first), then data to send, and
             after a receive the bytes read. Transfers complete at once. */
#define I2C_DONE 1u
#define I2C_NACK 2u
#define I2C_HALT 4u
#define I2C_SOFT_RST (1u << 5)
#define I2C_RECEIVE (1u << 10)
#define I2C_GO (1u << 12)
typedef struct { uint32_t cntl0, cntl1; uint8_t fifo[256]; int nfifo, head; camera_t *camera; } i2c_t;

static void i2c_transfer(i2c_t *e) {
    int n_addr = (e->cntl1 >> 8) & 7, n_data = e->cntl1 & 0x1F, n = e->nfifo - e->head;
    uint8_t q[256];
    memcpy(q, e->fifo + e->head, n);
    e->nfifo = e->head = 0;
    int na = n_addr < n ? n_addr : n;
    camera_t *dev = na && (q[0] >> 1) == CAMERA_ADDRESS ? e->camera : 0;
    e->cntl0 &= ~I2C_GO;
    e->cntl0 |= I2C_DONE;
    if (!dev) {
        e->cntl0 |= I2C_NACK;
    } else if ((e->cntl0 & I2C_RECEIVE) || (q[0] & 1)) {
        if (na > 1) camera_write(dev, q + 1, na - 1);
        e->nfifo = camera_read(dev, e->fifo, n_data);
    } else {
        uint8_t w[512];
        int k = 0;
        for (int i = 1; i < na; i++) w[k++] = q[i];
        for (int i = na; i < n && i - na < n_data; i++) w[k++] = q[i];
        camera_write(dev, w, k);
    }
}

static uint32_t i2c_read(i2c_t *e, uint32_t r) {
    if (r == I2C_DATA) return e->head < e->nfifo ? e->fifo[e->head++] : 0;
    return r == I2C_CNTL_0 ? e->cntl0 : e->cntl1;
}

static void i2c_write(i2c_t *e, uint32_t r, uint32_t v) {
    if (r == I2C_DATA) {
        if (e->nfifo < (int)sizeof e->fifo) e->fifo[e->nfifo++] = v & 0xFF;
    } else if (r == I2C_CNTL_1) {
        e->cntl1 = v;
    } else {
        uint32_t st = e->cntl0 & ~v & (I2C_DONE | I2C_NACK | I2C_HALT);
        e->cntl0 = (v & ~(I2C_DONE | I2C_NACK | I2C_HALT | I2C_SOFT_RST)) | st;
        if (v & I2C_SOFT_RST) e->nfifo = e->head = 0;
        if (v & I2C_GO) i2c_transfer(e);
    }
}

/* ---- the chip ----------------------------------------------------------------------- */
#define MAX_OTHER 64
struct ati {
    uint8_t iface[0x10000];           /* serial-interface registers */
    uint32_t regs[0x4000];            /* 0x10000-0x1FFFF */
    struct { uint32_t addr, value; } other[MAX_OTHER];   /* 32-bit writes elsewhere */
    int nother;
    uint8_t *mem, *veiled, *under;    /* veil memory: see the 2D engine */
    uint32_t *cp;                     /* PM4 words not yet executed */
    int ncp, cap;
    uint64_t draws;                   /* drawing operations, to detect new frames */
    i2c_t i2c;
    camera_t camera;
    outer_t outer;
};

static uint32_t reg(ati_t *c, uint32_t r) { return r < 0x10000 ? c->regs[r >> 2] : 0; }

static uint32_t *slot(ati_t *c, uint32_t a, int create) {
    if (a >= 0x10000 && a < 0x20000) return &c->regs[(a - 0x10000) >> 2];
    for (int i = 0; i < c->nother; i++) if (c->other[i].addr == a) return &c->other[i].value;
    if (!create || c->nother == MAX_OTHER) return 0;
    c->other[c->nother].addr = a;
    c->other[c->nother].value = 0;
    return &c->other[c->nother++].value;
}

static void outer_write(outer_t *o, const uint8_t *data, int n, int ncommands);
static void blit_registers(ati_t *c);

/* Operations complete instantly, so status bits always read as done / idle: RBBM_STATUS
   command FIFO free, nothing busy; DISP_DB_BUF_CNTL bit 1 (update pending) clear;
   GEN_INT_STATUS bit 11 set (waited for after display updates, 0x1091EE84); GRAPHIC_CTRL
   pclk/sclk_running (25/26) follow lcd_pclk/sclk_on (23/24). */
uint32_t ati_read32(ati_t *c, uint32_t addr) {
    uint32_t a = addr & ~3u, r = a - 0x10000;
    if (r == I2C_CNTL_0 || r == I2C_CNTL_1 || r == I2C_DATA) return i2c_read(&c->i2c, r);
    uint32_t *p = slot(c, a, 0), v = p ? *p : 0;
    if (r == RBBM_STATUS) return 0x40;
    if (r == GRAPHIC_CTRL) return (v & ~(3u << 25)) | (((v >> 23) & 3) << 25);
    if (r == DISP_DB_BUF_CNTL) return v & ~0x2u;
    if (r == GEN_INT_STATUS) return v | 0x800;
    return v;
}

/* CHIP_ID is read-only: the driver writes test patterns there, and the re-probe after a
   panel reset must still see the real ID. */
void ati_write32(ati_t *c, uint32_t addr, uint32_t value) {
    uint32_t a = addr & ~3u, r = a - 0x10000;
    if (r == I2C_CNTL_0 || r == I2C_CNTL_1 || r == I2C_DATA) { i2c_write(&c->i2c, r, value); return; }
    if (a != 0x10000) { uint32_t *p = slot(c, a, 1); if (p) *p = value; }
    if (r == RB_WPTR) {                               /* ring (re)start: read pointer follows */
        c->regs[RB_RPTR >> 2] = value;
        c->ncp = 0;
    } else if (r == DST_WIDTH_HEIGHT) {
        blit_registers(c);
    } else if (r == SUB_START) {                      /* bytes for the outer display */
        uint32_t s = reg(c, SUB_ADDR) & MEM_MASK, n = (value & 0xFF) + reg(c, SUB_LENGTH);
        uint32_t whole = (n + 3) & ~3u;
        if (s + whole > MEM_SIZE) whole = (MEM_SIZE - s) & ~3u;
        uint8_t *buf = malloc(whole + 4);
        for (uint32_t i = 0; i < whole; i += 4)       /* the stream goes out in 32-bit words */
            for (int k = 0; k < 4; k++) buf[i + k] = c->mem[s + i + 3 - k];
        outer_write(&c->outer, buf, n < whole ? (int)n : (int)whole, value & 0xFF);
        free(buf);
    }
}

static uint32_t ati_read8(ati_t *c, uint32_t addr) {
    if (addr < 0x10000) return c->iface[addr];
    if (addr < 0x20000) return (ati_read32(c, addr & ~3u) >> (8 * (addr & 3))) & 0xFF;
    return c->mem[addr & MEM_MASK];
}

void ati_write_tail(ati_t *c, uint32_t addr, const uint8_t *data, int n);

static void ati_write8(ati_t *c, uint32_t addr, uint32_t v) {
    if (addr < 0x10000) {
        c->iface[addr] = v;
    } else if (addr < 0x20000) {
        int sh = 8 * (addr & 3);
        uint32_t *p = slot(c, addr & ~3u, 0), old = p ? *p : 0;
        ati_write32(c, addr, (old & ~(0xFFu << sh)) | v << sh);
    } else {
        uint8_t b = v;
        ati_write_tail(c, addr, &b, 1);
    }
}

/* CIF_CNTL swap_fbuf_1: byte lanes of host writes to memory. Icons are uploaded with 3 (the
   two 16-bit halves of each word swapped); ignoring it mirrored every pixel pair. */
static uint32_t swap_word(uint32_t w, int mode) {
    if (mode == 1) return ((w & 0x00FF00FF) << 8) | ((w >> 8) & 0x00FF00FF);
    if (mode == 2) return __builtin_bswap32(w);
    if (mode == 3) return (w << 16) | (w >> 16);
    return w;
}

static void put_le32(uint8_t *p, uint32_t w) { p[0] = w; p[1] = w >> 8; p[2] = w >> 16; p[3] = w >> 24; }

static void run_cp(ati_t *c);

/* A host burst: registers if in the register window, otherwise chip memory. Words written
   into the command ring are executed in order. */
void ati_write_block(ati_t *c, uint32_t addr, const uint32_t *words, int n) {
    if (addr >= 0x10000 && addr < 0x20000) {
        for (int i = 0; i < n; i++) ati_write32(c, addr + 4 * i, words[i]);
        return;
    }
    if (!(addr >= RING_BASE && addr < RING_BASE + RING_SIZE)) {   /* chip memory */
        int swap = (reg(c, CIF_CNTL) >> 2) & 3;
        uint32_t a = addr & MEM_MASK;
        for (int i = 0; i < n; i++) {                             /* wraps around */
            uint32_t w = swap_word(words[i], swap);
            for (int k = 0; k < 4; k++) c->mem[(a + 4 * i + k) & MEM_MASK] = w >> (8 * k);
        }
        return;
    }
    for (int i = 0; i < n; i++) {
        int64_t off = ((int64_t)addr + 4 * i - RING_BASE) % RING_SIZE;
        put_le32(c->mem + RING_BASE + off, words[i]);
    }
    if (c->ncp + n > c->cap) {
        c->cap = (c->ncp + n) * 2;
        c->cp = realloc(c->cp, c->cap * sizeof *c->cp);
    }
    memcpy(c->cp + c->ncp, words, n * sizeof *words);
    c->ncp += n;
    run_cp(c);
    int64_t end = ((int64_t)addr + 4 * n - RING_BASE) % RING_SIZE;
    c->regs[RB_WPTR >> 2] = (uint32_t)(end / 4);
    int64_t rp = ((end - 4 * (int64_t)c->ncp) % RING_SIZE + RING_SIZE) % RING_SIZE;
    c->regs[RB_RPTR >> 2] = (uint32_t)(rp / 4);
}

/* A memory write shorter than a word (an odd-width image row ends in one): the bytes land
   where they would in a full word written at addr with the current swap mode. */
void ati_write_tail(ati_t *c, uint32_t addr, const uint8_t *data, int n) {
    uint32_t lanes = swap_word(0x01020304, (reg(c, CIF_CNTL) >> 2) & 3);
    for (int k = 0; k < 4; k++) {
        int src = (lanes >> (8 * k)) & 0xFF;          /* memory byte k <- source byte */
        if (src - 1 < n) c->mem[(addr + k) & MEM_MASK] = data[src - 1];
    }
}

/* ---- 2D engine ------------------------------------------------------------------------
   Destination: DST_OFFSET / DST_PITCH (pixels), RGB565. With GMC dst_clipping (bit 3)
   writes are limited to SC_TOP_LEFT - SC_BOTTOM_RIGHT (y << 16 | x, bottom-right
   exclusive). Mono source data (text) expands to DP_SRC_FRGD_CLR / DP_SRC_BKGD_CLR; fills
   and lines use DP_BRUSH_FRGD_CLR.
   Modes, from DP_MIX (and DP_GUI_MASTER_CNTL, which writes the same fields):
     dp_op (DP_MIX bit 24, GMC bit 31): arithmetic op. The firmware sets
       E2_ARITHMETIC_CNTL to 4, DST_SADDBLEND_SRC2 with both factors = GLOBAL_ALPHA: blend().
     bits 26-27, documented nowhere we have. Two uses are understood from what the firmware
       does and what the phone shows:
       - copies with ROP pair AA/CC (DP_MIX 23:16 / 7:0) are icon blits: the icon's flat
         background must not show (no colour-compare register is set; the background is the
         icon's top-left pixel);
       - white fills redraw translucent areas: a menu row the highlight leaves is filled
         white in this mode, and on the phone it is translucent again. So an alpha fill
         remembers what it veiled (veiled / under), and such a white fill re-veils it.
   The UI composes each screen in a layer surface (wallpaper or a patch of it, veiled by
   alpha fills, then dialogs) and copies it to the displayed surface. */
typedef struct { int64_t r, c0, c1; uint32_t a; } row_t;

/* The visible row runs of a w x h rectangle at (x, y): returns how many (rows filled). */
static int rows_of(ati_t *c, int64_t x, int64_t y, int64_t w, int64_t h, row_t **out) {
    int64_t base = reg(c, DST_OFFSET), pitch = reg(c, DST_PITCH), x0, y0, x1, y1;
    if (!(reg(c, DP_GUI_MASTER_CNTL) & 0x8)) { x0 = y0 = 0; x1 = y1 = 0x2000; }
    else {
        uint32_t tl = reg(c, SC_TOP_LEFT), br = reg(c, SC_BOTTOM_RIGHT);
        x0 = tl & 0xFFFF; y0 = tl >> 16; x1 = br & 0xFFFF; y1 = br >> 16;
    }
    int64_t c0 = x0 - x > 0 ? x0 - x : 0, c1 = w;
    if (x1 - x < c1) c1 = x1 - x;
    if (pitch - x < c1) c1 = pitch - x;
    if (c1 < 0) c1 = 0;
    *out = 0;
    if (c0 >= c1) return 0;
    int64_t r0 = y0 - y > 0 ? y0 - y : 0, r1 = h;
    if (y1 - y < r1) r1 = y1 - y;
    if (r1 < 0) r1 = 0;
    if (r1 <= r0) return 0;
    row_t *rows = malloc((r1 - r0) * sizeof *rows);
    for (int64_t r = r0; r < r1; r++) {
        rows[r - r0].r = r;
        rows[r - r0].a = (uint32_t)((base + ((y + r) * pitch + x + c0) * 2) & MEM_MASK);
        rows[r - r0].c0 = c0;
        rows[r - r0].c1 = c1;
    }
    *out = rows;
    return (int)(r1 - r0);
}

static void put_pixels(ati_t *c, uint32_t a, const uint8_t *data, int64_t n, int keep_veil) {
    if (a + n > MEM_SIZE) n = MEM_SIZE - a;
    memmove(c->mem + a, data, n);
    if (!keep_veil) memset(c->veiled + (a >> 1), 0, ((a + n) >> 1) - (a >> 1));
}

static int alpha_op(ati_t *c) {
    return ((reg(c, DP_MIX) >> 24) & 1) || (reg(c, DP_GUI_MASTER_CNTL) >> 31);
}
static int special(ati_t *c) { return ((reg(c, DP_MIX) >> 26) & 3) != 0; }

/* DST_SADDBLEND_SRC2: src * alpha + dst * (1 - alpha) per RGB565 channel, alpha_r/g/b from
   GLOBAL_ALPHA bytes 0/1/2 in sixteenths (the UI uses 8 and 11). The destination weight is
   taken as 1 - alpha: read literally (both factors = alpha, added, saturated), every
   alpha-11 layer the UI draws comes out white. */
static uint32_t blend(ati_t *c, uint32_t src, uint32_t dst) {
    uint32_t ga = reg(c, GLOBAL_ALPHA), out = 0;
    static const int shifts[3] = {11, 5, 0}, masks[3] = {31, 63, 31};
    uint32_t ks[3] = {ga & 0xFF, ga >> 8 & 0xFF, ga >> 16 & 0xFF};
    for (int i = 0; i < 3; i++) {
        double a = (ks[i] < 16 ? ks[i] : 16) / 16.0;
        double v = ((src >> shifts[i]) & masks[i]) * a + ((dst >> shifts[i]) & masks[i]) * (1 - a);
        uint32_t q = (uint32_t)(int64_t)(v + 0.5);
        out |= (q < (uint32_t)masks[i] ? q : (uint32_t)masks[i]) << shifts[i];
    }
    return out;
}

/* n pixels of a brush colour at address a, in the current mode. */
static void fill(ati_t *c, uint32_t a, uint32_t colour, int64_t n) {
    int aop = alpha_op(c), sp = special(c);
    if (!aop && !(sp && colour == 0xFFFF)) {
        if (a + 2 * n > MEM_SIZE) n = (MEM_SIZE - a) / 2;
        for (int64_t i = 0; i < n; i++) { c->mem[a + 2 * i] = colour; c->mem[a + 2 * i + 1] = colour >> 8; }
        if (!sp) memset(c->veiled + (a >> 1), 0, ((a + 2 * n) >> 1) - (a >> 1));
        return;
    }
    for (uint32_t p = a; p < a + 2 * n && p + 1 < MEM_SIZE; p += 2) {
        if (aop) {                                    /* veil: remember what is under it */
            c->under[p] = c->mem[p]; c->under[p + 1] = c->mem[p + 1];
            c->veiled[p >> 1] = 1;
        } else if (!c->veiled[p >> 1]) {
            c->mem[p] = colour; c->mem[p + 1] = colour >> 8;
            continue;
        }
        uint32_t v = blend(c, colour, c->under[p] | c->under[p + 1] << 8);
        c->mem[p] = v; c->mem[p + 1] = v >> 8;
    }
}

static void paint(ati_t *c, int64_t x, int64_t y, int64_t w, int64_t h) {
    row_t *rows;
    int n = rows_of(c, x, y, w, h, &rows);
    uint32_t colour = reg(c, DP_BRUSH_FRGD_CLR) & 0xFFFF;
    for (int i = 0; i < n; i++) fill(c, rows[i].a, colour, rows[i].c1 - rows[i].c0);
    free(rows);
}

static void polyline(ati_t *c, const uint32_t *p, int n) {
    uint32_t colour = reg(c, DP_BRUSH_FRGD_CLR) & 0xFFFF;
    for (int k = 0; k + 1 < n; k++) {             /* Bresenham, both end points included */
        int64_t x0 = p[k] & 0xFFFF, y0 = p[k] >> 16, x1 = p[k + 1] & 0xFFFF, y1 = p[k + 1] >> 16;
        int64_t dx = x1 > x0 ? x1 - x0 : x0 - x1, dy = -(y1 > y0 ? y1 - y0 : y0 - y1);
        int64_t sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1, err = dx + dy;
        for (;;) {
            row_t *rows;
            int nr = rows_of(c, x0, y0, 1, 1, &rows);
            for (int i = 0; i < nr; i++) fill(c, rows[i].a, colour, 1);
            free(rows);
            if (x0 == x1 && y0 == y1) break;
            int64_t e2 = 2 * err;
            if (e2 >= dy) { err += dy; x0 += sx; }
            if (e2 <= dx) { err += dx; y0 += sy; }
        }
    }
}

static void host_data(ati_t *c, int64_t x, int64_t y, int64_t w, int64_t h, const uint32_t *words, int nw) {
    int64_t npx = 4 * (int64_t)nw;
    uint8_t *px = malloc(npx + 4);
    for (int i = 0; i < nw; i++) put_le32(px + 4 * i, words[i]);
    int src_type = (reg(c, DP_GUI_MASTER_CNTL) >> 12) & 7;
    row_t *rows;
    int n = rows_of(c, x, y, w, h, &rows);
    if (src_type == 0 || src_type == 1) {             /* 1 bpp, leftmost pixel = bit 7 */
        uint32_t fg = reg(c, DP_SRC_FRGD_CLR) & 0xFFFF, bg = reg(c, DP_SRC_BKGD_CLR) & 0xFFFF;
        for (int k = 0; k < n; k++)
            for (int64_t col = rows[k].c0; col < rows[k].c1; col++) {
                int64_t i = rows[k].r * w + col;
                int on = ((i >> 3) < npx ? px[i >> 3] : 0) >> (7 - (i & 7)) & 1;
                if (on || src_type == 0) {            /* type 1: background left alone */
                    uint32_t a = rows[k].a + 2 * (uint32_t)(col - rows[k].c0), v = on ? fg : bg;
                    if (a + 1 < MEM_SIZE) { c->mem[a] = v; c->mem[a + 1] = v >> 8; }
                }
            }
    } else {                                          /* 16 bpp image */
        for (int k = 0; k < n; k++) {
            int64_t s = (rows[k].r * w + rows[k].c0) * 2, e = (rows[k].r * w + rows[k].c1) * 2;
            if (s > npx) s = npx;
            if (e > npx) e = npx;
            if (e > s) put_pixels(c, rows[k].a, px + s, e - s, 0);
            else put_pixels(c, rows[k].a, px, 0, 0);
        }
    }
    free(rows);
    free(px);
}

static void copy_rect(ati_t *c, uint32_t sbase, uint32_t spitch, int64_t sx, int64_t sy,
                      int64_t dx, int64_t dy, int64_t w, int64_t h) {
    uint32_t mix = reg(c, DP_MIX);
    int icon = ((mix >> 26) & 3) && ((mix >> 16) & 0xFF) == 0xAA && (mix & 0xFF) == 0xCC;
    if ((int64_t)spitch - sx < w) w = (int64_t)spitch - sx;
    int64_t rowlen = w > 0 ? 2 * w : 0;
    /* source row r, clipped to the chip memory's end */
    #define SRC_ROW(r) ((uint32_t)((sbase + ((sy + (r)) * (int64_t)spitch + sx) * 2) & MEM_MASK))
    uint32_t key = 0;                                 /* an icon's background: top-left pixel */
    if (h > 0 && rowlen >= 2 && SRC_ROW(0) + 2 <= MEM_SIZE)
        key = c->mem[SRC_ROW(0)] | c->mem[SRC_ROW(0) + 1] << 8;
    row_t *rows;
    int n = rows_of(c, dx, dy, w, h, &rows);
    /* read every visible source run first: source and destination may overlap */
    int64_t span = n ? 2 * (rows[0].c1 - rows[0].c0) : 0;
    uint8_t *buf = malloc(n * span + 2);
    int64_t *len = malloc((n + 1) * sizeof *len);
    for (int k = 0; k < n; k++) {
        uint32_t a = SRC_ROW(rows[k].r);
        int64_t avail = a + rowlen > MEM_SIZE ? MEM_SIZE - a : rowlen;   /* row as read */
        int64_t s = 2 * rows[k].c0, e = 2 * rows[k].c1;
        if (e > avail) e = avail;
        if (s > e) s = e;
        len[k] = e - s;
        memcpy(buf + k * span, c->mem + a + s, len[k]);
    }
    #undef SRC_ROW
    for (int k = 0; k < n; k++) {
        const uint8_t *row = buf + k * span;
        uint32_t a = rows[k].a;
        if (!icon) { put_pixels(c, a, row, len[k], 0); continue; }
        for (int64_t i = 0; i + 1 < len[k] && a + i + 1 < MEM_SIZE; i += 2) {
            uint32_t v = row[i] | row[i + 1] << 8;
            if (v != key) { c->mem[a + i] = row[i]; c->mem[a + i + 1] = row[i + 1]; }
        }
    }
    free(rows);
    free(len);
    free(buf);
}

/* Writing DST_WIDTH_HEIGHT (w << 16 | h) starts a blit from SRC_X_Y to DST_X_Y (both
   x << 16 | y); the firmware draws icons this way. */
static void blit_registers(ati_t *c) {
    uint32_t s = reg(c, SRC_X_Y), d = reg(c, DST_X_Y), wh = reg(c, DST_WIDTH_HEIGHT);
    copy_rect(c, reg(c, SRC_OFFSET), reg(c, SRC_PITCH), s >> 16, s & 0xFFFF, d >> 16, d & 0xFFFF,
              wh >> 16, wh & 0xFFFF);
    c->draws++;
}

static void packet3(ati_t *c, int op, const uint32_t *p, int n) {
    if (op == 0x19 && n >= 2) {                       /* host data: (y, x), (h, w), pixels */
        host_data(c, p[0] & 0xFFFF, p[0] >> 16, p[1] & 0xFFFF, p[1] >> 16, p + 2, n - 2);
    } else if (op == 0x1A) {                          /* PAINT_MULTI: (x, y), (w, h) */
        for (int k = 0; k < n - 1; k += 2)
            paint(c, p[k] >> 16, p[k] & 0xFFFF, p[k + 1] >> 16, p[k + 1] & 0xFFFF);
    } else if (op == 0x15) {                          /* POLYLINE */
        polyline(c, p, n);
    } else if (op == 0x1B) {                          /* BITBLT_MULTI: (src)(dst)(w, h) */
        for (int k = 0; k < n - 2; k += 3)
            copy_rect(c, reg(c, SRC_OFFSET), reg(c, SRC_PITCH), p[k] >> 16, p[k] & 0xFFFF,
                      p[k + 1] >> 16, p[k + 1] & 0xFFFF, p[k + 2] >> 16, p[k + 2] & 0xFFFF);
    } else {
        return;                                       /* not implemented */
    }
    c->draws++;
}

/* PM4 ring in chip memory; 0x21C / 0x220 are its read / write pointers in dwords. The
   driver waits for read == write == its own count (0x10B87A60); a wait that never ends
   trips the task-progress watchdog and the display is torn down. Type-3 opcodes are
   Radeon's with bit 7 clear. */
static void run_cp(ati_t *c) {
    int i = 0;
    while (i < c->ncp) {
        uint32_t h = c->cp[i], kind = h >> 30;
        if (kind == 0) {                              /* n registers from an index */
            int n = ((h >> 16) & 0x3FFF) + 1;
            if (i + 1 + n > c->ncp) break;
            for (int k = 0; k < n; k++) ati_write32(c, 0x10000 + (h & 0xFFFF) * 4 + 4 * k, c->cp[i + 1 + k]);
            i += 1 + n;
        } else if (kind == 1) {                       /* two registers */
            if (i + 3 > c->ncp) break;
            ati_write32(c, 0x10000 + (h & 0x7FF) * 4, c->cp[i + 1]);
            ati_write32(c, 0x10000 + ((h >> 11) & 0x7FF) * 4, c->cp[i + 2]);
            i += 3;
        } else if (kind == 2) {                       /* filler */
            i += 1;
        } else {                                      /* opcode + payload */
            int n = ((h >> 16) & 0x3FFF) + 1;
            if (i + 1 + n > c->ncp) break;
            packet3(c, (h >> 8) & 0xFF, c->cp + i + 1, n);
            i += 1 + n;
        }
        if (c->ncp == 0) return;                      /* a write restarted the ring */
    }
    memmove(c->cp, c->cp + i, (c->ncp - i) * sizeof *c->cp);
    c->ncp -= i;
}

/* ---- serial host interface (QSPI chip select 6) ------------------------------------------
   Frame (inside a 32-byte queue buffer): byte 0 = flags | (length - 1), flag 0x20 = write;
   bytes 1-3 = address. Reads: data from offset 12 (little-endian for registers). Writes:
   data from offset 8. Returns the reply length (out must hold n + 32 bytes). */
int ati_serial_transfer(ati_t *c, const uint8_t *data, int n, uint8_t *out) {
    memset(out, 0, n + 32);
    if (n < 4) return n;
    int len = (data[0] & 0x1F) + 1;
    uint32_t addr = (uint32_t)data[1] << 16 | data[2] << 8 | data[3];
    if (data[0] & 0x20) {
        int end = len < n ? len : n, k = end - 8;
        if (k == 4 && addr >= 0x10000)
            ati_write32(c, addr, data[8] | data[9] << 8 | data[10] << 16 | (uint32_t)data[11] << 24);
        else
            for (int i = 0; i < k; i++) ati_write8(c, addr + i, data[8 + i]);
        return n;
    }
    int k = len - 12 > 0 ? len - 12 : 0;
    if (k == 4 && addr >= 0x10000) put_le32(out + 12, ati_read32(c, addr));
    else for (int i = 0; i < k; i++) out[12 + i] = ati_read8(c, addr + i);
    return n > 12 + k ? n : 12 + k;
}

/* ---- outer display: 96 x 32 monochrome, on the outside of the flip -------------------------
   The chip forwards bytes to it: SUB_ADDR points at them in chip memory, SUB_LENGTH is the
   number of pixel bytes (96, one page row), writing SUB_START with the number of command
   bytes in front sends them. The stream goes out a 32-bit word at a time, low byte first.
   Commands: 0xB0-B7 page (8 pixel rows), 0x10-0x1F / 0x00-0x0F column high / low nibble,
   0xA1 / 0xA0 column order (A1 reads correctly: signal left, battery right). Each pixel
   byte is a column of 8 pixels, bit 0 at the top. */
static void outer_write(outer_t *o, const uint8_t *data, int n, int ncommands) {
    for (int i = 0; i < ncommands && i < n; i++) {
        int b = data[i];
        if (b >= 0xB0 && b <= 0xB7) o->page = b & 7;
        else if (b >= 0x10 && b <= 0x1F) o->column = (o->column & 0x0F) | (b & 0x0F) << 4;
        else if (b <= 0x0F) o->column = (o->column & 0xF0) | b;
        else if (b == 0xA0 || b == 0xA1) o->mirrored = b == 0xA0;
    }
    if (o->page >= OUTER_PAGES) return;
    int npx = n - ncommands, x = o->column, k = npx < OUTER_WIDTH - x ? npx : OUTER_WIDTH - x;
    if (k <= 0) return;
    for (int i = 0; i < k; i++) {
        int col = o->mirrored ? OUTER_WIDTH - 1 - (x + i) : x + i, b = data[ncommands + i];
        for (int bit = 0; bit < 8; bit++) o->pixels[(o->page * 8 + bit) * OUTER_WIDTH + col] = b >> bit & 1;
    }
    o->updates++;
}

/* ---- video overlay: the camera viewfinder ------------------------------------------------
   The camera app draws its viewfinder area in a key colour and the chip shows a YUV 4:2:0
   picture there on the way to the display (the frame in memory keeps the key colour).
   +0x434 bit 0 = overlay on; +0x438 low half = the key colour (0x0821); +0x43C / +0x444 /
   +0x44C = Y / U / V planes with pitches +0x440 / +0x448 / +0x450; +0x454 / +0x458 = width /
   height << 16 (176 x 144). The capture side fills two such frames (also at +0x55C.. and
   +0xE58.. / +0xE64..); the sensor's picture comes from the host (ati_camera_frame). Where
   the overlay sits on screen is taken from the keyed area's top-left corner. */
#define OVL_CTRL 0x434
#define OVL_KEY 0x438
#define OVL_W 0x454
#define OVL_H 0x458

int ati_overlay_on(ati_t *c) { return reg(c, OVL_CTRL) & 1; }

static void yuv_to_rgb(int y, int u, int v, uint8_t *o) {
    int c = y - 16, d = u - 128, e = v - 128;
    int r = (298 * c + 409 * e + 128) >> 8, g = (298 * c - 100 * d - 208 * e + 128) >> 8, b = (298 * c + 516 * d + 128) >> 8;
    o[0] = r < 0 ? 0 : r > 255 ? 255 : r; o[1] = g < 0 ? 0 : g > 255 ? 255 : g; o[2] = b < 0 ? 0 : b > 255 ? 255 : b;
}

/* The displayed frame (GRAPHIC_OFFSET, GRAPHIC_PITCH) as RGBA, w x h, overlay included. */
void ati_screen(ati_t *c, uint8_t *rgba, int w, int h) {
    uint32_t base = reg(c, 0x418), pitch = reg(c, 0x41C) ? reg(c, 0x41C) : 0x160;
    int ovl = ati_overlay_on(c), ox = w, oy = h;
    uint32_t key = reg(c, OVL_KEY) & 0xFFFF;
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++) {
            uint32_t a = (base + y * pitch + 2 * x) & (MEM_SIZE - 1), v = c->mem[a] | c->mem[(a + 1) & (MEM_SIZE - 1)] << 8;
            uint8_t *o = rgba + 4 * (y * w + x);
            o[0] = ((v >> 11) & 31) * 255 / 31; o[1] = ((v >> 5) & 63) * 255 / 63; o[2] = (v & 31) * 255 / 31; o[3] = 255;
            if (ovl && v == key) {
                if (y < oy) oy = y;
                if (x < ox) ox = x;
                o[3] = 0;                               /* marks the keyed pixels for below */
            }
        }
    if (!ovl || oy == h) return;
    uint32_t ya = reg(c, 0x43C), yp = reg(c, 0x440), ua = reg(c, 0x444), up = reg(c, 0x448), va = reg(c, 0x44C), vp = reg(c, 0x450);
    int sw = (int)(reg(c, OVL_W) >> 16), sh = (int)(reg(c, OVL_H) >> 16);
    for (int y = oy; y < h; y++)
        for (int x = ox; x < w; x++) {
            uint8_t *o = rgba + 4 * (y * w + x);
            if (o[3]) continue;
            o[3] = 255;
            int sx = x - ox, sy = y - oy;
            if (sx >= sw || sy >= sh) continue;
            yuv_to_rgb(c->mem[(ya + sy * yp + sx) & (MEM_SIZE - 1)], c->mem[(ua + sy / 2 * up + sx / 2) & (MEM_SIZE - 1)],
                       c->mem[(va + sy / 2 * vp + sx / 2) & (MEM_SIZE - 1)], o);
        }
}

/* A picture from the camera (RGBA, any size): scaled to the overlay's frame size and stored
   as YUV 4:2:0 in every frame the capture side uses. 0 if the camera is not running. */
int ati_camera_frame(ati_t *c, const uint8_t *rgba, int w, int h) {
    int sw = (int)(reg(c, OVL_W) >> 16), sh = (int)(reg(c, OVL_H) >> 16);
    if (!ati_overlay_on(c) || sw <= 0 || sh <= 0 || w <= 0 || h <= 0) return 0;
    static const uint32_t sets[4][3] = {{0x43C, 0x444, 0x44C}, {0x55C, 0x560, 0x564}, {0xE58, 0xE5C, 0xE60}, {0xE64, 0xE68, 0xE6C}};
    uint32_t yp = reg(c, 0x440), cp = reg(c, 0x448);
    for (int sy = 0; sy < sh; sy++)
        for (int sx = 0; sx < sw; sx++) {
            const uint8_t *s = rgba + 4 * ((sy * h / sh) * w + sx * w / sw);
            int r = s[0], g = s[1], b = s[2];
            int Y = ((66 * r + 129 * g + 25 * b + 128) >> 8) + 16;
            int U = ((-38 * r - 74 * g + 112 * b + 128) >> 8) + 128, V = ((112 * r - 94 * g - 18 * b + 128) >> 8) + 128;
            for (int k = 0; k < 4; k++) {
                uint32_t ya = reg(c, sets[k][0]), ua = reg(c, sets[k][1]), va = reg(c, sets[k][2]);
                if (!ya) continue;
                c->mem[(ya + sy * yp + sx) & (MEM_SIZE - 1)] = (uint8_t)Y;
                if (!(sx & 1) && !(sy & 1)) {
                    c->mem[(ua + sy / 2 * cp + sx / 2) & (MEM_SIZE - 1)] = (uint8_t)U;
                    c->mem[(va + sy / 2 * cp + sx / 2) & (MEM_SIZE - 1)] = (uint8_t)V;
                }
            }
        }
    c->draws++;                                       /* the picture changed */
    return 1;
}

/* ---- for the host ---------------------------------------------------------------------- */
ati_t *ati_new(void) {
    ati_t *c = calloc(1, sizeof *c);
    c->mem = calloc(1, MEM_SIZE);
    c->veiled = calloc(1, MEM_SIZE / 2);
    c->under = calloc(1, MEM_SIZE);
    c->iface[4] = 0x01;                               /* interface ID checked by 0x10920A4C */
    c->iface[5] = 0x11;
    c->regs[0] = CHIP_ID;
    c->camera.page = 4;
    c->camera.regs[4][0xFF] = 0x823A;
    c->i2c.camera = &c->camera;
    return c;
}

uint8_t *ati_mem(ati_t *c) { return c->mem; }
uint32_t ati_reg(ati_t *c, uint32_t r) { return reg(c, r); }
uint64_t ati_draws(ati_t *c) { return c->draws; }
outer_t *ati_outer(ati_t *c) { return &c->outer; }
