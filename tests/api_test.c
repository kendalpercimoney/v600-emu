/* The public interface (core/v600.h) on its own, no Python: boot, open the menu, dial,
   hear the ringing tone, close the flip; writes out/api_lcd.ppm and out/api_outer.ppm.
       make api-test && build/api_test firmware/v600.shx */
#include <stdio.h>
#include <stdlib.h>
#include "../core/v600.h"

static int failures;
#define CHECK(what, ok) do { int ok_ = (ok); printf("%s  %s\n", ok_ ? "PASS" : "FAIL", what); failures += !ok_; } while (0)

static void ppm(const char *path, const uint8_t *rgba, int w, int h) {
    FILE *f = fopen(path, "wb");
    if (!f) return;
    fprintf(f, "P6 %d %d 255\n", w, h);
    for (int i = 0; i < w * h; i++) fwrite(rgba + 4 * i, 1, 3, f);
    fclose(f);
}

static void press(v600_t *p, int key) {
    v600_key(p, key, 1);
    v600_run(p, 200000);
    v600_key(p, key, 0);
    v600_run(p, 400000);
}

int main(int argc, char **argv) {
    FILE *f = fopen(argc > 1 ? argv[1] : "firmware/v600.shx", "rb");
    if (!f) { perror("firmware"); return 2; }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *fw = malloc(n);
    if (fread(fw, 1, n, f) != (size_t)n) return 2;
    fclose(f);

    v600_t *p = v600_create(fw, n, 0, 0);
    CHECK("creates the phone", p != 0);
    v600_run(p, 45000000);
    static uint8_t lcd[V600_LCD_W * V600_LCD_H * 4], outer[V600_OUTER_W * V600_OUTER_H * 4];
    uint32_t s0 = v600_lcd(p, lcd);
    int colours = 0;
    for (int i = 0; i < V600_LCD_W * V600_LCD_H; i += 97) colours += lcd[4 * i] != lcd[4 * (i + 1)];
    CHECK("boots to a picture", s0 > 0 && colours > 5);

    press(p, V600_KEY_SOFT_RIGHT);                 /* "Personalise your phone?" no, no */
    press(p, V600_KEY_SOFT_RIGHT);
    press(p, V600_KEY_RED);
    press(p, V600_KEY_MENU);
    v600_run(p, 2000000);
    CHECK("the menu redraws the screen", v600_lcd(p, lcd) != s0);
    ppm("out/api_lcd.ppm", lcd, V600_LCD_W, V600_LCD_H);

    press(p, V600_KEY_RED);
    int16_t pcm[V600_AUDIO_RATE + 16];
    int64_t first;
    press(p, V600_KEY_5); press(p, V600_KEY_5); press(p, V600_KEY_5);
    v600_audio(p, pcm, V600_AUDIO_RATE, &first);
    press(p, V600_KEY_GREEN);
    v600_run(p, 1400000);
    int k = v600_audio(p, pcm, V600_AUDIO_RATE, &first), peak = 0;
    for (int i = 0; i < k; i++) if (abs(pcm[i]) > peak) peak = abs(pcm[i]);
    CHECK("the ringing tone plays", peak > 1000);
    v600_run(p, 6000000);
    press(p, V600_KEY_RED);
    v600_run(p, 2000000);

    uint32_t o0 = v600_outer_serial(p);
    v600_flip(p, 0);
    v600_run(p, 3000000);
    CHECK("the outer display updates when closed", v600_outer(p, outer, 0x5080FF, 0x101010) != o0);
    ppm("out/api_outer.ppm", outer, V600_OUTER_W, V600_OUTER_H);
    v600_flip(p, 1);
    v600_run(p, 2000000);

    size_t sz = v600_save_state(p, 0);
    uint8_t *state = malloc(sz + 1);
    CHECK("saves its state", v600_state_ready(p) && v600_save_state(p, state) == sz && sz % (4 + 0x20000) == 0);
    v600_t *q = v600_create(fw, n, state, sz);
    v600_run(q, 1000000);
    CHECK("boots again from the saved state", q != 0 && v600_save_state(q, 0) >= sz);
    printf("phone time %.1f s, state %zu bytes; %s\n", v600_seconds(p), sz,
           failures ? "FAILED" : "ALL PASSED");
    return failures != 0;
}
