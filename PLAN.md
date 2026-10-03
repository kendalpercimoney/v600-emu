# V600 low-level emulator: plan to the boot logo

Goal: boot the real Motorola V600 firmware (`TRIPLETS_G_0B.09.38R`, built 2004-07-12) on an
emulated CPU until it draws its power-up logo on a 176x220 display.

## What recon established

| Fact | Evidence | Consequence |
|---|---|---|
| CPU code is **big-endian** ARM7TDMI, mostly Thumb | `b570` = `push {r4-r6,lr}` at 0x108718A2 | Unicorn in `UC_MODE_ARM \| UC_MODE_BIG_ENDIAN`; verify BE Thumb works first |
| Flash map: `0x10040000` (178 KB), `0x10080000-0x1117FFFF` main code (13 MB), `0x11180000` resources/language (1 MB), `0x11380000` filesystem (12.4 MB), `0x11FE0000` certs | S-record regions + the layout table in the 0x10080000 header | Load each region at its address |
| **Bootloader `0x10000000-0x1003FFFF` is not in the file** | Header table references it; no S-records there | We start the firmware ourselves with the CPU state the bootloader would leave |
| Internal RAM at `0x03FC0000` | Literal `0x03FC3600` in startup code; RAM loader runs at `0x03FD0000` | Map 256 KB IRAM |
| External RAM around `0x12000000-0x123FFFFF` | Heaviest constant cluster in the code | Map 4 MB RAM there (size to be confirmed) |
| Peripherals around `0x2484xxxx/0x2485xxxx` | Constants in code and in the RAM loader | MMIO region, every access logged |
| Intel-style flash driver in the image (0x70/0xFF/0x20-0xD0/0xE8 commands) | Thumb code at 0x100800E0 | Flash needs a CFI/Intel command-set model so filesystem writes work |
| RTOS IRQ handler with context switch at 0x10A2333C (`0xFEEDFACE` frame marker) | Disassembly | Need a working interrupt controller and timer tick |
| Task names (`p/dl_clock`, `p/dl_keypad`, ...) and lots of debug strings | `strings` | Hooking the logger narrates the boot for free |
| 128 KB-block log-structured filesystem, already decoded | Earlier extraction | We can inspect what the firmware reads and writes |

## Approach

**Python and Unicorn for the discovery phase.** Unicorn handles big-endian ARM/Thumb at JIT speed,
and Python makes the MMIO hooks, tracing and quick experiments cheap. Speed only matters once the
OS is running; the hardware models can be ported to C or Swift later, once they're proven.

**Log-and-stub peripherals.** Every MMIO read or write goes through one dispatcher. Unknown
registers return a default value and get logged with PC and access size. Known registers get small
Python models. A stall detector spots when the PC loops on the same reads and prints the loop, the
register and the surrounding disassembly. That turns each stall into a question I can answer from
the code.

**Read the code to find the hardware, don't guess it.** For every stall: disassemble the polling
loop, find which bit it wants, follow where the value came from, and write the smallest model that
satisfies it. Record every register learned in `HARDWARE.md`.

**Framebuffer first, LCD controller second.** To get pixels on screen quickly, render candidate RAM
buffers as 176x220 images (RGB565/RGB444/8-bit) during boot and find the one that fills with the
logo. Then trace where that buffer goes (DMA, the LCD bus or a controller window) and model that
path properly.

**Use the firmware's own logging.** Find the debug print/trace function, hook it and print its
format strings. This often tells us which subsystem is waiting on what.

## Milestones

| # | Milestone | Done when |
|---|---|---|
| M0 | Project setup: venv, superfile loader, big-endian ARM/Thumb sanity test | A hand-assembled BE Thumb snippet runs correctly |
| M1 | Find the entry point the bootloader jumps to; map memory; first instructions run | Firmware runs until its first unmodelled hardware access |
| M2 | Early init: clocks, chip selects, C runtime (data copy, BSS zero), MMIO stubs | `main`-equivalent reached; logger hook prints boot messages |
| M3 | RTOS up: interrupt controller, timer tick, task scheduling | Several named tasks run; scheduler ticks |
| M4 | Boot-critical peripherals: power IC (power-up reason, battery), flash (CFI and Intel commands), DSP handshake stub, keypad idle, watchdog | Boot proceeds past power-up checks into the UI |
| M5 | Display: find the framebuffer, then the LCD path; show it in a window and save PNGs | **The Motorola boot logo appears** |

## Status (2026-10-03)

| # | State |
|---|---|
| M0 | Done. Unicorn ARM926 big-endian; CPSR.E must stay set on every CPSR write |
| M1 | Done. Entry `0x106289BE` from code-group header word 0 |
| M2 | Done. C runtime, boot ROM stubs, power IC, QSPI, watchdog |
| M3 | Done. AITC + OS countdown timer; idle fast-forward; tasks run, power-up app runs |
| M4 | Done enough: power-on reason, PDS, flash, DSP download + handshake, SIM absent |
| M5 | **Done: the HELLOMOTO boot screen renders** from the ATI Imageon's frame buffer (`out/screen.png`) |

Temporary shortcuts to remove later: boot battery check override (the ADC now works; retest
without it), SIM-absent overrides, PM4 opcodes 0x15 (polyline?) unimplemented, battery EPROM
parameters (0x00-0x5F) unknown.

## Status (2026-10-04): startup animation

The whole HELLOMOTO power-up animation plays (`boot_animation.png`, `tools/frames.py`). What it
took, in order of discovery:

1. Battery: PCAP ADC conversions + ADC-done interrupt (PCAP INT = edge port INT1, AITC 54);
   battery ID chip on the one-wire bus (DS2502, family 0x89, Motorola EPROM signature).
   Without these the phone powered itself off ~3 s after boot (cause 5, "invalid battery").
2. ATI: CHIP_ID read-only (the driver writes a test pattern there and re-probes after a panel
   reset), PM4 opcode 0x1B (screen-to-screen blit; back buffer 0x116B90 -> front 0x103D10),
   ring read pointer at 0x21C (a pointer that never moves looks like a hung chip).

Next: the idle screen. After the animation the display is re-initialised and stays black; the
idle wallpaper was seen in the back buffer in an earlier run but is not presented yet.

## Known risks

- **Missing bootloader state.** It sets up clocks, SDRAM timing and chip selects that the firmware
  may assume. Mitigation: infer those from the firmware's own re-initialisation code; if needed,
  set registers to plausible values before the jump.
- **DSP (56600) handshake.** The firmware may wait for the DSP to boot. Mitigation: fake the
  "DSP ready" responses; radio functions aren't needed for the logo.
- **Power IC over SPI.** Boot checks why the phone powered on (power key, charger, alarm).
  Mitigation: model the SPI transaction and answer "power key pressed".
- **Unicorn quirks.** Big-endian ARM is less tested in Unicorn, and Unicorn emulates ARMv5 where
  the phone has ARMv4T (interworking differences). Mitigation: M0 sanity tests; fall back to
  QEMU's `armeb` system model or a custom core if needed.
- **Copy protection / signature checks.** Code groups are signed ("CSF"); the bootloader verifies
  them, which we skip. The firmware might check again at runtime: patch out if found.

## Working rules

- Everything derives from the firmware file itself. No external code or other projects.
- Every discovery goes into `HARDWARE.md` (register, address, meaning, evidence).
- Reproducible runs: `python run.py` from a clean checkout boots as far as we've got.
- Commit at each milestone.
