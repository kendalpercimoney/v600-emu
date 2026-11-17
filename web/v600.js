// The emulated Motorola V600 behind the firmware contract a host app uses (the contract of
// the hand-made replica: canvases #lcd 176x220 and #ext 96x32, OS.key / keyUp / setOpen,
// OS.frame, OS.st / stateVersion / onState, Snd.ac, window.HW hooks).
//
// The phone runs in a "core" that this file talks to through a small transport:
//   http  the emulator running on a computer (tools/live.py), for development
//   wasm  the emulator compiled to WebAssembly, in this page (PORTING.md phase 4)
// Pick with ?core=http&server=http://host:8600 (default: http, the page's own server).
'use strict';

const params = new URLSearchParams(location.search);
const SERVER = params.get('server') || (location.protocol.startsWith('http') ? location.origin : 'http://localhost:8600');

// The app's key codes -> the emulator's key names (devices.Keypad.CODES, 'red').
const KEYS = {
  UP: 'up', DOWN: 'down', LEFT: 'left', RIGHT: 'right', OK: 'ok', L: 'soft_left', R: 'soft_right',
  M: 'menu', SEND: 'green', END: 'red', VOLUP: 'vol_up', VOLDN: 'vol_down', VOICE: 'voice',
  SMART: 'smart', '*': '*', '#': '#',
};
for (let d = 0; d <= 9; d++) KEYS[String(d)] = String(d);

// ---- transport: the emulator on a computer (tools/live.py) ------------------------------
const httpCore = {
  key(name, down) { return fetch(`${SERVER}/key?name=${encodeURIComponent(name)}&down=${down ? 1 : 0}`); },
  flip(open) { return fetch(`${SERVER}/control?do=flip&open=${open ? 1 : 0}`); },
  async lcd() {                                   // ImageBitmap of the main display, or null
    const r = await fetch(`${SERVER}/frame.png`);
    return createImageBitmap(await r.blob());
  },
  async outer() {                                 // 32 x 96 bits, rows, MSB first
    return new Uint8Array(await (await fetch(`${SERVER}/outer.bits`)).arrayBuffer());
  },
  async status() { return (await fetch(`${SERVER}/status`)).json(); },
  async audio() { return (await fetch(`${SERVER}/audio`)).arrayBuffer(); },
  async media() { return (await fetch(`${SERVER}/media`)).json(); },
};
const core = httpCore;

// ---- the displays -------------------------------------------------------------------------
const lcd = document.getElementById('lcd').getContext('2d');
const ext = document.getElementById('ext').getContext('2d');
lcd.imageSmoothingEnabled = false;
const extImage = ext.createImageData(96, 32);
let lastOuter = '';
const EXT_ON = [80, 150, 255], EXT_OFF = [0, 0, 0];   // blue pixels behind black glass

async function drawLcd() {
  const img = await core.lcd();
  lcd.drawImage(img, 0, 0, 176, 220);
  OS.frame++;
}

async function drawOuter() {
  const bits = await core.outer();
  const key = bits.join(',');
  if (key === lastOuter) return;
  lastOuter = key;
  for (let i = 0; i < 96 * 32; i++) {
    const c = bits[i >> 3] >> (7 - (i & 7)) & 1 ? EXT_ON : EXT_OFF;
    extImage.data.set([c[0], c[1], c[2], 255], 4 * i);
  }
  ext.putImageData(extImage, 0, 0);
  OS.frame++;
}

// ---- sound: 16 kHz PCM queued back to back, MP3 clips decoded here (as tools/live.html) ----
const RATE = 16000;
let actx = null, playAt = 0, mapSample = 0, mapTime = 0;
const clips = new Map();
const timeOf = s => mapTime + (s - mapSample) / RATE;
async function pumpAudio() {
  if (!actx) return;
  const body = await core.audio();
  if (body.byteLength < 4) return;
  const start = new DataView(body).getUint32(0, true), pcm = new Int16Array(body, 4);
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
  for (const e of await core.media()) {
    if (e.data) {
      const src = actx.createBufferSource();
      try { src.buffer = await actx.decodeAudioData(Uint8Array.from(atob(e.data), c => c.charCodeAt(0)).buffer); }
      catch (err) { continue; }
      src.connect(actx.destination);
      const late = actx.currentTime - timeOf(e.at);
      src.start(late > 0 ? 0 : timeOf(e.at), Math.max(late, 0));
      clips.set(e.id, src);
    } else if (clips.has(e.id)) {
      clips.get(e.id).stop(Math.max(timeOf(e.at), actx.currentTime));
      clips.delete(e.id);
    }
  }
}

// ---- the contract ----------------------------------------------------------------------
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
  incomingCall() {}, incomingMessage() {},        // the pretend network: not yet
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
  vibrate() {}, ringLight() {}, previewLight() {}, lightColor() { return '#3d9bff'; },
  flip(open) { OS.setOpen(open); }, haptic() {},
  camera: { prefiltered: false, start() {}, stop() {}, frame() { return false; } },
};
window.HW = Object.assign({}, defaults, window.HW || {});
function hw(name, ...args) { try { window.HW[name](...args); } catch (e) {} }

// ---- loops -------------------------------------------------------------------------------
let lastStatus = null;
async function tick() {
  try {
    const s = await core.status();
    if (s.stopped) console.warn('emulator stopped:', s.stopped);
    setState({ open: !!s.flip_open });
    if (!lastStatus || s.seconds !== lastStatus.seconds) {
      await drawLcd();
      await drawOuter();
    }
    lastStatus = s;
  } catch (e) { /* the emulator is not reachable: try again */ }
}
setInterval(tick, 100);
setInterval(() => pumpAudio().catch(() => {}), 100);
addEventListener('pointerdown', () => Snd.ac(), { once: true });

// desktop test keys (as in the replica's index.html)
const DESK = { ArrowUp: 'UP', ArrowDown: 'DOWN', ArrowLeft: 'LEFT', ArrowRight: 'RIGHT', Enter: 'OK',
               F1: 'L', F2: 'R', m: 'M', s: 'SEND', Escape: 'END', '+': 'VOLUP', '-': 'VOLDN' };
addEventListener('keydown', e => { const c = DESK[e.key] || (/^[0-9*#]$/.test(e.key) ? e.key : null);
  if (c && !e.repeat) { Snd.ac(); OS.key(c); } });
addEventListener('keyup', e => { const c = DESK[e.key] || (/^[0-9*#]$/.test(e.key) ? e.key : null);
  if (c) OS.keyUp(c); });
