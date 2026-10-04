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

## Status (2026-10-04, later): standby screen

After the animation the phone shows the "Insert SIM card" notice (`insert_sim.png`, the
firmware's language is Russian) and after ~60 s of inactivity the animated bubbles screensaver
(`screensaver.png`). What it took:

- Ring pointers: 0x220 (write pointer) advances with ring data, 0x21C (read pointer) with
  execution; a write to 0x220 restarts the ring. The driver's idle wait compares both with its
  own count; a wait that never ends trips a task-progress watchdog (0x10AE56C4) which tears the
  display down every ~0.6 s.
- Flip sensor on INT0: high = open. Low sends the UI to the outer (CLI) display.
- PM4: POLYLINE (0x15) and monochrome host data (text, GMC_SRC_DATATYPE 0/1, colour from
  DP_BRUSH_FRGD_CLR, leftmost pixel = bit 7).
- W100 register names (Linux w100fb.h) for status registers: RBBM_STATUS idle, GRAPHIC_CTRL
  clock-running bits.

## Status (2026-10-04, evening): usable in a browser

`tools/live.py` runs the phone in real time and serves it at http://localhost:8600 (screen,
clickable keys, keyboard shortcuts). Keys: on the V600 a key pulls its row line and possibly
one of two column lines low; KPDR reads back the SDK key code (`Keypad.CODES`). Idle
fast-forward is capped at the wall clock in live mode so key presses land at the right time.
Typing 112 on the Insert SIM screen gives the emergency-call screen.

Next: the outer CLI display, sound.

## Status (2026-10-05): all keys

- Navigation: the matrix codes the SDK leaves unnamed are, from the firmware's own key map,
  0xFEEF = up, 0xFFED = down, 0xFFBE = centre (OK); 0xFFDB is the Voice key, not Camera.
  0xFF3F / 0xFF9F are not keys (two rows at once).
- Red/End is the PCAP power key: ISR bit 7 on each change, PSTAT bit 7 = level (active low,
  bit 8 = ONOFF2 must read high). Short press ends/clears; held ~2 s switches the phone off.
- Without a SIM the Menu and soft keys only show "emergency calls only"
  ("Выз. служб спасения"): the menus need a SIM card. Next: emulate one.

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

## Status (2026-10-05, later): dialogs, icons, wallpaper

Compared with a photo of a real V600 on the Insert SIM screen, the wallpaper, the dialog
icon and the translucency were missing:
- Icons are drawn by register-triggered blits: writing DST_WIDTH_HEIGHT (0x1198) copies
  SRC (0x1190 = x << 16 | y) to DST (0x1194), as on Radeon. Masked blits (DP_MIX ROP pair
  AA/CC) treat black as transparent.
- Dialogs are drawn into a layer surface (0x116B90) and copied over the screen, which holds
  the wallpaper. The layer keeps a per-pixel alpha: plain clears empty it, alpha ops
  (dp_op, GLOBAL_ALPHA 8 or 11 of 16) veil it, DP_MIX bits 26-27 make source pixels
  (images, text, icons) opaque and leave brush pixels' alpha alone. The final copy
  (bits 26-27) blends the layer over the last plain full copy (the wallpaper).
- Remaining colour differences (lavender header and text versus dark blue and black in the
  photo) are probably a different skin on that phone's firmware.

## Status (2026-10-05, evening): SIM card, idle screen, menus

- `v600/sim.py`: the SCIM port 0 registers (timed in card clocks/etu, FIFOs, the wrapping
  general purpose counter, data interrupt = FIQ source 51, general = IRQ source 12) and a
  GSM 11.11 SIM (T=0, IMSI 001010123456789, CHV1 disabled, phonebook and SMS files).
- The phone stops the SIM clock when idle; restarting it must not reset the card.
- Exception stacks: the firmware never sets them (the bootloader does). The guessed FIQ
  stack overlapped the OS's SVC context; moved to 0x03FFB85C-0x03FFEEEB, which is unused.
- Result: English UI (no language on the SIM), "Personalise your phone?" on first boot,
  idle screen with No Service / "V600 EMU", working menus. Some titles do not render yet.

## Status (2026-10-06): image fixes

- DMA transfers of an odd number of halfwords lost their last pixel (and shifted the rows
  sent after it): image rows of odd width looked jagged. Tails are now delivered.
- CIF_CNTL (0x18) swap_fbuf: icons are uploaded with 16-bit halves swapped.
- Scissor clipping (SC_TOP_LEFT / SC_BOTTOM_RIGHT with GMC dst clipping): scrolling dialog
  text no longer overprints.
- Compositing: the layer surface holds the wallpaper itself (patches are re-uploaded when a
  menu cell is redrawn), and alpha fills blend in place. The earlier per-pixel alpha model
  is gone; it left stale highlights and white blocks.
- Open: dialog text drawn white on a white box during start-up (colour source unknown),
  soft-key labels have low contrast, icon transparency is inferred from the corner pixel.

## Status (2026-10-06, later): settings persist

The Intel flash model already handled program/erase; the file system writes (about 12 KB
in a short session). `Flash.save/load` keep the 128 KB blocks that differ from the
firmware image; live mode restores them from state/flash.bin at start, saves every 5 s
when idle and on exit (`--fresh` ignores the file). Verified: answering No to "Continue to
show Personalise Dialogue on power up?" survives a restart.
