# V600 emulator

A low-level emulator for the **Motorola V600** (2004), a flip phone I've always loved. This is a
passion project: the goal is to run the phone's real firmware, unmodified, and use it again on a
computer, from the boot animation through the menus.

![Boot animation](boot_animation.png)

## What works

- Real firmware (`TRIPLETS_G_0B.09.38R`) boots on an emulated big-endian ARM7TDMI (Neptune LTE
  baseband), through the full startup animation to the standby screen.
- Interrupt controller, timers, PCAP2 power chip (ADC, interrupts), battery ID chip (DS2502 over
  one-wire), keypad, flip sensor.
- ATI Imageon W2250 graphics: command ring and the 2D engine operations the UI uses (blits, fills,
  lines, mono glyph text).
- A virtual GSM SIM card on the emulated SIM interface: the phone completes SIM start-up,
  reaches the idle screen ("No Service") and its menus can be navigated.
- Sound: key clicks, the power-up tune, tone, MIDI and MP3 ringtones. The phone's DSP runs
  from ROM we don't have, so its audio side is recreated from the commands the firmware
  sends it (`core/audio.c`, with MIDI synthesis; MP3 is decoded by the browser).
- The camera app opens (black viewfinder: the sensor answers, but makes no picture yet).
- Runs in real time in a browser with clickable keys (`tools/live.py`); press "Sound" on the
  page to hear it.

| Idle | Menu | Screensaver |
|---|---|---|
| ![Idle](idle.png) | ![Menu](menu.png) | ![Screensaver](screensaver.png) |

## Running it

The firmware is not included. You need your own V600 firmware dump as a Motorola superfile at
`firmware/v600.shx`. Once, to set up:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

The emulator core is C (`core/`); `./phone` builds it with `make` (Xcode command line tools
or any C compiler).

Then:

```bash
./phone          # the phone in your browser (http://localhost:8600)
./phone test     # boot headless and check the main features: PASS / FAIL, about 30 s
./phone drive 45.0 menu +2.0 shot:menu   # a scripted headless session (see tools/drive.py)
```

The page has the keypad, the display, and controls: **Sound**, **Close / Open flip**,
**Save now**, **Restart** (reboot, keeping the saved state) and **Factory reset** (delete the
saved state and boot the firmware as shipped). The outer display (the clock on the lid) is shown
beside it. With the flip closed the phone behaves as it does closed: the main screen is dark,
only the side keys (volume, voice, smart) can be pressed, and the phone uses the outer display.

Keyboard: digits, `*` `#`, arrows, Space = OK, Enter = green key, Esc = red key (hold to
switch off), F1/F2 = soft keys, M = menu, +/- = volume.

Changes the phone makes to its flash (settings, contacts, messages) are saved to
`state/flash.bin` every few seconds and on exit, and restored next time.

`./phone test` boots from the firmware as shipped (it never touches `state/`) and checks: the
idle screen, registering on the pretend network, the menu, the ringing tone, an outgoing call
(set up, answered, hung up) and the outer display. Screenshots go to `out/selftest/`.

### In a browser or the iPhone app, with no server (WebAssembly)

The same core compiles to WebAssembly with the [Emscripten SDK](https://emscripten.org)
(about 2 GB; it needs Python 3.10+, e.g. `brew install python@3.12`):

```bash
git clone https://github.com/emscripten-core/emsdk.git ~/code/emsdk
cd ~/code/emsdk && EMSDK_PYTHON=python3.12 ./emsdk install latest && ./emsdk activate latest
```

(elsewhere: put `EMSDK=/path/to/emsdk` in `local.cfg`). Then:

```bash
make web         # build/web/: the page, the core (v600core.wasm) and the firmware, gzipped
make wasm-test   # the WebAssembly core in Node: boot, a key tap, speed
./phone ios      # the iPhone app fork (path in local.cfg: APP=...) with that page built in
```

Serve `build/web/` (e.g. `python3 -m http.server -d build/web`) and open it: the phone runs
in the page and keeps its data in the browser (IndexedDB). Without `firmware/v600.shx` the
page asks for the firmware file instead; `build/web/` holds your firmware, so never publish it.
`./phone ios --server=http://localhost:8654` builds the app against a `tools/live.py`
instead (handy while changing the core: no app rebuild).

## Layout

- `core/`: the emulator, in portable C: the CPU (`cpu.c`, `mem.c`), the machine loop and
  hooks (`run.c`, `machine.c`), one file per chip (`ati.c`, `sim.c`, `audio.c`, ...),
  the firmware hooks (`firmware.c`) and the board (`board.c`). `make` builds it.
- `web/`: the page for the browser and the app: `v600.js` (the app's firmware contract),
  `core-worker.js` (the WebAssembly core in real time), `firmware.js` (firmware and state).
- `v600/`: the Python handle on it that the tools use (`machine.py`, `board.py`).
- `tools/`: live mode, the self-test, scripted runs, golden traces, disassembly helpers.
- `tests/golden/`: the golden traces: digests of the whole machine through scripted
  sessions, recorded from the all-Python version (tag `py-reference`); `./phone check`.
- `PLAN.md`: approach and progress log. `HARDWARE.md`: every register learned and how.
  `PORTING.md`: the plan for the browser and iPhone builds.

## Next

- Incoming calls and text messages from the pretend network; the call voice path.
- Key-press tones (silent so far), the real-time clock (the phone shows 0:00), a camera picture.

This is an independent hobby project, not affiliated with or endorsed by Motorola.
