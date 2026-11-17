/* The V600 emulator's public interface: everything a front end (the browser build, the
   iPhone app, a test program) needs, and nothing else. See PORTING.md.

   Typical use:
       v600_t *p = v600_create(superfile, n, state, state_n);   // state may be 0
       every frame: v600_run(p, 16667); read v600_lcd / v600_outer when their serial
       changes; v600_audio for sound; v600_key / v600_flip on input;
       now and then: if v600_state_changed(p): v600_save_state(p, ...) to storage. */
#ifndef V600_H
#define V600_H
#include <stdint.h>
#include <stddef.h>

typedef struct v600 v600_t;

#define V600_LCD_W 176
#define V600_LCD_H 220
#define V600_OUTER_W 96
#define V600_OUTER_H 32
#define V600_AUDIO_RATE 16000

/* Keys, in the keypad's order; V600_KEY_RED is the power / end key. */
enum {
    V600_KEY_1, V600_KEY_2, V600_KEY_3, V600_KEY_4, V600_KEY_5, V600_KEY_6, V600_KEY_7,
    V600_KEY_8, V600_KEY_9, V600_KEY_STAR, V600_KEY_0, V600_KEY_HASH,
    V600_KEY_GREEN, V600_KEY_SOFT_RIGHT, V600_KEY_SOFT_LEFT, V600_KEY_MENU,
    V600_KEY_VOL_UP, V600_KEY_VOL_DOWN, V600_KEY_SMART, V600_KEY_VOICE,
    V600_KEY_RIGHT, V600_KEY_LEFT, V600_KEY_UP, V600_KEY_DOWN, V600_KEY_OK,
    V600_KEY_RED = -1
};

/* The phone, booting, from a superfile (the firmware dump) and a saved state (0 for none).
   Returns 0 if the firmware is not usable. */
v600_t  *v600_create(const uint8_t *superfile, size_t n, const uint8_t *state, size_t state_n);
void     v600_destroy(v600_t *);

/* Emulate `microseconds` of phone time (idle stretches are fast-forwarded). Returns 0, or
   1 if the CPU stopped on an error (the phone then stays as it is). */
int      v600_run(v600_t *, uint32_t microseconds);
double   v600_seconds(v600_t *);                       /* phone time since power-up */

/* Start the phone's clock: `now` = local time as seconds since 1970-01-01 (no time zone; the
   phone keeps local time). Until called, the clock reads 0 (keeps test runs repeatable). */
void     v600_set_time(v600_t *, int64_t now);

void     v600_key(v600_t *, int key, int down);
void     v600_flip(v600_t *, int open);

/* The pretend network. Someone calls the phone (number: digits, shown as the caller;
   may be empty); the far end hangs up. Both return 0 when not possible (not registered
   yet, already in a call / no call). */
int      v600_incoming_call(v600_t *, const char *number);
int      v600_hang_up(v600_t *);

/* The displays as RGBA (4 bytes per pixel, row by row); serial changes when the picture
   may have changed. The outer display is lit pixels in `on`, the rest in `off`. */
uint32_t v600_lcd(v600_t *, uint8_t *rgba);
uint32_t v600_lcd_serial(v600_t *);
uint32_t v600_outer(v600_t *, uint8_t *rgba, uint32_t on, uint32_t off);
uint32_t v600_outer_serial(v600_t *);

/* Sound made since the last call: 16-bit mono PCM at V600_AUDIO_RATE, up to max samples;
   *first = the first sample's index on the phone's sound timeline. */
int      v600_audio(v600_t *, int16_t *out, int max, int64_t *first);
/* Clips (MP3 ringtones) started or stopped since the last call, one at a time: returns 1
   and fills the event (start: data / n is the MP3 file, valid until the next call), or 0. */
typedef struct { int start, id; int64_t sample; const uint8_t *data; size_t n; } v600_media_t;
int      v600_media(v600_t *, v600_media_t *);

/* The phone's own data (settings, contacts, messages: the flash blocks that differ from
   the firmware), for storage. Returns the size; call with out = 0 for the size alone.
   Save only while v600_state_ready (no flash operation half done). */
int      v600_state_ready(v600_t *);
uint64_t v600_state_changes(v600_t *);                 /* grows with each flash write */
size_t   v600_save_state(v600_t *, uint8_t *out);

/* Lines from the firmware's own log and the emulator's messages. */
void     v600_set_log(v600_t *, void (*fn)(int kind, const char *text));

#endif
