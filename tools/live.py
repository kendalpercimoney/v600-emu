"""Run the phone in real time and use it from a browser.

    .venv/bin/python tools/live.py [port] [--fresh] [--state=FILE]   then open http://localhost:8600

The phone's flash changes (settings, contacts, messages) are kept in state/flash.bin (or
FILE) and restored on the next start; --fresh starts from the firmware image as shipped.

The emulator thread runs in short bursts and sleeps whenever virtual time gets ahead of the
wall clock (idle periods are fast-forwarded by the machine, then waited out here). The page
shows the display, plays the sound (button "Sound") and sends key presses; keyboard: digits,
* #, arrows, Space = centre (OK), Enter = green (send), Esc / Backspace = red (end; hold ~2 s
to switch off), F1 / F2 = left / right soft key, M = menu, +/- = volume.
"""
import io
import os
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image  # noqa: E402

from v600 import board, fwlog  # noqa: E402
from v600.board import CPU_IPS  # noqa: E402
from v600.machine import Machine  # noqa: E402
from v600.periph import Keypad  # noqa: E402

PAGE = Path(__file__).with_name('live.html')


class WallClock:
    """Timer entry that stops idle fast-forward at the present (wall-clock) moment, so input
    arriving while the phone idles lands at the right virtual time."""

    def __init__(self, phone):
        self.phone = phone

    def update(self):
        pass

    def next_event(self):
        p = self.phone
        if p.wall0 is None:
            return None
        now = p.virt0 + (time.monotonic() - p.wall0 + 0.01) * CPU_IPS
        return max(now, p.m.insns + 1) * p.m.counter.k


STATE = Path(__file__).resolve().parent.parent / 'state' / 'flash.bin'


class Phone:
    def __init__(self, fresh=False, state=STATE):
        self.state = state
        self.m = Machine(log=lambda *a: None)
        self.m.verbose_mmio = False
        board.build(self.m)
        self.m.reset()
        if state.exists() and not fresh:
            n = self.m.flash.load(state)
            print(f'restored {n} flash blocks from {state}', flush=True)
        self.saved_writes = 0
        self.last_save = time.monotonic()
        self.messages = []
        fwlog.install(self.m, sink=self.messages.append)
        self.keys = queue.Queue()
        self.lock = threading.Lock()
        self.png = b''
        self.frame_id = -1
        self.wall0 = None
        self.virt0 = 0
        self.m.timers.append(WallClock(self))

    def frame(self):
        """PNG of the visible screen, re-encoded only when the ATI drew something."""
        ati = self.m.ati
        fid = (ati.draws, ati.reg(0x418))
        if fid != self.frame_id:
            img = ati.screen().resize((352, 440), Image.NEAREST)
            buf = io.BytesIO()
            img.save(buf, 'PNG')
            self.png, self.frame_id = buf.getvalue(), fid
        return self.png

    def run(self):
        m = self.m
        self.wall0 = time.monotonic()
        while True:
            while not self.keys.empty():
                name, down = self.keys.get()
                board.press(m, name, down)
            ahead = (m.insns - self.virt0) / CPU_IPS - (time.monotonic() - self.wall0)
            if ahead > 0:
                time.sleep(min(ahead, 0.02))
                continue
            with self.lock:
                r = m.run(max_insns=200_000, poll_slices=10**9, spin_slices=10**9)
            if r == 'error':
                print('emulator stopped: CPU error', flush=True)
                return
            if ahead < -2.0:                    # can't keep up: let the clock slip
                self.wall0 += -ahead - 2.0
            if time.monotonic() - self.last_save > 5:
                self.save()

    def save(self):
        """Write changed flash blocks (settings, contacts, ...) to the state file."""
        f = self.m.flash
        self.last_save = time.monotonic()
        if f.writes != self.saved_writes and f.idle():
            with self.lock:
                f.save(self.state)
            self.saved_writes = f.writes

    def sound(self):
        """PCM (8 kHz, 16-bit little-endian) made since the last call. No lock: the emulator
        thread retakes it at once while catching up, which would starve this request."""
        return self.m.audio.pull()

    def status(self):
        lag = (time.monotonic() - self.wall0) - (self.m.insns - self.virt0) / CPU_IPS
        return {'seconds': round(self.m.insns / CPU_IPS, 1), 'lag': round(lag, 2),
                'log': self.messages[-6:]}


def handler(phone):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body, ctype):
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == '/':
                self.send(PAGE.read_bytes(), 'text/html; charset=utf-8')
            elif u.path == '/frame.png':
                self.send(phone.frame(), 'image/png')
            elif u.path == '/key':
                name = q.get('name', [''])[0]
                if name in Keypad.CODES or name == 'red':
                    phone.keys.put((name, q.get('down', ['1'])[0] == '1'))
                self.send(b'ok', 'text/plain')
            elif u.path == '/audio':
                self.send(phone.sound(), 'application/octet-stream')
            elif u.path == '/status':
                self.send(json.dumps(phone.status()).encode(), 'application/json')
            else:
                self.send_error(404)
    return H


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    port = int(args[0]) if args else 8600
    state = next((Path(a[8:]) for a in sys.argv if a.startswith('--state=')), STATE)
    phone = Phone(fresh='--fresh' in sys.argv, state=state)
    server = None
    for p in range(port, port + 10):        # bind first: a busy port must not leave the
        try:                                # emulator thread running
            server = ThreadingHTTPServer(('127.0.0.1', p), handler(phone))
            break
        except OSError:
            print(f'port {p} is in use, trying {p + 1}', flush=True)
    if server is None:
        sys.exit('no free port found')
    threading.Thread(target=phone.run, daemon=True).start()
    print(f'V600 running: http://localhost:{server.server_address[1]}  (Ctrl+C to quit)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            phone.save()
        except Exception as e:                  # never lose the exit over it
            print(f'could not save phone state: {e}', flush=True)
        os._exit(0)                         # skip interpreter teardown under a running CPU


if __name__ == '__main__':
    main()
