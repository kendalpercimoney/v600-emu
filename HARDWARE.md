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
| `0x2484xxxx-0x2485xxxx` | Peripheral block (several units) | Literals |
| `0x28880020` | External memory/chip-select controller (written at entry: `0x2201`, `0x3011`, `0x60000`, `0`) | `0x106289C0` |
| `0x29890000` | Interrupt controller? `+0x44` = pending source (-1 = none); handler table in RAM at `0x120BEBF8` | IRQ dispatcher `0x10D16A7C` |

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

## Other landmarks

| Address | What |
|---|---|
| `0x10A2333C` | RTOS IRQ entry (context switch, `0xFEEDFACE` frame marker) |
| `0x10A1506C` / `0x10A15434` | memcpy / memzero (veneers) |
| `0x108718A2` | "EaRy" hook in code-group header (bootloader calls with r0=0) |
| `0x100800E0` | Intel flash driver (copied to RAM to run) |
