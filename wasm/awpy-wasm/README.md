# awpy-wasm

Tarayıcıda CS2 demosunu **awpy 3.1.1** çekirdeğiyle ayrıştırıp sunucuya gidecek küçük bir
paket (gzip'li JSON, ~0,3-0,5 MB) üretir. Sunucu tarafı: `squadmind.parse.package.parse_package`
(paketten bugünkü `parse_demo` ile birebir aynı `DemoTables`).

## Derleme

Gerekenler: Rust ≥ 1.88, `rustup target add wasm32-unknown-unknown`, `wasm-bindgen` CLI **0.2.100**
(`cargo install wasm-bindgen-cli --version 0.2.100 --locked`; crate `=0.2.100` sabit).
`protoc` GEREKMEZ: `awpy-proto` protobuf kodunu depoda önceden üretilmiş (`proto.rs`) taşır.

```sh
cd wasm/awpy-wasm
cargo build --release --target wasm32-unknown-unknown
wasm-bindgen target/wasm32-unknown-unknown/release/awpy_wasm.wasm --out-dir pkg-web   --target web
wasm-bindgen target/wasm32-unknown-unknown/release/awpy_wasm.wasm --out-dir pkg-nomod --target no-modules
# Web sitesinin kullandığı kopyalar:
cp pkg-web/awpy_wasm.js pkg-web/awpy_wasm_bg.wasm pkg-web/awpy_wasm.d.ts ../../src/squadmind/web/static/wasm/
cp pkg-nomod/awpy_wasm.js ../../src/squadmind/web/static/wasm/awpy_wasm_nomodules.js
```

`awpy` bağımlılığı git etiketine sabit (`awpy-python-v3.1.1`), `Cargo.toml`'da. Kurulu Python
awpy'si değişirse (`uv.lock`) **etiketi de aynı sürüme çek, WASM'ı yeniden derle ve
`scripts/validate_package.py` ile 19 demoyu yeniden doğrula**; yoksa sunucu ve tarayıcı
farklı ayrıştırıcı sürümleriyle çalışır (pakette `awpy_version` var; Python tarafı kontrol edebilir).

## Kullanım

ES modül (`--target web`, ana yol; module worker içinde de çalışır):

```js
import init, { parse_package } from "/static/wasm/awpy_wasm.js";
await init({ module_or_path: "/static/wasm/awpy_wasm_bg.wasm" });
const json = parse_package(demoBytes);            // Uint8Array (açılmış .dem)
const gz = await new Response(new Blob([json]).stream()
  .pipeThrough(new CompressionStream("gzip"))).arrayBuffer();   // sunucuya gidecek paket
```

Klasik script / klasik worker için `awpy_wasm_nomodules.js`: `wasm_bindgen` global'ini tanımlar;
`await wasm_bindgen({ module_or_path: "/static/wasm/awpy_wasm_bg.wasm" })` (varsayılan yol
`awpy_wasm_nomodules_bg.wasm` olurdu, bu yüzden yol açıkça verilmeli).

Girdi AÇILMIŞ `.dem` baytlarıdır (`.dem.zst` / `.dem.gz` tarayıcıda önce açılır). Node'da deneme:

```sh
node node_pack.js <demo.dem|.dem.zst|.dem.gz> <cikis.json.gz>
```

## Paket biçimi (sürüm 1)

`{format_version, awpy_version, producer, tick_rate, snapshot_seconds, header, reductions,
<tablo>: {columns: [{name, dtype, values}]}, events: {olay: tablo}}`. Tablolar: rounds,
round_economy, bomb, kills, damages, smokes, fires, blinds, grenades, shots, players, stats,
snapshots (2 sn), places. Sütun adları/sırası awpy-python'un `*_to_frame` fonksiyonlarıyla aynı
(`src/frames.rs`, `src/snapshots.rs`); dtype adları i32 u32 i64 u64 f32 bool str.

Boyut için yalnız `parse_demo`'nun okumadığı veri atılır (sonuç aynı):
- `grenades`: her mermi için ilk + son tick satırı (`resolve_projectiles` bu kadarını okur),
- `shots`: yalnız utility atışları,
- `places` (`ticks(["m_szLastPlaceName"])`): yalnız kill / hasar / anlık görüntü tick'leri
  (satırlar tüm tick'lere uygulanan aynı oyuncu-kimliği mantığıyla üretilir).

## Doğrulama

`scripts/validate_package.py` (Node + Python gerekir): demo başına paket üretir, `parse_demo` ve
`parse_package` çıktılarındaki her tabloyu ve ham awpy veri setlerini karşılaştırır, süre/bellek/boyut ölçer.
