// Demo paketleyici Web Worker (module worker): demo baytları -> gzip'li paket.
//
// Ana iş parçacığı bir kaynaktan (bkz. sources.js) aldığı ArrayBuffer'ı bu worker'a devreder:
//   postMessage({type: "run", buffer}, [buffer])
// Worker şu iletileri geri yollar:
//   {type: "stage", stage: "ozet" | "aciliyor" | "isleniyor" | "sikistiriliyor"}
//   {type: "sha", sha256}                       (orijinal baytların özeti; ana taraf kopyaysa durdurabilir)
//   {type: "done", sha256, demSha256, demBytes, pack: ArrayBuffer, timings}
//   {type: "error", kind: "wasm" | "format" | "other", message}
//
// sha256 kuralı sunucudakiyle (precheck_demo) AYNI: seçilen dosyanın olduğu gibi baytları
// (.dem.zst ise sıkıştırılmış hâliyle). Ek olarak açılmış .dem'in özeti de yollanır; sunucu
// ikisini de `demo_files` kaydında arar (aynı demo .zst de .dem de olsa bir kez eklenir).

import { decompress as zstdDecompress } from "/static/vendor/fzstd.mjs";
import init, { parse_package } from "/static/wasm/awpy_wasm.js";

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const sha256 = async (u8) => hex(await crypto.subtle.digest("SHA-256", u8));
const isDem = (u8) => String.fromCharCode(...u8.subarray(0, 7)) === "PBDEMS2";

// Bayt imzasından biçim: dosya uzantısına güvenilmez
function kindOf(u8) {
  if (u8[0] === 0x28 && u8[1] === 0xb5 && u8[2] === 0x2f && u8[3] === 0xfd) return "zst";
  if (u8[0] === 0x1f && u8[1] === 0x8b) return "gz";
  if (isDem(u8)) return "dem";
  return null;
}

async function viaStream(u8, transform) {
  const stream = new Blob([u8]).stream().pipeThrough(transform);
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

self.onmessage = async (ev) => {
  if (ev.data.type !== "run") return;
  const post = (m, tr) => self.postMessage(m, tr || []);
  const t = { start: performance.now() };
  try {
    let u8 = new Uint8Array(ev.data.buffer);
    post({ type: "stage", stage: "ozet" });
    const origSha = await sha256(u8);
    post({ type: "sha", sha256: origSha });

    const kind = kindOf(u8);
    if (!kind) { post({ type: "error", kind: "format", message: "Dosya bir CS2 demosuna benzemiyor." }); return; }
    let dem = u8;
    if (kind !== "dem") {
      post({ type: "stage", stage: "aciliyor" });
      try {
        dem = kind === "zst" ? zstdDecompress(u8) : await viaStream(u8, new DecompressionStream("gzip"));
      } catch (e) {
        post({ type: "error", kind: "wasm", message: "Demo açılamadı (bellek yetmemiş ya da dosya bozuk olabilir)." }); return;
      }
    }
    u8 = null;                 // orijinal baytları bırak: bellek ~1 GB sınırına yakın çalışılıyor
    t.open = performance.now();
    if (!isDem(dem)) {
      post({ type: "error", kind: "format", message: "Açılan dosya bir CS2 demosuna benzemiyor." }); return;
    }
    const demSha = kind === "dem" ? origSha : await sha256(dem);

    post({ type: "stage", stage: "isleniyor" });
    let json;
    try {
      await init({ module_or_path: "/static/wasm/awpy_wasm_bg.wasm" });
      json = parse_package(dem);
    } catch (e) {
      post({ type: "error", kind: "wasm", message: "Tarayıcı demoyu işleyemedi: " + (e && e.message || e) }); return;
    }
    const demBytes = dem.length;
    dem = null;
    t.parse = performance.now();

    post({ type: "stage", stage: "sikistiriliyor" });
    const pack = await viaStream(json, new CompressionStream("gzip"));
    t.end = performance.now();
    post({ type: "done", sha256: origSha, demSha256: demSha, demBytes, pack: pack.buffer,
           timings: { openMs: t.open - t.start, parseMs: t.parse - t.open, gzipMs: t.end - t.parse,
                      jsonBytes: json.length, packBytes: pack.length } }, [pack.buffer]);
  } catch (e) {
    post({ type: "error", kind: "other", message: String(e && e.message || e) });
  }
};
