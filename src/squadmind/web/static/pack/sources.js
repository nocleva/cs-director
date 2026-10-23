// Demo kaynakları. Paketleme hattı yalnız bu arayüzü bilir:
//   { name: string, size: number, getBytes(): Promise<ArrayBuffer> }
// Kaynaklar: kullanıcının seçtiği dosya (fileSource) ve FACEIT Downloads API'nin imzalı URL'si
// (urlSource, `fetch(url)` -> arrayBuffer); hat değişmez.

export function fileSource(file) {
  return { name: file.name, size: file.size, file, getBytes: () => file.arrayBuffer() };
}

// FACEIT Downloads API kaynağı: imzalı link okuma ANINDA istenir (süreli; sırada beklerken bayatlamasın).
// getUrl(): Promise<string>. Tarayıcı CORS yüzünden indiremezse hata `kind: "fetch"` taşır;
// çağıran o maç için sunucu yedeğine (/api/faceit/sunucu-isle) düşer.
export function urlSource(name, map, getUrl) {
  const src = {
    name, size: 0, map, origin: "faceit", matchId: name.split(".")[0],
    async getBytes() {
      const url = await getUrl();
      let r;
      try { r = await fetch(url); }
      catch (e) { const err = new Error("Tarayıcı FACEIT'ten indiremedi (CORS)."); err.kind = "fetch"; throw err; }
      if (!r.ok) { const err = new Error("FACEIT indirme hatası: HTTP " + r.status); err.kind = "fetch"; throw err; }
      const buf = await r.arrayBuffer();
      src.size = buf.byteLength;
      return buf;
    },
  };
  return src;
}
