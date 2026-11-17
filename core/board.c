/* The V600 as a whole: memory map, firmware loading, reset state, and the chip models
   wired up the way the phone has them. Ported from v600/machine.py, board.py and
   superfile.py (tag py-reference); the build order is kept (it fixes the order of hooks
   and timers). */
#include <stdlib.h>
#include <string.h>
#include "machine.h"

#define IRAM_BASE 0x03FC0000u
#define IRAM_SIZE 0x40000u
#define RAM_BASE 0x12000000u
#define RAM_SIZE 0x400000u
#define FLASH_BASE 0x10000000u
#define FLASH_SIZE 0x2000000u
#define CPSR_E 0x200u               /* Unicorn's big-endian data flag, kept set */
#define ENTRY 0x106289BEu           /* Thumb; from header word 0 of the main code group */
#define CPU_IPS 52000000u           /* the CPU is taken to run 52 M instructions / s */

/* ---- Motorola P2K superfile (.shx): a text header, then S-record groups ------------------
   Every "S3 cc aaaaaaaa data.. ss" line (upper-case hex, then CR LF or LF) is loaded;
   consecutive records form one region. */
static int hexval(int c) { return c >= '0' && c <= '9' ? c - '0' : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1; }

static void load_superfile(machine_t *m, const uint8_t *d, int64_t n) {
    uint8_t *buf = malloc(256);
    for (int64_t i = 0; i + 14 <= n; i++) {
        if (d[i] != 'S' || d[i + 1] != '3') continue;
        int64_t j = i + 2;
        while (j < n && hexval(d[j]) >= 0) j++;
        int64_t hexlen = j - (i + 2);                         /* count, address, data, checksum */
        if (j < n && d[j] == '\r') j++;
        if (j >= n || d[j] != '\n' || hexlen < 12 || hexlen % 2) continue;
        uint32_t addr = 0;
        for (int k = 0; k < 8; k++) addr = addr << 4 | hexval(d[i + 4 + k]);
        int64_t nbytes = (hexlen - 12) / 2;                   /* data between address and sum */
        if (nbytes > 256) { buf = realloc(buf, nbytes); }
        for (int64_t k = 0; k < nbytes; k++)
            buf[k] = hexval(d[i + 12 + 2 * k]) << 4 | hexval(d[i + 13 + 2 * k]);
        arm_mem_write(m->cpu, addr, buf, (uint32_t)nbytes);
        i = j;
    }
    free(buf);
}

machine_t *v600_machine_new(const uint8_t *superfile, int64_t n) {
    arm_t *cpu = arm_new();
    arm_map(cpu, 0, 0x20000);                    /* vectors + boot ROM routines (firmware.c) */
    arm_map(cpu, IRAM_BASE, IRAM_SIZE);
    arm_map(cpu, RAM_BASE, RAM_SIZE);
    arm_map(cpu, FLASH_BASE, FLASH_SIZE);
    uint8_t *erased = malloc(FLASH_SIZE);        /* erased flash, incl. the missing bootloader */
    memset(erased, 0xFF, FLASH_SIZE);
    arm_mem_write(cpu, FLASH_BASE, erased, FLASH_SIZE);
    free(erased);
    machine_t *m = machine_new(cpu, 0.0);
    load_superfile(m, superfile, n);
    arm_map(cpu, MMIO_BASE, MMIO_SIZE);
    machine_init_run(m);
    return m;
}

/* CPU state the (missing) bootloader leaves: SVC mode, IRQ / FIQ off, a stack in IRAM.
   The firmware never sets the exception-mode stacks. SVC keeps the top of IRAM: it becomes
   the OS's interrupt / idle context and reaches below 0x03FFFE60. The others sit in
   0x03FFB85C-0x03FFEEEB, which nothing else touches; an FIQ stack inside the SVC area
   corrupted that context once the SIM (FIQ source 51) was active. */
void v600_reset(machine_t *m) {
    static const uint32_t stacks[6][2] = {{0x11, 0x03FFE800}, {0x12, 0x03FFE000}, {0x17, 0x03FFD000},
                                          {0x1B, 0x03FFC800}, {0x1F, 0x03FFC000}, {0x13, 0x03FFFF80}};
    for (int i = 0; i < 6; i++) {
        arm_reg_write(m->cpu, REG_CPSR, CPSR_E | 0xC0 | stacks[i][0]);
        arm_reg_write(m->cpu, 13, stacks[i][1]);
    }
    arm_reg_write(m->cpu, REG_CPSR, CPSR_E | 0xD3);
    /* Clock-speed code the bootloader leaves in IRAM; the delay loop (0x107FBD04) resets the
       phone unless it is 1..8 (13/16.8/19.5/26/39/40/48/52 MHz). 8 = 52 MHz. */
    uint8_t speed = 8;
    arm_mem_write(m->cpu, 0x03FC3600, &speed, 1);
    m->start = ENTRY | 1;
}

/* Power-IC register 23 is VENDOR_REV (PCAP2, Linux ezx-pcap.h), read as signal 0xE7 by the
   board-variant probe (0x10628B1C). Real value unknown; non-zero low bits select a real
   phone profile instead of "unsupported hardware -> reset". Register 0 (interrupt status)
   latches the power-on cause: power key pressed and nothing else -> normal power-up. */
#define PMIC_VENDOR_REV 0x1FFFFFFu
#define PMIC_ISR_POWER_KEY (1u << 8)
/* Persistent Data Storage (per-phone calibration / settings) at 0x10010000: two 32 KB
   blocks, each starting with "PDS_2006" and a 16-bit state word (barker). Not in the
   superfile, so an empty one is made. The finder (0x108735BC) accepts 0xFFFD or
   0xFFF8-0xFFFB; the header check (0x10874598) wants the signature. */
#define PDS_BASE 0x10010000u
#define PDS_BARKER 0xFFF8
/* Virtual time: the RTOS measures time in (RTR counter >> 19); the RTR counts 32.768 kHz
   in bits 63:17, so one unit is 4/32768 s = 122.07 us. */
#define TIME_UNIT_US (1e6 / 8192)
/* The idle task calls this to enter low-power wait (0x2484502C mode bits). */
#define IDLE_LOW_POWER 0x03FF0692u

void v600_build(machine_t *m, int flip_open, int sim_card) {
    bootrom_install(m);
    dsp_t *dsp = dsp_new(m);
    m->dsp = dsp;
    m->audio = audio_new(m, dsp, CPU_IPS);
    m->flash = flash_new(m);
    pmic_t *pmic = pmic_new();
    m->pmic = pmic;
    pmic_set_reg(pmic, 23, PMIC_VENDOR_REV);
    pmic_set_reg(pmic, 0, PMIC_ISR_POWER_KEY);
    /* QSPI devices: the power IC, and on chip select 6 the ATI Imageon's serial interface
       (CHIP_ID: device 0x5745, ATI vendor 0x1002), whose I2C bus has the camera sensor. */
    m->ati = ati_new();
    machine_attach(m, 0x24851000, 0x1000, (device_t *)qspi_new(m, pmic, m->ati));
    machine_attach(m, 0x2485BFF0, 0x10, (device_t *)dsp);         /* the MDI registers */
    machine_set_ticks(m, (1 << 19) / (TIME_UNIT_US * 1e-6 * CPU_IPS));
    machine_attach(m, 0x2484A000, 0x8, &counter_new(m)->d);
    machine_attach(m, 0x29890000, 0x100, &aitc_new(m)->d);
    machine_attach(m, 0x24854000, 0x40, (device_t *)dma_new(m, m->ati));
    ostimer_t *ost = ostimer_new(m);
    machine_attach(m, 0x24848000, 0x40, &ost->d);
    machine_add_timer(m, (void (*)(void *))ostimer_update, ostimer_next, ost);
    machine_hook(m, IDLE_LOW_POWER, machine_at_idle, 0, 1);
    machine_plain(m, 0x2484502C, 4);           /* low-power mode: hit on every idle pass */
    /* Unidentified unit at 0x24845000: +0x18 data, +0x10 bit 8 = start / busy (self-clearing). */
    machine_attach(m, 0x24845000, 0x1000, &regs_new(m, 0x1000, 0x10, 0x100)->d);
    /* SIM card interface (SCIM) with a virtual SIM card in port 0 (sim.c). */
    m->sim = sim_new(m, sim_card, CPU_IPS);
    machine_attach(m, 0x2484F000, 0x100, (device_t *)m->sim);
    machine_add_timer(m, (void (*)(void *))sim_update, (double (*)(void *))sim_next_event, m->sim);
    machine_add_timer(m, (void (*)(void *))audio_update, (double (*)(void *))audio_next_event, m->audio);
    /* Keypad port. Unmodelled it read as every key held, which the boot-time "user
       initiated panic" check (0x10AE52B0) took as a request and reset the display. */
    m->keypad = keypad_new(m);
    machine_attach(m, 0x2484E000, 0x10, &m->keypad->d);
    /* External interrupt pins, idle high. INT0 = HS_INT (flip / headset), INT1 = power IC. */
    m->edgeport = edgeport_new(m);
    edgeport_set_pin(m->edgeport, 0, flip_open);
    pmic_connect(pmic, m->edgeport);
    edgeport_set_pin(m->edgeport, 1, pmic_int_level(pmic));
    machine_attach(m, 0x2484C000, 0x8, &m->edgeport->d);
    /* One-wire bus to the battery's ID EPROM (DS2502). The reader (0x10B1D38E) checks each
       page's CRC and wants byte 0x2F = 0; the validator (0x1008DAA0 / 0x1008DB8A) wants
       "COPR" at 0x60, "MOTOROLA E.P CHARGE ONLY" at 0x68 and zero byte sums over 0x20-0x3F
       and 0x40-0x5F. Battery parameters (0x00-0x5F) are not decoded yet: all zero. */
    uint8_t battery[128] = {0};
    memcpy(battery + 0x60, "COPR", 4);
    memcpy(battery + 0x68, "MOTOROLA E.P CHARGE ONLY", 24);
    static const uint8_t serial[6] = {0x01, 0x02, 0x03, 0x04, 0x00, 0x50};
    machine_attach(m, 0x24856000, 0x8, &onewire_new(m, ds2502_new(serial, battery))->d);
    uint8_t pds[10] = {'P', 'D', 'S', '_', '2', '0', '0', '6', PDS_BARKER >> 8, PDS_BARKER & 0xFF};
    arm_mem_write(m->cpu, PDS_BASE, pds, 10);
    overrides_install(m, sim_card);
    m->network = network_new(m, CPU_IPS);
}

/* A key: 0.. in the keypad's order (periph.c), or -1 for the red key (the power key on
   the PCAP). */
void board_press(machine_t *m, int key, int down) {
    if (key < 0) pmic_power_key(m->pmic, down);
    else keypad_press(m->keypad, key, down);
}

/* The flip sensor on INT0 (pulled up): high = open (main display), low = closed (the UI
   moves to the outer display). */
void board_flip(machine_t *m, int open) { edgeport_set_pin(m->edgeport, 0, open); }

/* Parts of the board, for the host: 0 CPU, 1 keypad, 2 edge port, 3 power IC, 4 graphics
   chip, 5 audio, 6 flash, 7 network. */
void *v600_part(machine_t *m, int which) {
    void *parts[8] = {m->cpu, m->keypad, m->edgeport, m->pmic, m->ati, m->audio, m->flash, m->network};
    return which >= 0 && which < 8 ? parts[which] : 0;
}
