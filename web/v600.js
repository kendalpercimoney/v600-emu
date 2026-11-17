// The emulated Motorola V600 behind the firmware contract a host app uses (the contract of
// the hand-made replica: canvases #lcd 176x220 and #ext 96x32, OS.key / keyUp / setOpen,
// OS.frame, OS.st / stateVersion / onState, Snd.ac, window.HW hooks).
//
// The phone runs in a "core" that this file talks to through a small transport:
//   wasm  the emulator compiled to WebAssembly (core-worker.js), in this page: the default
//   http  the emulator running on a computer (tools/live.py), when a server is named:
//         ?server=http://host:8600, or window.V600_SERVER (config.js)
// Both report through the same callbacks (`on` below).
'use strict';

const params = new URLSearchParams(location.search);
const SERVER = params.get('server') || window.V600_SERVER || null;

// The app's key codes -> the emulator's key names (devices.Keypad.CODES, 'red').
const KEYS = {
  UP: 'up', DOWN: 'down', LEFT: 'left', RIGHT: 'right', OK: 'ok', L: 'soft_left', R: 'soft_right',
  M: 'menu', SEND: 'green', END: 'red', VOLUP: 'vol_up', VOLDN: 'vol_down', VOICE: 'voice',
  SMART: 'smart', '*': '*', '#': '#',
};
for (let d = 0; d <= 9; d++) KEYS[String(d)] = String(d);
// The key names in core/v600.h's order (V600_KEY_1 = 0 ...); 'red' is -1.
const KEY_ORDER = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '0', '#', 'green', 'soft_right',
  'soft_left', 'menu', 'vol_up', 'vol_down', 'smart', 'voice', 'right', 'left', 'up', 'down', 'ok'];

// ---- transport: the emulator on a computer (tools/live.py) ------------------------------
function httpCore(server, on) {
  const get = path => fetch(`${server}${path}`);
  let lastSeconds = null, lastOuter = '';
  async function tick() {
    const s = await (await get('/status')).json();
    on.status(s);
    if (s.seconds === lastSeconds) return;
    lastSeconds = s.seconds;
    on.lcd(await createImageBitmap(await (await get('/frame.png')).blob()));
    const bits = new Uint8Array(await (await get('/outer.bits')).arrayBuffer());
    const key = bits.join(',');
    if (key === lastOuter) return;
    lastOuter = key;
    const rgba = new Uint8ClampedArray(96 * 32 * 4);
    for (let i = 0; i < 96 * 32; i++) {
      const lit = bits[i >> 3] >> (7 - (i & 7)) & 1;
      rgba.set(lit ? [80, 150, 255, 255] : [0, 0, 0, 255], 4 * i);
    }
    on.outer(rgba);
  }
  async function sound() {
    const body = await (await get('/audio')).arrayBuffer();
    if (body.byteLength >= 4) on.audio(new DataView(body).getUint32(0, true), new Int16Array(body, 4));
    for (const e of await (await get('/media')).json()) {
      on.media(e.id, e.at, e.data ? Uint8Array.from(atob(e.data), c => c.charCodeAt(0)).buffer : null);
    }
  }
  setInterval(() => tick().catch(() => {}), 100);        // unreachable: try again
  setInterval(() => sound().catch(() => {}), 100);
  return {
    key(name, down) { get(`/key?name=${encodeURIComponent(name)}&down=${down ? 1 : 0}`); },
    flip(open) { get(`/control?do=flip&open=${open ? 1 : 0}`); },
    call(number) { get(`/control?do=call&number=${encodeURIComponent(number)}`); },
    hangUp() { get('/control?do=hangup'); },
    sms(from, text) { get(`/control?do=sms&number=${encodeURIComponent(from)}&text=${encodeURIComponent(text)}`); },
  };
}

// ---- transport: the WebAssembly core, in a Worker (or inline, where Workers fail) -------
function wasmCore(on) {
  let send = null, ready = false;
  const queue = [];                                     // input before the core is up
  const post = m => (ready ? send(m) : queue.push(m));

  function handle(m) {
    if (m.t === 'ready') { ready = true; queue.splice(0).forEach(send); }
    else if (m.t === 'lcd') on.lcd(new ImageData(new Uint8ClampedArray(m.rgba), 176, 220));
    else if (m.t === 'outer') on.outer(new Uint8ClampedArray(m.rgba));
    else if (m.t === 'audio') on.audio(m.start, new Int16Array(m.pcm));
    else if (m.t === 'media') on.media(m.id, m.at, m.mp3);
    else if (m.t === 'status') on.status(m);
    else if (m.t === 'vibrate') hw('vibrate', m.on);
    else if (m.t === 'backlight') on.backlight(m.on);
    else if (m.t === 'power') on.power(m.on);
    else if (m.t === 'light') on.light(m.rgb ? '#' + m.rgb.toString(16).padStart(6, '0') : null);
    else if (m.t === 'state') V600Store.saveState(m.bytes);
    else if (m.t === 'error') on.error(m.text);
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src; s.onload = resolve; s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  async function inline(startMsg) {
    await loadScript('v600core.js');
    await loadScript('core-worker.js');
    const h = self.V600Runner(handle, new URL('v600core.js', location.href).href);
    h(startMsg);
    return h;
  }

  (async () => {
    const [firmware, state] = await Promise.all([V600Store.firmware(), V600Store.state()]);
    const startMsg = { t: 'start', firmware, state: state || null, open: OS.st.open };
    let worker = null;
    try { worker = new Worker('core-worker.js'); } catch (e) { /* inline below */ }
    if (worker) {
      worker.onerror = async e => {                     // could not load: run in the page
        if (ready) return on.error(String(e.message || e));
        worker.terminate();
        const h = await inline(startMsg);
        send = m => h(m);
      };
      worker.onmessage = e => handle(e.data);
      worker.postMessage(startMsg);
      send = m => worker.postMessage(m);
    } else {
      const h = await inline(startMsg);
      send = m => h(m);
    }
  })().catch(e => on.error(String(e)));

  // Store the phone's data when the page goes away (the core also stores it every 5 s).
  addEventListener('visibilitychange', () => { if (document.hidden) post({ t: 'save' }); });
  addEventListener('pagehide', () => post({ t: 'save' }));

  return {
    key(name, down) { post({ t: 'key', key: name === 'red' ? -1 : KEY_ORDER.indexOf(name), down }); },
    flip(open) { post({ t: 'flip', open }); },
    call(number) { post({ t: 'call', number }); },
    hangUp() { post({ t: 'hangup' }); },
    sms(from, text) { post({ t: 'sms', from, text }); },
  };
}

// ---- the displays -------------------------------------------------------------------------
const lcd = document.getElementById('lcd').getContext('2d');
const ext = document.getElementById('ext').getContext('2d');
lcd.imageSmoothingEnabled = false;

// ---- sound: 16 kHz PCM queued back to back, MP3 clips decoded here (as tools/live.html) ----
const RATE = 16000;
let actx = null, playAt = 0, mapSample = 0, mapTime = 0;
const clips = new Map();
const timeOf = s => mapTime + (s - mapSample) / RATE;

async function playClip(id, at, mp3) {
  if (!mp3) {
    if (clips.has(id)) { clips.get(id).stop(Math.max(timeOf(at), actx.currentTime)); clips.delete(id); }
    return;
  }
  const src = actx.createBufferSource();
  try { src.buffer = await actx.decodeAudioData(mp3); } catch (err) { return; }
  src.connect(actx.destination);
  const late = actx.currentTime - timeOf(at);
  src.start(late > 0 ? 0 : timeOf(at), Math.max(late, 0));
  clips.set(id, src);
}

// ---- what the core reports ----------------------------------------------------------------
let lastLcd = null, backlit = true;
const on = {
  // The canvas is what the host shows, so a dark backlight is drawn into it.
  lcd(img) {
    lastLcd = img;
    if (img instanceof ImageData) lcd.putImageData(img, 0, 0);
    else lcd.drawImage(img, 0, 0, 176, 220);
    if (!backlit) { lcd.fillStyle = 'rgba(0, 0, 0, 0.65)'; lcd.fillRect(0, 0, 176, 220); }
    OS.frame++;
  },
  // Switched off (red held) or on again: dark displays, OS.st.power for the host.
  power(up) {
    if (up === OS.st.power) return;
    setState({ power: up });
    if (!up) {
      lastLcd = null;
      lcd.fillStyle = '#000'; lcd.fillRect(0, 0, 176, 220);
      ext.fillStyle = '#000'; ext.fillRect(0, 0, 96, 32);
      OS.frame++;
    }
  },
  // Backlight off (the timeout): the app's keypad light follows OS.st.sleep.
  backlight(lit) {
    if (lit === backlit) return;
    backlit = lit;
    setState({ sleep: lit ? 0 : 2 });
    if (lastLcd) on.lcd(lastLcd);
  },
  outer(rgba) {
    ext.putImageData(new ImageData(rgba, 96, 32), 0, 0);
    OS.frame++;
  },
  audio(start, pcm) {
    if (!actx) return;
    if (playAt > actx.currentTime + 0.5) playAt = actx.currentTime + 0.1;
    playAt = Math.max(playAt, actx.currentTime + 0.1);
    mapSample = start; mapTime = playAt;
    if (pcm.some(v => v !== 0)) {
      const buf = actx.createBuffer(1, pcm.length, RATE), ch = buf.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
      const src = actx.createBufferSource();
      src.buffer = buf; src.connect(actx.destination); src.start(playAt);
    }
    playAt += pcm.length / RATE;
  },
  media(id, at, mp3) { if (actx) playClip(id, at, mp3); },
  // The ring lights: the host's HW.ringLight(on) when they go on or off; its lightColor()
  // (the default below, unless the host has its own) gives the colour the phone drives.
  light(css) {
    if (css) lightCss = css;
    if (!!css !== lightOn) hw('ringLight', lightOn = !!css);
  },
  status(s) {
    window.V600_STATUS = s;                       // for diagnostics (phone time, stopped)
    if (s.stopped) console.warn('emulator stopped:', s.stopped);
    if (s.vibrate !== undefined && s.vibrate !== lastVibrate) hw('vibrate', lastVibrate = s.vibrate);
    if (s.light !== undefined) on.light(s.light);
    if (s.backlight !== undefined) on.backlight(s.backlight);
    if (s.power !== undefined) on.power(s.power);
    if (s.flip_open !== undefined) setState({ open: !!s.flip_open });
  },
  error(text) {
    console.error('V600:', text);
    if (/firmware/.test(text)) V600Store.forgetFirmware();
    lcd.fillStyle = '#000'; lcd.fillRect(0, 0, 176, 220);
    lcd.fillStyle = '#f66'; lcd.font = '11px sans-serif';
    lcd.fillText('Emulator error:', 6, 20);
    text.match(/.{1,30}/g).forEach((line, i) => lcd.fillText(line, 6, 36 + 14 * i));
    OS.frame++;
  },
};

// ---- the contract ----------------------------------------------------------------------
const madeUpNumber = () => '07700900' + String(Math.floor(Math.random() * 1000)).padStart(3, '0');
const SAMPLE_TEXTS = ['Running late, be there in 10', 'Did you see the match last night?',
  'Call me when you get this', 'Happy birthday! x', 'Pub at 8?'];

let lastVibrate = false, lightOn = false, lightCss = '#3d9bff';
const listeners = [];
function setState(changes) {
  let changed = false;
  for (const k in changes) if (OS.st[k] !== changes[k]) { OS.st[k] = changes[k]; changed = true; }
  if (!changed) return;
  OS.stateVersion++;
  for (const cb of listeners) try { cb(Object.assign({}, OS.st)); } catch (e) {}
}

const OS = {
  frame: 0,
  stateVersion: 0,
  st: { power: true, booting: false, open: true, sleep: 0, ringing: null, call: null, missed: 0 },
  key(code) {
    if (code === 'FLIP_OPEN' || code === 'FLIP_CLOSE') return OS.setOpen(code === 'FLIP_OPEN');
    if (!OS.st.open && !['VOLUP', 'VOLDN', 'VOICE', 'SMART'].includes(code)) return;   // closed
    if (KEYS[code]) { core.key(KEYS[code], true); hw('haptic', 'tick'); }
  },
  keyUp(code) { if (KEYS[code]) core.key(KEYS[code], false); },
  setOpen(open) { core.flip(!!open); setState({ open: !!open }); },
  powerOn() { core.key('red', true); setTimeout(() => core.key('red', false), 2500); },
  powerOff() { core.key('red', true); setTimeout(() => core.key('red', false), 2500); },
  onState(cb) { listeners.push(cb); },
  setSpeakerFilter() {},                          // the speaker model: not yet
  // The pretend network: someone calls (default: a made-up number from the range Ofcom
  // keeps for drama), and they hang up.
  incomingCall(number) { core.call(number || madeUpNumber()); },
  endCall() { core.hangUp(); },
  incomingMessage(from, text) {
    core.sms(from || madeUpNumber(), text || SAMPLE_TEXTS[Math.floor(Math.random() * SAMPLE_TEXTS.length)]);
  },
};
const Snd = {
  ac() {
    if (!actx) actx = new AudioContext();
    if (actx.state === 'suspended') actx.resume();
    return actx;
  },
};

// window.HW: the host's hooks over these defaults (as the replica's host.js merges them).
const defaults = {
  vibrate() {}, ringLight() {}, previewLight() {}, lightColor() { return lightCss; },
  flip(open) { OS.setOpen(open); }, haptic() {},
  camera: { prefiltered: false, start() {}, stop() {}, frame() { return false; } },
};
window.HW = Object.assign({}, defaults, window.HW || {});
function hw(name, ...args) { try { window.HW[name](...args); } catch (e) {} }

const core = SERVER ? httpCore(SERVER, on) : wasmCore(on);

// Every 10 s, how phone time keeps up with the clock (the app's console shows it).
let lastPace = null;
setInterval(() => {
  const s = window.V600_STATUS, now = performance.now() / 1000;
  if (!s) return;
  if (lastPace) {
    const rate = (s.seconds - lastPace[1]) / (now - lastPace[0]);
    console.log(`V600: phone time ${s.seconds.toFixed(1)} s, ${rate.toFixed(2)}x real time`);
  }
  lastPace = [now, s.seconds];
}, 10000);
addEventListener('pointerdown', () => Snd.ac(), { once: true });

// desktop test keys (as in the replica's index.html)
const DESK = { ArrowUp: 'UP', ArrowDown: 'DOWN', ArrowLeft: 'LEFT', ArrowRight: 'RIGHT', Enter: 'OK',
               F1: 'L', F2: 'R', m: 'M', s: 'SEND', Escape: 'END', '+': 'VOLUP', '-': 'VOLDN' };
addEventListener('keydown', e => { const c = DESK[e.key] || (/^[0-9*#]$/.test(e.key) ? e.key : null);
  if (c && !e.repeat) { Snd.ac(); OS.key(c); } });
addEventListener('keyup', e => { const c = DESK[e.key] || (/^[0-9*#]$/.test(e.key) ? e.key : null);
  if (c) OS.keyUp(c); });
