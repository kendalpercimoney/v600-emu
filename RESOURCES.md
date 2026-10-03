# External resources

Downloaded copies live in `docs/` (git-ignored, not redistributed); text extracts in `docs/text/`.

| Resource | Source | Use |
|---|---|---|
| Neptune LTE IC Baseband Specification v1.5 (2,600 pages) | firmware.center, Motorola E398 service docs (`U800_Neptune LTE IC Baseband Specification.pdf`) | The processor: memory map (ch. 6, p.1582 peripheral table), AITC (43), MQSPI (49), L1T (50), SIM (51), DSM (52), WDOG (53), EGPT (55), RTR (56), RTC (57), KPP keypad (58), DMAC (59), MDI DSP interface (37), CCM (5), GPIO (23) |
| ATI Imageon W2250 Technical Reference Manual rev 2.0 | same folder (`U1301_ATI_W2250_technical_reference_manual.pdf`) | Graphics chip: registers, PM4, display controller |
| Numonyx StrataFlash Wireless L30 datasheet | same folder | Flash command set |
| V600 / Triplets schematics, block diagram, L1-2 service manual, L3 debug guide | firmware.center, `V600 (BCC, Triplets)/Service Docs` | Which chips and how they are wired |
| Linux `drivers/mfd/ezx-pcap.c`, `include/linux/mfd/ezx-pcap.h` | kernel source (codebrowser.dev) | PCAP2 power IC register map |
| MotoFanRu/P2K-ELF-SDK | github.com/MotoFanRu/P2K-ELF-SDK | `ep1/pts/V600_TRIPLETS_G_0B.09.72R.pts` (function byte patterns), `res/V600_TRIPLETS_G_0B.09.72R/*.sym`; 157 of 311 patterns match our 0B.09.38R build exactly (`out/symbols_from_pts.json`) |

## Findings that change the model

- **Peripheral names** (spec p.1582): 0x24843000 RTC, 0x24845000 CCM, 0x24848000 EGPT, 0x24849000 WDOG,
  0x2484A000 RTR, 0x2484B000 DSM (not an RTC), 0x2484C000 INT (external interrupts), 0x2484E000 KPP,
  0x2484F000 SIM, 0x24850000 IIM, 0x24851000 MQSPI, 0x24852000 USB, 0x24853000 L1T, 0x24854000 DMAC,
  0x24856000 OWIRE, 0x24857000 UART2, 0x24858000 HAC, 0x2485B000 MDI, 0x2485C000-F000 MDM0-3.
- **Time base**: RTR is a 47-bit counter at 32.768 kHz; MSB = seconds, LSB[31:17] = 15-bit fraction.
  time() = counter >> 19 is therefore in 1/8192 s units (122 us), not 1 ms.
- **OS timer** is the EGPT's EPIT: 24-bit down counter, ITDR at +0x20/+0x22, compare ITCR at +0x24/+0x26,
  ITIF = EGPTSR bit 8, ITIE = EGPTIR bit 8, clocked from 32.768 kHz / 4. The "watchdog pre-warning"
  channel is the EGPT PWM (PWOR +0x12, PWCR +0x16, PWCNR +0x18).
- **PCAP2**: reg 23 is VENDOR_REV (not board straps); reg 9 ADC control, reg 10 ADC results
  (ADD1 9:0, ADD2 19:10); ISR bit 0 ADC done, 7 ONOFF, 8 ONOFF2, 5 alarm; regs 14-17 RTC.
- **V600 board**: graphics accelerator ("GA", ATI) on SPI (GA SPI CS/MOSI/MISO + GA INT); PCAP U900;
  4 MB RAM; Neptune U800.

## Reverse-engineering downloads (docs/re/)

| File | Source | Use |
|---|---|---|
| `V600_TRIPLETS_G_0B.09.72R.smg` (13 MB) | firmware.center, EXL `Development/P2K/CGs/` | Raw main code group (base 0x10080000) of the newer V600 build the SDK targets. `tools/port_syms.py` ports the SDK's 72R symbols to our 38R build by masked byte matching: 306 of 339 code symbols (`out/symbols_ported.json`); all 150 that overlap the pattern matches agree |
| `TRIPLETS_72R_R373_44R_REVERSE_ENGINEERING.zip` (87 MB) | firmware.center, EXL `Development/P2K/` | IDA 6 databases. `CG1_V300.bin.idb`: V300 code group, only ~320 non-automatic names (the SDK's). `gsm_flash_dev.elf.idb`: the ROKR E1 R373 build ELF from Motorola's internal build system (112,606 real names, same platform). Used at the user's request: names extracted locally, never committed |

Symbol pipeline (outputs in git-ignored `out/`; `v600/symbols.py` merges them):
`tools/port_syms.py` (SDK 72R -> 38R, exact), `tools/port_e1.py` (E1 -> 38R, masked bytes),
`tools/match_e1.py` (E1 -> 38R by structure: strings, constants, call graph; ~88% precise on
the SDK-known overlap). About 7,200 named functions in total; treat E1-structural names as hints.

## Second search (2026-10-04)

| Resource | Use |
|---|---|
| Linux `drivers/video/w100fb.h` (v3.10/v4.0; ATI Imageon W100), `docs/ref/` | Same register offsets as our W2250: 0x204 GEN_INT_STATUS, 0x410-0x430 LCD format / GRAPHIC_CTRL / GRAPHIC_OFFSET / PITCH / CRTC, 0x4B8 GPIO, 0x1004 DST_OFFSET, 0x106C DP_GUI_MASTER_CNTL, 0x107C DP_BRUSH_FRGD_CLR, 0x11AC SRC_OFFSET, 0x11BC/0x11C0 SC_TOP_LEFT/BOTTOM_RIGHT, 0x11C8 DP_CNTL, 0x12C4-0x12CC DP_DATATYPE/MIX/WRITE_MSK, 0x0140 RBBM_STATUS; plus bitfield structs. No CP ring registers (0x21C read pointer is inferred) |
| Linux `radeon_reg.h`, `docs/ref/` | PM4 type-3 2D opcodes. The W2250 uses them with bit 7 clear: 0x15 POLYLINE, 0x1A PAINT_MULTI, 0x1B BITBLT_MULTI (0x19 = host data) |
| P2K-ELF-SDK `sdk/hw_keypad.h` (/tmp/p2ksdk) | Raw KPDR value per key (mask 0xFC00): 2 driven columns (KBC0-1) + rows wired straight to ground, 8 rows. Red/End = power key on the PCAP. `sdk/ati.h`, `dal.h`, `dl_keypad.h`: AHI/DAL structures and logical key codes |
| E1 names | Display-timeout / backlight app (`BLCHandleDisplayTimeout`, `map_displaytimeout`), battery ROM driver (`HAPI_BATTERY_ROM_*`), `Pm4WaitSpace` (IRAM) |
| Searched, nothing found | Motorola battery EPROM layout, PCAP2 datasheet, any other P2K emulator |
