# CS2 FACEIT Gaming Director — Proje Rehberi

> Bu dosya Claude Code'un her oturumda okuduğu proje hafızasıdır. Proje kök dizinine `CLAUDE.md` adıyla koy.

## 1. Vizyon

FACEIT CS2 maçlarının geçmiş verilerini (istatistik + demo dosyaları) toplayıp:
- takımın **başarılı ve eksik** taktiklerini tespit eden,
- oyuncu istatistiklerini **yorumlayan** (sadece sayı değil, "neden" ve "ne yapmalı"),
- oyuncuları **rollerine** (entry, AWP, support, lurker, anchor, rotator) göre sınıflandıran,
- bu rollere, silah tercihlerine ve seviyelerine göre **taktik yazan**,
- **rakip takımı** maç öncesi scout eden

bir "Gaming Director" ajanı kurmak.

**Dürüstlük kuralı:** Analizler veriye dayanır. Veri yetersizse ajan bunu söyler, tahmin uydurmaz. Her öneri hangi metrikten geldiğini gösterir (ör. "B site retake başarın %18, 11 denemede").

## 2. Veri Kaynakları

| Kaynak | Ne verir | Erişim | Faz |
|---|---|---|---|
| FACEIT Data API | Oyuncu profili, ELO/level, maç geçmişi, maç başı skor tablosu (K/D, ADR, HS%, MVP, multi-kill), harita, takımlar, demo **resource URL** | Ücretsiz API key (developers.faceit.com) | 1 |
| FACEIT Downloads API | Demo dosyası için imzalı indirme linki | Ayrı başvuru gerekir, onay ~30 gün | 2 |
| Manuel demo yükleme | `.dem` / `.dem.gz` dosyaları (FACEIT maç sayfasından "Watch demo" ile indirilir) | Kullanıcı `data/demos/inbox/` klasörüne atar | 1 |
| CS2 Game State Integration (GSI) | Kendi oyuncunun canlı durumu (can, para, silah, round fazı, skor) | Valve'in resmi özelliği, config dosyası ile | 3 |

**Önemli:** Downloads API başvurusunu hemen yap; onay gelene kadar manuel demo yükleme ile ilerle. Sistem iki yolu da aynı pipeline'a sokmalı.

### Canlı analiz hakkında
- GSI resmi ve güvenli, ama **oynarken sadece kendi oyuncunun verisini** verir; rakip pozisyonu vermez.
- Ekran okuma, bellek okuma, minimap'ten rakip tespiti gibi yöntemler FACEIT Anti-Cheat tarafından yakalanabilir ve ban riski taşır. **Bu projede kullanılmayacak.**
- Faz 3'te güvenli canlı özellik: GSI ile ekonomi takibi ("bu round force buy yerine eco önerilir"), round arası hatırlatmalar, maç sonrası anında rapor.

## 3. Teknoloji Yığını

| Katman | Seçim | Neden |
|---|---|---|
| Dil | Python 3.11+ | Demo parser ekosistemi Python'da |
| Paket yönetimi | `uv` | Hızlı, lock dosyalı |
| Demo parser | `awpy` (ana), `demoparser2` (ham tick sorguları için yedek) | awpy hazır KAST/ADR/opening/trade + Polars DataFrame verir |
| FACEIT API | `httpx` ile ince istemci (veya `faceit` paketi) | Rate limit ve cache kontrolü bizde olsun |
| Veritabanı | DuckDB (tek dosya: `data/director.duckdb`) | Analitik sorgular, Polars ile doğal uyum, sunucu gerektirmez |
| Ajan | Faz 1: Claude Code subagent'ları; Faz 2: Claude API (tool use) | Önce maliyetsiz prototip, sonra bağımsız uygulama |
| Arayüz | Faz 1: CLI (`typer`) + Markdown raporlar; Faz 2: Streamlit | Önce doğru analiz, sonra güzel ekran |
| Görselleştirme | `awpy[plot]` / matplotlib (heatmap, ölüm noktaları) | Harita üstü görsel |
| Test | `pytest` + örnek demo fixture | Metriklerin doğruluğu kritik |

## 4. Dizin Yapısı

```
cs2-director/
├── CLAUDE.md
├── .env                      # FACEIT_API_KEY=... (git'e girmez)
├── pyproject.toml
├── .claude/
│   └── agents/
│       ├── director.md       # Ana gaming director ajanı
│       ├── scout.md          # Rakip analizi ajanı
│       └── tactician.md      # Taktik yazan ajan
├── src/director/
│   ├── faceit/client.py      # Data API istemcisi (rate limit + disk cache)
│   ├── ingest/
│   │   ├── matches.py        # Maç geçmişi + istatistik çekme
│   │   └── demos.py          # Demo indirme / inbox'tan alma / .gz açma
│   ├── parse/demo_parser.py  # awpy → normalize tablolar
│   ├── db/schema.sql         # DuckDB şeması
│   ├── analysis/
│   │   ├── player_metrics.py # Oyuncu metrikleri
│   │   ├── roles.py          # Rol sınıflandırma
│   │   ├── team_tactics.py   # Takım taktik desenleri
│   │   ├── economy.py        # Ekonomi analizi
│   │   └── opponent.py       # Rakip profili
│   ├── reports/              # Markdown rapor üreticileri
│   └── cli.py                # typer komutları
├── data/
│   ├── demos/inbox/          # Manuel demolar buraya
│   ├── demos/processed/
│   ├── cache/                # API yanıt cache'i
│   └── director.duckdb
├── reports/                  # Üretilen raporlar
└── tests/
```

## 5. Veri Modeli (DuckDB)

| Tablo | Anahtar alanlar |
|---|---|
| `players` | steam_id, faceit_id, nickname, elo, level |
| `matches` | match_id, map, date, team_a, team_b, score, demo_url, demo_status |
| `match_player_stats` | match_id, player_id, kills, deaths, adr, hs_pct, kast, rating |
| `rounds` | match_id, round_no, winner_side, reason, ct/t_buy_type, ct/t_equipment, bomb_site, plant_tick, ct_faction |
| `kills` | match_id, round_no, tick, time_in_round, attacker/victim (+_name, _side, _place, _x/_y/_z), weapon, headshot, is_opening, is_trade_kill, is_traded |
| `utility` | match_id, round_no, time_in_round, thrower (+_name, _side), type (smoke/flash/he/molly), x/y/z, land_place, flashed_enemies, flashed_teammates, blind_duration, damage |
| `positions_snapshot` | match_id, round_no, tick, time_in_round, player, side, x, y, z, place_name, health, money, equipment_value, active_weapon (her 2 saniyede bir, sadece canlı round + hayattaki oyuncu) |
| `demo_player_stats` | match_id, steam_id, kills, deaths, adr, kast, opening_kills/deaths, traded_deaths, clutch, utility (awpy) |

Şema v2 (`schema_version` tablosu). Isınma ve bıçak roundları elenir; demo verisi her işlemede FACEIT skor tablosuyla karşılaştırılır (kill/death ±1, ADR ±5).

`place_name` (demodaki `m_szLastPlaceName` alanı: "BombsiteA", "LongA" vb.; utility için en yakın bilinen konumdan türetilir) taktik analizinin temelidir: koordinatı insanın anlayacağı bölgeye çevirir.

## 6. Metrikler ve Rol Sınıflandırma

### Oyuncu metrikleri
- **Opening duel:** deneme oranı, kazanma oranı (T ve CT ayrı)
- **Trade:** trade edilen ölüm %, trade yapma %
- **KAST, ADR, HS%, multi-kill roundlar, clutch (1vX) kazanma**
- **Utility:** flash assist, düşman kör etme süresi, utility hasarı, round başı atılan utility
- **Silah profili:** silah bazında kill payı, AWP kill payı, pistol round başarısı
- **Ekonomi:** eco/force/full buy roundlarda performans, ölürken kaybedilen ekipman değeri
- **Zamanlama:** round içinde ilk temas zamanı, ölüm zamanı dağılımı

### Rol sınıflandırma (kural tabanlı başla, sonra kalibre et)
| Rol | Sinyal |
|---|---|
| Entry | Opening duel deneme oranı takımda en yüksek, T tarafında siteye ilk giren |
| AWPer | Kill'lerin %35+'ı AWP ile |
| Support | Flash assist ve utility hasarı yüksek, entry'den 2-5 sn sonra siteye giriş |
| Lurker | T tarafında takımın çoğunluğundan uzak bölgede, geç ilk temas |
| Anchor | CT'de aynı sitede sabit kalma oranı yüksek |
| Rotator | CT'de round içinde bölge değiştirme oranı yüksek |

Her rol bir **güven skoru** ile verilir; az maçta düşük güven gösterilir.

### Takım taktik analizi
- T tarafı: round başına ilk 20 saniyedeki pozisyon dağılımından **default / rush / split / execute** desenlerini kümele (ör. KMeans veya basit kural), her desenin kazanma oranı.
- Site bazında plant oranı ve plant sonrası kazanma.
- CT tarafı: setup dağılımı (2-1-2, 3-1-1 vb.), retake başarısı.
- Ekonomi kararları: force buy sonrası kazanma, eco round kazanma.

## 7. Ajanlar

### `director` (ana ajan)
- DuckDB'ye **sadece okuma** amaçlı SQL ve analiz fonksiyonları üzerinden erişir.
- Çıktı formatı: Özet → Güçlü yönler → Zayıf yönler → 3-5 somut öneri (her biri metrikle desteklenmiş) → Sonraki maç için odak.
- Yetersiz veri varsa söyler (minimum: oyuncu analizi için 5 maç, taktik deseni için 20 round).

### `scout` (rakip analizi)
- Rakip oyuncuların son 20 maçını Data API'den çeker: harita havuzu, harita kazanma oranları, oyuncu rolleri (istatistikten tahmini), silah tercihleri.
- Rakip demosu varsa taktik desenlerini de çıkarır.
- Çıktı: ban/pick önerisi, dikkat edilecek oyuncular, beklenen oyun tarzı.

### `tactician` (taktik yazan)
- Girdi: harita, kendi takımın rolleri + silah tercihleri + güçlü bölgeleri, opsiyonel rakip profili.
- Çıktı: T tarafı 3 strat (default, hızlı execute, split) + CT tarafı 2 setup; her stratta oyuncu adıyla pozisyon, utility görevi, zamanlama.

## 8. Yol Haritası

| Faz | Hedef | Bitti sayılır |
|---|---|---|
| 0 | Proje iskeleti, `.env`, FACEIT istemcisi | `director fetch-player <nick>` çalışıyor |
| 1a | Maç geçmişi + istatistikleri DuckDB'ye | Son 30 maç DB'de |
| 1b | Manuel demo inbox → parse → DB | Bir demo tüm tablolara yazılıyor |
| 1c | Oyuncu metrikleri + rol sınıflandırma | `director report player <nick>` Markdown rapor |
| 1d | Claude Code subagent'ları | `director` ajanı rapordan yorum ve öneri üretiyor |
| 2a | Takım taktik desenleri + ekonomi | `director report team` |
| 2b | Rakip scout | `director scout <match_id>` |
| 2c | Tactician + harita heatmap görselleri | Strat dokümanı oyuncu isimleriyle |
| 2d | Downloads API entegrasyonu + Streamlit arayüz | Demolar otomatik iniyor |
| 3 | GSI ile güvenli canlı ekonomi asistanı | Round arası öneri gösteriliyor |

## 9. Kod Kuralları
- Tüm kod tam, çalışır halde ve **satır içi yorumlu** (Türkçe yorum).
- Fonksiyonlar tip ipuçlu; metrik fonksiyonlarının her birinin testi var.
- API anahtarları sadece `.env`'den okunur, asla koda yazılmaz.
- FACEIT API çağrıları disk cache'li ve rate-limit'e saygılı (429 gelirse exponential backoff).
- Her metrik hesaplaması bilinen bir demo üzerinde manuel doğrulanır (ör. scoreboard'daki ADR ile karşılaştır).
- Terimler: kod içinde İngilizce isimler, raporlarda Türkçe metin (oyun terimleri İngilizce kalabilir: entry, trade, retake, eco).

## 10. Mevcut Durum (22.10.2026 itibarıyla)

> Yeni oturum buradan devam eder. İş ilerledikçe bu bölümü güncelle.

### Biten fazlar
| Faz | Durum |
|---|---|
| 0, 1a | ✅ FACEIT istemcisi, `sync` / `summary`. Kullanıcı: **Nocleva** (büyük N; FACEIT nickname büyük/küçük harfe duyarlı), level 9, 99 maç DB'de |
| 1b | ✅ Demo inbox → awpy parse → DB. Parser v3 (wallbang + hasar; utility hasarı kırpılmış). 18 Dust2 maçı işlendi, hepsi FACEIT ile doğrulandı |
| 1c | ✅ Oyuncu metrikleri, takım içi rol puanları, silah maruziyeti (round başına), kill / hasar değeri (`analysis/impact.py`), `director report player <nick> [--map dust2]` |
| 1d | ✅ `director` ajanı 22.10'da ilk gerçek analizi üretti (Nocleva, Dust2). `scout` ve `tactician` henüz denenmedi |
| 2a+ | ⏳ Başlamadı |

### Veri ve ortam
- Veri OneDrive DIŞINDA: `C:\Users\furka\cs-director-data` (`.env` → `DIRECTOR_DATA_DIR`). İçinde `director.duckdb`, `cache/`, `demos/{inbox,processed,tmp}`.
- `.env`: `FACEIT_API_KEY`, `DIRECTOR_DATA_DIR`, `DIRECTOR_AUTO_DELETE=0`.
- `uv` yolu: `C:\Users\furka\.local\bin` (PATH'te değilse ekle). Python 3.12 (`.python-version`), awpy 3.x, DuckDB 2.x, Polars 2.x.
- Windows konsolunda Türkçe çıktı için `PYTHONUTF8=1` ve `PYTHONIOENCODING=utf-8`.
- Downloads API başvurusu: yapıldı mı bilinmiyor; demolar şimdilik manuel indiriliyor (`demo-links`).

### Komutlar
| Komut | Ne yapar |
|---|---|
| `director sync <nick> -n 100` | FACEIT maç geçmişi + skor tablosu (upsert) |
| `director demo-links <nick> [--map dust2] [--outdated]` | İndirilecek demoların maç sayfası linkleri → `reports/demo_links.md` |
| `director ingest-demos [--delete/--keep] [--details]` | Inbox'taki demoları işler, FACEIT ile doğrular, processed/'a taşır |
| `director reprocess-demos` | processed/'daki demoları güncel parser ile yeniden işler (parser'a alan eklenince) |
| `director clean-demos` | Verisi DB'de olan demo dosyalarını onayla siler |
| `director report player <nick> [--map]` | Oyuncu raporu → `reports/player_<nick>.md` |
| `director query "<SELECT>"` | Salt-okunur SQL (ajanlar için) |

### Alınan kararlar ve nedenleri
- **Otomatik silme kapalı:** Parser'a yeni alan eklenince (wallbang, hasar) silinmiş 13 demo yeniden indirilmek zorunda kalındı. Parser oturana kadar demolar `processed/`'da kalır; güncelleme `reprocess-demos` ile.
- **FACEIT ile tutmayan demo** (kill/death ±1, ADR ±5) DB'ye alınmaz, maç `demo_status='mismatch'` olur, dosya inceleme için saklanır. Doğrulama: 17 sağlam maçta 180/180 oyuncu-maç kill sayısı FACEIT ile birebir.
- **AWPer rolü = AWP + Scout payı** (eşik %35): rolü belirleyen silah değil, pozisyon/açı.
- **Wallbang:** `kills.penetrated > 0` kesin (motor bilgisi). `damages.through_wall` tahmini: çoklu çizgi görüş kontrolü, wallbang'lerin ~%73'ünü yakalar, direkt vuruşların ~%3'ünü yanlış işaretler. Raporlarda "tahmini" diye geçer.
- **Lobi karşılaştırması:** "iyi/kötü" yargısı aynı maçlardaki diğer oyunculara göre. Belirgin fark: yüzdelerde ≥5 puan, diğerlerinde ≥%15; ≥20 örnek ve ≥10 olay.
- **Pozisyon değerlendirmesi** (kullanıcının isteği): sadece kill değil; opening, hasar, wallbang / duvar arkası vuruş, bölgede ölüm ve trade edilme birlikte.
- **Hasar = kırpılmış hasar:** `damages.dmg_health` ham (20 canlıya AWP 115 yazar). Analiz `director.parse.damage.cap_damage` ile kalan cana göre kırpar (`dmg` kolonu, `load_demo_data` içinde); 180/180 oyuncu-maç ADR'ı FACEIT ile ≤2,3 farkla tutuyor. Parser v3 utility (HE / molotof) hasarını da kırpar.
- **Kill değeri** (kullanıcının isteği: "low bırakmak da kritik, eco kill'in değeri farklı"): `impact.py`. Kill türü (katilin hasar payı ≥90 tek başına, <40 bitirme), kazanma olasılığı katkısı (WPA: sağ kalan sayısına göre, bu maçların verisinden, WP_PRIOR=10 ile yumuşatılmış, ekonomi hariç), ekonomi bağlamı (eco'ya kill, fakirken kill, rakip ekipmanı), öldürmeyen hasar (low ≤40, asist ≥41, hasar desteği). Sağ kalan sayısı 5 kişilik takım varsayar.
- **Silah karşılaştırması round başına:** her round'a oyuncunun ana silahı atanır (`round_main_weapon`: konum kayıtlarında en çok tutulan birincil silah, yoksa pistol); `weapon_exposure` taraf × ana silah, opsiyonel bölge ve alım tipi filtresiyle. Toplam kill/hasar ile silah kıyası yapılmaz.

### Şimdiye kadarki bulgular (Nocleva, 18 Dust2 maçı)
- Rol: iki tarafta AWPer (güven yüksek; keskin nişancı payı %50 = AWP %30 + Scout %20, lobi %11). İkincil: T'de entry, CT'de Mid'den rotasyon (CT'de 15/18 maç Mid).
- Lobiden iyi: K/D 1.19 (lobi 0.98), ADR 92 (78), T opening kazanma %58 (45 deneme; lobi %45), utility hasarı.
- Lobiden zayıf: flash başına kör edilen takım arkadaşı 0.70 (lobi 0.56).
- CT ölümlerinin %34'ü Mid Doors'ta (47 ölüm), sadece 2'si trade edildi.
- Mid Doors (CT) Scout vs AWP, **round başına**: toplamda eşit görünen kill/opening (30/10) maruziyet farkından. Scout 77 round (41'i force), AWP 51 round. Sadece full buy (AWP 50, Scout 36 round): kill/R 0,60 vs 0,42 · opening/R 0,20 vs 0,08 · hasar/R 65 vs 64 · wallbang kill/R 0,06 vs 0,14 · round kazanma %60 vs %56. Yani AWP kill ve opening'de önde, Scout hasarda eşit ve duvar arkasında önde, ~3.000$ ucuz. (Önceki "Scout hasarı 7.227" rakamı kırpılmamış hasardı; kırpılmış 4.605.)
- Kill değeri (lobiye göre): asist / round 0,23 (0,16), hasar desteği / round 19 (15), low bırakma 0,28 (0,19) → öldürmeyen hasarla takıma çok iş hazırlıyor; bunun büyük kısmı Scout'tan (43 low bırakma, 29'u takımca bitirildi, 46 asist; AWP'de 12 / 8 / 8). Zayıf: fakirken kill / round 0,47 (0,67; 96 round). Eco'daki rakibe kill payı %11 (lobi %6).
- director ajanının ilk analizi: Mid Doors ölümleri iki desen: (a) ilk 20 sn'de karşı sniper düellosu (~20 ölüm, 17'si Top of Mid AWP/Scout'tan), (b) 30-45. sn'de Scout'la kapıda tek başına yakalanma (~27 ölüm). Ajanın "yakında takım arkadaşı" (800 birim) hesabı ad hoc, kodda değil.
- FACEIT geneli: Dust2 %71 kazanma; Mirage %31 (13 maç), Inferno K/D 0.80 (9 maç) zayıf.

### Açık işler (öncelik sırasıyla)
1. Ölüm anında en yakın takım arkadaşı mesafesi metriği (ajanın ad hoc hesabını koda taşı).
2. `inbox/` içindeki `1-806933ec-...dem.zst` awpy ile okunamıyor (protobuf UTF-8 hatası); kullanıcı silecek ya da başka bir parser (demoparser2) denenecek.
3. Faz 2a: T tarafı ilk 20 sn pozisyonlarından taktik desenleri (default / rush / split / execute) + ekonomi kararları → `director report team`.
4. Diğer haritalar için `analysis/maps.py` alan tanımı (şimdilik sadece Dust2).

### Bilinen teknik tuzaklar
- DuckDB 2.x: `.pl()` yok → `pl.from_arrow(con.cursor().sql(sql, params=...).to_arrow_table())`. Açık sonuç varken aynı bağlantıda yeni sorgu hata verir (ayrı `cursor()` kullan). `ON CONFLICT DO UPDATE` içinde `current_timestamp` yerine `now()`.
- Polars DataFrame'i DuckDB'ye yazmak için `pyarrow` gerekir.
- DuckDB 2.x relation'da `fetchone()` yok → `.fetchall()[0]`. `matches.map` değerleri `de_` önekli (`de_dust2`); CLI `--map dust2` kabul eder.
- awpy: `damages.dmg_health` kırpılmamış ham hasar (bkz. kararlar). `kills`/`smokes` ısınma round'unu da içerir (`round_windows` ile elenir); smoke atanı çoğunlukla boş gelir (`resolve_projectiles` ile `shots`'tan eşlenir); bölge adı ham alan `m_szLastPlaceName`.
