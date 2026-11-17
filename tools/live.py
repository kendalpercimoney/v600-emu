"""Run the phone in real time and use it from a browser.

    .venv/bin/python tools/live.py [port] [--fresh] [--state=FILE] [--open]
    (or ./v600) then open http://localhost:8600; --open opens it in the browser.

The phone's flash changes (settings, contacts, messages) are kept in state/flash.bin (or
FILE) and restored on the next start; --fresh starts from the firmware image as shipped.

The page also has Restart (reboot, keeping the saved state), Factory reset (delete it and
boot the firmware as shipped), Save now, the flip, and the outer display.

The emulator thread runs in short bursts and sleeps whenever virtual time gets ahead of the
wall clock (idle periods are fast-forwarded by the machine, then waited out here). The page
shows the display, plays the sound (button "Sound") and sends key presses; keyboard: digits,
* #, arrows, Space = centre (OK), Enter = green (send), Esc / Backspace = red (end; hold ~2 s
to switch off), F1 / F2 = left / right soft key, M = menu, +/- = volume.
"""
import base64
import io
import os
import json
import queue
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from v600 import board, fwlog  # noqa: E402
from v600.board import CPU_IPS  # noqa: E402
from v600.machine import Machine  # noqa: E402
from v600.devices import Keypad  # noqa: E402

PAGE = Path(__file__).with_name('live.html')
WEB = Path(__file__).resolve().parent.parent / 'web'


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


MIN_HOLD = 0.1                              # seconds a key stays down at least
STATE = Path(__file__).resolve().parent.parent / 'state' / 'flash.bin'


class Phone:
    """The emulated phone plus what the page can ask of it. Everything that touches the
    machine runs on the emulator thread (run()), fed through the `commands` queue."""

    def __init__(self, fresh=False, state=STATE):
        self.state = state
        self.messages = []
        self.commands = queue.Queue()
        self.lock = threading.Lock()
        self.png = self.outer_png = b''
        self.frame_id = self.outer_id = None
        self.boot(fresh)

    def boot(self, fresh=False):
        """(Re)start the phone from the firmware, plus the saved flash unless fresh."""
        m = Machine(log=lambda *a: None)
        m.verbose_mmio = False
        board.build(m)
        m.reset()
        if self.state.exists() and not fresh:
            n = m.flash.load(self.state)
            print(f'restored {n} flash blocks from {self.state}', flush=True)
        self.messages.clear()
        self.pressed = {}                   # key -> instruction count when it went down
        self.releases = []                  # (instruction count, key): key-ups held back
        fwlog.install(m, sink=self.messages.append)
        m.timers.append(WallClock(self))
        self.flip_open = True
        self.stopped = None
        self.saved_writes = 0
        self.last_save = time.monotonic()
        self.wall0, self.virt0 = time.monotonic(), 0
        self.m = m

    def frame(self):
        """PNG of the main display, re-encoded only when the ATI drew something."""
        ati = self.m.ati
        fid = (id(ati), ati.draws, ati.reg(0x418))
        if fid != self.frame_id:
            self.png, self.frame_id = png(ati.screen().resize((352, 440), Image.NEAREST)), fid
        return self.png

    def outer(self):
        """PNG of the outer (closed-flip) display, 96 x 32 pixels shown 3x."""
        disp = self.m.ati.outer
        fid = (id(disp), disp.updates)
        if fid != self.outer_id:
            self.outer_png, self.outer_id = png(disp.image().resize((288, 96), Image.NEAREST)), fid
        return self.outer_png

    def run(self):
        while True:
            while not self.commands.empty():
                self.handle(*self.commands.get())
            m = self.m
            for due, key in [r for r in self.releases if r[0] <= m.insns]:
                self.releases.remove((due, key))
                board.press(m, key, False)
            ahead = (m.insns - self.virt0) / CPU_IPS - (time.monotonic() - self.wall0)
            if ahead > 0 or self.stopped:
                time.sleep(min(max(ahead, 0), 0.02) or 0.02)
                continue
            with self.lock:
                r = m.run(max_insns=200_000, poll_slices=10**9, spin_slices=10**9)
            if r == 'error':
                self.stopped = 'CPU error (Restart to boot again)'
                print('emulator stopped: CPU error', flush=True)
            if ahead < -2.0:                    # can't keep up: let the clock slip
                self.wall0 += -ahead - 2.0
            if time.monotonic() - self.last_save > 5:
                self.save()

    def handle(self, what, *args):
        if what == 'key':
            # A tap's down and up can arrive together, faster than the firmware scans the
            # keypad: keep each key down for at least MIN_HOLD of phone time.
            key, down = args
            if down:
                self.pressed[key] = self.m.insns
                board.press(self.m, key, True)
            else:
                due = self.pressed.pop(key, 0) + int(MIN_HOLD * CPU_IPS)
                if due > self.m.insns:
                    self.releases.append((due, key))
                else:
                    board.press(self.m, key, False)
        elif what == 'flip':
            self.flip_open = args[0]
            board.set_flip(self.m, self.flip_open)
        elif what == 'restart':
            self.save()
            self.boot()
        elif what == 'factory':
            if self.state.exists():
                self.state.unlink()
            self.boot(fresh=True)
        elif what == 'save':
            self.save(force=True)

    def save(self, force=False):
        """Write changed flash blocks (settings, contacts, ...) to the state file."""
        f = self.m.flash
        self.last_save = time.monotonic()
        if (force or f.writes != self.saved_writes) and f.idle():
            with self.lock:
                f.save(self.state)
            self.saved_writes = f.writes

    def sound(self):
        """Index of the first sample (uint32 LE), then PCM (16 kHz, 16-bit LE) made since the
        last call. No lock: the emulator thread retakes it at once while catching up, which
        would starve this request."""
        start, pcm = self.m.audio.pull()
        return (start & 0xFFFFFFFF).to_bytes(4, 'little') + pcm

    def media(self):
        """Clips (MP3 ringtones) started or stopped since the last call, as JSON."""
        out = []
        for kind, ident, at, *rest in self.m.audio.media_events():
            e = {'id': ident, 'at': at}
            if kind == 'start':
                e.update(mime=rest[0], data=base64.b64encode(rest[1]).decode())
            else:
                e['stop'] = True
            out.append(e)
        return json.dumps(out).encode()

    def status(self):
        lag = (time.monotonic() - self.wall0) - (self.m.insns - self.virt0) / CPU_IPS
        return {'seconds': round(self.m.insns / CPU_IPS, 1), 'lag': round(lag, 2),
                'log': self.messages[-6:], 'flip_open': self.flip_open, 'stopped': self.stopped}


def png(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def handler(phone):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body, ctype):
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Access-Control-Allow-Origin', '*')      # web/ in the app
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
            elif u.path == '/outer.png':
                self.send(phone.outer(), 'image/png')
            elif u.path == '/outer.bits':                # 32 x 96 pixels, 1 bit each, rows
                self.send(np.packbits(phone.m.ati.outer.pixels != 0).tobytes(), 'application/octet-stream')
            elif u.path == '/web/config.js':             # web/ served here talks to this server
                self.send(b'window.V600_SERVER = location.origin;\n', 'text/javascript')
            elif u.path.startswith('/web/'):             # the app's front end (web/)
                f = (WEB / u.path[5:]).resolve()
                if WEB in f.parents and f.is_file():
                    types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript'}
                    self.send(f.read_bytes(), types.get(f.suffix, 'application/octet-stream'))
                else:
                    self.send_error(404)
            elif u.path == '/key':
                name = q.get('name', [''])[0]
                if os.environ.get('V600_KEYLOG'):
                    print('key', name, q.get('down', ['1'])[0], flush=True)
                if name in Keypad.CODES or name == 'red':
                    phone.commands.put(('key', name, q.get('down', ['1'])[0] == '1'))
                self.send(b'ok', 'text/plain')
            elif u.path == '/control':          # restart, factory, save, flip
                what = q.get('do', [''])[0]
                if what == 'flip':
                    phone.commands.put(('flip', q.get('open', ['1'])[0] == '1'))
                elif what in ('restart', 'factory', 'save'):
                    phone.commands.put((what,))
                self.send(b'ok', 'text/plain')
            elif u.path == '/audio':
                self.send(phone.sound(), 'application/octet-stream')
            elif u.path == '/media':
                self.send(phone.media(), 'application/json')
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
    url = f'http://localhost:{server.server_address[1]}'
    print(f'V600 running: {url}  (Ctrl+C to quit)', flush=True)
    if '--open' in sys.argv:
        webbrowser.open(url)
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
