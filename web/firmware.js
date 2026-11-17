// Where the WebAssembly phone's inputs live: the firmware (the user's own V600 dump) and the
// phone's saved state, both in IndexedDB.
//
// The firmware, first found of: IndexedDB (picked earlier); firmware.shx.gz next to this
// page (a private build: make web copies firmware/v600.shx there; never published);
// a file the user picks (stored for next time).
'use strict';

const V600Store = (() => {
  const DB = 'v600-emu', STORE = 'files';

  function open() {
    return new Promise((resolve, reject) => {
      const r = indexedDB.open(DB, 1);
      r.onupgradeneeded = () => r.result.createObjectStore(STORE);
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(r.error);
    });
  }

  async function op(mode, fn) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode), r = fn(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(r.result);
      tx.onerror = () => reject(tx.error);
    });
  }

  const get = key => op('readonly', s => s.get(key)).catch(() => undefined);
  const put = (key, value) => op('readwrite', s => s.put(value, key)).catch(e => console.warn('not stored:', e));
  const del = key => op('readwrite', s => s.delete(key)).catch(() => undefined);

  async function bundled() {
    try {
      const r = await fetch('firmware.shx.gz');
      if (!r.ok) return null;
      return await new Response(r.body.pipeThrough(new DecompressionStream('gzip'))).arrayBuffer();
    } catch (e) {
      return null;
    }
  }

  // Ask for the file: a full-page prompt with a file picker (the click is the user's own).
  function pick() {
    return new Promise(resolve => {
      const box = document.createElement('label');
      box.style.cssText = 'position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;' +
        'justify-content:center;gap:10px;background:#000;color:#9cf;font:14px -apple-system,sans-serif;' +
        'text-align:center;padding:20px;z-index:9;cursor:pointer';
      box.innerHTML = '<b>V600 firmware needed</b><span>Tap to choose your own V600 flash dump ' +
        '(a .shx superfile). It stays on this device.</span>';
      const input = document.createElement('input');
      input.type = 'file';
      input.style.display = 'none';
      input.onchange = async () => {
        const f = input.files[0];
        if (!f) return;
        box.remove();
        resolve(await f.arrayBuffer());
      };
      box.appendChild(input);
      document.body.appendChild(box);
    });
  }

  async function firmware() {
    const stored = await get('firmware');
    if (stored) return stored;
    const b = await bundled();
    if (b) return b;
    const picked = await pick();
    await put('firmware', picked);
    return picked;
  }

  return {
    firmware,
    state: () => get('state'),
    saveState: bytes => put('state', bytes),
    forgetState: () => del('state'),
    forgetFirmware: () => del('firmware'),
  };
})();
