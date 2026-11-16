/* SIM card: the Neptune smart-card interface (SCIM, port 0) and a virtual GSM SIM.

   Interface registers (Neptune LTE spec ch. 51, 16-bit, offsets from 0x2484F000):
     0x10 PORT0_CNTL  bit 1 sven (card power), 2 sten, 3 srst (1 = reset asserted), 4 scen
     0x12 CNTL        bit 1 icm (initial character mode, cleared by a valid TS), 5:4 clk_sel
                      (ref/4, /3, /5, ref), 8:6 baud_sel (372, 256, ... card clocks per etu),
                      10:9 gpcnt_clk_sel (off, card clock, receive clock, etu clock)
     0x14 RCV_THRESHOLD 4:0;  0x16 ENABLE bit 0 rcv_en, bit 1 xmt_en
     0x18 XMT_STATUS  bit 8 gpcnt, 7 tdtf, 6 tfo, 5 tc, 4 etc, 3 tfe, 0 xte (w1c)
     0x1A RCV_STATUS  bit 5 rdrf (>= rdt bytes), 4 rfd (any byte), 0 oef (w1c)
     0x1C INT_MASK    1 = masked: 0 rim, 1 tcim, 2 oim, 3 etcim, 4 tfeim, 5 xtm, 6 tfom,
                      7 tdtfm, 8 gpcntm, 9 cwtm, 10 rtm
     0x1E PORT0_XMT_BUF, 0x20 PORT0_RCV_BUF;  0x26 XMT_THRESHOLD 3:0
     0x2C RESET_CNTL bit 1 flush_rcv, 2 flush_xmt, 3 soft_reset;  0x30 GPCNT compare value
   Interrupts: data (tc, etc, tfe, rdrf, tdtf) = AITC source 51, general (xte, oef,
   gpcnt, ...) = 12. The card answers in virtual real time: a character is 12 etu, an etu
   372 card clocks at ref/4 = 3.25 MHz, about 1.4 ms per byte.

   The arithmetic follows the Python model this was ported from (v600/sim.py at tag
   py-reference) operation for operation, doubles included, so timings agree exactly. */
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include "machine.h"

#define REF_HZ 13000000.0
#define DATA_SOURCE 51
#define GENERAL_SOURCE 12
enum { P0CNTL = 0x10, CNTL = 0x12, RCVTH = 0x14, ENABLE = 0x16, XSTAT = 0x18, RSTAT = 0x1A,
       IMASK = 0x1C, P0XMT = 0x1E, P0RCV = 0x20, XTH = 0x26, RESET_CNTL = 0x2C, GPCNT = 0x30 };

/* ---- the card: GSM 11.11, T=0, a small file system, CHV1 (PIN) disabled ---------------- */
enum { T_MF, T_DF, T_EF };
typedef struct {
    int fid, parent, type, structure, rec_len, nrec, rec;   /* structure 0 / 1 linear / 3 cyclic */
    uint8_t *data;
    int size;
} sim_file_t;

#define MAX_FILES 40
typedef struct {
    sim_file_t files[MAX_FILES];
    int nfiles, df, state, need, nbuf;
    sim_file_t *ef;
    uint8_t hdr[5], buf[300], last[32];
    int nlast;
} card_t;

static const uint8_t ATR[] = {0x3B, 0x02, 0x14, 0x50};   /* direct, T=0, defaults, 2 hist. */
static const char IMSI[] = "001010123456789";            /* MCC 001 / MNC 01: test network */

static sim_file_t *add(card_t *c, int fid, int parent, int type) {
    sim_file_t *f = &c->files[c->nfiles++];
    memset(f, 0, sizeof *f);
    f->fid = fid; f->parent = parent; f->type = type;
    return f;
}

static void ef_data(card_t *c, int fid, int parent, const uint8_t *d, int n) {
    sim_file_t *f = add(c, fid, parent, T_EF);
    f->data = malloc(n);
    memcpy(f->data, d, n);
    f->size = n;
}

static void ef_ff(card_t *c, int fid, int parent, int n) {
    uint8_t b[64];
    memset(b, 0xFF, n);
    ef_data(c, fid, parent, b, n);
}

static void ef_records(card_t *c, int fid, int parent, int len, int k, int cyclic, const uint8_t *first) {
    sim_file_t *f = add(c, fid, parent, T_EF);
    f->structure = cyclic ? 3 : 1;
    f->rec_len = len; f->nrec = k; f->size = len * k;
    f->data = malloc(f->size);
    for (int i = 0; i < k; i++) {
        if (first) memcpy(f->data + i * len, first, len);
        else memset(f->data + i * len, 0xFF, len);
    }
}

static int bcd_swapped(const char *digits, uint8_t *out) {
    char d[32];
    int n = (int)strlen(digits);
    memcpy(d, digits, n);
    if (n % 2) d[n++] = 'F';
    for (int i = 0; i < n; i += 2) {
        int lo = d[i] == 'F' ? 15 : d[i] - '0', hi = d[i + 1] == 'F' ? 15 : d[i + 1] - '0';
        out[i / 2] = (uint8_t)(hi << 4 | lo);
    }
    return n / 2;
}

static void card_build(card_t *c) {
    enum { MF = 0x3F00, GSM = 0x7F20, TEL = 0x7F10 };
    uint8_t b[256];
    add(c, MF, -1, T_MF);
    add(c, GSM, MF, T_DF);
    add(c, TEL, MF, T_DF);
    ef_data(c, 0x2FE2, MF, b, bcd_swapped("8901010123456789012", b));     /* ICCID */
    ef_ff(c, 0x2F05, MF, 4);                                             /* ELP */
    ef_ff(c, 0x6F05, GSM, 4);                                            /* LP */
    {                                                                    /* IMSI */
        uint8_t rest[16];
        int n = bcd_swapped(IMSI + 1, rest), len = (int)strlen(IMSI);
        b[0] = (uint8_t)(1 + n);
        b[1] = (uint8_t)((IMSI[0] - '0') << 4 | (len % 2 ? 9 : 1));      /* parity: 9 = odd */
        memcpy(b + 2, rest, n);
        memset(b + 2 + n, 0xFF, 8 - 1 - n);
        ef_data(c, 0x6F07, GSM, b, 2 + n + (8 - 1 - n));
    }
    memset(b, 0xFF, 8); b[8] = 0x07; ef_data(c, 0x6F20, GSM, b, 9);       /* Kc */
    ef_ff(c, 0x6F30, GSM, 24);                                           /* PLMNsel */
    b[0] = 0x0A; ef_data(c, 0x6F31, GSM, b, 1);                          /* HPPLMN */
    /* SST: 1 CHV1 disable, 2 ADN, 4 SMS, 7 PLMNsel, 9 MSISDN, 10 EXT1, 12 SMSP, 13 LND, 17 SPN */
    ef_data(c, 0x6F38, GSM, (const uint8_t[]){0xCF, 0x30, 0xCF, 0x03, 0x03}, 5);
    b[0] = 0x01; memcpy(b + 1, "V600 EMU", 8); memset(b + 9, 0xFF, 8);
    ef_data(c, 0x6F46, GSM, b, 17);                                      /* SPN */
    memset(b, 0, 16); ef_data(c, 0x6F74, GSM, b, 16);                    /* BCCH */
    ef_data(c, 0x6F78, GSM, (const uint8_t[]){0x00, 0x01}, 2);           /* ACC */
    ef_ff(c, 0x6F7B, GSM, 12);                                           /* FPLMN */
    ef_data(c, 0x6F7E, GSM, (const uint8_t[]){0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0xF1, 0x10, 0xFF,
                                              0xFE, 0xFF, 0x01}, 11);   /* LOCI */
    ef_data(c, 0x6FAD, GSM, (const uint8_t[]){0, 0, 0, 2}, 4);           /* AD */
    b[0] = 0x02; ef_data(c, 0x6FAE, GSM, b, 1);                          /* phase 2 */
    memset(b, 0, 3); ef_data(c, 0x6F37, GSM, b, 3);                      /* ACMmax */
    memset(b, 0, 3); ef_records(c, 0x6F39, GSM, 3, 1, 1, b);             /* ACM */
    ef_ff(c, 0x6F41, GSM, 5);                                            /* PUCT */
    ef_ff(c, 0x6F45, GSM, 2);                                            /* CBMI */
    ef_records(c, 0x6F3A, TEL, 28, 20, 0, 0);                            /* ADN */
    ef_records(c, 0x6F3B, TEL, 28, 5, 0, 0);                             /* FDN */
    memset(b, 0xFF, 176); b[0] = 0; ef_records(c, 0x6F3C, TEL, 176, 10, 0, b);   /* SMS */
    ef_records(c, 0x6F3D, TEL, 14, 2, 0, 0);                             /* CCP */
    ef_records(c, 0x6F40, TEL, 28, 2, 0, 0);                             /* MSISDN */
    ef_records(c, 0x6F42, TEL, 40, 1, 0, 0);                             /* SMSP */
    ef_ff(c, 0x6F43, TEL, 2);                                            /* SMSS */
    ef_records(c, 0x6F44, TEL, 28, 5, 1, 0);                             /* LND */
    ef_records(c, 0x6F4A, TEL, 13, 5, 0, 0);                             /* EXT1 */
}

static sim_file_t *find_df(card_t *c, int fid) {
    for (int i = 0; i < c->nfiles; i++)
        if (c->files[i].type != T_EF && c->files[i].fid == fid) return &c->files[i];
    return 0;
}

static sim_file_t *find_ef(card_t *c, int parent, int fid) {
    for (int i = 0; i < c->nfiles; i++)
        if (c->files[i].type == T_EF && c->files[i].parent == parent && c->files[i].fid == fid)
            return &c->files[i];
    return 0;
}

static int response(card_t *c, sim_file_t *f, uint8_t *r) {
    if (f->type != T_EF) {
        int n_df = 0, n = 0;
        for (int i = 0; i < c->nfiles; i++)
            if (c->files[i].parent == f->fid) { n++; n_df += c->files[i].type == T_DF; }
        const uint8_t head[] = {0, 0, 0x10, 0x00, (uint8_t)(f->fid >> 8), (uint8_t)f->fid,
                                f->type == T_MF ? 1 : 2, 0, 0, 0, 0, 0, 9,
                                0x91,                    /* clock stop, 3 V, CHV1 disabled */
                                (uint8_t)n_df, (uint8_t)(n - n_df), 4, 0,
                                0x83, 0x8A, 0x83, 0x8A}; /* CHV1/2 initialised, 3 / 10 tries */
        memcpy(r, head, sizeof head);
        return sizeof head;
    }
    const uint8_t ef[] = {0, 0, (uint8_t)(f->size >> 8), (uint8_t)f->size,
                          (uint8_t)(f->fid >> 8), (uint8_t)f->fid,
                          4, f->structure == 3 ? 0x40 : 0, 0, 0, 0, 1, 2,
                          (uint8_t)f->structure, (uint8_t)f->rec_len};
    memcpy(r, ef, sizeof ef);
    return sizeof ef;
}

static sim_file_t *select_file(card_t *c, int fid) {
    if (fid == 0x3F00) return find_df(c, 0x3F00);
    int cur = c->df, parent = find_df(c, cur)->parent;
    int order[3] = {cur, parent, 0x3F00};
    for (int k = 0; k < 3; k++) {
        int df = order[k];
        if (df < 0) continue;
        sim_file_t *f = find_ef(c, df, fid);
        if (f) return f;
        f = find_df(c, fid);
        if (f && (f->parent == df || fid == parent)) return f;
    }
    return 0;
}

static int card_reset(card_t *c) {
    c->df = 0x3F00;
    c->ef = 0;
    c->nlast = 0;
    c->state = 0;                   /* header */
    c->nbuf = 0;
    c->need = 0;
    return 0;
}

#define SW(a, b) (sw[0] = (a), sw[1] = (b))

/* Runs a command: status word in sw, response data in out (returns its length). */
static int command(card_t *c, int ins, int p1, int p2, int p3, const uint8_t *data, int ndata,
                   uint8_t sw[2], uint8_t *out) {
    if (ins == 0xA4) {                                           /* SELECT */
        sim_file_t *f = ndata >= 2 ? select_file(c, data[0] << 8 | data[1]) : 0;
        if (!f) { SW(0x94, 0x04); return 0; }
        if (f->type == T_EF) c->ef = f;
        else { c->df = f->fid; c->ef = 0; }
        c->nlast = response(c, f, c->last);
        SW(0x9F, c->nlast);
        return 0;
    }
    if (ins == 0xC0) {                                           /* GET RESPONSE */
        int n = p3 ? p3 : 256;
        if (n > c->nlast) { SW(0x67, c->nlast); return 0; }
        memcpy(out, c->last, n);
        SW(0x90, 0);
        return n;
    }
    if (ins == 0xF2) {                                           /* STATUS */
        uint8_t r[32];
        int nr = response(c, find_df(c, c->df), r), n = p3 ? p3 : 256;
        memset(out, 0, n);
        memcpy(out, r, nr < n ? nr : n);
        SW(0x90, 0);
        return n;
    }
    sim_file_t *f = c->ef;
    if (ins == 0xB0 || ins == 0xD6) {                            /* READ / UPDATE BINARY */
        if (!f) { SW(0x94, 0x00); return 0; }
        if (f->structure != 0) { SW(0x94, 0x08); return 0; }
        int off = p1 << 8 | p2, n = p3 ? p3 : 256;
        if (off + (ins == 0xD6 ? ndata : n) > f->size) { SW(0x67, 0x00); return 0; }
        SW(0x90, 0);
        if (ins == 0xD6) { memcpy(f->data + off, data, ndata); return 0; }
        memcpy(out, f->data + off, n);
        return n;
    }
    if (ins == 0xB2 || ins == 0xDC) {                            /* READ / UPDATE RECORD */
        if (!f) { SW(0x94, 0x00); return 0; }
        if (!f->nrec) { SW(0x94, 0x08); return 0; }
        int cur = f->rec, n = f->nrec;
        if (p2 == 0x02) cur = cur < n ? cur + 1 : (f->structure == 3 ? 1 : 0);
        else if (p2 == 0x03) cur = cur > 1 ? cur - 1 : (f->structure == 3 ? n : 0);
        else if (p2 == 0x04) cur = p1 ? p1 : cur;
        if (cur < 1 || cur > n) { SW(0x94, 0x02); return 0; }
        if ((p3 ? p3 : 256) != f->rec_len) { SW(0x67, f->rec_len); return 0; }
        f->rec = cur;
        SW(0x90, 0);
        uint8_t *rec = f->data + (cur - 1) * f->rec_len;
        if (ins == 0xDC) {                                       /* rec[:] = data */
            memcpy(rec, data, ndata < f->rec_len ? ndata : f->rec_len);
            return 0;
        }
        memcpy(out, rec, f->rec_len);
        return f->rec_len;
    }
    if (ins == 0xA2) { SW(0x94, 0x04); return 0; }               /* SEEK */
    if (ins == 0x32) { SW(0x98, 0x04); return 0; }               /* INCREASE */
    if (ins == 0x88) {                                           /* RUN GSM ALGORITHM */
        memset(c->last, 0, 12);
        c->nlast = 12;                                           /* SRES + Kc */
        SW(0x9F, 0x0C);
        return 0;
    }
    if (ins == 0x20 || ins == 0x24 || ins == 0x26 || ins == 0x28 || ins == 0x2C ||
        ins == 0xFA || ins == 0x04 || ins == 0x44 || ins == 0x10 || ins == 0x14 || ins == 0xC2) {
        SW(0x90, 0);                                             /* CHV, sleep, SAT: accepted */
        return 0;
    }
    SW(0x6D, 0x00);
    return 0;
}

static int incoming(int ins) {
    static const uint8_t in[] = {0xA4, 0xD6, 0xDC, 0x20, 0x24, 0x26, 0x28, 0x2C, 0x88, 0xA2,
                                 0x32, 0x10, 0x14, 0xC2};
    for (unsigned i = 0; i < sizeof in; i++) if (in[i] == ins) return 1;
    return 0;
}

static int outgoing(int ins) {
    return ins == 0xB0 || ins == 0xB2 || ins == 0xC0 || ins == 0xF2 || ins == 0x12;
}

static int execute(card_t *c, const uint8_t *data, int ndata, uint8_t *reply) {
    uint8_t sw[2], out[300];
    int n = command(c, c->hdr[1], c->hdr[2], c->hdr[3], c->hdr[4], data, ndata, sw, out);
    if (outgoing(c->hdr[1]) && sw[0] == 0x90) {
        reply[0] = c->hdr[1];
        memcpy(reply + 1, out, n);
        reply[1 + n] = sw[0]; reply[2 + n] = sw[1];
        return n + 3;
    }
    reply[0] = sw[0]; reply[1] = sw[1];
    return 2;
}

/* One byte from the phone; returns how many bytes the card sends back (in reply). */
static int card_feed(card_t *c, int byte, uint8_t *reply) {
    if (c->nbuf < (int)sizeof c->buf) c->buf[c->nbuf++] = byte;
    if (c->state == 0) {
        if (c->nbuf < 5) return 0;
        memcpy(c->hdr, c->buf, 5);
        c->nbuf = 0;
        if (c->hdr[0] != 0xA0) { reply[0] = 0x6E; reply[1] = 0; return 2; }
        if (incoming(c->hdr[1]) && c->hdr[4]) {
            c->state = 1;
            c->need = c->hdr[4];
            reply[0] = c->hdr[1];                                /* procedure byte */
            return 1;
        }
        return execute(c, 0, 0, reply);
    }
    if (c->nbuf >= c->need) {
        uint8_t data[300];
        int n = c->nbuf;
        memcpy(data, c->buf, n);
        c->nbuf = 0;
        c->state = 0;
        return execute(c, data, n, reply);
    }
    return 0;
}

/* ---- the interface ---------------------------------------------------------------------- */
#define OUT_CAP 2048
#define TX_CAP 1024

struct sim {
    device_t d;
    card_t *card;
    double ips;
    uint32_t r[0x100];
    uint8_t rx[64]; int nrx;
    uint8_t tx[TX_CAP]; int tx_head, ntx;
    struct { uint64_t due; uint8_t b; } out[OUT_CAP];
    int out_head, nout;
    int tx_busy, tx_byte, gp_running, awake;
    uint64_t tx_busy_until, gp_start;
    int64_t gp_matches;
};

static double card_hz(sim_t *s) {
    static const double div[4] = {4, 3, 5, 1};
    return REF_HZ / div[(s->r[CNTL] >> 4) & 3];
}

static double etu_clocks(sim_t *s) {
    static const double etu[8] = {372, 256, 128, 64, 32, 32, 16, 372};
    return etu[(s->r[CNTL] >> 6) & 7];
}

static uint64_t byte_insns(sim_t *s) { return (uint64_t)(12 * etu_clocks(s) / card_hz(s) * s->ips); }

static uint64_t now(sim_t *s) { return s->d.m->insns; }

/* General-purpose counter value (a double, as in the model it came from); 0 if stopped. */
static int gp_count(sim_t *s, double *n) {
    int sel = (s->r[CNTL] >> 9) & 3;
    if (!sel || !s->gp_running) return 0;
    double clocks = (double)(now(s) - s->gp_start) * card_hz(s) / s->ips;
    *n = sel == 1 ? clocks : clocks / etu_clocks(s) * (sel == 2 ? 16 : 1);
    return 1;
}

static double gp_target(sim_t *s) { return (double)s->r[GPCNT] + 65536.0 * (double)s->gp_matches; }

static void send(sim_t *s, const uint8_t *data, int n, uint64_t start) {
    uint64_t t = s->nout ? s->out[(s->out_head + s->nout - 1) % OUT_CAP].due : 0;
    if (start > t) t = start;
    for (int i = 0; i < n && s->nout < OUT_CAP; i++) {
        t += byte_insns(s);
        int k = (s->out_head + s->nout++) % OUT_CAP;
        s->out[k].due = t;
        s->out[k].b = data[i];
    }
}

/* A powered card answers (ATR) once it is clocked and out of reset: after power-up (cold) or
   after the reset line is released (warm). Stopping and restarting the clock (the phone does
   this when idle, the card allows it) does not reset it. */
static void port_changed(sim_t *s, uint32_t value) {
    if (!(value & 0x02) || (value & 0x08)) {
        s->awake = 0;
        s->nout = s->out_head = 0;
        return;
    }
    if (s->card && !s->awake && (value & 0x10)) {
        s->awake = 1;
        card_reset(s->card);
        s->ntx = s->tx_head = 0;
        send(s, ATR, sizeof ATR, now(s) + (uint64_t)(1000 / card_hz(s) * s->ips));
    }
}

static void status(sim_t *s) {
    uint32_t rs = s->r[RSTAT] & ~0x30u, th = s->r[RCVTH] & 0x1F;
    if (s->nrx) rs |= 0x10;
    if ((uint32_t)s->nrx >= (th > 1 ? th : 1)) rs |= 0x20;
    s->r[RSTAT] = rs;
}

static void irq(sim_t *s) {
    status(s);
    uint32_t mask = s->r[IMASK], xs = s->r[XSTAT], rs = s->r[RSTAT];
    int data = ((xs & 0x20) && !(mask & 0x02)) || ((xs & 0x10) && !(mask & 0x08)) ||
               ((xs & 0x08) && !(mask & 0x10)) || ((rs & 0x20) && !(mask & 0x01)) ||
               ((xs & 0x80) && !(mask & 0x80));
    int general = ((xs & 0x100) && !(mask & 0x100)) || ((xs & 0x01) && !(mask & 0x20)) ||
                  ((xs & 0x40) && !(mask & 0x40)) || ((rs & 0x01) && !(mask & 0x04));
    aitc_set_line(s->d.m->aitc, DATA_SOURCE, data);
    aitc_set_line(s->d.m->aitc, GENERAL_SOURCE, general);
}

static int active_card(sim_t *s) {
    uint32_t v = s->r[P0CNTL];
    return (v & 0x02) && (v & 0x10) && !(v & 0x08);
}

void sim_update(sim_t *s) {
    uint64_t t = now(s);
    while (s->nout && s->out[s->out_head].due <= t) {          /* card -> receive FIFO */
        if ((s->r[ENABLE] & 1) && s->nrx >= 32) {
            s->r[RSTAT] |= 0x01;                               /* overrun (oef) */
            if (s->r[CNTL] & 0x08) {                           /* onack: NACK, card resends */
                uint64_t shift = t + byte_insns(s) - s->out[s->out_head].due;
                for (int i = 0; i < s->nout; i++) s->out[(s->out_head + i) % OUT_CAP].due += shift;
                break;
            }
            s->out_head = (s->out_head + 1) % OUT_CAP; s->nout--;   /* the byte is lost */
            continue;
        }
        uint8_t b = s->out[s->out_head].b;
        s->out_head = (s->out_head + 1) % OUT_CAP; s->nout--;
        if (s->r[ENABLE] & 1) {
            if ((s->r[CNTL] & 0x02) && (b == 0x3B || b == 0x3F))
                s->r[CNTL] &= ~0x02u;                          /* a valid TS ends icm */
            s->rx[s->nrx++] = b;
        }
    }
    if (s->tx_busy && t >= s->tx_busy_until) {
        int b = s->tx_byte;
        s->tx_busy = 0;
        if (s->card && active_card(s)) {
            uint8_t reply[300];
            int n = card_feed(s->card, b, reply);
            if (n) send(s, reply, n, t + byte_insns(s));
        }
        if (!s->ntx) s->r[XSTAT] |= 0x30;                      /* etc, tc */
    }
    if (!s->tx_busy && s->ntx && (s->r[ENABLE] & 2)) {
        s->tx_byte = s->tx[s->tx_head];
        s->tx_head = (s->tx_head + 1) % TX_CAP; s->ntx--;
        s->tx_busy = 1;
        s->tx_busy_until = t + byte_insns(s);
        if ((uint32_t)s->ntx <= (s->r[XTH] & 0xF)) s->r[XSTAT] |= 0x80;   /* tdtf */
        if (!s->ntx) s->r[XSTAT] |= 0x08;                      /* tfe */
    }
    double n;
    if (gp_count(s, &n) && n >= gp_target(s)) {
        s->gp_matches = (int64_t)floor((n - (double)s->r[GPCNT]) / 65536.0) + 1;
        s->r[XSTAT] |= 0x100;
    }
    irq(s);
}

/* Next time something happens, in counter units (instructions x ticks per instruction),
   or -1. */
double sim_next_event(sim_t *s) {
    uint64_t best = UINT64_MAX;
    if (s->nout) best = s->out[s->out_head].due;
    if (s->tx_busy) { if (s->tx_busy_until < best) best = s->tx_busy_until; }
    else if (s->ntx && (s->r[ENABLE] & 2)) { if (now(s) + 1 < best) best = now(s) + 1; }
    double n;
    if (gp_count(s, &n)) {
        double left = gp_target(s) - n;
        int sel = (s->r[CNTL] >> 9) & 3;
        double per_clock = s->ips / card_hz(s) * (sel == 1 ? 1 : etu_clocks(s) / (sel == 2 ? 16 : 1));
        int64_t k = (int64_t)(left * per_clock) + 1;
        uint64_t t = now(s) + (uint64_t)(k > 1 ? k : 1);
        if (t < best) best = t;
    }
    return best == UINT64_MAX ? -1 : (double)best * s->d.m->ticks_per_insn;
}

static uint32_t reg_read(sim_t *s, uint32_t off) {
    sim_update(s);
    if (off == P0RCV) {
        uint32_t v = 0;
        if (s->nrx) { v = s->rx[0]; memmove(s->rx, s->rx + 1, --s->nrx); }
        irq(s);
        return v;
    }
    status(s);
    return s->r[off & 0xFF];
}

static uint32_t sim_read(device_t *d, uint32_t off, int size) {
    sim_t *s = (sim_t *)d;
    if (size == 4) {
        uint32_t hi = sim_read(d, off, 2);
        return hi << 16 | sim_read(d, off + 2, 2);
    }
    if (size == 1) {
        uint32_t v = sim_read(d, off & ~1u, 2);
        return off % 2 == 0 ? v >> 8 : v & 0xFF;
    }
    return reg_read(s, off);
}

static void sim_write(device_t *d, uint32_t off, int size, uint32_t value) {
    sim_t *s = (sim_t *)d;
    if (size == 4) {
        sim_write(d, off, 2, value >> 16);
        sim_write(d, off + 2, 2, value & 0xFFFF);
        return;
    }
    if (size == 1) {
        uint32_t cur = s->r[(off & ~1u) & 0xFF];
        value = off % 2 == 0 ? (cur & 0x00FF) | value << 8 : (cur & 0xFF00) | value;
        off &= ~1u;
    }
    off &= 0xFF;
    value &= 0xFFFF;
    if (off == XSTAT) {
        s->r[off] &= ~(value & 0x01F9);
    } else if (off == RSTAT) {
        s->r[off] &= ~(value & 0x0F01);
    } else if (off == P0XMT) {
        if (s->ntx < TX_CAP) s->tx[(s->tx_head + s->ntx++) % TX_CAP] = value & 0xFF;
        s->r[XSTAT] &= ~0x38u;                                  /* transmission pending */
    } else if (off == RESET_CNTL) {
        if (value & 0x0A) s->nrx = 0;                           /* flush_rcv / soft_reset */
        if (value & 0x0C) s->ntx = s->tx_head = 0;              /* flush_xmt / soft_reset */
        s->r[off] = value & ~0x0Eu;
    } else {
        uint32_t old = s->r[off];
        s->r[off] = value;
        if (off == P0CNTL) port_changed(s, value);
        if (off == CNTL && ((value >> 9) & 3) != ((old >> 9) & 3)) {
            s->gp_running = ((value >> 9) & 3) != 0;
            s->gp_start = now(s);
            s->gp_matches = 0;
        }
        if (off == GPCNT) {                                     /* next match of the count */
            double n = 0;
            gp_count(s, &n);
            double v = (double)(value & 0xFFFF);
            s->gp_matches = 0;
            if (n > v) {
                int64_t k = (int64_t)floor((n - v) / 65536.0) + 1;
                s->gp_matches = k > 0 ? k : 0;
            }
        }
    }
    sim_update(s);
}

sim_t *sim_new(machine_t *m, int with_card, double ips) {
    sim_t *s = calloc(1, sizeof *s);
    s->d = (device_t){sim_read, sim_write, m};
    s->ips = ips;
    s->r[CNTL] = 0x0006;
    s->r[RCVTH] = 0x0001;
    s->r[XSTAT] = 0x0038;               /* tc, etc, tfe set out of reset */
    s->r[IMASK] = 0x1FFF;               /* everything masked out of reset */
    if (with_card) {
        s->card = calloc(1, sizeof *s->card);
        card_build(s->card);
        card_reset(s->card);
    }
    return s;
}
