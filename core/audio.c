/* The DSP's audio side, at the level of the commands the ARM sends it: what it is asked to
   play, rendered as PCM, and the replies the firmware waits for.

   Commands are type 8 messages (dsp.c): word 0 opcode, word 1 = id << 8 | payload length.
   Media data comes as type 0 messages. Replies set bit 15 of the opcode and go on queue
   0xB; the receiver 0x106939B0 takes 0x9980, 0x9A80, 0x9B80, 0x9C80/1, 0x9D80-3, 0x9E80-2,
   0x9F80-4 and 0xA182.

   Tones (the audio manager)
     0x1B80, tone sequence (built at 0x10694640 from a melody or a table such as
     USA_DIAL_TONE 0x10A31A58):
       w2      the tone's id (DL_AudPlayTone's argument, e.g. 0x12 for a key click)
       w5      number of tones << 8 | number of cadence pairs
       flags   one byte per tone, padded to a word; 1 = starts after the previous tones
               end, 0 = sounds together with the previous one (inferred: dial tones pair)
       tones   (coefficient, duration, 0): coefficient = cos(2 pi f / 8000) in Q15, the
               recursive-oscillator form (USA_DIAL_TONE has 0x7B31 / 0x786E = 350 / 440 Hz);
               duration in 8 kHz samples, 0x7FFF = until stopped; 0x7FFF coefficient = rest
       cadence (on, off) pairs, 8 kHz samples: the tones loop during "on", silence during
               "off", and the cadence repeats until stopped (ring tones, e.g. Continental).
     Without a cadence the sequence plays once and the DSP replies 0x9B80 [id << 8 | 2, w2,
     0] when it ends; until then the audio manager holds back further tones. 0x1B83, just
     before, gives one gain per tone (0x0800 for key clicks); 0x1B81 [w2, 1] stops.
   MIDI ringtones (the MIDI output filter, MME_MOF_*)
     The filter streams the file as type 0 packets [0x5E80, words, bytes] and sends 0x1E80
     to play (then [0x0405, ...] while playing). The DSP asks for more by putting 0x8002 in
     MRR0 (dsp.c); the filter sends more only while ROM 0xFFD0 says fewer than 0x82 words
     are queued. When the tune ends the DSP sends 0x9E80 [1]; 0x1E81 [reason] stops it early
     and gets the same reply, without which the filter never starts another tune.
   Sampled sounds (the audio/video output filter, MME_AVOF_*): 0x1F82 [handle, codec, 0,
     rate] opens a session, answered by 0x9F80 [handle]; frames follow as [0x5F80, words,
     handle, 0x0080, flags, time ms, frame] (MP3: one 120-byte MPEG-2 layer III frame
     each), 0x1F84 [handle, 1] plays, 0x1F83 [handle, 1] closes, answered by 0x9F81. The
     frames are handed on as a whole file (media events) for the listener to decode.

   Ported from v600/audio.py and midi.py (tag py-reference). Everything the firmware can
   observe (replies, their timing, MIDI data requests) is exact; the synthesis is libm's
   where the original used numpy's, and the drums' noise has its own generator. */
#include <math.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include "machine.h"
#ifndef M_PI
#define M_PI 3.14159265358979323846   /* not in strict C99 (Emscripten) */
#endif

#define RATE 16000
#define TONE_RATE 8000
#define SCALE (RATE / TONE_RATE)
#define CONTINUOUS 0x7FFF
#define UNITY 0x2000                 /* tone gain that plays at full scale (0x0800 -> 1/4) */
#define MIDI_MORE 0x8002
#define MIDI_AHEAD 2.0               /* seconds of MIDI parsed ahead of playback */
#define MASTER 0.3
static const uint16_t MIDI_ENDED[3] = {0x9E80, 0x0001, 0x0001};

/* ---- MIDI: a Standard MIDI File parser fed in pieces, and a small GM synthesiser -----------
   The phone's own synthesiser is DSP code in mask ROM; this one only aims to be
   recognisable: additive voices per instrument family, simple drums. The phone streams
   ringtones as format 0 files whose track length is 0x7FFFFFFF (open-ended): events are
   parsed as they arrive. Other files are parsed once all their tracks are in. */
typedef struct { double h[9]; int nh; double attack, decay, release; } family_t;
static const family_t FAMILIES[12] = {          /* harmonics, attack s, decay s (0 = held), release s */
    {{1, .5, .25, .12}, 4, .004, .7, .08},                  /* piano */
    {{1, 0, .3, 0, .1}, 5, .002, .5, .1},                   /* chromatic percussion */
    {{1, .6, .4, .3, .2}, 5, .01, 0, .05},                  /* organ */
    {{1, .6, .35, .2, .1}, 5, .003, .45, .08},              /* guitar */
    {{1, .45, .15}, 3, .005, .8, .06},                      /* bass */
    {{1, .5, .33, .25, .2, .16}, 6, .06, 0, .15},           /* strings */
    {{1, .5, .33, .25, .2}, 5, .08, 0, .2},                 /* ensemble */
    {{1, .7, .5, .35, .25, .18, .12}, 7, .03, 0, .08},      /* brass */
    {{1, 0, .5, 0, .3, 0, .2}, 7, .02, 0, .06},             /* reed */
    {{1, .15, .05}, 3, .03, 0, .06},                        /* pipe */
    {{1, 0, .33, 0, .2, 0, .14, 0, .11}, 9, .005, 0, .05},  /* synth lead */
    {{1, .5, .33, .25}, 4, .15, 0, .3},                     /* synth pad */
};
static const family_t DEFAULT_FAMILY = {{1, .5, .33, .25}, 4, .01, .6, .1};

typedef struct { double start; double *x; int64_t n; } note_out_t;

/* One note: `seconds` held, then its release. */
static double *voice(int key, double seconds, int program, double level, double bend, int64_t *len) {
    const family_t *fam = program / 8 < 12 ? &FAMILIES[program / 8] : &DEFAULT_FAMILY;
    double f = 440.0 * pow(2, (key - 69 + bend) / 12), sum = 0;
    int64_t n = (int64_t)((seconds + fam->release) * RATE);
    double *w = calloc(n > 0 ? n : 1, sizeof *w);
    for (int k = 0; k < fam->nh; k++) sum += fam->h[k];
    for (int64_t i = 0; i < n; i++) {
        double t = (double)i / RATE, wave = 0;
        for (int k = 0; k < fam->nh; k++)
            if (fam->h[k] && (k + 1) * f < RATE / 2) wave += fam->h[k] * sin(2 * M_PI * (k + 1) * f * t);
        double env = fmin(1.0, t / fam->attack);
        if (fam->decay) env *= exp(-t / (fam->decay * pow(440 / f, .3)));
        if (t > seconds) env *= exp(-(t - seconds) / (fam->release / 3));
        w[i] = level * env * wave / sum;
    }
    *len = n;
    return w;
}

/* Gaussian noise for the drums (the original used numpy's generator). */
static uint64_t rng_state = 0x9E3779B97F4A7C15ull;
static double gauss(void) {
    double u, v, s;
    do {
        rng_state ^= rng_state << 13; rng_state ^= rng_state >> 7; rng_state ^= rng_state << 17;
        u = (double)(rng_state >> 11) / 9007199254740992.0 * 2 - 1;
        rng_state ^= rng_state << 13; rng_state ^= rng_state >> 7; rng_state ^= rng_state << 17;
        v = (double)(rng_state >> 11) / 9007199254740992.0 * 2 - 1;
        s = u * u + v * v;
    } while (s >= 1 || s == 0);
    return u * sqrt(-2 * log(s) / s);
}

static void add_noise(double *x, int64_t total, double gain, double sec, double tau, int bright) {
    int64_t n = (int64_t)(sec * RATE);
    double prev = 0;
    for (int64_t i = 0; i < n && i < total; i++) {
        double g = gauss(), v = bright ? g - prev : g;
        prev = g;
        x[i] += gain * v * exp(-(double)i / RATE / tau);
    }
}

static void add_sweep(double *x, int64_t total, double gain, double f0, double f1, double sec, double tau) {
    int64_t n = (int64_t)(sec * RATE);
    double phase = 0;
    for (int64_t i = 0; i < n && i < total; i++) {
        double t = (double)i / RATE;
        phase += f1 + (f0 - f1) * exp(-t / .03);
        x[i] += gain * sin(2 * M_PI * phase / RATE) * exp(-t / tau);
    }
}

/* General MIDI percussion (channel 10), roughly. Lengths match the original exactly. */
static double *drum(int key, double level, int64_t *len) {
    double sec;
    if (key == 35 || key == 36) sec = .3;
    else if (key == 38 || key == 40) sec = .25;
    else if (key == 42 || key == 44) sec = .08;
    else if (key == 46) sec = .35;
    else if (key == 49 || key == 52 || key == 55 || key == 57) sec = 1.2;
    else if (key == 51 || key == 53 || key == 59) sec = .6;
    else if (key >= 41 && key <= 50) sec = .35;
    else sec = .15;
    int64_t n = (int64_t)(sec * RATE);
    double *x = calloc(n > 0 ? n : 1, sizeof *x);
    if (key == 35 || key == 36) add_sweep(x, n, 1, 150, 50, .3, .12);
    else if (key == 38 || key == 40) { add_noise(x, n, .6, .25, .06, 1); add_sweep(x, n, .5, 300, 180, .25, .05); }
    else if (key == 42 || key == 44) add_noise(x, n, .5, .08, .02, 1);
    else if (key == 46) add_noise(x, n, .5, .35, .12, 1);
    else if (key == 49 || key == 52 || key == 55 || key == 57) add_noise(x, n, .5, 1.2, .4, 1);
    else if (key == 51 || key == 53 || key == 59) add_noise(x, n, .4, .6, .25, 1);
    else if (key >= 41 && key <= 50) add_sweep(x, n, 1, 80 + (key - 41) * 25, 60 + (key - 41) * 18, .35, .14);
    else add_noise(x, n, .4, .15, .05, 0);
    for (int64_t i = 0; i < n; i++) x[i] *= level;
    *len = n;
    return x;
}

typedef struct {
    uint8_t *data; int64_t len, cap;
    int have_header, fmt, ntracks, div;
    int64_t pos;                    /* next event (open-ended single track), or -1 */
    double ticks_seconds, end_seconds;
    int64_t tempo;
    int running, ended;
    int program[16], volume[16], expression[16];
    double bend[16];
    struct { int ch, key, vel; double start; } held[256];
    int nheld;
    note_out_t *notes; int nnotes, cnotes;
} midi_t;

static midi_t *midi_new(void) {
    midi_t *s = calloc(1, sizeof *s);
    s->pos = -1;
    s->tempo = 500000;
    for (int c = 0; c < 16; c++) { s->volume[c] = 100; s->expression[c] = 127; }
    return s;
}

static void midi_free(midi_t *s) {
    if (!s) return;
    for (int i = 0; i < s->nnotes; i++) free(s->notes[i].x);
    free(s->notes);
    free(s->data);
    free(s);
}

static void release(midi_t *s, int ch, int key, double now) {
    int i = 0;
    while (i < s->nheld && !(s->held[i].ch == ch && s->held[i].key == key)) i++;
    if (i == s->nheld) return;
    double start = s->held[i].start;
    int vel = s->held[i].vel;
    memmove(&s->held[i], &s->held[i + 1], (s->nheld - i - 1) * sizeof s->held[0]);
    s->nheld--;
    double level = MASTER * vel / 127 * s->volume[ch] / 127 * s->expression[ch] / 127;
    int64_t n;
    double *x;
    if (ch == 9) x = drum(key, level, &n);
    else {
        double held = fmin(fmax(now - start, .03), 8.0);   /* damaged data can hold for hours */
        x = voice(key, held, s->program[ch], level, s->bend[ch], &n);
    }
    if (s->nnotes == s->cnotes) {
        s->cnotes = s->cnotes ? 2 * s->cnotes : 16;
        s->notes = realloc(s->notes, s->cnotes * sizeof *s->notes);
    }
    s->notes[s->nnotes++] = (note_out_t){start, x, n};
    double end = start + (double)n / RATE;
    if (end > s->end_seconds) s->end_seconds = end;
}

static void apply(midi_t *s, const uint8_t *ev, int n) {
    double now = s->ticks_seconds;
    int st = ev[0];
    if (st == 0xFF) {
        if (n > 1 && ev[1] == 0x51 && n >= 3)
            s->tempo = (int64_t)ev[n - 3] << 16 | ev[n - 2] << 8 | ev[n - 1];
        else if (n > 1 && ev[1] == 0x2F) {
            while (s->nheld) release(s, s->held[0].ch, s->held[0].key, now);
            s->ended = 1;
        }
        return;
    }
    int kind = st >> 4, ch = st & 0xF;
    if (kind == 0x9 && n > 2 && ev[2]) {
        release(s, ch, ev[1], now);
        if (s->nheld < 256) {
            s->held[s->nheld].ch = ch; s->held[s->nheld].key = ev[1];
            s->held[s->nheld].vel = ev[2]; s->held[s->nheld].start = now;
            s->nheld++;
        }
    } else if (kind == 0x8 || kind == 0x9) {
        release(s, ch, ev[1], now);
    } else if (kind == 0xC) {
        s->program[ch] = ev[1];
    } else if (kind == 0xB && (ev[1] == 7 || ev[1] == 11)) {
        (ev[1] == 7 ? s->volume : s->expression)[ch] = ev[2];
    } else if (kind == 0xE) {
        s->bend[ch] = (double)((ev[2] << 7 | ev[1]) - 8192) / 8192 * 2;
    }
}

static int vlq(const uint8_t *d, int64_t len, int64_t *p, int64_t *v) {
    *v = 0;
    for (;;) {
        if (*p >= len) return 0;
        int b = d[(*p)++];
        *v = *v << 7 | (b & 0x7F);
        if (!(b & 0x80)) return 1;
    }
}

/* One event at p: delta ticks, the event's bytes; 0 if incomplete. */
static int event(midi_t *s, const uint8_t *d, int64_t len, int64_t *p, int64_t *dt, uint8_t *ev, int *nev) {
    int64_t q = *p, n;
    if (!vlq(d, len, &q, dt) || q >= len) return 0;
    int st = d[q];
    if (st == 0xFF || st == 0xF0 || st == 0xF7) {
        int64_t r = q + (st == 0xFF ? 2 : 1);
        if (!vlq(d, len, &r, &n) || r + n > len) return 0;
        int64_t k = r + n - q;
        *nev = (int)(k < 512 ? k : 512);
        memcpy(ev, d + q, *nev);
        *p = r + n;
        return 1;
    }
    if (st & 0x80) { s->running = st; q++; }
    n = (s->running >> 4 == 0xC || s->running >> 4 == 0xD) ? 1 : 2;
    if (q + n > len) return 0;
    ev[0] = s->running;
    memcpy(ev + 1, d + q, n);
    *nev = (int)n + 1;
    *p = q + n;
    return 1;
}

static void run_track(midi_t *s) {
    uint8_t ev[512];
    while (!s->ended) {
        int64_t dt, p = s->pos;
        int nev;
        if (!event(s, s->data, s->len, &p, &dt, ev, &nev)) break;
        s->pos = p;
        s->ticks_seconds += (double)(dt * s->tempo) / 1e6 / s->div;
        apply(s, ev, nev);
    }
}

typedef struct { int64_t tick; int track, index; uint8_t ev[64]; int n; } timed_t;
static int by_time(const void *a, const void *b) {
    const timed_t *x = a, *y = b;
    if (x->tick != y->tick) return x->tick < y->tick ? -1 : 1;
    if (x->track != y->track) return x->track - y->track;
    return x->index - y->index;
}

/* Format 1: merge the tracks once every one has arrived. */
static void complete_file(midi_t *s) {
    const uint8_t *d = s->data;
    int64_t p = 14;
    timed_t *events = 0;
    int nevents = 0, cap = 0;
    for (int track = 0; track < s->ntracks; track++) {
        if (p + 8 > s->len || memcmp(d + p, "MTrk", 4)) { free(events); return; }
        int64_t n = (int64_t)d[p + 4] << 24 | d[p + 5] << 16 | d[p + 6] << 8 | d[p + 7];
        if (p + 8 + n > s->len) { free(events); return; }
        int64_t q = p + 8, end = p + 8 + n, tick = 0, dt;
        s->running = 0;
        uint8_t ev[512];
        int nev;
        while (q < end && event(s, d, end, &q, &dt, ev, &nev)) {   /* damaged: keep what parsed */
            tick += dt;
            if (nevents == cap) { cap = cap ? 2 * cap : 256; events = realloc(events, cap * sizeof *events); }
            timed_t *t = &events[nevents];
            t->tick = tick; t->track = track; t->index = nevents;
            t->n = nev < 64 ? nev : 64;
            memcpy(t->ev, ev, t->n);
            nevents++;
        }
        p = end;
    }
    qsort(events, nevents, sizeof *events, by_time);
    int64_t last = 0;
    for (int i = 0; i < nevents; i++) {
        s->ticks_seconds += (double)((events[i].tick - last) * s->tempo) / 1e6 / s->div;
        last = events[i].tick;
        apply(s, events[i].ev, events[i].n);
    }
    free(events);
    const uint8_t eot[3] = {0xFF, 0x2F, 0x00};
    apply(s, eot, 3);
}

static void midi_feed(midi_t *s, const uint8_t *chunk, int64_t n) {
    if (s->len + n > s->cap) {
        s->cap = (s->len + n) * 2 + 64;
        s->data = realloc(s->data, s->cap);
    }
    memcpy(s->data + s->len, chunk, n);
    s->len += n;
    const uint8_t *d = s->data;
    if (!s->have_header && s->len >= 22) {
        s->fmt = d[8] << 8 | d[9];
        s->ntracks = d[10] << 8 | d[11];
        s->div = d[12] << 8 | d[13];
        s->have_header = 1;
        if (((uint32_t)d[18] << 24 | d[19] << 16 | d[20] << 8 | d[21]) == 0x7FFFFFFF) s->pos = 22;
    }
    if (!s->have_header || s->ended) return;
    if (s->pos >= 0) run_track(s);
    else complete_file(s);
}

/* ---- what is playing --------------------------------------------------------------------- */
typedef struct { double off, dur, f, level; } tone_t;
typedef struct {
    int is_tones, is_midi;
    int64_t start, end;             /* end < 0: until stopped */
    tone_t notes[32]; int nnotes;
    double length;                  /* may be infinite */
    int64_t cad[32][2]; int ncad;
    double *samples; int64_t n;     /* clips */
} sound_t;

typedef struct { int kind; int id; int64_t at; uint8_t *data; int64_t n; } media_event_t;   /* kind 1 start, 0 stop */

#define MAX_SOUNDS 512
#define MAX_REPLIES 64
#define MAX_SESSIONS 8
#define MAX_EVENTS 21

struct audio {
    machine_t *m;
    dsp_t *dsp;
    uint64_t ips;
    pthread_mutex_t lock;
    sound_t *playing[MAX_SOUNDS]; int nplaying;
    uint16_t gains[64]; int ngains;
    struct { int64_t due; uint16_t w[4]; int n; } replies[MAX_REPLIES]; int nreplies;
    int64_t pulled;                 /* next sample a listener gets, or -1 */
    midi_t *midi;
    int64_t midi_start, midi_asked; /* midi_start < 0: not playing */
    struct { uint32_t handle; uint8_t *frames; int64_t n, cap; } sessions[MAX_SESSIONS];
    int nsessions;
    struct { uint32_t handle; int id; } media[MAX_SESSIONS];   /* clips playing, by handle */
    int nmedia;
    media_event_t events[MAX_EVENTS]; int nevents;
    int next_id;
};

static int64_t now_samples(audio_t *a) { return (int64_t)(a->m->insns * RATE / a->ips); }

static void reply(audio_t *a, int64_t due, const uint16_t *w, int n) {
    if (a->nreplies == MAX_REPLIES) return;
    a->replies[a->nreplies].due = due;
    memcpy(a->replies[a->nreplies].w, w, n * sizeof *w);
    a->replies[a->nreplies].n = n;
    a->nreplies++;
}

static void add_sound(audio_t *a, sound_t *t) {
    if (a->nplaying == MAX_SOUNDS) { free(t->samples); free(t); return; }
    a->playing[a->nplaying++] = t;
}

static void cut(audio_t *a, int midi, int64_t at) {
    for (int i = 0; i < a->nplaying; i++) {
        sound_t *t = a->playing[i];
        if ((midi ? t->is_midi : t->is_tones) && (t->end < 0 || t->end > at)) t->end = at;
    }
}

static void event_add(audio_t *a, int kind, int id, int64_t at, const uint8_t *data, int64_t n) {
    if (a->nevents == MAX_EVENTS) {                        /* keep the last 20, then this */
        free(a->events[0].data);
        memmove(a->events, a->events + 1, (MAX_EVENTS - 1) * sizeof a->events[0]);
        a->nevents--;
    }
    media_event_t *e = &a->events[a->nevents++];
    e->kind = kind; e->id = id; e->at = at; e->n = n;
    e->data = n ? malloc(n) : 0;
    if (n) memcpy(e->data, data, n);
}

static int session(audio_t *a, uint32_t handle) {
    for (int i = 0; i < a->nsessions; i++) if (a->sessions[i].handle == handle) return i;
    return -1;
}

static int media_of(audio_t *a, uint32_t handle) {
    for (int i = 0; i < a->nmedia; i++) if (a->media[i].handle == handle) return i;
    return -1;
}

static void tones(audio_t *a, const uint16_t *w, int n, int64_t now) {
    sound_t *t = calloc(1, sizeof *t);
    t->is_tones = 1;
    int count = n > 5 ? w[5] >> 8 : 0, npairs = n > 5 ? w[5] & 0xFF : 0, nflags = (count + 1) / 2;
    int base = 6 + nflags;
    #define W(i) ((i) < n ? w[i] : 0)
    #define FLAG(i) ((W(6 + (i) / 2) >> ((i) % 2 ? 0 : 8)) & 0xFF)
    double tt = 0, prev = 0;
    for (int i = 0; i < count; i++) {
        int coef = W(base + 3 * i), d = W(base + 3 * i + 1);
        double at = (i == 0 || FLAG(i)) ? tt : prev;
        double dur = d == CONTINUOUS ? INFINITY : (double)(d * SCALE);
        if (coef != CONTINUOUS && t->nnotes < 32) {
            double c = (coef & 0x8000 ? coef - 0x10000 : coef) / 32768.0;
            double g = i < a->ngains ? a->gains[i] : UNITY / 4;
            double f = acos(fmax(-1, fmin(1, c))) * TONE_RATE / (2 * M_PI);
            t->notes[t->nnotes++] = (tone_t){at, dur, f, fmin(1.0, g / UNITY)};
        }
        prev = at;
        tt = fmax(tt, at + dur);
    }
    int avail = n - (base + 3 * count);                   /* complete pairs present only */
    if (avail / 2 < npairs) npairs = avail > 0 ? avail / 2 : 0;
    for (int k = 0; k < npairs && t->ncad < 32; k++) {
        t->cad[t->ncad][0] = (int64_t)W(base + 3 * count + 2 * k) * SCALE;
        t->cad[t->ncad][1] = (int64_t)W(base + 3 * count + 2 * k + 1) * SCALE;
        t->ncad++;
    }
    #undef W
    #undef FLAG
    t->length = fmax(tt, 1);
    t->start = now;
    t->end = !t->ncad && isfinite(t->length) ? now + (int64_t)t->length : -1;
    add_sound(a, t);
    if (t->end >= 0) {
        uint16_t r[4] = {0x9B80, (uint16_t)((w[1] & 0xFF00) | 2), n > 2 ? w[2] : 0, 0};
        reply(a, t->end, r, 4);
    }
}

void audio_command(audio_t *a, const uint16_t *w, int n) {
    if (n < 1) return;
    pthread_mutex_lock(&a->lock);
    int64_t now = now_samples(a);
    #define W(i) ((i) < n ? w[i] : 0)
    uint16_t op = w[0];
    if (op == 0x1B80) {
        tones(a, w, n, now);
    } else if (op == 0x1B83) {
        a->ngains = 0;
        for (int i = 2; i < n && a->ngains < 64; i++) a->gains[a->ngains++] = w[i];
    } else if (op == 0x1B81) {
        cut(a, 0, now);
    } else if (op == 0x1E80 && a->midi_start < 0) {      /* later ones: [0x0405, ...] updates */
        a->midi_start = now;
        if (!a->midi) a->midi = midi_new();
    } else if (op == 0x1E81) {                            /* [reason]: 1 done, 5 deleted */
        cut(a, 1, now);
        if (a->midi_start >= 0) reply(a, now + 16, MIDI_ENDED, 3);
        midi_free(a->midi);
        a->midi = 0;
        a->midi_start = -1;
    } else if (op == 0x1F82) {
        uint32_t h = (uint32_t)W(2) << 16 | W(3);
        int i = session(a, h);
        if (i < 0 && a->nsessions < MAX_SESSIONS) { i = a->nsessions++; a->sessions[i].frames = 0; a->sessions[i].cap = 0; }
        if (i >= 0) { a->sessions[i].handle = h; a->sessions[i].n = 0; }
        uint16_t r[4] = {0x9F80, 0x0002, W(2), W(3)};
        reply(a, now + 16, r, 4);
    } else if (op == 0x1F84 && W(4) == 1) {
        uint32_t h = (uint32_t)W(2) << 16 | W(3);
        int i = session(a, h);
        if (i >= 0 && a->sessions[i].n && media_of(a, h) < 0 && a->nmedia < MAX_SESSIONS) {
            a->media[a->nmedia].handle = h;
            a->media[a->nmedia++].id = a->next_id;
            event_add(a, 1, a->next_id, now, a->sessions[i].frames, a->sessions[i].n);
            a->next_id++;
        }
    } else if (op == 0x1F83) {
        uint32_t h = (uint32_t)W(2) << 16 | W(3);
        int j = media_of(a, h), i = session(a, h);
        if (j >= 0) {
            event_add(a, 0, a->media[j].id, now, 0, 0);
            a->media[j] = a->media[--a->nmedia];
        }
        if (i >= 0) {
            free(a->sessions[i].frames);
            a->sessions[i] = a->sessions[--a->nsessions];
        }
        uint16_t r[4] = {0x9F81, 0x0002, W(2), W(3)};
        reply(a, now + 16, r, 4);
    }
    #undef W
    pthread_mutex_unlock(&a->lock);
}

/* Type 0 messages: packets of [opcode, words, payload]. */
void audio_data(audio_t *a, const uint16_t *w, int nw) {
    pthread_mutex_lock(&a->lock);
    int64_t len = 2 * (int64_t)nw;
    uint8_t *raw = malloc(len + 4);
    for (int i = 0; i < nw; i++) { raw[2 * i] = w[i] >> 8; raw[2 * i + 1] = w[i]; }
    for (int64_t i = 0; i + 4 <= len; ) {
        int op = raw[i] << 8 | raw[i + 1];
        int64_t n = raw[i + 2] << 8 | raw[i + 3], bn = 2 * n;
        if (i + 4 + bn > len) bn = len - i - 4;
        const uint8_t *body = raw + i + 4;
        if (op == 0x5E80) {
            if (!a->midi) a->midi = midi_new();
            midi_feed(a->midi, body, bn);
        } else if (op == 0x5F80) {
            uint32_t h = 0;
            for (int k = 0; k < 4 && k < bn; k++) h = h << 8 | body[k];
            int s = session(a, h);
            if (s >= 0 && bn > 12) {
                if (a->sessions[s].n + bn - 12 > a->sessions[s].cap) {
                    a->sessions[s].cap = (a->sessions[s].n + bn) * 2;
                    a->sessions[s].frames = realloc(a->sessions[s].frames, a->sessions[s].cap);
                }
                memcpy(a->sessions[s].frames + a->sessions[s].n, body + 12, bn - 12);
                a->sessions[s].n += bn - 12;
            }
        }
        i += 4 + 2 * n;
    }
    free(raw);
    pthread_mutex_unlock(&a->lock);
}

/* Words waiting in the DSP's data queue (ROM 0xFFD0; capacity 0x400): MP3 frames go while
   there is room (0x10AECBF0), MIDI data only below 0x82 words. Data is taken as it
   arrives, so the queue always reads empty. */
int audio_space(audio_t *a) { (void)a; return 0; }

static void midi_tick(audio_t *a, int64_t now) {
    midi_t *s = a->midi;
    for (int i = 0; i < s->nnotes; i++) {
        sound_t *c = calloc(1, sizeof *c);
        c->is_midi = 1;
        c->start = a->midi_start + (int64_t)(s->notes[i].start * RATE);
        c->samples = s->notes[i].x;
        c->n = s->notes[i].n;
        c->end = c->start + c->n;
        add_sound(a, c);
    }
    s->nnotes = 0;
    double played = (double)(now - a->midi_start) / RATE;
    if (!s->ended && s->ticks_seconds < played + MIDI_AHEAD && now - a->midi_asked > RATE / 10) {
        a->midi_asked = now;
        dsp_mdi_receive(a->dsp, MIDI_MORE);
    }
    if (s->ended && played >= s->end_seconds) {
        dsp_post(a->dsp, 0xB, MIDI_ENDED, 3);
        midi_free(a->midi);
        a->midi = 0;
        a->midi_start = -1;
    }
}

void audio_update(audio_t *a) {
    pthread_mutex_lock(&a->lock);
    int64_t now = now_samples(a);
    int k = 0;
    for (int i = 0; i < a->nreplies; i++) {               /* due replies, in order */
        if (a->replies[i].due <= now) dsp_post(a->dsp, 0xB, a->replies[i].w, a->replies[i].n);
        else a->replies[k++] = a->replies[i];
    }
    a->nreplies = k;
    if (a->midi_start >= 0) midi_tick(a, now);
    int listening = a->pulled >= 0 && a->pulled > now - RATE;
    int64_t keep = listening ? a->pulled : now - RATE;
    k = 0;
    for (int i = 0; i < a->nplaying; i++) {
        sound_t *t = a->playing[i];
        if (t->end < 0 || t->end > keep) a->playing[k++] = t;
        else { free(t->samples); free(t); }
    }
    a->nplaying = k;
    pthread_mutex_unlock(&a->lock);
}

/* Next event in counter units (instructions x ticks per instruction), or -1. */
double audio_next_event(audio_t *a) {
    pthread_mutex_lock(&a->lock);
    int64_t best = INT64_MAX;
    for (int i = 0; i < a->nreplies; i++) if (a->replies[i].due < best) best = a->replies[i].due;
    if (a->midi_start >= 0 && now_samples(a) + RATE / 10 < best) best = now_samples(a) + RATE / 10;
    pthread_mutex_unlock(&a->lock);
    if (best == INT64_MAX) return -1;
    return (double)((uint64_t)best * a->ips / RATE + 1) * a->m->ticks_per_insn;
}

static double render(sound_t *t, int64_t idx) {
    if (!t->is_tones) return t->samples[idx - t->start];
    int64_t rel = idx - t->start;
    double ppos;
    if (t->ncad) {
        int64_t period = 0, at = 0;
        for (int i = 0; i < t->ncad; i++) period += t->cad[i][0] + t->cad[i][1];
        if (period <= 0) return 0;
        int64_t pos = rel % period;
        ppos = -1;
        for (int i = 0; i < t->ncad; i++) {
            if (pos >= at && pos < at + t->cad[i][0])
                ppos = isfinite(t->length) ? (double)((pos - at) % (int64_t)t->length) : (double)(pos - at);
            at += t->cad[i][0] + t->cad[i][1];
        }
    } else {
        ppos = (double)rel;
    }
    double out = 0;
    for (int i = 0; i < t->nnotes; i++) {
        const tone_t *n = &t->notes[i];
        if (ppos >= n->off && ppos < n->off + n->dur)
            out += n->level * sin(2 * M_PI * n->f * (ppos - n->off) / RATE);
    }
    return out;
}

/* PCM (16-bit, RATE) from the last pull up to now: returns the sample count, the first
   sample's index in *start. out holds max samples (RATE is always enough). */
int audio_pull(audio_t *a, int64_t *start, int16_t *out, int max) {
    pthread_mutex_lock(&a->lock);
    int64_t now = now_samples(a), s = a->pulled < 0 ? now : (a->pulled > now - RATE ? a->pulled : now - RATE);
    a->pulled = now;
    int n = (int)(now - s);
    if (n > max) n = max;
    double *mix = calloc(n > 0 ? n : 1, sizeof *mix);
    for (int i = 0; i < a->nplaying; i++) {
        sound_t *t = a->playing[i];
        int64_t lo = t->start > s ? t->start : s, hi = t->end < 0 || t->end > now ? now : t->end;
        if (hi > s + n) hi = s + n;
        for (int64_t k = lo; k < hi; k++) mix[k - s] += render(t, k);
    }
    for (int i = 0; i < n; i++) out[i] = (int16_t)(fmax(-1, fmin(1, mix[i])) * 32767);
    free(mix);
    *start = s;
    pthread_mutex_unlock(&a->lock);
    return n;
}

/* Media events since the last call: copies event i (kind 1 start / 0 stop, id, sample) and
   returns its data length; the host then takes the bytes with audio_event_data. */
int audio_events(audio_t *a) {
    pthread_mutex_lock(&a->lock);
    int n = a->nevents;
    pthread_mutex_unlock(&a->lock);
    return n;
}

int64_t audio_event(audio_t *a, int i, int *kind, int *id, int64_t *at, uint8_t *data, int64_t max) {
    pthread_mutex_lock(&a->lock);
    media_event_t *e = &a->events[i];
    *kind = e->kind; *id = e->id; *at = e->at;
    int64_t n = e->n;
    if (data) memcpy(data, e->data, n < max ? n : max);
    pthread_mutex_unlock(&a->lock);
    return n;
}

void audio_events_clear(audio_t *a) {
    pthread_mutex_lock(&a->lock);
    for (int i = 0; i < a->nevents; i++) free(a->events[i].data);
    a->nevents = 0;
    pthread_mutex_unlock(&a->lock);
}

audio_t *audio_new(machine_t *m, dsp_t *dsp, uint64_t ips) {
    audio_t *a = calloc(1, sizeof *a);
    a->m = m;
    a->dsp = dsp;
    a->ips = ips;
    a->pulled = -1;
    a->midi_start = -1;
    a->next_id = 1;
    pthread_mutex_init(&a->lock, 0);
    dsp_set_audio(dsp, a);
    return a;
}
