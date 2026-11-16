/* The DSP as the ARM sees it: the MCU-DSP interface (MDI) registers, and a high-level
   stand-in for the DSP's side of their message queues. There is no DSP core: its program
   is in the Neptune's mask ROM, which we do not have.

   Messages go through boot-ROM routines (the machine hooks them, see v600/bootrom.py):
     0xFFF4  receive(queue, buf) -> 1 if a message was copied into buf (16-bit words)
     0x100C2 the same (used by the audio reply receiver 0x106939B0)
     0xFFD0  queued(queue) -> words waiting in the DSP's data queue (type 0 sends)
     0x101CC send(type, buf, nwords, flag) -> 1 on success
   Boot handshake (0x1064E8BC): after the code download the DSP posts 0xA080 ("alive") on
   queue 0xD; the MCU sends type 0xC [0x2080, 0]; the DSP answers 0xA081 with its ROM
   version in word 2. The MCU picks DSP patches by version from the table at 0x10040010
   (only 0x62D5 there). Type 8 messages are audio commands and type 0 the media data they
   play (audio.c); replies go on queue 0xB.
   Replies on queue 0xB reach the audio manager through MDI general interrupt 0: its
   handler (0x10AD3364, AITC source 17) clears MSR bit 11 and, if the queue's two indices in
   shared memory (0x2485D934 / 0x2485D936) differ, calls the reply receiver 0x106939B0,
   which reads queue 0xB until it is empty.

   MDI registers at 0x2485BFF2-0x2485BFFF (Neptune spec ch. 37; offsets from 0x2485BFF0):
   +2 MCVR, +4 MCR (bit 7: DSP held in reset during the download, 0x10AD5B90), +6 MSR,
   +8/+A MTR1/0 (to the DSP), +C/+E MRR1/0 (from the DSP). MSR reads with both receive
   registers full (bits 15/14) and MRR0 = 0x8001 ("block OK"): what the download waits for.
   MSR bit 11 is general interrupt 0 (w1c). A word the DSP puts in MRR0 raises AITC source
   42; its handler (0x10AD339E) passes it to the data callbacks registered with MME_FW_MDI
   (0x10AE73E6): 0x8002 asks the MIDI filter for more data. Reading MRR0 takes the word. */
#include <stdlib.h>
#include <string.h>
#include "machine.h"

#define DSP_ROM_VERSION 0x62D5
#define AUDIO_QUEUE 0xB
#define QUEUE_INDICES 0x2485D934u
#define GINT0_SOURCE 17
#define RECEIVE_SOURCE 42
#define MSR 0x6
#define MRR0 0xE
#define MGIP0 (1u << 11)
#define NQUEUES 32
#define QCAP 64
#define MAXWORDS 8

typedef struct { uint16_t w[MAXWORDS]; int n; } message_t;

struct dsp {
    device_t d;                     /* the MDI registers */
    uint8_t regs[0x10];
    uint32_t msr;
    int mrr0;                       /* word from the DSP until read, or -1 */
    struct { message_t m[QCAP]; int head, n; } q[NQUEUES];
    audio_t *audio;
};

static void line(dsp_t *p) { aitc_set_line(p->d.m->aitc, GINT0_SOURCE, (p->msr & MGIP0) != 0); }

void dsp_mdi_receive(dsp_t *p, uint32_t word) {           /* the DSP writes MRR0 */
    p->mrr0 = (int)word;
    aitc_set_line(p->d.m->aitc, RECEIVE_SOURCE, 1);
}

static uint32_t mdi_read(device_t *d, uint32_t off, int size) {
    dsp_t *p = (dsp_t *)d;
    if (size == 4) {
        uint32_t hi = mdi_read(d, off, 2);
        return hi << 16 | mdi_read(d, off + 2, 2);
    }
    if (off == MSR) return p->msr;
    if (off == MRR0) {
        if (p->mrr0 < 0) return 0x8001;
        uint32_t v = (uint32_t)p->mrr0;
        p->mrr0 = -1;
        aitc_set_line(d->m->aitc, RECEIVE_SOURCE, 0);
        return v;
    }
    return off + 1 < sizeof p->regs ? (uint32_t)p->regs[off] << 8 | p->regs[off + 1] : 0;
}

static void mdi_write(device_t *d, uint32_t off, int size, uint32_t v) {
    dsp_t *p = (dsp_t *)d;
    if (size == 4) {
        mdi_write(d, off, 2, v >> 16);
        mdi_write(d, off + 2, 2, v & 0xFFFF);
    } else if (off == MSR) {
        p->msr &= ~(v & MGIP0);
        line(p);
    } else {
        for (int i = 0; i < size && off + i < sizeof p->regs; i++) p->regs[off + i] = v >> (8 * (size - 1 - i));
    }
}

static void write_indices(dsp_t *p) {
    uint8_t b[8] = {0, (uint8_t)p->q[AUDIO_QUEUE].n, 0, 0, 0, 0, 0, 0};
    arm_mem_write(p->d.m->cpu, QUEUE_INDICES, b, 8);
}

/* A message from the DSP. */
void dsp_post(dsp_t *p, int q, const uint16_t *words, int n) {
    if (q < 0 || q >= NQUEUES || p->q[q].n == QCAP) return;
    message_t *msg = &p->q[q].m[(p->q[q].head + p->q[q].n++) % QCAP];
    msg->n = n < MAXWORDS ? n : MAXWORDS;
    memcpy(msg->w, words, msg->n * sizeof *words);
    if (q == AUDIO_QUEUE) {
        write_indices(p);
        p->msr |= MGIP0;                                   /* general interrupt 0 */
        line(p);
    }
}

/* ROM receive(queue, buf): 1 if a message was copied to buf. */
int dsp_receive(dsp_t *p, uint32_t q, uint32_t buf) {
    if (q >= NQUEUES || !p->q[q].n) return 0;
    message_t *msg = &p->q[q].m[p->q[q].head];
    p->q[q].head = (p->q[q].head + 1) % QCAP;
    p->q[q].n--;
    uint8_t b[2 * MAXWORDS];
    for (int i = 0; i < msg->n; i++) { b[2 * i] = msg->w[i] >> 8; b[2 * i + 1] = msg->w[i]; }
    arm_mem_write(p->d.m->cpu, buf, b, 2 * msg->n);
    if (q == AUDIO_QUEUE) write_indices(p);
    return 1;
}

/* ROM send(type, buf, nwords, flag): always accepted. */
int dsp_send(dsp_t *p, uint32_t type, uint32_t buf, uint32_t n) {
    uint8_t *b = malloc(2 * (size_t)n + 2);
    uint16_t *w = malloc(2 * (size_t)n + 2);
    if (arm_mem_read(p->d.m->cpu, buf, b, 2 * n)) memset(b, 0, 2 * (size_t)n);
    for (uint32_t i = 0; i < n; i++) w[i] = (uint16_t)(b[2 * i] << 8 | b[2 * i + 1]);
    if (type == 8) audio_command(p->audio, w, (int)n);
    else if (type == 0) audio_data(p->audio, w, (int)n);
    else if (type == 0xC && n && w[0] == 0x2080) {
        const uint16_t version[3] = {0xA081, 0x0000, DSP_ROM_VERSION};
        dsp_post(p, 0xD, version, 3);
    }
    free(w);
    free(b);
    return 1;
}

dsp_t *dsp_new(machine_t *m) {
    dsp_t *p = calloc(1, sizeof *p);
    p->d = (device_t){mdi_read, mdi_write, m};
    p->msr = 0xC000;
    p->mrr0 = -1;
    const uint16_t alive = 0xA080;                         /* once the DSP code is running */
    p->q[0xD].m[0].w[0] = alive;
    p->q[0xD].m[0].n = 1;
    p->q[0xD].n = 1;
    return p;
}

void dsp_set_audio(dsp_t *p, audio_t *a) { p->audio = a; }
