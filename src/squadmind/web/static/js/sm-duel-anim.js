// SquadMind mini düello animasyonu (ES modülü, bağımlılıksız).
// playDuel(svgOrContainer, {a, b, visible, seed, outcome, onEnd, r}) -> {stop()}
// Koordinatlar ekran pikselidir. Modül hiçbir şey hesaplamaz ve sayı göstermez:
// outcome ('a' | 'b' | null) yalnız çağırandan gelir; null = karşılaşma, kazanan yok.
const NS = 'http://www.w3.org/2000/svg';
const TOTAL = 2300;                    // ms: toplam süre
const T_SIGHT = [350, 750];            // görüş çizgisi çizilir
const T_AIM = [750, 1250];             // nişan kilitlenmesi
const T_FIRE = 1250;                   // atışların başı
const KO_DUR = 450;                    // sonuç vurgusu süresi
// Silah profilleri: atış zamanları (ms), iz hızı (px/sn), iz uzunluğu, kalınlık, parlama boyu
const W = {
  rifle:  { t: [0, 75, 150, 225],           spd: 3000, len: 46,  w: 1.6, fl: 9,  dur: 90 },
  awp:    { t: [0],                         spd: 4500, len: 150, w: 2.6, fl: 15, dur: 160, smoke: 1 },
  pistol: { t: [0, 170],                    spd: 2500, len: 30,  w: 1.3, fl: 6,  dur: 70 },
  smg:    { t: [0, 45, 90, 135, 180, 225],  spd: 2800, len: 26,  w: 1.1, fl: 6,  dur: 60 },
  scout:  { t: [0],                         spd: 3500, len: 90,  w: 1.6, fl: 10, dur: 110 },
  heavy:  { t: [0, 130, 260],               spd: 2200, len: 56,  w: 2.4, fl: 12, dur: 110 },
};

// Tohumlu rastgele (yalnız efekt varyasyonu için)
function rng(seed) {
  let s = (seed | 0) >>> 0;
  return () => {
    s = (s + 0x6D2B79F5) >>> 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t ^= t + Math.imul(t ^ (t >>> 7), 61 | t);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const clamp = (v, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, v));
const ease = (x) => 1 - Math.pow(1 - clamp(x), 3);
const el = (name, attrs, parent) => {
  const n = document.createElementNS(NS, name);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
};
// CSS özel değişkenini oku (squadmind.css tokenları)
function token(host, name, fb) {
  try { return getComputedStyle(host).getPropertyValue(name).trim() || fb; } catch (e) { return fb; }
}

// Silah simgesi (soyut çubuklar; gerçek model çizilmez), (x,y) başlangıç, yön d
function iconPath(kind, x, y, dx, dy) {
  const L = { rifle: 16, awp: 24, pistol: 8, smg: 12, scout: 20, heavy: 15 }[kind] || 14;
  const px = -dy, py = dx;
  let d = `M${x} ${y}L${x + dx * L} ${y + dy * L}`;
  if (kind === 'awp' || kind === 'scout') d += `M${x + dx * L * .45 + px * 3} ${y + dy * L * .45 + py * 3}l${-px * 3} ${-py * 3}`; // dürbün
  if (kind !== 'pistol') d += `M${x + dx * 3} ${y + dy * 3}l${px * 4} ${py * 4}`; // kabza
  return d;
}

export function playDuel(host, opts) {
  const o = opts || {};
  const A = o.a, B = o.b, R = o.r || 14;
  const svg = host && host.namespaceURI === NS && host.tagName === 'svg' ? host : null;
  let wrap = null, root;
  if (svg) root = el('g', { 'pointer-events': 'none' }, svg);
  else {                                   // kapsayıcı: üstüne şeffaf SVG bindir
    wrap = el('svg', { style: 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible' });
    if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
    host.appendChild(wrap);
    root = el('g', {}, wrap);
  }
  const cT = token(host, '--accent', '#FF6A1F'), cCT = token(host, '--accent-2', '#38E1D0');
  const cV = token(host, '--violet', '#8B6CFF'), cL = token(host, '--line', '#F4F6FB');
  const cBg = token(host, '--bg-0', '#0D1018');
  const col = (s) => (s.side === 'CT' ? cCT : cT);
  const dx = B.x - A.x, dy = B.y - A.y, dist = Math.hypot(dx, dy) || 1;
  const ux = dx / dist, uy = dy / dist;
  const wa = W[A.weapon] || W.rifle, wb = W[B.weapon] || W.rifle;
  const rand = rng(o.seed || 1);
  const reduce = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  const vis = o.visible !== false;
  // Sonuç yalnız görüş varken geçerli; kazananı çağıran verir
  const out = vis && (o.outcome === 'a' || o.outcome === 'b') ? o.outcome : null;

  // Sabit düğümler (tek kez kurulur; karelerde yalnız öznitelik değişir)
  const sight = el('line', { x1: A.x, y1: A.y, x2: A.x, y2: A.y, stroke: cL, 'stroke-width': 1.2, 'stroke-linecap': 'round', opacity: 0 }, root);
  const speed = el('path', { stroke: cV, 'stroke-width': 1, fill: 'none', 'stroke-linecap': 'round', opacity: 0 }, root);
  const side = (s, w) => ({
    s, w,
    ring: el('circle', { cx: s.x, cy: s.y, r: 0, fill: 'none', stroke: col(s), 'stroke-width': 1.2, opacity: 0 }, root),
    glyph: el('path', { fill: 'none', stroke: cV, 'stroke-width': 1.2, opacity: 0 }, root),
    trace: el('path', { fill: 'none', stroke: col(s), 'stroke-linecap': 'round', opacity: 0 }, root),
    flash: el('path', { fill: cL, opacity: 0 }, root),
    spark: el('path', { fill: 'none', stroke: cL, 'stroke-width': 1.4, 'stroke-linecap': 'round', opacity: 0 }, root),
    smoke: el('circle', { fill: cL, opacity: 0 }, root),
    icon: el('path', { fill: 'none', stroke: col(s), 'stroke-width': 2, 'stroke-linecap': 'round', opacity: 0 }, root),
    dim: el('circle', { cx: s.x, cy: s.y, r: R + 1, fill: cBg, opacity: 0 }, root),       // kaybeden: sönme
    ko: el('path', { fill: 'none', stroke: cL, 'stroke-width': 1.6, 'stroke-linecap': 'round', opacity: 0 }, root), // KO çizgileri
    glow: el('circle', { cx: s.x, cy: s.y, r: R, fill: 'none', stroke: col(s), 'stroke-width': 3, opacity: 0 }, root), // kazanan: parlama
  });
  const SA = side(A, wa), SB = side(B, wb);
  const mark = el('path', { fill: 'none', stroke: cL, 'stroke-width': 1.4, 'stroke-linecap': 'round', opacity: 0 }, root);
  // Etiketler (varsa) jetonun altında
  for (const s of [A, B]) if (s.label) { const t = el('text', { x: s.x, y: s.y + R + 12, 'text-anchor': 'middle', fill: cL, 'font-size': 10, opacity: .8 }, root); t.textContent = s.label; }
  // Atış varyasyonları tohumdan, önceden (kare başına rastgele yok)
  const vary = (w) => w.t.map(() => ({ j: (rand() - .5) * 0.07, k: 5 + rand() * 4, a: rand() * 6.28 }));
  SA.v = vary(wa); SB.v = vary(wb);
  const ko0 = rand() * 6.28;

  // Kazananın son mermisi isabet edince sonuç vurgusu başlar
  const winW = out === 'a' ? wa : wb;
  const koAt = T_FIRE + Math.max(...winW.t) + (dist - 2 * R) / winW.spd * 1000 + 60;

  // Tek tarafın efektlerini t anında çiz; miss: kaybeden tarafın izi rakibi ıskalar
  function drawSide(S, O, ux1, uy1, t, miss) {
    const { s, w } = S, o2 = O.s, now = t - T_FIRE;
    let tr = '', fl = '', sp = '', tw = 0, trO = 0, flO = 0, spO = 0, smO = 0;
    const mx = s.x + ux1 * R, my = s.y + uy1 * R;           // namlu noktası (jeton kenarı)
    const hx = o2.x - ux1 * R, hy = o2.y - uy1 * R;         // isabet noktası (rakip kenarı)
    const span = Math.hypot(hx - mx, hy - my);
    w.t.forEach((ts, i) => {
      const age = now - ts, v = S.v[i];
      if (age < 0) return;
      const ang = Math.atan2(uy1, ux1) + v.j + (miss ? 0.16 * (v.j < 0 ? -1 : 1) + v.j : 0);
      const cx = Math.cos(ang), cy = Math.sin(ang);
      if (age < w.dur) {                                      // namlu parlaması: 4 uçlu yıldız
        const f = 1 - age / w.dur, L = w.fl * (0.6 + f * 0.6), q = L * .28;
        const px = -cy, py = cx;
        fl += `M${mx} ${my}L${mx + cx * L * .5 + px * q} ${my + cy * L * .5 + py * q}L${mx + cx * L} ${my + cy * L}L${mx + cx * L * .5 - px * q} ${my + cy * L * .5 - py * q}Z`;
        flO = Math.max(flO, f);
      }
      const travel = span / w.spd * 1000, p = clamp(age / travel);
      if (age < travel + 120) {                               // iz: kuyruk başı takip eder
        const head = p * span * (miss ? 1.15 : 1), tail = Math.max(0, head - w.len);
        tr += `M${mx + cx * tail} ${my + cy * tail}L${mx + cx * head} ${my + cy * head}`;
        trO = Math.max(trO, 1 - clamp((age - travel) / 120)); tw = w.w;
      }
      const sa = age - travel;
      if (!miss && sa > 0 && sa < 170) {                      // isabet kıvılcımı (rakip kenarında)
        const f = sa / 170, n = 5;
        for (let k = 0; k < n; k++) {
          const a = v.a + k * 6.283 / n + (k % 2) * .3, r0 = 2 + f * 3, r1 = r0 + v.k * (1 - f * .4) * (1 - f * .3) + 2;
          sp += `M${hx + Math.cos(a) * r0} ${hy + Math.sin(a) * r0}L${hx + Math.cos(a) * r1} ${hy + Math.sin(a) * r1}`;
        }
        spO = Math.max(spO, 1 - f);
      }
      if (w.smoke && age > 0 && age < 520) {                  // AWP: namlu dumanı
        smO = 0.28 * (1 - age / 520); S.smoke.setAttribute('cx', mx + cx * 8); S.smoke.setAttribute('cy', my + cy * 8);
        S.smoke.setAttribute('r', 3 + age / 520 * 9);
      }
    });
    S.trace.setAttribute('d', tr); S.trace.setAttribute('stroke-width', tw); S.trace.setAttribute('opacity', trO);
    S.flash.setAttribute('d', fl); S.flash.setAttribute('opacity', flO);
    S.spark.setAttribute('d', sp); S.spark.setAttribute('opacity', spO);
    S.smoke.setAttribute('opacity', smO);
  }

  // Nişan halkası: dışarıdan içeri daralır, minik eşkenar dörtgen glif
  function drawAim(S, ux1, uy1, t) {
    const p = ease((t - T_AIM[0]) / (T_AIM[1] - T_AIM[0]));
    const gone = clamp((t - (T_FIRE + 500)) / 300);
    const o2 = (t >= T_AIM[0] ? 1 : 0) * (1 - gone);
    S.ring.setAttribute('r', R + 16 - p * 9); S.ring.setAttribute('opacity', o2 * (0.35 + p * 0.5));
    const g = 5 + (1 - p) * 3, a = Math.atan2(uy1, ux1) + (1 - p) * 1.2;
    const gx = S.s.x + Math.cos(a) * (R + 10), gy = S.s.y + Math.sin(a) * (R + 10);
    S.glyph.setAttribute('d', `M${gx - g} ${gy}L${gx} ${gy - g}L${gx + g} ${gy}L${gx} ${gy + g}Z`);
    S.glyph.setAttribute('opacity', o2 * clamp(p * 1.5));
  }

  // Sonuç vurgusu: kaybeden sönük + minik "KO" kıvılcımı, kazanan kısa parlama (metin yok)
  function drawOutcome(t) {
    const f = clamp((t - koAt) / KO_DUR);
    const L = out === 'a' ? SB : SA, Wn = out === 'a' ? SA : SB;
    if (t < koAt) return;
    L.dim.setAttribute('opacity', ease(f) * 0.72);
    L.dim.setAttribute('r', R + 1 - ease(f) * 3);
    let d = '';                                               // anime vuruş: uzun ışınlar + çapraz kesik
    for (let k = 0; k < 8; k++) {
      const a = ko0 + k * 0.785, r0 = R + 2 + f * 6, r1 = r0 + (k % 2 ? 8 : 14) * (1 - f * .5);
      d += `M${L.s.x + Math.cos(a) * r0} ${L.s.y + Math.sin(a) * r0}L${L.s.x + Math.cos(a) * r1} ${L.s.y + Math.sin(a) * r1}`;
    }
    d += `M${L.s.x - R} ${L.s.y - R * .6}L${L.s.x + R} ${L.s.y + R * .6}`;
    L.ko.setAttribute('d', d); L.ko.setAttribute('opacity', (1 - f) * 0.95);
    Wn.glow.setAttribute('r', R + 2 + ease(f) * 8); Wn.glow.setAttribute('opacity', Math.sin(f * Math.PI) * 0.8);
  }

  function frame(t) {
    const sp = clamp((t - T_SIGHT[0]) / (T_SIGHT[1] - T_SIGHT[0]));
    const fade = clamp((t - (TOTAL - 500)) / 400);             // bitişte çizgi sönükleşir
    if (!vis) {                                                 // görüş yok: kesik çizgi yarıda söner, işaret
      const q = ease(sp) * .45;
      sight.setAttribute('x2', A.x + dx * q); sight.setAttribute('y2', A.y + dy * q);
      sight.setAttribute('stroke-dasharray', '3 5');
      sight.setAttribute('opacity', (t < T_SIGHT[0] ? 0 : 0.35) * (1 - fade * .5));
      const mxm = A.x + dx * .45, mym = A.y + dy * .45, s = 5, pop = ease((t - 750) / 250);
      mark.setAttribute('d', `M${mxm - s} ${mym - s}L${mxm + s} ${mym + s}M${mxm + s} ${mym - s}L${mxm - s} ${mym + s}`);
      mark.setAttribute('opacity', pop * .45 * (1 - fade * .5));
      return;
    }
    sight.setAttribute('x2', A.x + dx * ease(sp)); sight.setAttribute('y2', A.y + dy * ease(sp));
    sight.setAttribute('opacity', (t < T_SIGHT[0] ? 0 : 0.55 + 0.25 * clamp((t - T_AIM[1]) / 200)) * (1 - fade * .55));
    drawAim(SA, ux, uy, t); drawAim(SB, -ux, -uy, t);
    drawSide(SA, SB, ux, uy, t, out === 'b'); drawSide(SB, SA, -ux, -uy, t, out === 'a');
    if (out) drawOutcome(t);
    // Anime hız çizgileri: atış anında görüş çizgisine paralel, minik
    const sf = clamp(1 - (t - T_FIRE) / 260);
    if (t >= T_FIRE && sf > 0) {
      const px = -uy, py = ux; let d = '';
      for (let i = 0; i < 4; i++) {
        const off = (i - 1.5) * 7, a0 = .15 + i * .17, a1 = a0 + .12;
        d += `M${A.x + dx * a0 + px * off} ${A.y + dy * a0 + py * off}L${A.x + dx * a1 + px * off} ${A.y + dy * a1 + py * off}`;
      }
      speed.setAttribute('d', d); speed.setAttribute('opacity', sf * .5);
    } else speed.setAttribute('opacity', 0);
  }

  // Azaltılmış hareket: statik kare (görüş çizgisi + silah simgeleri + sonuç işareti)
  function staticFrame() {
    if (vis) {
      sight.setAttribute('x2', B.x); sight.setAttribute('y2', B.y); sight.setAttribute('opacity', .6);
      for (const [S, k] of [[SA, 1], [SB, -1]]) {
        S.icon.setAttribute('d', iconPath(S.s.weapon, S.s.x + k * ux * (R + 6), S.s.y + k * uy * (R + 6), k * ux, k * uy));
        S.icon.setAttribute('opacity', .9);
      }
      if (out) {
        (out === 'a' ? SB : SA).dim.setAttribute('opacity', .6);
        (out === 'a' ? SA : SB).glow.setAttribute('opacity', .6);
      }
    } else {
      const mxm = A.x + dx * .5, mym = A.y + dy * .5;
      mark.setAttribute('d', `M${mxm - 5} ${mym - 5}L${mxm + 5} ${mym + 5}M${mxm + 5} ${mym - 5}L${mxm - 5} ${mym + 5}`);
      mark.setAttribute('opacity', .5);
    }
  }

  let raf = 0, done = false, t0 = 0;
  const finish = () => { if (done) return; done = true; cancelAnimationFrame(raf); if (o.onEnd) o.onEnd(); };
  if (reduce) { staticFrame(); setTimeout(finish, 0); }
  else {
    const total = vis ? TOTAL : 1400;
    const loop = (ts) => {
      if (done) return;
      if (!t0) t0 = ts;
      const t = ts - t0;
      frame(Math.min(t, total));
      if (t >= total) finish(); else raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
  }
  return {
    // Durdur ve sahneyi temizle
    stop() { done = true; cancelAnimationFrame(raf); (wrap || root).remove(); },
  };
}
export default playDuel;
