/* The public interface (v600.h) on top of the machine. */
#include <stdlib.h>
#include <string.h>
#include "machine.h"
#include "v600.h"

#define FLASH_BASE 0x10000000u
#define FLASH_SIZE 0x2000000u
#define BLOCK 0x20000u
#define CPU_IPS 52000000u
/* Key timing. A touch screen sends a tap's down and up together, and taps faster than the
   phone's keypad scan: each key stays down at least MIN_HOLD, and a key tapped while another
   tap is still being held waits until that one is up plus KEY_GAP (two keys down at once
   read as neither). Keys go to the board through a small queue of timed events. */
#define MIN_HOLD (CPU_IPS / 10)
#define KEY_GAP (CPU_IPS / 25)
#define MAX_KEY_EVENTS 64
#define NKEYS 26                       /* V600_KEY_RED (-1) .. V600_KEY_OK, stored at key + 1 */

struct v600 {
    machine_t *m;
    uint8_t *original;              /* flash as built: firmware + breakpoints */
    uint64_t deadline;              /* v600_run stops the idle fast-forward here */
    int failed;
    uint64_t last_draws;
    uint32_t last_base, lcd_serial;
    int media_next;
    uint8_t *media_data;
    uint64_t down_at[NKEYS];        /* when each key went (or goes) down */
    uint64_t busy_until;            /* a tapped key is held until then (+ the gap) */
    struct { uint64_t due; int key, down; } keys[MAX_KEY_EVENTS];
    int nkeys;
};

/* The earliest queued key event, or UINT64_MAX. */
static uint64_t next_key(v600_t *p) { return p->nkeys ? p->keys[0].due : UINT64_MAX; }

static void keys_due(v600_t *p) {
    int i = 0;
    for (; i < p->nkeys && p->keys[i].due <= p->m->insns; i++) board_press(p->m, p->keys[i].key, p->keys[i].down);
    memmove(p->keys, p->keys + i, (p->nkeys - i) * sizeof p->keys[0]);
    p->nkeys -= i;
}

static void queue_key(v600_t *p, uint64_t due, int key, int down) {
    if (p->nkeys == MAX_KEY_EVENTS) return;
    int i = p->nkeys;
    while (i > 0 && p->keys[i - 1].due > due) { p->keys[i] = p->keys[i - 1]; i--; }   /* keep order */
    p->keys[i].due = due; p->keys[i].key = key; p->keys[i].down = down;
    p->nkeys++;
}

static void no_update(void *ctx) { (void)ctx; }

static double deadline_next(void *ctx) {
    v600_t *p = ctx;
    uint64_t r = next_key(p), t = r < p->deadline ? r : p->deadline;
    if (t < p->m->insns + 1) t = p->m->insns + 1;
    return (double)t * p->m->ticks_per_insn;
}

/* State file: for each saved block, its 4-byte address (big-endian) then the 128 KB block. */
static void load_state(v600_t *p, const uint8_t *s, size_t n) {
    for (size_t i = 0; i + 4 + BLOCK <= n; i += 4 + BLOCK) {
        uint32_t a = (uint32_t)s[i] << 24 | s[i + 1] << 16 | s[i + 2] << 8 | s[i + 3];
        if (a - FLASH_BASE < FLASH_SIZE && a % BLOCK == 0) arm_mem_write(p->m->cpu, a, s + i + 4, BLOCK);
    }
}

v600_t *v600_create(const uint8_t *superfile, size_t n, const uint8_t *state, size_t state_n) {
    if (!superfile || n < 64) return 0;
    v600_t *p = calloc(1, sizeof *p);
    p->m = v600_machine_new(superfile, (int64_t)n);
    v600_build(p->m, 1, 1);
    v600_reset(p->m);
    fwlog_install(p->m);
    p->original = malloc(FLASH_SIZE);
    arm_mem_read(p->m->cpu, FLASH_BASE, p->original, FLASH_SIZE);
    if (state) load_state(p, state, state_n);
    machine_add_timer(p->m, no_update, deadline_next, p);
    return p;
}

void v600_destroy(v600_t *p) {
    if (!p) return;
    free(p->original);
    free(p->media_data);
    free(p);                         /* (the machine's own memory is not reclaimed) */
}

int v600_run(v600_t *p, uint32_t microseconds) {
    if (p->failed) return 1;
    machine_t *m = p->m;
    if (m->powered_off) return 0;
    p->deadline = m->insns + (uint64_t)microseconds * (CPU_IPS / 1000000);
    while (m->insns < p->deadline) {
        keys_due(p);                     /* (the deadline timer stops idle skips at them) */
        /* One slice at a time: an idle fast-forward inside a longer run would jump the clock
           to the deadline and the run would then carry on past it. */
        if (machine_run(m, 20000, 20000, 0xFFFFFFFF, 0xFFFFFFFF) == RUN_ERROR) {
            p->failed = 1;
            return 1;
        }
        if (m->powered_off) return 0;
    }
    return 0;
}

double v600_seconds(v600_t *p) { return (double)p->m->insns / CPU_IPS; }

void v600_key(v600_t *p, int key, int down) {
    int k = key + 1;
    if (k < 0 || k >= NKEYS) return;
    uint64_t now = p->m->insns;
    if (down) {
        p->down_at[k] = now > p->busy_until ? now : p->busy_until;
        queue_key(p, p->down_at[k], key, 1);
    } else {
        uint64_t up = p->down_at[k] + MIN_HOLD > now ? p->down_at[k] + MIN_HOLD : now;
        queue_key(p, up, key, 0);
        if (up + KEY_GAP > p->busy_until) p->busy_until = up + KEY_GAP;
    }
    keys_due(p);
}
void v600_flip(v600_t *p, int open) { board_flip(p->m, open); }
int v600_incoming_call(v600_t *p, const char *number) { return network_incoming_call(p->m->network, number); }
int v600_hang_up(v600_t *p) { return network_hang_up(p->m->network); }
int v600_incoming_sms(v600_t *p, const char *from, const char *text) {
    return network_incoming_sms(p->m->network, from, text);
}
void v600_set_time(v600_t *p, int64_t now) { pmic_start_clock(p->m->pmic, p->m, CPU_IPS, now); }

/* -- displays ------------------------------------------------------------------------------ */
uint32_t v600_lcd_serial(v600_t *p) {
    uint64_t draws = ati_draws(p->m->ati);
    uint32_t base = ati_reg(p->m->ati, 0x418);
    if (draws != p->last_draws || base != p->last_base) {
        p->last_draws = draws;
        p->last_base = base;
        p->lcd_serial++;
    }
    return p->lcd_serial;
}

uint32_t v600_lcd(v600_t *p, uint8_t *rgba) {
    ati_t *c = p->m->ati;
    const uint8_t *mem = ati_mem(c);
    uint32_t base = ati_reg(c, 0x418), pitch = ati_reg(c, 0x41C) ? ati_reg(c, 0x41C) : 0x160;
    for (int y = 0; y < V600_LCD_H; y++)
        for (int x = 0; x < V600_LCD_W; x++) {
            uint32_t a = (base + y * pitch + 2 * x) & 0x3FFFFF, v = mem[a] | mem[(a + 1) & 0x3FFFFF] << 8;
            uint8_t *o = rgba + 4 * (y * V600_LCD_W + x);
            o[0] = ((v >> 11) & 31) * 255 / 31;
            o[1] = ((v >> 5) & 63) * 255 / 63;
            o[2] = (v & 31) * 255 / 31;
            o[3] = 255;
        }
    return v600_lcd_serial(p);
}

uint32_t v600_outer_serial(v600_t *p) { return (uint32_t)ati_outer(p->m->ati)->updates; }

uint32_t v600_outer(v600_t *p, uint8_t *rgba, uint32_t on, uint32_t off) {
    const outer_t *o = ati_outer(p->m->ati);
    for (int i = 0; i < V600_OUTER_W * V600_OUTER_H; i++) {
        uint32_t c = o->pixels[i] ? on : off;
        rgba[4 * i] = c >> 16; rgba[4 * i + 1] = c >> 8; rgba[4 * i + 2] = c; rgba[4 * i + 3] = 255;
    }
    return (uint32_t)o->updates;
}

/* -- sound ------------------------------------------------------------------------------------ */
int v600_audio(v600_t *p, int16_t *out, int max, int64_t *first) {
    return audio_pull(p->m->audio, first, out, max);
}

int v600_media(v600_t *p, v600_media_t *e) {
    audio_t *a = p->m->audio;
    if (p->media_next >= audio_events(a)) {
        audio_events_clear(a);
        p->media_next = 0;
        return 0;
    }
    int kind, id;
    int64_t at, n = audio_event(a, p->media_next, &kind, &id, &at, 0, 0);
    free(p->media_data);
    p->media_data = n ? malloc(n) : 0;
    if (n) audio_event(a, p->media_next, &kind, &id, &at, p->media_data, n);
    p->media_next++;
    e->start = kind; e->id = id; e->sample = at; e->data = p->media_data; e->n = (size_t)n;
    return 1;
}

/* -- the phone's data --------------------------------------------------------------------- */
int v600_state_ready(v600_t *p) { return flash_idle(p->m->flash); }
uint64_t v600_state_changes(v600_t *p) { return flash_writes(p->m->flash); }

size_t v600_save_state(v600_t *p, uint8_t *out) {
    flash_flush(p->m->flash);
    uint32_t addr[512];
    uint8_t bytes[2048];
    int len[512];
    int np = machine_flash_patches(p->m, addr, bytes, len, 512);   /* code, not phone data */
    for (int k = 0; k < np; k++) memcpy(p->original + (addr[k] - FLASH_BASE), bytes + 4 * k, len[k]);
    uint8_t *blk = malloc(BLOCK);
    size_t n = 0;
    for (uint32_t off = 0; off < FLASH_SIZE; off += BLOCK) {
        arm_mem_read(p->m->cpu, FLASH_BASE + off, blk, BLOCK);
        if (!memcmp(blk, p->original + off, BLOCK)) continue;
        if (out) {
            uint32_t a = FLASH_BASE + off;
            out[n] = a >> 24; out[n + 1] = a >> 16; out[n + 2] = a >> 8; out[n + 3] = a;
            memcpy(out + n + 4, blk, BLOCK);
        }
        n += 4 + BLOCK;
    }
    free(blk);
    return n;
}

int v600_vibrator(v600_t *p) { return pmic_vibrator(p->m->pmic); }
uint32_t v600_lights(v600_t *p) { return qspi_lights(p->m->qspi); }
int v600_backlight(v600_t *p) { return pmic_backlight(p->m->pmic); }
int v600_powered_off(v600_t *p) { return p->m->powered_off; }

void v600_set_log(v600_t *p, void (*fn)(int kind, const char *text)) { machine_set_log(p->m, fn); }
