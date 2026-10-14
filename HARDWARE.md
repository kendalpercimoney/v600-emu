# V600 hardware and firmware notes

Everything here is derived from `firmware/v600.shx` (TRIPLETS_G_0B.09.38R). Addresses are physical.
Byte order: **big-endian** (BE-32) ARM7TDMI code, mostly Thumb.

## Memory map

| Range | What | Evidence |
|---|---|---|
| `0x03FC0000-0x03FFFFFF` | Internal RAM (256 KB). Firmware places IRQ/FIQ veneers at `0x03FC3604`/`0x03FC3694`, data up to `0x03FFAA3C+` | Scatter-load table at `0x108718C2` |
| `0x10000000-0x1003FFFF` | Bootloader flash. **Not in the superfile.** Entry reads `[0x10000013]` and may call `[0x10000018]` | `0x10628994` |
| `0x10040000-0x1007FFFF` | Code group (178 KB used); header points to same entry | Header table |
| `0x10080000-0x1117FFFF` | Main code group (13.3 MB used). Header word 0 = entry | Header |
| `0x11180000-0x1137FFFF` | Resources/language group | Header table |
| `0x11380000-0x11FDFFFF` | Flash filesystem (128 KB log-structured blocks) | Decoded earlier |
| `0x11FE0000-0x11FE07FF` | Certificates / security data | Strings |
| `0x12000000-0x123FFFFF` | External RAM (4 MB). Data/BSS up to `~0x12400000` | Scatter table, `0x12000000` probed 3x at boot |
| `0x28880020` | External memory/chip-select controller (written at entry: `0x2201`, `0x3011`, `0x60000`, `0`) | `0x106289C0` |
| `0x0000-0x1FFFF` | Boot ROM (not in superfile): MCU-DSP messaging (`0xFFF4` receive, `0x101CC` send) and a few other services; stubbed in `bootrom.py` | Veneers in the firmware |

## Peripherals (modelled)

| Address | Unit | What we know |
|---|---|---|
| `0x24841000` | GPIO / pin mux | Bulk setup at `0x108715F4`; pin reads |
| `0x24845000` | Clock / power management | `+0x2C` low bits = CPU low-power mode (idle loop); `+0x10` bit 8 start/busy |
| `0x24848000` | OS timer | `+0x20/+0x22` = 32-bit countdown in time() units, latched on low write; `+0x06` bit 8 IE; `+0x04` bit 8 status (W1C); AITC source 14. `+0x12/+0x16/+0x18` = watchdog pre-warning channel (source 15 -> reset) |
| `0x24849000` | Watchdog | `+0` control (bit 4 soft reset, bit 5 assert), `+2` service `0x5555/0xAAAA`. Reset requests (`0x10AB7FB8`) spin until the chip resets; not modelled, so a firmware fatal error freezes the phone |
| `0x2484A000` | 64-bit free-running counter | `+0` high, `+4` low; time() = counter >> 19 (`0x1089B6A4`) |
| `0x2484B000` | RTC-like timer (source 48) | `+0x20` enable, `+0x38` IE mask, `+0x34` status |
| `0x2484F000` | SIM card interface (SCIM) | Neptune spec ch. 51; virtual GSM card in `sim.py`. 32-byte receive FIFO; CNTL bit 3 `onack` makes the card resend instead of losing bytes (176-byte SMS records overrun it) |
| `0x24851000` | QSPI | `+0x006` start (`0x20|ch`, bit 5 self-clears), `+0x010` bits 0/8 queue active, `+0x012/14` per-channel done (1 = idle, W1C), `+0x400` command RAM, `+0x480..` data RAM. Driver queue table at `[[0x1232E5F8]]` |
| `0x24854000` | DMA to the ATI host port | `+0/+2` source, `+4/+6` count, `+0x18` control (1 start, bit 6 done W1C, source 26), `+0x1C` bit 8 = address phase (first word = ATI address) |
| `0x2485BFF2` | MCU-DSP interface (MDI) registers | Neptune spec ch. 37: `+2` MCVR, `+4` MCR (bit 7 holds the DSP in reset), `+6` MSR (bit 11 general interrupt 0, W1C -> AITC 17), `+8/+A` MTR1/0, `+C/+E` MRR1/0 (`0x8001` = download block OK) |
| `0x2485C800` | DSP shared RAM | Download buffers; `0x2485D934/36` audio reply queue (0xB) write/read index, compared by the MDI ISR `0x10AD3364` |
| `0x29890000` | AITC interrupt controller | Freescale AITC layout (see `irq.py`); handler table `0x120BEBF8` |

## External chips

| Chip | Interface | Notes |
|---|---|---|
| Power IC (PCAP2-style) | QSPI chip select 3 (queue 0, hw channel 5) | 32 x 25-bit regs; word = W bit31, reg bits 30:26, data 24:0. Reg 0 = ISR (W1C; bit 8 power key, 7, 5 = power-on causes); reg 23 = board straps (signal 0xE7) |
| **ATI Imageon (CHIP_ID 0x5745 / 0x1002)** | Serial host interface on QSPI chip select 6 (queues 12/14, hw channels 3/16) + DMA write port | Driver: ATI Handheld Interface 1.27. Serial frame: byte0 = flags (0x20 write) / len-1, bytes1-3 address; read data from byte 12, write data from byte 8; 32-bit regs little-endian. Regs at 0x10000+. Memory little-endian. Command ring (PM4) 16 KB at 0x80000. Display: graphics base `0x418` (0x103D10), pitch `0x41C` (352), active 176x220 |
| Micron MT9V111 camera (I2C 0xB8) | ATI I2C master `0x988` CNTL_0 / `0x98C` CNTL_1 / `0x990` data FIFO (Radeon multimedia I2C layout) | Probe `0x1066392A`: R1 = 4 (core page), R255 chip version 0x823A. Without the I2C engine the driver timed out, freed its handle and crashed on it. No image yet (black viewfinder) |
| DSP (56600-family) | MDI + boot-ROM messaging (`0xFFF4` / `0x100C2` receive, `0x101CC` send) | Patches downloaded from code group 0x10040000; ROM version 0x62D5 (patch table 0x10040010). Type 8 = audio commands (word 0 opcode, word 1 id<<8 \| length), replies opcode\|0x8000 on queue 0xB via general interrupt 0; see `audio.py` |
| Intel flash 0x0089/0x880F | Memory bus | Intel command set; driver accepts IDs in table at IRAM `0x03FC4284` |

## ATI PM4 commands seen

- Type 0: `[31:30]=0`, count-1 in `[29:16]`, register dword index in `[15:0]`.
- Type 1: two registers, indices in `[10:0]` and `[21:11]`, two data words.
- Type 3 `0x19`: host-data blit, `[(y<<16)|x, (h<<16)|w, pixels...]` into DST_OFFSET (`0x1004`) / DST_PITCH (`0x1008`, pixels).
- Type 3 `0x1A`: paint rectangle `[(x<<16)|y, (w<<16)|h]` with DP_BRUSH_FRGD_CLR (`0x107C`).
- Type 3 `0x1B`: 3 words, not implemented yet.

## Boot path

1. Bootloader (missing) jumps to `[0x10080000]` = **`0x106289BF` (Thumb `0x106289BE`)**.
2. `0x106289BE`: chip-select setup at `0x28880020`; 3 dummy reads of `0x12000000`;
   `0x10628994` (bootloader hook check: `[0x10000013]==0xFF` and `0x2484E006`); then ARM
   `__scatterload` at `0x106286FC` (veneer `0x107E96C8`), which ends in `__rt_entry` `0x107E5F20`.
3. `main` = `0x106289FE` (Thumb): calls `0x106286FC`, `0x10AB7F1E`, then boot sequence `0x108712FE`.
4. Boot sequence `0x108712FE`: ~50 init steps tagged with progress codes 0x14-0x2F via
   `0x10A150F8` (veneer to empty stub `0x106330B8`). Ends with system init `0x10AB9280`
   (includes RTOS init `0x106287D8`) and scheduler start `0x10AB929C`.
5. RTOS task table: 44 tasks, ROM copy at `0x10CFF650`, RAM at `0x1232E010`
   (record: stack size, priority, entry, stack top). Task entries are 10-byte stubs at
   `0x107FBB28+`: `checkpoint(N); bl body`.

## Per-phone data we synthesise

- **PDS** at `0x10010000` (2 x 32 KB): signature `PDS_2006` + barker `0xFFF8` at +8, otherwise empty.
- Product type (PDS item `0x293`) = 3, from header byte `0x10080019` (override).
- Bootloader clock code at IRAM `0x03FC3600` = 8 (52 MHz).
- No SIM card (overrides on card reset/deactivate).

## Other landmarks

| Address | What |
|---|---|
| `0x10A2333C` | RTOS IRQ entry (context switch, `0xFEEDFACE` frame marker) |
| `0x10A1506C` / `0x10A15434` | memcpy / memzero (veneers) |
| `0x108718A2` | "EaRy" hook in code-group header (bootloader calls with r0=0) |
| `0x100800E0` | Intel flash driver (copied to RAM to run) |
