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

**Asıl amaç: takımı yönetmek.** Her özellik şu sorulardan birine hizmet etmeli: kim nerede iyi, nasıl bir taktik kurabiliriz, nereleri tutmak mantıklı, gücümüz / farkımız nerede, kime destek gerekli, takımın birbiriyle uyumlu olduğu noktalar. Uymayan fikir "sonra" listesine gider.

**Dürüstlük kuralı:** Analizler veriye dayanır. Veri yetersizse ajan bunu söyler, tahmin uydurmaz. Her öneri hangi metrikten geldiğini gösterir (ör. "B site retake başarın %18, 11 denemede").

## 2. Veri Kaynakları

| Kaynak | Ne verir | Erişim | Faz |
|---|---|---|---|
| FACEIT Data API | Oyuncu profili, ELO/level, maç geçmişi, maç başı skor tablosu (K/D, ADR, HS%, MVP, multi-kill), harita, takımlar, demo **resource URL** | Ücretsiz API key (developers.faceit.com) | 1 |
| FACEIT Downloads API | Demo dosyası için imzalı indirme linki | Ayrı başvuru gerekir, onay ~30 gün | 2 |
| Manuel demo yükleme | `.dem` / `.dem.gz` dosyaları (FACEIT maç sayfasından "Watch demo" ile indirilir) | Kullanıcı `data/demos/inbox/` klasörüne atar | 1 |
| ~~CS2 Game State Integration (GSI)~~ | Kendi oyuncunun canlı durumu | Valve'in resmi özelliği | iptal (maç içi yorum yok) |

**Önemli:** Downloads API başvurusunu hemen yap; onay gelene kadar manuel demo yükleme ile ilerle. Sistem iki yolu da aynı pipeline'a sokmalı.

### Canlı analiz hakkında
- GSI resmi ve güvenli, ama **oynarken sadece kendi oyuncunun verisini** verir; rakip pozisyonu vermez.
- Ekran okuma, bellek okuma, minimap'ten rakip tespiti gibi yöntemler FACEIT Anti-Cheat tarafından yakalanabilir ve ban riski taşır. **Bu projede kullanılmayacak.**
- **Maç sırasında / round başına yorum ve canlı sesli destek iptal edildi** (22.10.2026, kullanıcı kararı: kafa karıştırıyor, amaç takımı yönetmek). Kapsamda kalan: maç öncesi brifing (önce yazılı) ve maç sonrası analiz.

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
| 2b | Kendi oyununu doğrula: desen / rol etiketlerini demodan kontrol, öneri takibi (öneri → metrik → sonraki 5-10 maç), zayıf haritalar (Mirage, Inferno) | Etiket doğruluğu sayıyla biliniyor; öneriler önce / sonra raporlanıyor |
| 2c | Takım yönetimi analizi: kim nerede iyi, nereleri tutmalı, güç farkımız, kime destek, oyuncu uyumu; tactician stratları | Takım raporu kadroya pozisyon / görev öneriyor |
| 3a | **Web'de takım demo ekibi:** link ile katılım, FACEIT nickname, demo yükleme, hesabın analizi. Aynı demo bir kez: maç ID + boyut + içerik özeti; demo yükleyenin maçında olmalı | Premade kadro (4 kişi) web'den yükleyip raporunu görüyor ve yorumluyor |
| 3b | İzinli veri paylaşımı (kim kimin verisini görür), takım kurma kriterleri | Paylaşım izinle açılıyor |
| 3c | Rakip scout + maç öncesi yazılı brifing | `scout <match_id>` → brifing |
| 4 | Downloads API ile otomatik demo (watch gereksiz), FACEIT ile giriş | Demolar elle yüklenmeden geliyor |

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
| 2a | ✅ `director report team <nick> [--map]`: T desenleri (rush / execute / split / girilmedi + default işareti), giriş rotaları, CT dizilişleri, plant / retake, ekonomi (`analysis/team_tactics.py`, `analysis/economy.py`) |
| 3a | 🟡 Web arayüzü çalışıyor (yerel + tünel); takım arkadaşlarıyla deneme bekleniyor |
| 2c | ✅ Takım raporunda "Takım yönetimi" (`analysis/team_management.py`, sadece Dust2): çekirdek kadro, kim nerede iyi (CT alanı / T koridoru sıralaması), güç farkı (takım vs rakip), kime destek, trade matrisi, yakın oynama. tactician çekirdek kadro planını yazdı (pozisyon, CT 2 setup, T 3 strat, kurallar); takip `director track` ile |

### Veri ve ortam
- Veri OneDrive DIŞINDA: `C:\Users\furka\cs-director-data` (`.env` → `DIRECTOR_DATA_DIR`). İçinde `director.duckdb`, `cache/`, `demos/{inbox,processed,tmp}`.
- `.env`: `FACEIT_API_KEY`, `DIRECTOR_DATA_DIR`, `DIRECTOR_AUTO_DELETE=0`.
- `uv` yolu: `C:\Users\furka\.local\bin` (PATH'te değilse ekle). Python 3.12 (`.python-version`), awpy 3.x, DuckDB 2.x, Polars 2.x.
- Windows konsolunda Türkçe çıktı için `PYTHONUTF8=1` ve `PYTHONIOENCODING=utf-8`.
- Downloads API başvurusu **beklemeye alındı**; demolar elle indiriliyor (`demo-links` → tarayıcı → `director watch`). `FaceitClient.get_demo_download_url` arayüzü hazır ama `NotImplementedError`; onay gelince sadece o doldurulacak.
- `.env`: `DOWNLOADS_DIR` (`watch` için; varsayılan kullanıcı Downloads klasörü).

### Komutlar
| Komut | Ne yapar |
|---|---|
| `director sync <nick> -n 100` | FACEIT maç geçmişi + skor tablosu (upsert) |
| `director demo-links <nick> [--map dust2] [--outdated]` | İndirilecek demoların maç sayfası linkleri → `reports/demo_links.md` |
| `director ingest-demos [--delete/--keep] [--details]` | Inbox'taki demoları işler, FACEIT ile doğrular, processed/'a taşır |
| `director watch [--dir] [--existing] [--delete/--keep]` | İndirme klasörünü izler; yeni demo (boyutu 3 sn sabit kalınca) inbox'a taşınıp işlenir, tek satır sonuç |
| `director reprocess-demos` | processed/'daki demoları güncel parser ile yeniden işler (parser'a alan eklenince) |
| `director clean-demos` | Verisi DB'de olan demo dosyalarını onayla siler |
| `director report player <nick> [--map]` | Oyuncu raporu → `reports/player_<nick>.md` |
| `director report team <nick> [--map]` | Takım raporu → `reports/team_<nick>.md` |
| `director web [--port 8000]` | Web arayüzü (Faz 3a): davet linkiyle katılım, demo yükleme, oyuncu / takım raporu. Dışarı açmak: `cloudflared tunnel --url http://127.0.0.1:8000` (adres her başlatmada değişir) |
| `director track <plans/*.json>` | Öneri takibi: planın kurallarına bağlı metrikler, plan tarihinden önce / sonra → `reports/track_<plan>.md` |
| `director query "<SELECT>"` | Salt-okunur SQL (ajanlar için) |

### Alınan kararlar ve nedenleri
- **Otomatik silme kapalı:** Parser'a yeni alan eklenince (wallbang, hasar) silinmiş 13 demo yeniden indirilmek zorunda kalındı. Parser oturana kadar demolar `processed/`'da kalır; güncelleme `reprocess-demos` ile.
- **Aynı demo bir kez** (kullanıcının ana iş kuralı, web yüklemesi için de): `precheck_demo` parse'tan önce boyut (20 MB – 2 GB), içerik özeti (sha256, `demo_files` tablosu, şema v4) ve dosya adındaki maçı kontrol eder; kadrodan bulunan maç da işlenmişse red; `uploader` verilirse demodaki oyunculardan biri olmalı. FACEIT ile tutmayan demo da kaydedilir (tekrar denenemez). Başka maçın ID'siyle adlandırılmış demo FACEIT doğrulamasına takılır. `reprocess-demos` kontrolü atlar (`allow_known`).
- **`watch` durum değeri `processed`** (istekte `parsed` geçiyordu): analiz `demo_status = 'processed'` okuyor; ayrı bir değer demoyu analiz dışında bırakırdı. Klasörde zaten duran demolar varsayılan olarak alınmaz (`--existing`). DB bağlantısı yalnızca demo işlenirken açık (watch çalışırken diğer komutlar DB'yi kullanabilsin).
- **Dosya adında maç ID'si yoksa eşleştirme:** harita + kadro örtüşmesi (≥8 oyuncu) + tarih: dosya tarihinden (+1 saat pay) sonra başlayan maç aday değil, en yakın tarihli seçilir.
- **FACEIT ile tutmayan demo** (kill/death ±1, ADR ±5) DB'ye alınmaz, maç `demo_status='mismatch'` olur, dosya inceleme için saklanır. Doğrulama: 17 sağlam maçta 180/180 oyuncu-maç kill sayısı FACEIT ile birebir.
- **AWPer rolü = AWP + Scout payı** (eşik %35): rolü belirleyen silah değil, pozisyon/açı.
- **Wallbang:** `kills.penetrated > 0` kesin (motor bilgisi). `damages.through_wall` tahmini: çoklu çizgi görüş kontrolü, wallbang'lerin ~%73'ünü yakalar, direkt vuruşların ~%3'ünü yanlış işaretler. Raporlarda "tahmini" diye geçer.
- **Lobi karşılaştırması:** "iyi/kötü" yargısı aynı maçlardaki diğer oyunculara göre. Belirgin fark: yüzdelerde ≥5 puan, diğerlerinde ≥%15; ≥20 örnek ve ≥10 olay.
- **Pozisyon değerlendirmesi** (kullanıcının isteği): sadece kill değil; opening, hasar, wallbang / duvar arkası vuruş, bölgede ölüm ve trade edilme birlikte.
- **Hasar = kırpılmış hasar:** `damages.dmg_health` ham (20 canlıya AWP 115 yazar). Analiz `director.parse.damage.cap_damage` ile kalan cana göre kırpar (`dmg` kolonu, `load_demo_data` içinde); 180/180 oyuncu-maç ADR'ı FACEIT ile ≤2,3 farkla tutuyor. Parser v3 utility (HE / molotof) hasarını da kırpar.
- **Kill değeri** (kullanıcının isteği: "low bırakmak da kritik, eco kill'in değeri farklı"): `impact.py`. Kill türü (katilin hasar payı ≥90 tek başına, <40 bitirme), kazanma olasılığı katkısı (WPA: sağ kalan sayısına göre, bu maçların verisinden, WP_PRIOR=10 ile yumuşatılmış, ekonomi hariç), ekonomi bağlamı (eco'ya kill, fakirken kill, rakip ekipmanı), öldürmeyen hasar (low ≤40, asist ≥41, hasar desteği). Sağ kalan sayısı 5 kişilik takım varsayar.
- **Yalnız ölüm** (`death_isolation`): ölüm anında en yakın hayattaki takım arkadaşı >900 birim (3B; arkadaşın ölüm anına en yakın konum kaydı, ≤128 tick). Eşik veriden: ≤700 birimde ölümlerin ~%26'sı, 900 ötesinde ~%7'si trade ediliyor. Son kalan oyuncunun ölümü sayılmaz. Raporda Trade metrikleri + ölüm yerlerinde yalnız / arkadaş yakında ayrımı.
- **T desen kuralları** (`team_tactics.py`; Dust2 koridor / giriş alanı tanımları `maps.py` LANES / HIT_AREAS, 184 plant round'undan): hedef site = plant edilen ya da giriş alanında en çok bulunulan; giriş = ≥3 T aynı anda giriş alanında (yoksa plant anı); rush ≤25 sn, sonra ≥2 koridor = split, tek = execute. Default = 15-20. sn'de ≥3 koridora yayılma. CT dizilişi 15-35. sn'de A-Mid-B sayıları, sadece 5 CT'nin yeri bilinen round'lar (~%60).
- **Takım = oyuncunun her maçtaki takımı.** Nocleva'nın 18 Dust2 maçının 11'inde aynı çekirdek kadro (DeloneCS, _squ1rtle_, nicorett3).
- **Takım yönetimi** (Dust2'de, şimdilik eldeki veriyle; ayrıntılı çalışma herkes web'den kendi demolarını yükleyince): çekirdek = oyuncu + maçların ≥ yarısında birlikte oynayanlar, diğerleri "diğer". "Kim nerede iyi" tek isim seçmez, alan başına sıralar (Net/R = (kill − ölüm)/round); atama tactician'ın işi. Trade eşleştirme: takım arkadaşını öldüren rakibi 5 sn içinde öldürmek (awpy'nin trade sayısıyla tutarlı: 244 / 230).
- **Web (Faz 3a, `src/director/web/`):** FastAPI + Jinja2, kendi bilgisayarında + Cloudflare quick tunnel (kullanıcı kararı: önce risksiz test, sonra VPS). Katılım: `?davet=<WEB_INVITE_CODE>` + FACEIT nickname (FACEIT'ten doğrulanır, son 100 maç çekilir). Yükleme tarayıcıdan 32 MB parçalar (tünel tek istekte ~100 MB sınırı); birleşen dosya bildirilen boyuta eşit değilse işlenmez; `ingest_demo(uploader=steam_id)` → aynı demo bir kez + yükleyen maçta olmalı. İşçi tek iş parçacığı. Rapor HTML'i kaçışlanır (XSS). **DuckDB tek süreç:** `director web` açıkken CLI komutları DB'yi açamaz; ya web'i durdur ya da demoları web'den yükle. `.env`: WEB_INVITE_CODE, WEB_SECRET, WEB_TEAM_OWNER, WEB_MAP, WEB_REQUIRED_DEMOS.
- **Öneri takibi** (`analysis/tracking.py`): plan bir JSON (`plans/`): sahibi, harita, tarih, maddeler (metrik türü + parametre + hedef yönü). Kod oyuncu adı bilmez. Durum ancak plan sonrası ≥3 maçla yazılır; küçük fark (3 puan / %10) "değişim yok". İlk plan: `plans/dust2_cekirdek_2026-10-22.json` (tactician'ın çekirdek kadro planı; ajan çıktıları claude.ai dokümanında: "CS2 Director — Ajan Çıktıları").
- **Ekonomi:** awpy alım tipi; pistol kazanılınca 2. round alımı da "force" sayılıyor. Kayıp sonrası karar o round + sonraki round ile değerlendirilir.
- **Silah karşılaştırması round başına:** her round'a oyuncunun ana silahı atanır (`round_main_weapon`: konum kayıtlarında en çok tutulan birincil silah, yoksa pistol); `weapon_exposure` taraf × ana silah, opsiyonel bölge ve alım tipi filtresiyle. Toplam kill/hasar ile silah kıyası yapılmaz.

### Şimdiye kadarki bulgular (Nocleva, 18 Dust2 maçı)
- Rol: iki tarafta AWPer (güven yüksek; keskin nişancı payı %50 = AWP %30 + Scout %20, lobi %11). İkincil: T'de entry, CT'de Mid'den rotasyon (CT'de 15/18 maç Mid).
- Lobiden iyi: K/D 1.19 (lobi 0.98), ADR 92 (78), T opening kazanma %58 (45 deneme; lobi %45), utility hasarı.
- Lobiden zayıf: flash başına kör edilen takım arkadaşı 0.70 (lobi 0.56).
- CT ölümlerinin %34'ü Mid Doors'ta (47 ölüm), sadece 2'si trade edildi.
- Mid Doors (CT) Scout vs AWP, **round başına**: toplamda eşit görünen kill/opening (30/10) maruziyet farkından. Scout 77 round (41'i force), AWP 51 round. Sadece full buy (AWP 50, Scout 36 round): kill/R 0,60 vs 0,42 · opening/R 0,20 vs 0,08 · hasar/R 65 vs 64 · wallbang kill/R 0,06 vs 0,14 · round kazanma %60 vs %56. Yani AWP kill ve opening'de önde, Scout hasarda eşit ve duvar arkasında önde, ~3.000$ ucuz. (Önceki "Scout hasarı 7.227" rakamı kırpılmamış hasardı; kırpılmış 4.605.)
- Kill değeri (lobiye göre): asist / round 0,23 (0,16), hasar desteği / round 19 (15), low bırakma 0,28 (0,19) → öldürmeyen hasarla takıma çok iş hazırlıyor; bunun büyük kısmı Scout'tan (43 low bırakma, 29'u takımca bitirildi, 46 asist; AWP'de 12 / 8 / 8). Zayıf: fakirken kill / round 0,47 (0,67; 96 round). Eco'daki rakibe kill payı %11 (lobi %6).
- Yalnız ölüm %41 (96/232; lobi %36, belirgin zayıf). Mid Doors CT 47 ölüm: yalnız 26 (trade 2), takım arkadaşı yakındayken (medyan ~490 birim) 19 (trade **0**). Yani Mid Doors'ta sorun sadece yalnızlık değil: karşıdan vuran sniper'ı trade etmek için arkadaşın aynı açıya peek atması gerekiyor; "yanına trade oyuncusu" tek başına yetmez.
- director ajanının ilk analizi: Mid Doors ölümleri iki desen: (a) ilk 20 sn'de karşı sniper düellosu (~20 ölüm, 17'si Top of Mid AWP/Scout'tan), (b) 30-45. sn'de Scout'la kapıda tek başına yakalanma (~27 ölüm). Ajanın "yakında takım arkadaşı" (800 birim) hesabı ad hoc, kodda değil.
- Takım (Dust2, 188 T / 210 CT round): A rush %74 (31 round, plant sonrası %88) ve A split %100 (10, az örnek) vs A execute %53 (53; long'dan %48, short'tan %44). B execute %77 (22). Plant sonrası kazanma T %80+; rakip plant edince retake A %20 (50), B %32 (47). CT 2-2-1 %72 (40), 3-1-1 %59 (27). Kayıptan sonra çoğunlukla force (100 round, %43; sonraki round %52); eco nadir (14; o round %7, sonraki %79). Pistol kazanılınca 2. round 20/20.
- FACEIT geneli: Dust2 %71 kazanma; Mirage %31 (13 maç), Inferno K/D 0.80 (9 maç) zayıf.

### Açık işler (öncelik sırasıyla; bkz. bölüm 8)
0. **Yol:** 2b kendi oyununu doğrula → 2c takım yönetimi analizi → 3a web'de takım demo ekibi. Asıl amaç takımı yönetmek; maç içi yorum iptal.
1. `inbox/` içindeki `1-806933ec-...dem.zst` awpy ile okunamıyor (protobuf UTF-8 hatası); kullanıcı silecek ya da başka bir parser (demoparser2) denenecek.
2. Faz 2b: desen / rol etiketlerini demodan doğrulama + öneri takibi (önce / sonra); Mirage ve Inferno demoları + bölge tanımları. Faz 3c: `director scout <match_id>` (rakip scout). Takım raporu fonksiyonları (`team_rounds`, `t_round_patterns`, `ct_setups`) rakip için de kullanılabilir: rakip oyuncunun steam_id'si verilince onun takımını analiz eder.
3. T desen sınıflandırmasını birkaç round'da demodan izleyerek doğrula (özellikle "girilmedi" ve default eşiği).
4. Diğer haritalar için `analysis/maps.py` alan tanımı (şimdilik sadece Dust2).

### Bilinen teknik tuzaklar
- DuckDB 2.x: `.pl()` yok → `pl.from_arrow(con.cursor().sql(sql, params=...).to_arrow_table())`. Açık sonuç varken aynı bağlantıda yeni sorgu hata verir (ayrı `cursor()` kullan). `ON CONFLICT DO UPDATE` içinde `current_timestamp` yerine `now()`.
- Polars DataFrame'i DuckDB'ye yazmak için `pyarrow` gerekir.
- DuckDB 2.x relation'da `fetchone()` yok → `.fetchall()[0]`. `matches.map` değerleri `de_` önekli (`de_dust2`); CLI `--map dust2` kabul eder.
- awpy: `damages.dmg_health` kırpılmamış ham hasar (bkz. kararlar). `kills`/`smokes` ısınma round'unu da içerir (`round_windows` ile elenir); smoke atanı çoğunlukla boş gelir (`resolve_projectiles` ile `shots`'tan eşlenir); bölge adı ham alan `m_szLastPlaceName`.
