# Porting plan: one C core, built for the Mac tools, the browser and the iPhone app

Goal: the emulator lives in one place (a portable C core). Every change is made there once,
then built for each target. The iPhone app is a separate fork (its own name, bundle ID and
signing) that only receives build products; it never contains emulator source.

```
                 core/  (C99, no OS calls)
                   │  make
      ┌────────────┼──────────────────────┐
  libv600core.dylib     v600core.wasm (+ JS glue)
      │                    │
  Python tools          web/  drop-in "firmware" folder (HANDOFF contract)
  (./phone test,           │                       │
   drive, live)       ./phone play (browser)   iPhone app fork (WKWebView)
```

## Why WebAssembly for the app, not a native library

- The app already renders its phone from a folder loaded in a WKWebView, through a small,
  documented contract: `#lcd` (176x220) and `#ext` (96x32) canvases, `OS.frame`,
  `OS.key / keyUp / setOpen`, `OS.st` + `OS.onState`, `window.HW` hooks
  (`camera.frame`, `vibrate`, `ringLight`, `haptic`), sound through Web Audio.
- A folder that implements the same contract on top of the real emulator is a drop-in
  replacement: the app's 3D body, shell, haptics and camera plumbing stay as they are.
- iOS forbids apps from generating machine code (no JIT, so Unicorn would be slow), but the
  WKWebView's JavaScriptCore compiles WebAssembly to native code itself.
- The same build runs in any browser, so `./phone play` becomes static files (no Python
  server) and the web showroom could use it too.

## Phases

Each phase ends with something runnable and `./phone test` passing.

### 1. CPU core in C, checked against Unicorn — done (2026-11-16)
Result: `core/cpu.c` + `core/mem.c` (~800 lines). `V600_CPU=c` runs the whole emulator on
it through `v600/core.py` (the Unicorn calls Machine uses). `tools/lockstep.py` shows the
same registers (all modes), internal RAM and RAM as Unicorn every 0.25 s through 20 s of
boot (1.04 billion instructions); `./phone test` passes, 17 s instead of 24 s. Unicorn
details matched on the way: a Thumb BL pair counts as one instruction when both halves sit
on one 1 KB page (QEMU's page size here); instructions cannot change CPSR bit 9 (E).
Not done yet: the decoded-instruction cache (not needed so far: the CPU is ~15% of the
run time, Python models the rest).

Plan as written:
- `core/cpu.c`: ARMv4T/v5 interpreter, ARM and Thumb, big-endian, the modes and
  exceptions the firmware uses (IRQ, SVC, undefined, abort), CP15 basics. Decoded-instruction
  cache per 4 KB page, invalidated on writes to code pages (replaces `ctl_remove_cache`).
- `core/bus.c`: memory map from `machine.py` (flash, RAM, IRAM, MMIO page table that
  dispatches to chip models), instruction counter and timer queue (`next_event`), idle
  fast-forward.
- Breakpoint hooks become a sorted table checked at block entry: no BKPT patching needed.
- **Check:** lockstep runner (`tools/lockstep.py`) steps Unicorn and the C core side by side
  from reset, comparing registers every instruction and memory writes every block, until
  the first difference. Target: identical to the idle screen (~13 s of phone time).

### 2. Chip models in C, one file per Python module — done (2026-11-17)
Result: all of it is C (~5,300 lines in `core/`): every chip model, the machine loop,
breakpoint hooks, the firmware hooks (boot ROM, overrides, firmware log, pretend network)
and the board. Python (`v600/`, ~700 lines) is a handle for the tools. The check is the
golden traces (`tests/golden/`, `./phone check`): digests of registers, RAM, graphics
memory, the outer display, sound and media clips every 0.25 s through two scripted
sessions (a call; the ringtone list with tones, MP3 and MIDI), recorded from the
all-Python version tagged `py-reference`; all identical, except that synthesised sound is
compared by loudness (C's sin / exp and random numbers instead of numpy's). Speed: the
self-test runs 70 s of phone time in 4 s (was 24 s); about 125 M instructions / s.

Plan as written:
`irq, periph (timers, keypad, GPIO, DMA, UART/SCIM), flash, bootrom, ds2502, sim, ati
(+ outer display, I2C), camera, dsp, audio, midi, network, overrides, fwlog`.
Same names, same docstrings with the firmware addresses, ported one at a time.
- During the port a Python flag picks each model's implementation, so the selftest runs
  with any mix; parity per chip: same MMIO trace on the same boot.
- **Once all are ported, the C core is the only implementation.** The Python package
  becomes a thin `ctypes` binding (`v600/core.py`) plus the tools. No duplicated logic.

### 3. Public API (`core/v600.h`), the only thing the targets see — done (2026-11-17)
Result: `core/v600.h` / `v600.c`: create from the superfile and a saved state, run for a
span of phone time, keys and flip, both displays as RGBA with change serials, PCM and MP3
clip events, the phone's state for storage, the log. `make api-test` (tests/api_test.c)
boots, opens the menu, dials, hears the ringing tone, closes the flip, saves and reboots
from the state, all through this interface alone.

Plan as written:
```c
v600_t *v600_create(const uint8_t *superfile, size_t n, const uint8_t *flash_state, size_t m);
int     v600_run(v600_t *, uint32_t microseconds);           /* real time to emulate */
void    v600_key(v600_t *, int key, int down);               /* V600_KEY_* */
void    v600_flip(v600_t *, int open);
const uint32_t *v600_lcd(v600_t *, uint32_t *serial);        /* 176x220 RGBA, serial bumps on change */
const uint8_t  *v600_outer(v600_t *, uint32_t *serial);      /* 96x32, 1 byte per pixel */
size_t  v600_audio(v600_t *, int16_t *out, size_t max);      /* 16 kHz mono PCM */
int     v600_media(v600_t *, v600_media_event *);            /* MP3 clips: start/stop + bytes */
void    v600_camera_frame(v600_t *, const uint8_t *yuv, int w, int h);
uint32_t v600_state(v600_t *);                               /* power, call, ringing, vibrate, lights */
size_t  v600_save_flash(v600_t *, uint8_t *out);             /* changed blocks, for storage */
```

### 4. Builds (`Makefile`, one command each)
- `make native`: `libv600core.dylib` for the Python tools (clang -O2).
- `make wasm`: Emscripten → `dist/web/v600core.{js,wasm}`, `-O3 -flto`, no threads,
  memory growth on.
- `make web`: `dist/web/` = the wasm + `web/` adapter (see 5).
- `make test`: native build, then the selftest and a short lockstep check.
- The Emscripten SDK is a ~1 GB download: ask before installing it.

### 5. Web adapter (`web/`): the HANDOFF contract on top of the core
- `index.html` with `#lcd` and `#ext`; `host.js` keeps the same `window.HW` merge rules.
- `os.js` shim: `OS.key(code)` maps the app's codes (`L R M SEND END OK UP…`, `VOLUP VOLDN
  VOICE SMART`, digits) to `V600_KEY_*`; `OS.keyUp`, `OS.setOpen`, `OS.powerOn/Off`
  (power key hold); `OS.frame` from the two serials; `OS.st` and `OS.onState` from
  `v600_state` (power, open, call, ringing; `sleep` from the backlight PWM).
- Runs the core in a Worker (`v600_run` in ~10 ms steps against `performance.now()`),
  transfers frames to the page; sound through an AudioWorklet fed from `v600_audio`,
  MP3 clips through `decodeAudioData` (as live.html does today).
- `HW` hooks driven by the hardware: `vibrate` from the vibrator output, `ringLight` from
  the alert LEDs, `haptic('tick')` on key presses.
- Camera: `HW.camera.frame(ctx, w, h)` draws into an offscreen canvas, converted to YUV and
  passed to `v600_camera_frame`, which the camera model serves through the ATI capture path.
- Storage: firmware superfile and flash state in IndexedDB (the state is 2.6 MB, too big
  for the app's localStorage blob). First run asks for the superfile (file picker); the
  firmware is never bundled in a distributed build.

### 6. The app fork and its pipeline — working with the http transport (2026-11-17)
Result: the fork (its own bundle ID, name "V600 Emu", document type and local-network
permission) is built by `./phone ios`: web/ is staged with a config.js (the emulator's
address), embedded by the fork's package builders, and the app is built for the
Simulator. In the Simulator the 3D V600 shows the real firmware: the idle screen when
opened, the outer display's clock when closed, keys and flip going through. Until the
WebAssembly core exists, the page talks to tools/live.py on the Mac (http transport).

Plan as written:
- Fork the app into its own folder (name "V600 Emu", bundle ID `com.kendal.v600emu`,
  separate signing, app group, URL schemes and storage) so it installs beside the original.
- The only coupling: the fork's firmware folder is a build product. `./phone ios` runs
  `make web`, syncs `dist/web/` into the fork's web folder, then `xcodebuild` + install on
  the simulator or a device. The app path lives in a git-ignored `local.cfg`, so this
  public repo never names it.
- Loop for an emulator change: edit `core/` → `./phone test` → `./phone ios`.

### 7. After it runs on the phone
Camera capture path in the core (real viewfinder), key-press tones, the real-time clock,
incoming calls and SMS from the app's existing "incoming call / message" buttons
(they map onto `network.c`), performance work if the interpreter is short of 52 MIPS.

## Risks
- **Speed.** The phone needs 52 MIPS (the ARM7 at 52 MHz), most of it idle and
  fast-forwarded. A cached-decode interpreter typically reaches 100+ MIPS natively and
  roughly half that in WebAssembly; measure in phase 1 and add block threading if short.
- **Port correctness.** Handled by the lockstep runner and per-chip MMIO trace parity.
- **Firmware.** Motorola's code: users supply their own dump; nothing ships in the app.
