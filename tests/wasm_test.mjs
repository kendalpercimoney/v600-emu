// The WebAssembly build (make wasm) on its own, in Node: boot, tap the menu, measure speed.
//   node tests/wasm_test.mjs [firmware/v600.shx]
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const V600Core = createRequire(import.meta.url)('../build/web/v600core.js');
const wasm = readFileSync(new URL('../build/web/v600core.wasm', import.meta.url));
const M = await V600Core({
  instantiateWasm: (imports, done) => { WebAssembly.instantiate(wasm, imports).then(r => done(r.instance)); return {}; },
});
const fw = readFileSync(process.argv[2] || 'firmware/v600.shx');
const at = M._malloc(fw.length);
M.HEAPU8.set(fw, at);
const p = M._v600_create(at, fw.length, 0, 0);
M._free(at);
if (!p) throw new Error('v600_create failed');

const lcd = M._malloc(176 * 220 * 4);
const screen = () => (M._v600_lcd(p, lcd), M.HEAPU8.slice(lcd, lcd + 176 * 220 * 4));
const run = s => { for (let t = 0; t < s; t += 0.5) M._v600_run(p, 500000); };
const tap = k => { M._v600_key(p, k, 1); M._v600_key(p, k, 0); run(0.6); };
const MENU = 15, SOFT_RIGHT = 13, RED = -1;

let t0 = performance.now();
run(45);
const boot = (performance.now() - t0) / 1000;
tap(SOFT_RIGHT); tap(SOFT_RIGHT); tap(RED); run(2);
const idle = screen();
tap(MENU); run(2);
const menu = screen();
let changed = 0;
for (let i = 0; i < idle.length; i += 4) changed += idle[i] !== menu[i] || idle[i + 1] !== menu[i + 1];
t0 = performance.now();
run(20);
const idleRate = 20 / ((performance.now() - t0) / 1000);
console.log(`boot: 45 s of phone time in ${boot.toFixed(1)} s; idle runs ${idleRate.toFixed(0)}x real time`);
console.log(changed > 176 * 220 / 4 ? 'PASS  a tap opens the menu' : `FAIL  a tap opens the menu (${changed} pixels)`);
