// The phone itself: the WebAssembly core (make wasm -> v600core.js / .wasm) run in real
// time, in a Worker (or inline in the page, where Workers are unavailable: V600Runner).
//
// In:  {t:'start', firmware: ArrayBuffer, state: ArrayBuffer|null, open}
//      {t:'key', key: index (core/v600.h, -1 = red), down} {t:'flip', open} {t:'save'}
//      {t:'call', number} (someone calls) {t:'hangup'} (they hang up) {t:'sms', from, text}
// Out: {t:'ready'} {t:'error', text} {t:'lcd', rgba} {t:'outer', rgba} {t:'audio', start, pcm}
//      {t:'media', id, at, mp3|null} {t:'status', seconds, stopped} {t:'state', bytes}
//      {t:'vibrate', on} {t:'light', rgb} (the ring lights, 0xRRGGBB, 0 = dark) {t:'backlight', on}
//      {t:'camera', on}: the viewfinder runs; then {t:'frame', rgba, w, h} pictures come in
//      {t:'power', on}: the phone switched itself off (red held); the red key switches it on
//      again: a new core from the firmware and the state saved as it went off.
'use strict';

self.V600Runner = function (post, coreUrl) {
  const LCD = 176 * 220 * 4, OUTER = 96 * 32 * 4, AUDIO = 16000;
  const OUTER_ON = 0x5096FF, OUTER_OFF = 0x000000;   // blue pixels behind black glass
  const STEP_MS = 10, SAVE_MS = 5000, CLOCK_MS = 60000;
  let M, p, buf, stopped = null;
  let t0 = 0, phone0 = 0, lcdSerial = -1, outerSerial = -1, changes = -1n, lastSave = 0, lastClock = 0;
  let vibrating = 0, light = 0, backlight = -1, camera = 0, frameAt = 0, frameLen = 0;
  let firmware = null, state = null, open = true, off = false;

  // The phone keeps local time: seconds since 1970 as read on a wall clock here. Set again
  // every minute, as phone time stops while the page is hidden (a time set on the phone stays).
  function setClock() {
    const d = new Date();
    M._v600_set_time(p, BigInt(Math.floor(d.getTime() / 1000) - d.getTimezoneOffset() * 60));
    lastClock = performance.now();
  }

  async function start(msg) {
    firmware = msg.firmware;
    state = msg.state && msg.state.byteLength ? msg.state : null;
    open = msg.open !== false;
    await boot();
    post({ t: 'ready' });
  }

  // A fresh core (each power-on: a whole new instance, so the old one's memory goes).
  async function boot() {
    if (!self.V600Core) importScripts(coreUrl);
    M = await self.V600Core({ locateFile: f => new URL(f, coreUrl).href });
    const fw = new Uint8Array(firmware), at = M._malloc(fw.length);
    M.HEAPU8.set(fw, at);
    let st = 0, n = 0;
    if (state) {
      n = state.byteLength;
      st = M._malloc(n);
      M.HEAPU8.set(new Uint8Array(state), st);
    }
    p = M._v600_create(at, fw.length, st, n);
    M._free(at);
    if (st) M._free(st);
    if (!p) return post({ t: 'error', text: 'this firmware file is not usable' });
    buf = M._malloc(LCD + OUTER + 2 * AUDIO + 64);
    if (!open) M._v600_flip(p, 0);
    lcdSerial = outerSerial = backlight = -1;
    vibrating = light = camera = 0;
    frameAt = frameLen = 0;
    off = false;
    setClock();
    changes = M._v600_state_changes(p);
    t0 = performance.now();
    phone0 = M._v600_seconds(p);
    lastSave = t0;
    setTimeout(step, 0);
  }

  // The firmware cut its power: store what it wrote on the way down, go dark, stop.
  function powerOff() {
    save(true);
    post({ t: 'vibrate', on: false });
    post({ t: 'light', rgb: 0 });
    post({ t: 'power', on: false });
    off = true;
    p = 0;
    M = null;
  }

  // Keep phone time level with the clock: run what is due, at most 0.1 s at a time; if the
  // core falls more than a second behind, let the clock slip rather than race to catch up.
  function step() {
    const now = performance.now();
    if (!stopped) {
      let due = (now - t0) / 1000 - (M._v600_seconds(p) - phone0);
      if (due > 1) { t0 += (due - 1) * 1000; due = 1; }
      if (due > 0 && M._v600_run(p, Math.min(due, 0.1) * 1e6)) {
        stopped = 'the CPU stopped on an error';
      }
      publish();
      if (M._v600_powered_off(p)) return powerOff();
      if (now - lastSave > SAVE_MS) save();
      if (now - lastClock > CLOCK_MS) setClock();
    }
    setTimeout(step, STEP_MS);
  }

  function publish() {
    const H = M.HEAPU8;
    if (M._v600_lcd_serial(p) !== lcdSerial) {
      lcdSerial = M._v600_lcd(p, buf);
      const rgba = H.slice(buf, buf + LCD).buffer;
      post({ t: 'lcd', rgba }, [rgba]);
    }
    if (M._v600_outer_serial(p) !== outerSerial) {
      outerSerial = M._v600_outer(p, buf + LCD, OUTER_ON, OUTER_OFF);
      const rgba = H.slice(buf + LCD, buf + LCD + OUTER).buffer;
      post({ t: 'outer', rgba }, [rgba]);
    }
    const pcmAt = buf + LCD + OUTER, firstAt = pcmAt + 2 * AUDIO;
    const n = M._v600_audio(p, pcmAt, AUDIO, firstAt);
    if (n > 0) {
      const start = Number(new BigInt64Array(H.buffer, firstAt, 1)[0]);
      const pcm = M.HEAPU8.slice(pcmAt, pcmAt + 2 * n).buffer;
      post({ t: 'audio', start, pcm }, [pcm]);
    }
    const ev = firstAt + 8;                              // v600_media_t: 24 bytes on wasm32
    while (M._v600_media(p, ev)) {
      const v = new DataView(M.HEAPU8.buffer, ev, 24);
      const start = v.getInt32(0, true), id = v.getInt32(4, true);
      const at = Number(v.getBigInt64(8, true)), data = v.getUint32(16, true), len = v.getUint32(20, true);
      const mp3 = start ? M.HEAPU8.slice(data, data + len).buffer : null;
      post({ t: 'media', id, at, mp3 }, mp3 ? [mp3] : []);
    }
    const v = M._v600_vibrator(p);
    if (v !== vibrating) { vibrating = v; post({ t: 'vibrate', on: !!v }); }
    const cam = M._v600_camera_on(p);
    if (cam !== camera) { camera = cam; post({ t: 'camera', on: !!cam }); }
    const b = M._v600_backlight(p);
    if (b !== backlight) { backlight = b; post({ t: 'backlight', on: !!b }); }
    const l = M._v600_lights(p) >>> 0;
    if (l !== light) { light = l; post({ t: 'light', rgb: l }); }
    post({ t: 'status', seconds: M._v600_seconds(p), stopped });
  }

  // The phone's own data (settings, contacts...): stored by the page whenever it changed.
  function save(force) {
    lastSave = performance.now();
    const c = M._v600_state_changes(p);
    if ((c === changes && !force) || !M._v600_state_ready(p)) return;
    changes = c;
    const n = M._v600_save_state(p, 0), at = M._malloc(n || 1);
    M._v600_save_state(p, at);
    const bytes = M.HEAPU8.slice(at, at + n).buffer;
    M._free(at);
    state = bytes.slice(0);                              // for the next power-on
    post({ t: 'state', bytes }, [bytes]);
  }

  return function (msg) {
    try {
      if (msg.t === 'start') return start(msg).catch(e => post({ t: 'error', text: String(e) }));
      if (msg.t === 'flip') open = !!msg.open;
      if (off && msg.t === 'key' && msg.key === -1 && msg.down) {          // red: switch on
        off = false;
        return boot().then(() => post({ t: 'power', on: true }))
                     .catch(e => post({ t: 'error', text: String(e) }));
      }
      if (!p) return;
      if (msg.t === 'key') M._v600_key(p, msg.key, msg.down ? 1 : 0);
      else if (msg.t === 'flip') M._v600_flip(p, msg.open ? 1 : 0);
      else if (msg.t === 'save') save(true);
      else if (msg.t === 'frame') {                     // a camera picture
        const px = new Uint8Array(msg.rgba);
        if (px.length > frameLen) { if (frameAt) M._free(frameAt); frameAt = M._malloc(px.length); frameLen = px.length; }
        M.HEAPU8.set(px, frameAt);
        M._v600_camera_frame(p, frameAt, msg.w, msg.h);
      }
      else if (msg.t === 'call') {
        const bytes = new TextEncoder().encode(msg.number + '\0'), at = M._malloc(bytes.length);
        M.HEAPU8.set(bytes, at);
        M._v600_incoming_call(p, at);
        M._free(at);
      } else if (msg.t === 'hangup') M._v600_hang_up(p);
      else if (msg.t === 'sms') {
        const a = new TextEncoder().encode(msg.from + '\0'), b = new TextEncoder().encode(msg.text + '\0');
        const at = M._malloc(a.length + b.length);
        M.HEAPU8.set(a, at);
        M.HEAPU8.set(b, at + a.length);
        M._v600_incoming_sms(p, at, at + a.length);
        M._free(at);
      }
    } catch (e) {
      post({ t: 'error', text: String(e) });
    }
  };
};

if (typeof WorkerGlobalScope !== 'undefined' && self instanceof WorkerGlobalScope) {
  const handle = self.V600Runner((m, transfer) => self.postMessage(m, transfer || []),
                                 new URL('v600core.js', self.location.href).href);
  self.onmessage = e => handle(e.data);
}
