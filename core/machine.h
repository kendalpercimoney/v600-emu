/* The V600 around the CPU: virtual clock, MMIO bus and the chip models ported to C.

   Ported from the Python models one at a time (PORTING.md phase 2); each port keeps the
   golden trace (tests/golden.jsonl) identical. Models not ported yet are reached through
   the host callback (host devices). */
#ifndef V600_MACHINE_H
#define V600_MACHINE_H
#include <stdint.h>
#include "arm.h"

#define MMIO_BASE 0x20000000u
#define MMIO_SIZE 0x10000000u
#define TRACE_LEN 64
#define MAX_DEVICES 48
#define MAX_PLAIN 16

typedef struct machine machine_t;

/* Every C device starts with this. */
typedef struct device {
    uint32_t (*read)(struct device *, uint32_t off, int size);
    void (*write)(struct device *, uint32_t off, int size, uint32_t value);
    machine_t *m;
} device_t;

/* Not-yet-ported models: (device id, is_write, offset, size, value) -> value read. */
typedef uint32_t (*host_device_fn)(int id, int write, uint32_t off, int size, uint32_t value);

typedef struct { uint32_t pc; char kind; uint32_t addr; int size; uint32_t value; } trace_t;

/* -- interrupt controller (AITC, irq.c) -- */
typedef struct {
    device_t d;
    uint32_t intcntl, nimask;
    uint64_t enable, type, raw, force;
    uint8_t prio[64];
} aitc_t;

/* -- OS wake-up timer (irq.c) -- */
typedef struct {
    device_t d;
    uint8_t r[0x40];
    int armed;
    uint64_t fire_at;               /* free-running counter value */
} ostimer_t;

/* -- free-running counter (periph.c) -- */
typedef struct { device_t d; } counter_t;

/* -- register file with self-clearing bits (periph.c) -- */
typedef struct {
    device_t d;
    uint8_t *mem;
    uint32_t size, clear_word, clear_mask;
} regs_t;

/* -- keypad port (periph.c) -- */
typedef struct {
    device_t d;
    uint32_t kpcr, kpsr, kddr, cols, pressed;   /* pressed: bit per key (KEY_CODES order) */
    int was_down;
} keypad_t;

/* -- INT edge port (periph.c) -- */
typedef struct {
    device_t d;
    uint32_t eppar, epddr, epdr, epfr, pins;
} edgeport_t;

/* -- battery ID EPROM on the one-wire bus (ds2502.c) -- */
typedef struct {
    uint8_t rom[8], mem[128], status[8];
    int state, idx, ninbits, inbits, cmd, nargs;
    uint8_t args[8];
    uint8_t out[4096];              /* bits queued for the master */
    int out_head, out_len;
} ds2502_t;

typedef struct {
    device_t d;
    ds2502_t *device;
    uint32_t ctrl, div;
} onewire_t;

/* -- graphics chip (ati.c), its outer display; DMA (dma.c); QSPI + power IC (qspi.c) -- */
#define OUTER_WIDTH 96
#define OUTER_PAGES 4
typedef struct {
    uint8_t pixels[OUTER_PAGES * 8 * OUTER_WIDTH];   /* 1 = lit, row-major, 32 x 96 */
    int page, column, mirrored;
    uint64_t updates;                                /* to tell when the picture changed */
} outer_t;
typedef struct ati ati_t;
typedef struct dma dma_t;
typedef struct qspi qspi_t;
typedef struct pmic pmic_t;
typedef struct dsp dsp_t;           /* MDI registers + message queues (dsp.c) */
typedef struct audio audio_t;       /* the DSP's audio side (audio.c) */

/* -- NOR flash (flash.c), SIM interface + card (sim.c) -- */
typedef struct flash flash_t;
typedef struct sim sim_t;

/* -- running (run.c) -- */
#define MAX_HANDLERS 8
#define MAX_ONCE 16
#define MAX_TIMERS 16
#define MAX_CALLS 32
#define STALL_WINDOW 4096
enum { RUN_BUDGET, RUN_ERROR, RUN_HOOK, RUN_STALL };
typedef void (*hook_fn)(machine_t *, void *ctx);
typedef void (*call_then_fn)(machine_t *, void *ctx, uint32_t r0);
typedef struct { hook_fn fn; void *ctx; int host_id; } handler_t;   /* fn 0: host handler */
typedef struct { uint32_t addr; handler_t h[MAX_HANDLERS]; int nh; uint8_t orig[4]; int norig, emulate, planted; } bp_t;
typedef struct { machine_t *m; handler_t h; } code_hook_t;
typedef struct { void (*update)(void *); double (*next)(void *); void *ctx; int host_id; } timer_entry_t;
typedef struct { uint32_t fn, args[4]; int nargs; call_then_fn then; void *ctx; } call_t;
typedef void (*log_fn)(int kind, const char *text);          /* kind 0 machine, 1 firmware */
typedef void (*host_hook_fn)(int id);
typedef double (*host_timer_fn)(int id, int what);            /* 0 update, 1 next event */

struct machine {
    arm_t *cpu;
    uint64_t insns;                 /* virtual clock: instructions executed */
    double ticks_per_insn;          /* free-running counter ticks per instruction */
    aitc_t *aitc;
    struct { uint32_t base, size; device_t *dev; int host_id; } devices[MAX_DEVICES];
    int ndevices;
    struct { uint32_t begin, end; } plain[MAX_PLAIN];
    int nplain;
    host_device_fn host;
    uint64_t mmio_reads, mmio_writes;
    trace_t trace[TRACE_LEN];
    /* running */
    uint32_t start;                 /* where the next run() continues (bit 0 = Thumb) */
    char stopped[64];               /* set by machine_stop (a hook): run() returns RUN_HOOK */
    int idle_hint, fault, fault_access, fault_size;
    uint32_t fault_addr, fault_value, fault_pc;
    uint64_t irq_count;
    bp_t *bps; int nbps, cbps, iram_bps;
    uint32_t once[MAX_ONCE]; int nonce;
    uint32_t lifted[MAX_ONCE]; int nlifted;
    timer_entry_t timers[MAX_TIMERS]; int ntimers;
    call_t calls[MAX_CALLS]; int call_head, ncalls, in_call;
    call_t current;
    uint32_t call_context[17];
    log_fn log;
    host_hook_fn host_hook;
    host_timer_fn host_timer;
    /* the board (board.c) */
    struct flash *flash;
    struct sim *sim;
    struct dsp *dsp;
    struct audio *audio;
    struct ati *ati;
    struct pmic *pmic;
    keypad_t *keypad;
    edgeport_t *edgeport;
    struct network *network;
    uint32_t trace_pos;             /* wraps; TRACE_LEN divides 2^32 */
};

machine_t *machine_new(arm_t *cpu, double ticks_per_insn);
void       machine_free(machine_t *);
void       machine_set_host(machine_t *, host_device_fn);
void       machine_attach(machine_t *, uint32_t base, uint32_t size, device_t *dev);
void       machine_attach_host(machine_t *, uint32_t base, uint32_t size, int host_id);
void       machine_plain(machine_t *, uint32_t addr, uint32_t size);
uint64_t   machine_counter(machine_t *);          /* free-running counter value */
uint64_t   machine_insns(machine_t *);
void       machine_set_insns(machine_t *, uint64_t);
void       machine_set_ticks(machine_t *, double);
uint64_t   machine_mmio_reads(machine_t *);
uint64_t   machine_mmio_writes(machine_t *);
int        machine_trace(machine_t *, trace_t *out);

void       machine_log(machine_t *, int kind, const char *fmt, ...);
uint32_t   machine_read32(machine_t *, uint32_t addr);
void       machine_hook(machine_t *, uint32_t addr, hook_fn fn, void *ctx, int once_per_slice);
void       machine_hook_host(machine_t *, uint32_t addr, int host_id, int once_per_slice);
void       machine_add_timer(machine_t *, void (*update)(void *), double (*next)(void *), void *ctx);
void       machine_add_host_timer(machine_t *, int host_id);
void       machine_remove_host_timer(machine_t *, int host_id);
void       machine_call(machine_t *, uint32_t fn, const uint32_t *args, int nargs, call_then_fn then, void *ctx);
void       machine_at_idle(machine_t *, void *unused);
void       machine_stop(machine_t *, const char *why);
int        machine_run(machine_t *, uint64_t max_insns, uint32_t slice, uint32_t poll_slices, uint32_t spin_slices);
void       machine_init_run(machine_t *);
int        machine_flash_patches(machine_t *, uint32_t *addr, uint8_t *bytes, int *len, int max);
/* firmware.c: hooks into the firmware */
typedef struct network network_t;
void       bootrom_install(machine_t *);
void       overrides_install(machine_t *, int sim_card);
void       fwlog_install(machine_t *);
network_t *network_new(machine_t *, uint64_t ips);
int        network_registered(network_t *);
int        network_from_phone(network_t *, uint8_t *kinds, int max);
/* board.c */
machine_t *v600_machine_new(const uint8_t *superfile, int64_t n);
void       v600_reset(machine_t *);
void       v600_build(machine_t *, int flip_open, int sim_card);
void       v600_press(machine_t *, int key, int down);      /* KEY_* order, -1 = red */
void       v600_flip(machine_t *, int open);
void      *v600_part(machine_t *, int which);
void       machine_set_log(machine_t *, log_fn);
void       machine_set_host_hook(machine_t *, host_hook_fn);
void       machine_set_host_timer(machine_t *, host_timer_fn);
uint32_t   machine_start(machine_t *);
void       machine_set_start(machine_t *, uint32_t);
const char *machine_stopped(machine_t *);
double     machine_ticks(machine_t *);
uint64_t   machine_irq_count(machine_t *);
aitc_t     *aitc_new(machine_t *);
void        aitc_set_line(aitc_t *, int src, int level);
int         aitc_irq_line(aitc_t *);
int         aitc_fiq_line(aitc_t *);
ostimer_t  *ostimer_new(machine_t *);
void        ostimer_update(ostimer_t *);
uint64_t    ostimer_next_event(ostimer_t *);      /* UINT64_MAX: none */
double      ostimer_next(void *);                 /* the same as a double, or -1 */
counter_t  *counter_new(machine_t *);
regs_t     *regs_new(machine_t *, uint32_t size, uint32_t clear_word, uint32_t clear_mask);
keypad_t   *keypad_new(machine_t *);
void        keypad_press(keypad_t *, int key, int down);
edgeport_t *edgeport_new(machine_t *);
void        edgeport_set_pin(edgeport_t *, int n, int level);
ds2502_t   *ds2502_new(const uint8_t serial[6], const uint8_t memory[128]);
int         ds2502_reset(ds2502_t *);
int         ds2502_bit(ds2502_t *, int v);
onewire_t  *onewire_new(machine_t *, ds2502_t *);
flash_t    *flash_new(machine_t *);
void        flash_flush(flash_t *);
int         flash_idle(flash_t *);
uint64_t    flash_writes(flash_t *);
ati_t      *ati_new(void);
uint32_t    ati_read32(ati_t *, uint32_t addr);
void        ati_write32(ati_t *, uint32_t addr, uint32_t value);
void        ati_write_block(ati_t *, uint32_t addr, const uint32_t *words, int n);
void        ati_write_tail(ati_t *, uint32_t addr, const uint8_t *data, int n);
int         ati_serial_transfer(ati_t *, const uint8_t *data, int n, uint8_t *out);
uint8_t    *ati_mem(ati_t *);
uint32_t    ati_reg(ati_t *, uint32_t r);
uint64_t    ati_draws(ati_t *);
outer_t    *ati_outer(ati_t *);
dma_t      *dma_new(machine_t *, ati_t *);
pmic_t     *pmic_new(void);
void        pmic_set_reg(pmic_t *, int reg, uint32_t v);
void        pmic_connect(pmic_t *, edgeport_t *int_pin);
int         pmic_int_level(pmic_t *);
void        pmic_power_key(pmic_t *, int down);
qspi_t     *qspi_new(machine_t *, pmic_t *, ati_t *);
dsp_t      *dsp_new(machine_t *);
void        dsp_set_audio(dsp_t *, audio_t *);
void        dsp_post(dsp_t *, int q, const uint16_t *words, int n);
void        dsp_mdi_receive(dsp_t *, uint32_t word);
int         dsp_receive(dsp_t *, uint32_t q, uint32_t buf);
int         dsp_send(dsp_t *, uint32_t type, uint32_t buf, uint32_t n);
audio_t    *audio_new(machine_t *, dsp_t *, uint64_t ips);
void        audio_command(audio_t *, const uint16_t *w, int n);
void        audio_data(audio_t *, const uint16_t *w, int n);
int         audio_space(audio_t *);
void        audio_update(audio_t *);
double      audio_next_event(audio_t *);          /* counter units, or -1 */
int         audio_pull(audio_t *, int64_t *start, int16_t *out, int max);
int         audio_events(audio_t *);
int64_t     audio_event(audio_t *, int i, int *kind, int *id, int64_t *at, uint8_t *data, int64_t max);
void        audio_events_clear(audio_t *);
sim_t      *sim_new(machine_t *, int with_card, double ips);
void        sim_update(sim_t *);
double      sim_next_event(sim_t *);              /* counter units, or -1 */

#endif
