// Düello animasyonu duman testi: sahte DOM ile kare durumları (deterministik)
import { playDuel } from '../../src/squadmind/web/static/js/sm-duel-anim.js';
const NS = 'http://www.w3.org/2000/svg';
const mk = (tag) => ({ tagName: tag, namespaceURI: NS, attrs: {}, kids: [], style: {},
  setAttribute(k, v) { this.attrs[k] = String(v); }, appendChild(c) { this.kids.push(c); c.parent = this; return c; },
  remove() { if (this.parent) this.parent.kids = this.parent.kids.filter((x) => x !== this); }, set textContent(v) {} });
globalThis.document = { createElementNS: (_n, t) => mk(t) };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '', position: 'static' });
let cb = null; globalThis.requestAnimationFrame = (f) => { cb = f; return 1; }; globalThis.cancelAnimationFrame = () => {};
function run(outcome, visible, times) {
  const svg = mk('svg'); let ended = false;
  playDuel(svg, { a: { x: 100, y: 100, side: 'T', weapon: 'rifle' }, b: { x: 400, y: 160, side: 'CT', weapon: 'awp' },
    visible, seed: 7, outcome, onEnd: () => { ended = true; } });
  const root = svg.kids[0];
  for (const t of times) cb(t + 1); // ilk çağrı t0'ı belirler
  const op = (i) => Number(root.kids[i].attrs.opacity || 0);
  // düğüm sırası: sight, speed, [ring,glyph,trace,flash,spark,smoke,icon,dim,ko,glow] x A, x B, mark
  return { dimA: op(2 + 7), dimB: op(12 + 7), glowA: op(2 + 9), glowB: op(12 + 9), ended };
}
const out = {};
for (const oc of ['a', 'b', null]) out[String(oc)] = run(oc, true, [0, 2299, 2400]);
out.novis = run('a', false, [0, 1500]);
const fail = (m) => { console.error('FAIL ' + m); process.exit(1); };
if (!(out.a.dimB > 0.5 && out.a.dimA === 0)) fail('a: kaybeden b sönmeli ' + JSON.stringify(out.a));
if (!(out.b.dimA > 0.5 && out.b.dimB === 0)) fail('b: kaybeden a sönmeli ' + JSON.stringify(out.b));
if (!(out.null.dimA === 0 && out.null.dimB === 0)) fail('null: kimse sönmemeli');
if (out.novis.dimA || out.novis.dimB) fail('görüş yokken sonuç yok');
if (!out.a.ended) fail('onEnd çağrılmadı');
console.log('OK');
