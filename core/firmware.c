/* Hooks into the firmware: the boot-ROM routines, targeted overrides, the firmware's own
   log, and a pretend GSM network. Ported from v600/bootrom.py, overrides.py, fwlog.py and
   network.py (tag py-reference). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "machine.h"

static uint32_t r(machine_t *m, int n) { return arm_reg_read(m->cpu, n); }

/* Skip the rest of the current function: r0 = value, back to lr. */
static void return_from(machine_t *m, uint32_t value) {
    arm_reg_write(m->cpu, 0, value);
    arm_reg_write(m->cpu, 15, r(m, 14));
}

/* ---- boot ROM ---------------------------------------------------------------------------
   The firmware calls ~9 routines below 0x20000 that live in the chip's boot ROM (not in
   the superfile). Each gets a Thumb `bx lr` and a hook that sets r0. The exception vectors
   (bootloader-owned, at 0) point IRQ / FIQ at the firmware's veneers that the C runtime
   copies into internal RAM; the others land on traps that report and stop. */
#define TRAP 0x100u
#define IRQ_VENEER 0x03FC3604u
#define FIQ_VENEER 0x03FC3694u
static const char *VECTOR_NAMES[6] = {"reset", "undefined instruction", "SWI", "prefetch abort",
                                      "data abort", "reserved"};

enum { ROM_ZERO, ROM_QUEUED, ROM_RECEIVE, ROM_SEND };
static const struct { uint32_t addr; const char *name; int kind; } ROUTINES[] = {
    {0x2188, "rom_2188_mode", ROM_ZERO},   /* result compared with 2 at 0x1087102E / 0x108710C8 */
    {0x37CE, "rom_37ce_sec", ROM_ZERO},    /* (0x11, buf, 0, r3, [sp]) security service */
    {0xFCDC, "rom_fcdc_init", ROM_ZERO},   /* first call of the boot sequence, no args */
    {0xFFD0, "dsp_queued", ROM_QUEUED},    /* (queue) -> words waiting (dsp.c) */
    {0xFFF4, "dsp_receive", ROM_RECEIVE},  /* (queue, buf) -> 1 if a message */
    {0x100C2, "dsp_receive2", ROM_RECEIVE},/* the same; the audio reply receiver */
    {0x101CC, "dsp_send", ROM_SEND},       /* (type, buf, nwords, flag) -> 1 */
    {0x102AA, "rom_102aa", ROM_ZERO},      /* (1) */
    {0x102C0, "rom_102c0", ROM_ZERO},
};

static void rom_stub(machine_t *m, void *ctx) {
    int i = (int)(intptr_t)ctx;
    uint32_t v = 0;
    switch (ROUTINES[i].kind) {
    case ROM_QUEUED: v = audio_space(m->audio); break;
    case ROM_RECEIVE: v = dsp_receive(m->dsp, r(m, 0), r(m, 1)); break;
    case ROM_SEND: v = dsp_send(m->dsp, r(m, 0), r(m, 1), r(m, 2)); break;
    }
    arm_reg_write(m->cpu, 0, v);
}

static void trap(machine_t *m, void *ctx) {
    const char *name = ctx;
    machine_log(m, 0, "!! exception: %s  lr=%08x spsr=%08x (thumb=%d)", name, r(m, 14),
                r(m, REG_SPSR), (r(m, REG_SPSR) >> 5) & 1);
    machine_stop(m, name);
}

void bootrom_install(machine_t *m) {
    static const uint8_t ldr_pc[4] = {0xE5, 0x9F, 0xF0, 0x18}, b_self[4] = {0xEA, 0xFF, 0xFF, 0xFE};
    static const uint8_t bx_lr[2] = {0x47, 0x70};
    for (uint32_t slot = 0; slot < 8; slot++) {           /* ldr pc, [pc, #0x18] */
        arm_mem_write(m->cpu, slot * 4, ldr_pc, 4);
        uint32_t t = slot < 6 ? TRAP + 8 * slot : slot == 6 ? IRQ_VENEER : FIQ_VENEER;
        uint8_t b[4] = {t >> 24, t >> 16, t >> 8, t};
        arm_mem_write(m->cpu, 0x20 + slot * 4, b, 4);
    }
    for (uint32_t slot = 0; slot < 6; slot++) {
        arm_mem_write(m->cpu, TRAP + 8 * slot, b_self, 4);
        machine_hook(m, TRAP + 8 * slot, trap, (void *)VECTOR_NAMES[slot], 0);
    }
    for (unsigned i = 0; i < sizeof ROUTINES / sizeof ROUTINES[0]; i++) {
        arm_mem_write(m->cpu, ROUTINES[i].addr, bx_lr, 2);
        machine_hook(m, ROUTINES[i].addr, rom_stub, (void *)(intptr_t)i, 0);
    }
}

/* ---- overrides ----------------------------------------------------------------------------
   Where a real model would need per-phone data we don't have. */
#define PRODUCT_TYPE 3          /* expected by the reflash check (0x10AB9304): header byte 0x10080019 */

/* 0x10647372(item, buf) -> 0 on success. Item 0x293 is the product type. */
static void read_secure_item(machine_t *m, void *unused) {
    if (r(m, 0) == 0x293) {
        uint8_t b = PRODUCT_TYPE;
        arm_mem_write(m->cpu, r(m, 1), &b, 1);
        return_from(m, 0);
    }
}

/* 0x10AE4784 powers off if the ADC battery reading (table 0x121BFF14) is below 0x68. The
   reading arrives via the power IC's ADC-done interrupt, not modelled yet. TEMPORARY. */
static void skip(machine_t *m, void *unused) { return_from(m, 0); }

void overrides_install(machine_t *m, int sim_card) {
    if (!sim_card) {
        /* No SIM card: the reset state machine (0x101B83C8) calls card reset 0x103A0624 and
           retries via deactivate 0x103A08C8; both busy-wait on the interface, so reset never
           gets an answer and deactivate completes at once. */
        machine_hook(m, 0x103A0624, skip, 0, 0);
        machine_hook(m, 0x103A08C8, skip, 0, 0);
    }
    machine_hook(m, 0x10647372, read_secure_item, 0, 0);
    machine_hook(m, 0x10AE4784, skip, 0, 0);
}

/* ---- the firmware's log: its vsprintf (0x103F919C, (buf, fmt, va_list *)) -------------- */
#define VSPRINTF 0x103F919Cu

static int cstr(machine_t *m, uint32_t a, char *out, int limit) {
    int n = 0;
    while (n < limit) {
        uint8_t b;
        if (arm_mem_read(m->cpu, a + n, &b, 1)) return -1;
        if (!b) break;
        out[n++] = (char)b;
    }
    out[n] = 0;
    return n;
}

static void vsprintf_hook(machine_t *m, void *unused) {
    char fmt[256], out[1024], s[256];
    if (cstr(m, r(m, 1), fmt, 200) < 0) strcpy(fmt, "<bad ptr>");
    uint8_t b[4];
    uint32_t argp = 0;
    if (!arm_mem_read(m->cpu, r(m, 2), b, 4)) argp = (uint32_t)b[0] << 24 | b[1] << 16 | b[2] << 8 | b[3];
    int k = 0;
    for (const char *p = fmt; *p && k < (int)sizeof out - 64; p++) {
        if (*p != '%') { out[k++] = *p; continue; }
        const char *q = p + 1;                             /* flags, width, precision, length */
        while (*q && strchr("-+ #0", *q)) q++;
        while (*q >= '0' && *q <= '9') q++;
        if (*q == '.') { q++; while (*q >= '0' && *q <= '9') q++; }
        while (*q == 'l' || *q == 'h') q++;
        if (!*q || !strchr("diuxXcsp%", *q)) { out[k++] = *p; continue; }
        if (*q == '%') { out[k++] = '%'; p = q; continue; }
        uint32_t v = machine_read32(m, argp);
        argp += 4;
        switch (*q) {
        case 'd': case 'i': k += snprintf(out + k, sizeof out - k, "%d", (int32_t)v); break;
        case 'u': k += snprintf(out + k, sizeof out - k, "%u", v); break;
        case 'x': k += snprintf(out + k, sizeof out - k, "%x", v); break;
        case 'X': k += snprintf(out + k, sizeof out - k, "%X", v); break;
        case 'p': k += snprintf(out + k, sizeof out - k, "%#x", v); break;
        case 'c': out[k++] = (char)(v & 0xFF); break;
        case 's':
            if (cstr(m, v, s, 200) < 0) strcpy(s, "<bad ptr>");
            k += snprintf(out + k, sizeof out - k, "%s", s);
            break;
        }
        if (k >= (int)sizeof out - 64) break;
        p = q;
    }
    while (k && (out[k - 1] == '\n' || out[k - 1] == ' ' || out[k - 1] == '\r')) k--;
    out[k] = 0;
    if (m->log) m->log(1, out);
}

void fwlog_install(machine_t *m) { machine_hook(m, VSPRINTF, vsprintf_hook, 0, 0); }

/* ---- a pretend GSM network ------------------------------------------------------------
   At the boundary between the phone's protocol stack and its services. The radio side
   cannot run (layer 1 is driven by the DSP, whose program is mask ROM we do not have), so
   mobility management (MM) never answers; this answers the services directly with the
   messages MM would send (OS messages, posted through machine_call).

   Registration (service provider DL_SIG_REG_GSM_SP, state table 0x10412E48):
     MNMM_REG_REQ 0x14C01, built by send_mnmm_reg_req (0x100C177A, r0 = type; type 1 once
         the SIM is ready).
     0x14C40 from MM (handler 0x100C1ED4): service status; MM sends one at power-up, which
         is how we learn the services' port.
     0x14C41 from MM (handler 0x100C21EA): registration result, +2 status (4: home; 2, 3:
         roaming triangle), +3..+5 network (BCD as in GSM 04.08), +7..+8 location area.
   Calls (GSM 04.08 call control, carried over an MM connection, group MMCM):
     0x15001 connection request [protocol (3 = CC), transaction, ...] -> 0x15020 [protocol,
         transaction, 0] (built by MM at 0x1094F808) to the port MM looks up with
         0x10A1D4C4(protocol, transaction); 0x15000 data to the network [protocol,
         transaction, length, L3]; 0x15040 data from the network [protocol, transaction,
         length (2 bytes), L3]; 0x15003 connection released by the phone.
     An outgoing call: SETUP -> CALL PROCEEDING, ALERTING, CONNECT (answered by CONNECT
     ACKNOWLEDGE); DISCONNECT -> RELEASE -> RELEASE COMPLETE.
     An incoming call: 0x15042 [protocol, transaction, length (2 bytes), L3] delivers the
     first message of a transaction the network opens (call control's table at 0x104123D8
     takes SETUP, RELEASE COMPLETE or STATUS there; 0x15040's, at 0x104123A4, the rest).
     SETUP (bearer: speech; calling party number) -> the phone's CALL CONFIRMED, ALERTING;
     answered, its CONNECT -> CONNECT ACKNOWLEDGE. The network's messages carry transaction
     flag 0 (it opened the transaction), the phone's flag 1; the MMCM transaction field
     holds it in bit 3 (8 | value), or call control releases the connection at once.
     The far end hangs up: DISCONNECT (cause 16, normal clearing) -> the phone's RELEASE ->
     RELEASE COMPLETE.
   Signal strength: layer 1 sends 0x11810 (built at 0x108D6884) to the port at 0x121B7020:
     +0 level in percent (0, 10, 30, 50, 70, 90 for 0-5 bars), +1 dBm + 110. */
#define CREATE_MESSAGE 0x1087BEF4u      /* (size, id, 0, 0) -> message body */
#define SEND_MESSAGE 0x10AC73A0u        /* (message, port, 0); the id is at message - 8 */
#define SEND_REG_REQ 0x100C177Au
#define MNMM_STATUS 0x14C40u
#define MNMM_REG_CNF 0x14C41u
#define REGISTERED 4
#define SIGNAL_IND 0x11810u
#define SIGNAL_PORT 0x121B7020u
#define MMCM_DATA_REQ 0x15000u
#define MMCM_EST_REQ 0x15001u
#define MMCM_REL_REQ 0x15003u
#define MMCM_EST_CNF 0x15020u
#define MMCM_DATA_IND 0x15040u
#define MMCM_EST_IND 0x15042u
#define MMCM_PORT 0x10A1D4C4u
#define CALL_CONTROL 3
enum { ALERTING = 0x01, CALL_PROCEEDING = 0x02, SETUP = 0x05, CONNECT = 0x07,
       CONNECT_ACK = 0x0F, DISCONNECT = 0x25, RELEASE = 0x2D, RELEASE_COMPLETE = 0x2A };
#define INCOMING_TI 0                   /* transaction the network opens for a call */
#define TI_FLAG 8                       /* in the MMCM transaction field: not opened by the phone */
#define ANSWER_AFTER 4.0                /* seconds of ringing before the far end answers */
#define REGISTRATION_DELAY 2.0
static const uint8_t SIGNAL[2] = {90, 110 - 65};                  /* 5 bars, -65 dBm */
static const uint8_t PLMN[3] = {0x00, 0xF1, 0x10};               /* MCC 001, MNC 01 */
static const uint8_t LAC[2] = {0x00, 0x01};

enum { EV_REGISTER, EV_CONNECT, EV_TO_PHONE, EV_INCOMING };
typedef struct { uint64_t due; int kind, call; uint32_t a, b, c; } net_event_t;

struct network {
    machine_t *m;
    uint64_t ips;
    int have_port, have_call_port, registered;
    uint32_t port, call_port;
    int in_call;                              /* a call transaction is open */
    uint32_t call_ti, call_first;             /* its transaction and the phone's first L3 byte */
    char caller[24];                          /* the incoming call's number */
    net_event_t events[64]; int nevents;
    uint8_t from_phone[256]; int nfrom;       /* call-control types the phone sent (tests) */
};

typedef struct { network_t *n; uint32_t port; int len; uint8_t body[64]; } post_t;

static void filled(machine_t *m, void *ctx, uint32_t message) {
    post_t *p = ctx;
    arm_mem_write(m->cpu, message, p->body, p->len);
    uint32_t args[3] = {message, p->port, 0};
    machine_call(m, SEND_MESSAGE, args, 3, 0, 0);
    free(p);
}

/* Send an OS message to the phone's services, as the protocol stack would. */
static void post(network_t *n, uint32_t ident, const uint8_t *body, int len, uint32_t port) {
    post_t *p = malloc(sizeof *p);
    p->n = n; p->port = port; p->len = len;
    memcpy(p->body, body, len);
    uint32_t args[4] = {(uint32_t)len, ident, 0, 0};
    machine_call(n->m, CREATE_MESSAGE, args, 4, filled, p);
}

static void after(network_t *n, double seconds, int kind, int call, uint32_t a, uint32_t b, uint32_t c) {
    if (n->nevents == 64) return;
    n->events[n->nevents++] = (net_event_t){n->m->insns + (uint64_t)(seconds * (double)n->ips), kind, call, a, b, c};
}

static void drop_call_events(network_t *n) {
    int k = 0;
    for (int i = 0; i < n->nevents; i++) if (!n->events[i].call) n->events[k++] = n->events[i];
    n->nevents = k;
}

static void found(machine_t *m, void *ctx, uint32_t port) {     /* the MM connection is up */
    post_t *p = ctx;
    p->n->call_port = port;
    p->n->have_call_port = 1;
    post(p->n, MMCM_EST_CNF, p->body, 3, port);
    free(p);
}

static void connect(network_t *n, uint32_t protocol, uint32_t transaction) {
    post_t *p = malloc(sizeof *p);
    p->n = n;
    p->body[0] = protocol; p->body[1] = transaction; p->body[2] = 0;
    uint32_t args[2] = {protocol, transaction};
    machine_call(n->m, MMCM_PORT, args, 2, found, p);
}

static void to_phone(network_t *n, uint32_t transaction, uint32_t first, uint32_t kind) {
    uint8_t body[10] = {CALL_CONTROL, (uint8_t)transaction, 0, 2, (uint8_t)(first ^ 0x80), (uint8_t)kind};
    int len = 6;
    if (kind == DISCONNECT) {                           /* cause: 16, normal call clearing */
        const uint8_t cause[4] = {0x08, 0x02, 0xE0, 0x90};
        memcpy(body + 6, cause, 4);
        body[3] += 4;
        len += 4;
    }
    if (kind == RELEASE_COMPLETE) n->in_call = 0;
    post(n, MMCM_DATA_IND, body, len, n->call_port);    /* the other end of the transaction */
}

/* SETUP for an incoming call: speech, and the caller's number (unknown type, ISDN plan). */
static int incoming_setup(network_t *n, uint8_t *l3) {
    int k = 0, digits = 0;
    l3[k++] = CALL_CONTROL | INCOMING_TI << 4;
    l3[k++] = SETUP;
    l3[k++] = 0x04; l3[k++] = 0x01; l3[k++] = 0xA0;     /* bearer capability: speech, full rate */
    uint8_t bcd[12] = {0};
    for (const char *c = n->caller; *c && digits < 22; c++)
        if (*c >= '0' && *c <= '9') { bcd[digits / 2] |= (uint8_t)((*c - '0') << (digits % 2 ? 4 : 0)); digits++; }
    if (digits) {
        if (digits % 2) bcd[digits / 2] |= 0xF0;
        int nb = (digits + 1) / 2;
        l3[k++] = 0x5C; l3[k++] = (uint8_t)(1 + nb); l3[k++] = 0x81;
        memcpy(l3 + k, bcd, nb);
        k += nb;
    }
    return k;
}

static void incoming_port(machine_t *m, void *ctx, uint32_t port) {
    network_t *n = ctx;
    n->call_port = port;
    n->have_call_port = 1;
    uint8_t body[48] = {CALL_CONTROL, TI_FLAG | INCOMING_TI, 0, 0};
    int len = incoming_setup(n, body + 4);
    body[3] = (uint8_t)len;
    post(n, MMCM_EST_IND, body, 4 + len, port);
}

static void call_control(network_t *n, uint32_t transaction, const uint8_t *l3) {
    int kind = l3[1] & 0x3F;
    if (n->nfrom < (int)sizeof n->from_phone) n->from_phone[n->nfrom++] = kind;
    n->call_ti = transaction;
    n->call_first = l3[0];
    if (kind == SETUP) n->in_call = 1;
    if (kind == CONNECT) {                              /* an incoming call, answered */
        after(n, 0.2, EV_TO_PHONE, 1, transaction, l3[0], CONNECT_ACK);
    } else if (kind == SETUP) {
        after(n, 0.3, EV_TO_PHONE, 1, transaction, l3[0], CALL_PROCEEDING);
        after(n, 1.0, EV_TO_PHONE, 1, transaction, l3[0], ALERTING);
        after(n, 1.0 + ANSWER_AFTER, EV_TO_PHONE, 1, transaction, l3[0], CONNECT);
    } else if (kind == DISCONNECT) {
        drop_call_events(n);
        after(n, 0.2, EV_TO_PHONE, 1, transaction, l3[0], RELEASE);
    } else if (kind == RELEASE) {
        after(n, 0.2, EV_TO_PHONE, 1, transaction, l3[0], RELEASE_COMPLETE);
    } else if (kind == RELEASE_COMPLETE) {
        n->in_call = 0;
    }
}

static void sent(machine_t *m, void *ctx) {
    network_t *n = ctx;
    uint32_t message = r(m, 0), ident = machine_read32(m, message - 8);
    uint8_t h[3];
    if (ident == MNMM_STATUS && !n->have_port) {
        n->port = r(m, 1);
        n->have_port = 1;
    } else if (ident == MMCM_EST_REQ) {
        arm_mem_read(m->cpu, message, h, 2);
        if (h[0] == CALL_CONTROL) after(n, 0.3, EV_CONNECT, 0, h[0], h[1], 0);
    } else if (ident == MMCM_DATA_REQ) {
        arm_mem_read(m->cpu, message, h, 3);
        if (h[0] == CALL_CONTROL) {
            uint8_t l3[256] = {0};
            arm_mem_read(m->cpu, message + 3, l3, h[2]);
            call_control(n, h[1], l3);
        }
    } else if (ident == MMCM_REL_REQ) {
        drop_call_events(n);
        n->in_call = 0;
    }
}

static void reg_req(machine_t *m, void *ctx) {
    if (r(m, 0) == 1) after(ctx, REGISTRATION_DELAY, EV_REGISTER, 0, 0, 0, 0);
}

static void register_phone(network_t *n) {
    n->registered = 1;
    uint8_t body[0x20] = {0};
    body[2] = REGISTERED;
    memcpy(body + 3, PLMN, 3);
    memcpy(body + 7, LAC, 2);
    post(n, MNMM_REG_CNF, body, sizeof body, n->port);
    post(n, SIGNAL_IND, SIGNAL, 2, machine_read32(n->m, SIGNAL_PORT));
}

static void network_update(void *ctx) {
    network_t *n = ctx;
    net_event_t due[64];
    int nd = 0, k = 0;
    for (int i = 0; i < n->nevents; i++) {
        if (n->events[i].due <= n->m->insns) due[nd++] = n->events[i];
        else n->events[k++] = n->events[i];
    }
    n->nevents = k;
    for (int i = 0; i < nd; i++) {
        if (due[i].kind == EV_REGISTER) register_phone(n);
        else if (due[i].kind == EV_CONNECT) connect(n, due[i].a, due[i].b);
        else if (due[i].kind == EV_INCOMING) {
            uint32_t args[2] = {CALL_CONTROL, INCOMING_TI};
            machine_call(n->m, MMCM_PORT, args, 2, incoming_port, n);
        }
        else to_phone(n, due[i].a, due[i].b, due[i].c);
    }
}

static double network_next(void *ctx) {
    network_t *n = ctx;
    if (!n->nevents) return -1;
    uint64_t best = n->events[0].due;
    for (int i = 1; i < n->nevents; i++) if (n->events[i].due < best) best = n->events[i].due;
    return (double)best * n->m->ticks_per_insn;
}

network_t *network_new(machine_t *m, uint64_t ips) {
    network_t *n = calloc(1, sizeof *n);
    n->m = m;
    n->ips = ips;
    machine_hook(m, SEND_MESSAGE, sent, n, 0);
    machine_hook(m, SEND_REG_REQ, reg_req, n, 0);
    machine_add_timer(m, network_update, network_next, n);
    return n;
}

int network_registered(network_t *n) { return n->registered; }

/* Someone calls the phone (number: digits, may be empty). 0 if the phone is not on the
   network or already in a call. */
int network_incoming_call(network_t *n, const char *number) {
    if (!n->registered || n->in_call) return 0;
    snprintf(n->caller, sizeof n->caller, "%s", number ? number : "");
    n->in_call = 1;
    n->call_ti = TI_FLAG | INCOMING_TI;
    n->call_first = CALL_CONTROL | 0x80 | INCOMING_TI << 4;        /* as the phone will reply */
    after(n, 0.1, EV_INCOMING, 1, 0, 0, 0);
    return 1;
}

/* The far end hangs up (or stops calling): 0 if there is no call. */
int network_hang_up(network_t *n) {
    if (!n->in_call) return 0;
    for (int i = 0; i < n->nevents; i++)
        if (n->events[i].kind == EV_INCOMING) {         /* not delivered yet: just forget it */
            drop_call_events(n);
            n->in_call = 0;
            return 1;
        }
    if (!n->have_call_port) return 0;
    drop_call_events(n);
    after(n, 0.1, EV_TO_PHONE, 1, n->call_ti, n->call_first, DISCONNECT);
    return 1;
}

int network_from_phone(network_t *n, uint8_t *kinds, int max) {
    int k = n->nfrom < max ? n->nfrom : max;
    memcpy(kinds, n->from_phone, k);
    return k;
}
