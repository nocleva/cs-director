// Node'da WASM paketleyiciyi çalıştırır: demo (.dem / .dem.zst / .dem.gz) -> paket (.json.gz).
//
// Kullanım:  node node_pack.js <demo> <cikis.json.gz> [pkg-web klasörü]
// Stdout'a tek satır JSON yazar: süreler, tepe bellek (maxRSS), paket boyutları.
// Doğrulama betiği (scripts/validate_package.py) bu çıktıyı okur.
"use strict";
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const { pathToFileURL } = require("url");

async function main() {
  const [demoPath, outPath, pkgArg] = process.argv.slice(2);
  if (!demoPath || !outPath) {
    console.error("kullanım: node node_pack.js <demo> <cikis.json.gz> [pkg-web]");
    process.exit(2);
  }
  const pkgDir = path.resolve(pkgArg || path.join(__dirname, "pkg-web"));
  const t0 = process.hrtime.bigint();
  const secs = (a, b) => Number(b - a) / 1e9;

  // WASM modülünü yükle (ES modül çıktısı; baytlar verildiği için ağa çıkmaz)
  const mod = await import(pathToFileURL(path.join(pkgDir, "awpy_wasm.js")).href);
  mod.initSync({ module: fs.readFileSync(path.join(pkgDir, "awpy_wasm_bg.wasm")) });

  // Demoyu aç (tarayıcıda bu adım DecompressionStream / zstd-wasm ile yapılır)
  const raw = fs.readFileSync(demoPath);
  let dem;
  if (demoPath.endsWith(".zst")) dem = zlib.zstdDecompressSync(raw);
  else if (demoPath.endsWith(".gz")) dem = zlib.gunzipSync(raw);
  else dem = raw;
  const u8 = new Uint8Array(dem.buffer, dem.byteOffset, dem.length);
  const t1 = process.hrtime.bigint();

  const json = mod.parse_package(u8); // Uint8Array (JSON)
  const t2 = process.hrtime.bigint();

  const gz = zlib.gzipSync(Buffer.from(json.buffer, json.byteOffset, json.length), { level: 9 });
  const t3 = process.hrtime.bigint();
  fs.writeFileSync(outPath, gz);

  console.log(JSON.stringify({
    demo: path.basename(demoPath),
    demo_bytes: dem.length,
    open_s: secs(t0, t1),
    wasm_s: secs(t1, t2),
    gzip_s: secs(t2, t3),
    json_bytes: json.length,
    gzip_bytes: gz.length,
    max_rss_mb: Math.round(process.resourceUsage().maxRSS / 1024),
  }));
}

main().catch((e) => { console.error("HATA", e); process.exit(1); });
