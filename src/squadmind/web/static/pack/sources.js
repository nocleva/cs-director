// Demo kaynakları. Paketleme hattı yalnız bu arayüzü bilir:
//   { name: string, size: number, getBytes(): Promise<ArrayBuffer> }
// Şimdilik tek kaynak: kullanıcının seçtiği dosya. İleride FACEIT Downloads API'den gelen
// imzalı URL aynı arayüzle eklenecek (`fetch(url)` -> arrayBuffer); hat değişmez.

export function fileSource(file) {
  return { name: file.name, size: file.size, file, getBytes: () => file.arrayBuffer() };
}
