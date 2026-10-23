// Bir demo kaynağını (sources.js arayüzü) Web Worker'da pakete çevirir.
// Her demo için YENİ worker açılır ve bitince kapatılır: WASM belleği (~1 GB) tamamen serbest kalır,
// bir demonun bellek hatası sonrakini bozmaz.

export class PackError extends Error {
  constructor(kind, message) { super(message); this.kind = kind; }
}

// onStage(stage), onSha(sha256) -> true dönerse (kopya) iş durdurulur ve PackError("duplicate") atılır.
// Dönen nesne: {promise, cancel()}; promise {sha256, demSha256, pack: ArrayBuffer, timings} verir.
export function packSource(source, { onStage = () => {}, onSha = () => false } = {}) {
  let worker = null, cancelled = false, rejectFn = null;
  const promise = new Promise((resolve, reject) => {
    rejectFn = reject;
    (async () => {
      try {
        worker = new Worker("/static/pack/pack_worker.js", { type: "module" });
      } catch (e) {
        reject(new PackError("wasm", "Bu tarayıcı modül worker'ı desteklemiyor.")); return;
      }
      worker.onerror = (e) => { reject(new PackError("wasm", "Worker yüklenemedi: " + (e.message || "bilinmeyen hata"))); };
      worker.onmessage = (ev) => {
        const m = ev.data;
        if (m.type === "stage") onStage(m.stage);
        else if (m.type === "sha") { if (onSha(m.sha256)) reject(new PackError("duplicate", "Bu demo zaten seçilmiş.")); }
        else if (m.type === "done") resolve(m);
        else if (m.type === "error") reject(new PackError(m.kind, m.message));
      };
      onStage("okunuyor");
      let buffer;
      try { buffer = await source.getBytes(); }
      catch (e) {
        // Ağdan okuma hatası (FACEIT, CORS) ayrı tür: sunucu yedeği denenebilsin
        if (e && e.kind === "fetch") reject(new PackError("fetch", e.message));
        else reject(new PackError("wasm", (e && e.message) || "Dosya okunamadı (bellek yetmemiş olabilir)."));
        return;
      }
      if (cancelled) return;
      worker.postMessage({ type: "run", buffer }, [buffer]);
    })();
  }).finally(() => { if (worker) worker.terminate(); });
  return {
    promise,
    cancel() { cancelled = true; if (worker) worker.terminate(); if (rejectFn) rejectFn(new PackError("cancelled", "Durduruldu.")); },
  };
}

// Paketi sunucuya tek istekle yollar. Başarıda {id}; hatada Error(detail) atar.
export async function uploadPackage(source, map, packed) {
  const q = new URLSearchParams({ name: source.name, sha256: packed.sha256, dem_sha256: packed.demSha256,
                                  size: String(source.size || 0), map });
  if (source.origin === "faceit") q.set("kaynak", "faceit");  // demo_files.source = faceit-tarayici
  const r = await fetch("/api/paket?" + q, { method: "POST", body: packed.pack,
                                              headers: { "Content-Type": "application/octet-stream" } });
  if (!r.ok) {
    let detail = "reddedildi";
    try { detail = (await r.json()).detail || detail; } catch (e) { /* gövde JSON değil */ }
    const err = new Error(detail); err.status = r.status; throw err;
  }
  return r.json();
}
