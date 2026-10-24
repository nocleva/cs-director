# SquadMind — Proje Rehberi

> Bu dosya Claude Code'un her oturumda okuduğu proje hafızasıdır. Proje kök dizinine `CLAUDE.md` adıyla koy.

## 1. Vizyon (23.10.2026'da yenilendi)

SquadMind bir **düello ve taktik arenası**: CS2 oyuncuları FACEIT ile katılır, demo izni verir; sistem
maçlarından pozisyona göre gücünü hesaplar. Oyuncu kendini kuş bakışı haritada sürükle-bırak yerleştirir,
karşısına **bir FACEIT seviyesinde ortalama rakip** ya da başka bir oyuncu koyar; ikisi birbirini görüyorsa
düelloyu kimin kazanacağı hesaplanır ve **kazananın jetonu yeşile döner**. Böylece arkadaşlar peek'lerde
kim daha iyi diye pozisyonları kapıştırır. Sonraki adım: ekip kurup taktik çizmek (ör. A rush) ve takımları
karşılaştırıp kazananı göstermek.

- **Herkes kıyaslanabilir:** FACEIT ile katılan her oyuncu. Katılmamış oyuncu için FACEIT geçmişi erişilebiliyorsa
  ortalama bir puanla ilerlenir (kişisel profil gizli; onay için demo izni şart; onaysız profil rakip koyamaz,
  sadece kendini konumlar).
- **ELO her zaman FACEIT'ten canlı alınır;** DB'de / sunucuda saklı ELO geçersizdir.
- **Görüş kuralı:** iki oyuncu birbirini görmüyorsa düello hesaplanmaz (`analysis/geometry.py`).
- **Arka plan analizi sürer:** coach (director), scout ve tactician ajanları oyuncuları analiz eder;
  puanlama sistemini (istatistik ekibi + profesör) hepsi destekler.
- **Dürüstlük kuralı:** analizler veriye dayanır. Veri yetersizse söylenir, tahmin uydurulmaz. Her olasılık
  aralığı ve güven etiketiyle gösterilir; sınamayı geçmeyen model "güvenilir" diye sunulmaz.
- **Gizli hedef ve kullanıcı tercihleri:** Claude'un hafızasında (repo'ya yazılmaz).

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
| Veritabanı | DuckDB (tek dosya: `data/squadmind.duckdb`) | Analitik sorgular, Polars ile doğal uyum, sunucu gerektirmez |
| Ajan | Faz 1: Claude Code subagent'ları; Faz 2: Claude API (tool use) | Önce maliyetsiz prototip, sonra bağımsız uygulama |
| Arayüz | Faz 1: CLI (`typer`) + Markdown raporlar; Faz 2: Streamlit | Önce doğru analiz, sonra güzel ekran |
| Görselleştirme | `awpy[plot]` / matplotlib (heatmap, ölüm noktaları) | Harita üstü görsel |
| Test | `pytest` + örnek demo fixture | Metriklerin doğruluğu kritik |

## 4. Dizin Yapısı

```
cs-director/                  # proje klasörü (paket: src/squadmind)
├── CLAUDE.md
├── .env                      # FACEIT_API_KEY=... (git'e girmez)
├── pyproject.toml
├── .claude/
│   └── agents/
│       ├── director.md       # Ana gaming director ajanı
│       ├── scout.md          # Rakip analizi ajanı
│       └── tactician.md      # Taktik yazan ajan
├── src/squadmind/
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
│   └── squadmind.duckdb
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

## 7. Ajanlar (`.claude/agents/`)

| Ajan | Rol |
|---|---|
| `professor` | Matematik profesörü; istatistik ekibinin başı. Raporları değerlendirir, ayrılıklarda karar verir, puanlama formülü değişikliklerini onaylar |
| `statistician-rating` | Oyuncu puanı: canlı FACEIT ELO + küçültmeli stat puanı, ortalama rakip, aralıklar |
| `statistician-duel` | Düello ve pozisyon: görüş, mesafe, peek / tutma, silah, pozisyona özgü güç |
| `statistician-validation` | Sınama, kalibrasyon, sızıntı ve veri kalitesi denetimi (bağımsız yeniden ölçüm) |
| `kawanagari` | Grafik tasarımcı Yushito Kawanagari (eski Japon oyun yapımcısı, modern tarz): görsel dil, sayfalar, animasyon |
| `director` | Coach: oyuncu / takım yorumu, "neden" ve "ne yapmalı" |
| `scout` | Rakip analizi |
| `tactician` | Taktik / strat yazımı (arenadaki takım taktikleri için) |

İstatistikçiler bulgularını profesöre raporlar; çelişkide kendi başlarına karar vermez, profesöre danışır.
Yeni ajan tanımı ancak yeni oturumda adıyla çağrılabilir; aynı oturumda tanımın içeriği genel ajana verilir.

## 8. Yol Haritası (23.10.2026'da yenilendi)

| Adım | Hedef | Bitti sayılır |
|---|---|---|
| A1 | **Düello arenası (Dust2):** kendini yerleştir, karşına FACEIT seviyesinde ortalama rakip ya da oyuncu koy, görüş + olasılık, kazanan yeşil | Giriş yapan oyuncu canlı ELO'suyla düello sonucu görüyor |
| A2 | Yeni görsel dil (Kawanagari): harita, jetonlar, animasyon | Arena ve giriş sayfaları yeni tarzda |
| A3 | Pozisyona özgü güç (istatistik ekibi + profesör onayı) | Sınamada tabanı geçen pozisyon etkisi ya da "veri yetmiyor" kararı |
| A4 | Takım taktiği simülasyonu (ör. A rush, 5'e 5): rotalar, sıralı düellolar, kazanan takım | Kurulan ekip bir taktiği oynatıp sonucu görüyor |
| A5 | Katılmamış oyuncular: FACEIT geçmişinden ortalama puan (gizli profil), onay için demo izni | Arkadaş arama ve kıyaslama |
| A6 | FACEIT ile giriş (çalışıyor ama dalgalı; kilit / secret notları bölüm 10), Downloads API ile otomatik demo | Demolar elle yüklenmeden geliyor |
| A7 | Diğer haritalar (Mirage, Inferno ...): bölge ve peek tanımları | Harita seçimi açılıyor |

Eski ekranlar (taktik tahtası `/taktik`, desen doğrulama, eski oluşturucu, Markdown rapor sayfaları) 23.10.2026'da siteden
kaldırıldı; analiz modülleri ve CLI raporları arka planda sürüyor.

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
| 1c | ✅ Oyuncu metrikleri, takım içi rol puanları, silah maruziyeti (round başına), kill / hasar değeri (`analysis/impact.py`), `squadmind report player <nick> [--map dust2]` |
| 1d | ✅ `director` ajanı 22.10'da ilk gerçek analizi üretti (Nocleva, Dust2). `scout` ve `tactician` henüz denenmedi |
| 2a | ✅ `squadmind report team <nick> [--map]`: T desenleri (rush / execute / split / girilmedi + default işareti), giriş rotaları, CT dizilişleri, plant / retake, ekonomi (`analysis/team_tactics.py`, `analysis/economy.py`) |
| 3a | 🟡 Web arayüzü çalışıyor (yerel + tünel); takım arkadaşlarıyla deneme bekleniyor |
| 2c | ✅ Takım raporunda "Takım yönetimi" (`analysis/team_management.py`, sadece Dust2): çekirdek kadro, kim nerede iyi (CT alanı / T koridoru sıralaması), güç farkı (takım vs rakip), kime destek, trade matrisi, yakın oynama. tactician çekirdek kadro planını yazdı (pozisyon, CT 2 setup, T 3 strat, kurallar); takip `squadmind track` ile |

### Sunucu (23.10.2026'dan itibaren canlı site burada)
- OVH VPS-1 (2 vCPU, 4 GB, 40 GB NVMe, 12 ay, OVH günlük yedeği açık), `vps-ab5e968e.vps.ovh.net` / 213.32.17.246, Ubuntu 26.04.
- Giriş yalnız SSH anahtarıyla: `ssh -i ~/.ssh/squadmind_vps ubuntu@213.32.17.246` (sudo şifresiz) ya da `squadmind@…` (uygulama kullanıcısı; yalnız squadmind-web için systemctl izni). Şifreyle giriş kapalı; `ubuntu` şifresi (yalnız OVH KVM acil durum için) yerelde `~/.ssh/squadmind_vps_ubuntu_sifre.txt`. Güvenlik duvarı: yalnız SSH; site Cloudflare tüneliyle gelir.
- Kod `/opt/squadmind` (+`.env`), veri `/var/lib/squadmind` (DuckDB + .wal, cache, logs, backups). Servisler: `squadmind-web`, `squadmind-tunnel`, `squadmind-backup.timer` (her gece 04:17 UTC, `.duckdb` + `.wal` tar.gz, son 14).
- Güncelleme: commit → `scripts/server/deploy.sh 213.32.17.246`. Kurulum / taşıma betikleri ve geri dönüş adımları `scripts/server/README.md`.
- Bu bilgisayardaki yerel sunucu ve Görev Zamanlayıcı `SquadMind` görevi KAPALI (aynı tünel iki yerde çalışmasın). Yerelde DB'ye CLI ile çalışmak için sunucudan kopya al; yerel `squadmind-data/squadmind.duckdb` artık eski.
- DuckDB taşırken/yedeklerken `.wal` dosyası da alınmalı (ilk taşımada unutuldu, son yazımlar eksik kaldı; düzeltildi).

### Veri ve ortam
- Veri OneDrive DIŞINDA: `C:\Users\furka\squadmind-data` (`.env` → `SQUADMIND_DATA_DIR`). İçinde `squadmind.duckdb`, `cache/`, `demos/{inbox,processed,tmp}`.
- `.env`: `FACEIT_API_KEY`, `SQUADMIND_DATA_DIR`, `SQUADMIND_AUTO_DELETE=0`.
- `uv` yolu: `C:\Users\furka\.local\bin` (PATH'te değilse ekle). Python 3.12 (`.python-version`), awpy 3.x, DuckDB 2.x, Polars 2.x.
- Windows konsolunda Türkçe çıktı için `PYTHONUTF8=1` ve `PYTHONIOENCODING=utf-8`.
- Downloads API başvurusu **beklemeye alındı**; demolar elle indiriliyor (`demo-links` → tarayıcı → `squadmind watch`). `FaceitClient.get_demo_download_url` arayüzü hazır ama `NotImplementedError`; onay gelince sadece o doldurulacak.
- `.env`: `DOWNLOADS_DIR` (`watch` için; varsayılan kullanıcı Downloads klasörü).

### Komutlar
| Komut | Ne yapar |
|---|---|
| `squadmind sync <nick> -n 100` | FACEIT maç geçmişi + skor tablosu (upsert) |
| `squadmind demo-links <nick> [--map dust2] [--outdated]` | İndirilecek demoların maç sayfası linkleri → `reports/demo_links.md` |
| `squadmind ingest-demos [--delete/--keep] [--details]` | Inbox'taki demoları işler, FACEIT ile doğrular, processed/'a taşır |
| `squadmind watch [--dir] [--existing] [--delete/--keep]` | İndirme klasörünü izler; yeni demo (boyutu 3 sn sabit kalınca) inbox'a taşınıp işlenir, tek satır sonuç |
| `squadmind reprocess-demos` | processed/'daki demoları güncel parser ile yeniden işler (parser'a alan eklenince) |
| `squadmind clean-demos` | Verisi DB'de olan demo dosyalarını onayla siler |
| `squadmind report player <nick> [--map]` | Oyuncu raporu → `reports/player_<nick>.md` |
| `squadmind report team <nick> [--map]` | Takım raporu → `reports/team_<nick>.md` |
| `squadmind web [--port 8000]` | Web arayüzü (Faz 3a): davet linkiyle katılım, demo yükleme, oyuncu / takım raporu. Kalıcı adres **https://app.squadmind.info** (Cloudflare named tunnel `squadmind`, ayar `~/.cloudflared/config.yml`; çalıştırma: `cloudflared tunnel run squadmind`). Otomatik başlatma: Görev Zamanlayıcı `SquadMind` görevi oturum açılınca `scripts/start-squadmind.ps1`'i çalıştırır (sunucu + tünel, günlükler `<veri>/logs/`); kurulum `scripts/install-autostart.ps1`. Not: kullanıcı `cloudflared` Windows hizmetini de kurdu ama ayarsız (LocalSystem, config sistem profilinde yok) → boşta |
| `squadmind track <plans/*.json>` | Öneri takibi: planın kurallarına bağlı metrikler, plan tarihinden önce / sonra → `reports/track_<plan>.md` |
| `squadmind query "<SELECT>"` | Salt-okunur SQL (ajanlar için) |

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
- **Hasar = kırpılmış hasar:** `damages.dmg_health` ham (20 canlıya AWP 115 yazar). Analiz `squadmind.parse.damage.cap_damage` ile kalan cana göre kırpar (`dmg` kolonu, `load_demo_data` içinde); 180/180 oyuncu-maç ADR'ı FACEIT ile ≤2,3 farkla tutuyor. Parser v3 utility (HE / molotof) hasarını da kırpar.
- **Kill değeri** (kullanıcının isteği: "low bırakmak da kritik, eco kill'in değeri farklı"): `impact.py`. Kill türü (katilin hasar payı ≥90 tek başına, <40 bitirme), kazanma olasılığı katkısı (WPA: sağ kalan sayısına göre, bu maçların verisinden, WP_PRIOR=10 ile yumuşatılmış, ekonomi hariç), ekonomi bağlamı (eco'ya kill, fakirken kill, rakip ekipmanı), öldürmeyen hasar (low ≤40, asist ≥41, hasar desteği). Sağ kalan sayısı 5 kişilik takım varsayar.
- **Yalnız ölüm** (`death_isolation`): ölüm anında en yakın hayattaki takım arkadaşı >900 birim (3B; arkadaşın ölüm anına en yakın konum kaydı, ≤128 tick). Eşik veriden: ≤700 birimde ölümlerin ~%26'sı, 900 ötesinde ~%7'si trade ediliyor. Son kalan oyuncunun ölümü sayılmaz. Raporda Trade metrikleri + ölüm yerlerinde yalnız / arkadaş yakında ayrımı.
- **T desen kuralları** (`team_tactics.py`; Dust2 koridor / giriş alanı tanımları `maps.py` LANES / HIT_AREAS, 184 plant round'undan): hedef site = plant edilen ya da giriş alanında en çok bulunulan; giriş = ≥3 T aynı anda giriş alanında (yoksa plant anı); rush ≤25 sn, sonra ≥2 koridor = split, tek = execute. Default = 15-20. sn'de ≥3 koridora yayılma. CT dizilişi 15-35. sn'de A-Mid-B sayıları, sadece 5 CT'nin yeri bilinen round'lar (~%60).
- **Takım = oyuncunun her maçtaki takımı.** Nocleva'nın 18 Dust2 maçının 11'inde aynı çekirdek kadro (DeloneCS, _squ1rtle_, nicorett3).
- **Takım yönetimi** (Dust2'de, şimdilik eldeki veriyle; ayrıntılı çalışma herkes web'den kendi demolarını yükleyince): çekirdek = oyuncu + maçların ≥ yarısında birlikte oynayanlar, diğerleri "diğer". "Kim nerede iyi" tek isim seçmez, alan başına sıralar (Net/R = (kill − ölüm)/round); atama tactician'ın işi. Trade eşleştirme: takım arkadaşını öldüren rakibi 5 sn içinde öldürmek (awpy'nin trade sayısıyla tutarlı: 244 / 230).
- **Web (Faz 3a, `src/squadmind/web/`):** FastAPI + Jinja2, kendi bilgisayarında + Cloudflare quick tunnel (kullanıcı kararı: önce risksiz test, sonra VPS). Katılım: `?davet=<WEB_INVITE_CODE>` + FACEIT nickname (FACEIT'ten doğrulanır, son 100 maç çekilir). Yükleme tarayıcıdan 32 MB parçalar (tünel tek istekte ~100 MB sınırı); birleşen dosya bildirilen boyuta eşit değilse işlenmez; `ingest_demo(uploader=steam_id)` → aynı demo bir kez + yükleyen maçta olmalı. İşçi tek iş parçacığı. Rapor HTML'i kaçışlanır (XSS). **DuckDB tek süreç:** `squadmind web` açıkken CLI komutları DB'yi açamaz; ya web'i durdur ya da demoları web'den yükle. `.env`: WEB_INVITE_CODE, WEB_SECRET, WEB_TEAM_OWNER, WEB_MAP, WEB_REQUIRED_DEMOS.
- **Takım oluşturma ve harita bazlı demo** (şema v6, `analysis/team_builder.py`, `/takimlar`): FACEIT ile giren herkes takım kurar (en fazla 7). Öneri = en çok aynı tarafta oynanan (≥2 maç), arama = DB'deki nickname'ler + FACEIT'te tam ad. Eklenen oyuncu `davet` durumunda; verisi (FACEIT özeti, harita bazında demo) ancak kendisi kabul edince (`üye`) takımla paylaşılır. Takım sayfası: kadro kartları, harita hazırlığı, ikili birlikte oynama / kazanma, birlikteyken haritalar. Demolarım: harita kartları (≤5 kırmızı, 6-9 turuncu, ≥10 yeşil; `MIN_DEMOS=10`), yüklemede harita seçimi (`map_hint`; asıl harita demodan, farklıysa not). Kadro sayfaları (WEB_TEAM_OWNER'ın takım raporu, taktik tahtası) `web_users.team_access` ile: davet, sahibin kendisi ya da sahiple ≥3 ortak maç; v6 öncesi kullanıcılar erişimi korur.
- **FACEIT girişi, 23.10 öğleden sonra bulgular (önceki 401 analizlerinin çoğu bunlarla geçersiz):** (1) `.env`'deki eski secret geçersizdi; App Studio'daki güncel secret yazıldı. (2) `code_verifier` gönderilince FACEIT secret'a bakmıyor (doğru / yanlış secret aynı yanıt) → doğru istek: Basic + `redirect_uri`, doğrulayıcı yok; `.env`: `FACEIT_FLOW=code`, `FACEIT_PKCE=0`. (3) FACEIT kısa sürede birkaç başarısız token isteğinden sonra istemciyi bir süre kilitliyor (aynı sahte kod önce 400, sonra 401): toplu yoklama YAPMA; `squadmind faceit-check` seyrek kullan, açılışta / hata anında otomatik kontrol kaldırıldı.
- **Yalnız FACEIT ile giriş (kullanıcı kararı, 24.10):** kayıt yok; ilk FACEIT girişinde davetsiz hesap açılır, sonraki girişlerde aynı hesaba girilir. E-posta+şifre, `/katil`, `/kayit`, şifre sıfırlama rotaları `WEB_PASSWORD_LOGIN=1` ile açılır (varsayılan kapalı: GET `/`'a yönlenir, POST 404). Kadro sayfası erişimi (`team_access`) eski kurala bağlı (sahip ya da ≥3 ortak maç). Yedek: `squadmind login-link <nick>` kayıtsız oyuncu için de çalışır (yük: steam+faceit+nickname).
- **FACEIT ile giriş** (`web/faceit_auth.py`, FACEIT Connect: OAuth2 yetki kodu + PKCE S256, uç noktalar `api.faceit.com/auth/v1/openid_configuration`'dan): kullanıcı bilgisinden sadece `guid` alınır, profil / Steam / seviye Data API `get_player`'dan. `.env`: `FACEIT_CLIENT_ID`, `FACEIT_CLIENT_SECRET`, `WEB_BASE_URL`; istemci developers.faceit.com → App Studio'da açılır, dönüş adresi `<WEB_BASE_URL>/giris/faceit/donus`. Ayarlıysa nickname ile katılım kapanır (başkasının adıyla girilemesin); davet kodu gerekmez: yeni oyuncu takım sahibiyle (WEB_TEAM_OWNER) aynı tarafta ≥3 maçı varsa (DB'deki skor tablolarından, `TEAM_MIN_SHARED`) doğrudan girer; yoksa davet koduyla. Kayıtlı oyuncu her zaman girer. Nickname ile katılımda (kimlik doğrulanmadığı için) davet hep şart. Ayarlı değilse eski nickname formu. **Durum (23.10.2026): çalışmıyor.** İstemci `3f5187ec-…` (App Studio'da tek tür: "Authorization Code with PKCE", secret var). Giriş sayfası 6 karakterlik kod veriyor, token adımı her biçimde 401 (`Basic realm="oauth2/client"`; Basic / gövde, PKCE'li / PKCE'siz, redirect'li / redirect'siz denendi; `/accounts` ve kök adres). Sahte kodla Basic + doğrulayıcı 400 → kimlik doğru. Implicit akış (`response_type=token`, kodda hazır, `FACEIT_FLOW=implicit`) denendi: FACEIT onay ekranında takılıyor, dönüş yok; `.env`: `FACEIT_FLOW=code`. FACEIT giriş sayfasının kaynak kodundan (23.10): sayfa PKCE'yi destekliyor ve iletiyor (`zAuthorizeRequest`, RFC 7636); kodu yeni "faceit-connect" servisi (`accounts.faceit.com/api/v1/authorize`) üretiyor, token eski `auth-api`'de; izin ekranındaki onay isteği (`POST /authorize`) `redirect_uri` göndermiyor; implicit akışta anahtar `token` adıyla gelir (dönüş sayfası iki adı da okur). **23.10 öğleden sonra:** `.env`'deki secret ile sahte kodlu istek artık 400 değil 401 (yanlış secret'la aynı; client_credentials / refresh / JSON / query hepsi 401) → FACEIT şu an istemci kimliğini hiç kabul etmiyor; secret yenilenmiş ya da istemci kapalı olabilir (FACEIT: yeni secret eskisini hemen geçersiz kılar). Kontrol: `squadmind faceit-check` (400 = kimlik geçerli, 401 = secret reddedildi; DB açmaz); `web` açılışta ve token 401'inde aynı kontrolü günlüğe yazar. Önce secret App Studio'da yenilenip `.env`'ye yazılmalı, `faceit-check` 400 verince gerçek giriş denenmeli; o zaman da 401 ise sorun koddadır (kod başka istemci kimliğine kesiliyor olabilir: onay ekranı `client_id: authorization.client.id` gönderiyor) ve FACEIT desteğine sorulacak. Yedek: davet linkiyle nickname girişi (FACEIT girişi açıkken de davetle gelen için açık).
- **Taktik tahtası** (`viz/board.py`, kullanıcının isteği: "futbol taktik tahtası gibi ama gerçek harita, kuş bakışı, spotlar, çizilmiş taktik, yüksek kalite"): harita awpy navmesh'inden kendi çizimimiz (Valve radar görseli değil), duvar konturu + 3 yükseklik tonu, hedef site turuncu. Rotalar navmesh'te en kısa yol + string-pulling (duvara girmeden düzleştirme), aynı yoldan gidenler paralel şerit. Utility gerçek boyutta (smoke 144 birim), atış yayı noktalı. Rakip spotları veriden (biz T'deyken rakip CT 15-25. sn, 150 birim ızgara, `merge_spots`). Çıktı stil etiketsiz SVG. Tema: "Keskin Taktik Masası" (neredeyse siyah, turuncu T / turkuaz destek). Taslaklar claude.ai "SquadMind — Tema seçenekleri" (A3 = tahta). **Web'de `/taktik`** (`viz/tactic.py` + `web/templates/tactic.html`, TAKTIK teması, tasarım AI'ın görselleri): strat elle yazılır (`plans/strats/*.json`: anahtar kareler, utility, görev, "şu an" satırları, eğer… ise dalları, round sekmeleri); harita / rotalar / kareler navmesh'ten, rakip spotları ve sekme sayıları (T pistol, pistol sonrası 2. round) takımın verisinden. Önbellek demo sayısı ya da strat değişince yenilenir. "Eğer… ise" gerekçeleri veriden: dalda `metrics` (öneri takibinin `measure` türleri) + metinde `{ad}` / `{ad.k}` / `{ad.n}`; örnek yoksa "veri yok", payda <10 ise "Az örnek." (Elle yazılmış eski sayıların bir kısmı yanlıştı: Nocleva T mid opening 11-1 değil 17-8, nicorett3 utility hasarı 5,8 değil 2,61.)
- **Peek hatları ve düello gücü** (`analysis/duels.py`, `maps.PEEK_LINES`, `analysis/geometry.py`; taktik oluşturucunun çekirdeği): Dust2'de 10 hat (kullanıcı onayladı, 23.10). Düello = silahlı rakip kill'i; güç iki kademe küçültmeli (genel → %50, hat+taraf → genel), karşılaşma = taban + etki farkı. Mesafe dilimi yakın <500 / orta <1.100 / uzak (oyun birimi). Görüş: awpy `VisibilityChecker` (duvarsız kill'lerin %94'ü görünür çıktı). **Sınama (ilk 12 maç → son 6): kazananı bilme %54, taban kadar** — rakiplerin geçmişi yok (hiçbir sınama düellosunda iki oyuncu da ≥20 düellolu değil). Karşılaşma tahmini rakip verisi (Downloads API / rakibin FACEIT seviyesi) gelmeden gösterilmemeli. Ölçüm: demo 170-220 MB zst, açma+ayrıştırma ~4 sn (16 çekirdek), DB'de ~1 MB / demo.
- **Oyuncu gücü + karşılaşma olasılığı** (`analysis/rating.py`, statistician, 23.10; duels.py'nin karşılaşma modelinin yerine): her oyuncunun maçtan ÖNCEKİ son 100 FACEIT maçı (`FaceitClient.get_player_match_stats`, `to` milisaniye) → kill payı K/(K+D), seviyesine göre beklenene ampirik Bayes küçültmesi (w = τ²/(τ²+σ²/n); 10 maçta 0,39, 99 maçta 0,86) → stat puanının ELO karşılığı κ=0,25 ile kısılıp ±300 ELO'da sınırlanır → **birleşik ELO**. Karşılaşma: logit = taban(%50) + 1,386·tanh(β·Δ/100/1,386), β = 0,047 ± 0,007 / 100 ELO; olasılık asla %20-%80 dışına çıkmaz. Güven: "az veri" (ELO yok / <20 maç), "belirgin" (%90 aralık %50'yi dışlıyor), "belirsiz". **Sınama** (ileriye kayan pencere, 12 test maçı, 1.999 düello; `scripts/eval_rating.py`): log-kayıp 0,6859 vs yazı-tura 0,6931 (+0,0073 [+0,0034, +0,0112]), sadece seviye 0,6867, hat×taraf ortalaması 0,6970 (yazı-turadan kötü), eski duels.py 0,6957; kalibrasyon eğimi 1,00. Stat puanının seviyenin üstüne katkısı küçük ve belirsiz (+0,0008 [−0,0002, +0,0019]). ADR, demo artığı, hat artığı, hat×taraf / mesafe tabanı katkı vermedi → kapalı. Çekirdek oyuncular demoda tam beklenen oranda (Nocleva beklenen %54,4, gerçek %54,5; 578 düello). Tek düello çoğunlukla %40-60: model "kim favori"yi söyler, sonucu değil.
- **Head-to-head (23.10, statistician-duel):** aynı maçtaki önceki karşılıklı düellolar sonrakini ELO + silah + formun ötesinde tahmin etmiyor (Δlog-kayıp −0,0002 [−0,0009, +0,0005], 2.607 düello, ileri + bir-maç-dışarıda sınama). Önceki H2H lideri sonrakini %51,4 kazanıyor (ELO farkı açıklıyor), aynı hatta %50,9 (234; yalnız >5 puanlık etki dışlanabilir). Karar: arenada H2H ağırlığı 0; gösterilirse yalnız betimleyici + "tahmini etkilemez". Daha çok maçla yeniden ölçülecek.
- **Silah etkisi (profesör, 23.10):** varsayılan 3 özellikli model (tabanca, AWP uzak ≥1100, AWP yakın/orta; `WeaponEffect.simple=True`), ELO tabanına +0,018 [+0,012; +0,023]. Sınıf × mesafe modeli (`simple=False`; tabanca/SMG/tüfek/Scout/AWP/ağır × yakın<500/orta/uzak, hiyerarşik küçültme) ek kazancı sıfırı dışlamadı → yalnız betimleyici (`duels.class_band_record`). Ekonomi kontrolü etkileri değiştirmiyor.
- **Düello parametreleri (map-architect ön eleme + statistician-validation, profesör 23.10):** CAN kabul: düello başlangıcından (iki oyuncu arası ilk hasar) önceki can farkı, `rating.HP_COEF` 0,717 / 50 HP (0,801 × ileri eğim 0,895), SE 0,082; ELO/silah eşitken 50 HP ≈ %67; arenada iki oyuncu için 1-100 girdi, ana olasılığa eklenir, kart "can eşitken" satırını da gösterir. Eski can tanımı (kill−2 sn) düelloların %20'sinde düellonun kendi hasarını içeriyordu (sızıntı). RET: trade arayan (can varken ek katkı yok; A4 simülasyonunda yeniden), bomba kurulu, momentum / art arda kill (can + trade'in gölgesi), clutch, sayı üstünlüğü, lurk, round zamanı, ekipman değeri (silahın vekili). Ölçülemez: no-scope (10 kill), açı / kim önce gördü (2 sn kayıt), kör olma, zırh. Yükseklik: |dz|>192'de yüksek olan %70 (n=43), yalnız betimleyici, daha çok demoyla yeniden.
- **Tarayıcıda demo işleme (23.10):** awpy 3.1.1'in Rust çekirdeği WASM'a derlendi (`wasm/awpy-wasm/`, çıktı `web/static/wasm/`); tarayıcı demodan ~0,4 MB gzip'li paket üretir (232 MB yerine), sunucu `parse/package.py::parse_package` ile aynı `DemoTables`'ı kurar. 19/19 demoda tüm tablolar awpy yoluyla birebir aynı (`scripts/validate_package.py`). WASM ~9 sn, tepe ~1 GB (Node; tarayıcıda henüz denenmedi). Python awpy sürümü değişirse `Cargo.toml` etiketi + WASM yeniden derlenip doğrulama tekrar koşulmalı. **Web yüklemesi (23.10):** `/demolar` varsayılan olarak tarayıcıda işler (`static/pack/`: `sources.js` kaynak arayüzü `{name,size,getBytes()}` — ileride Downloads API URL'si aynı arayüzle; `pack_worker.js` Web Worker: sha256 → açma (gzip `DecompressionStream`, zstd `static/vendor/fzstd.mjs` MIT) → WASM → gzip; `pack.js`). Çoklu seçim, sırayla TEK demo, her demo yeni worker; hata `Tam demoyu yükle` yedeğine yönlendirir. Sunucu `POST /api/paket` (≤8 MB) → işçi → `ingest/demos.py::ingest_package`. **sha256 kuralı:** `sha256` = seçilen dosyanın olduğu gibi baytları (tam demo yolundaki `precheck_demo` ile AYNI), `dem_sha256` = açılmış .dem; ikisi de `demo_files`'ta aranır (şema v9: `source` 'demo'/'paket', `dem_sha256`). Paket yolunda ek kontroller: awpy/format sürümü, FACEIT skor tablosu şart, harita, round sayısı = FACEIT skoru, oyuncu kümesi = kadro, yeterli oyuncunun demo istatistiği; uymazsa mismatch (veri yazılmaz). Edge (headless) denemesi: 170 MB .zst → 10,7 sn, paket 351 KB, Edge tepe ~1,6 GB.
- **A4 round simülasyonu (24.10, statistician-duel + profesör):** `analysis/simulate.py` (zamanlı rotalar, görüş, onaylı düello modeli, Monte Carlo 2000 tekrar 0,04-0,2 sn; plant sonrası veriden tablo). Sınama `scripts/eval_simulate.py`, 398 round: yazı-tura ve ELO tabanını geçiyor ama silah sınıfı sayıları tabanından KÖTÜ (−0,024 [−0,040; −0,006] ileri, −0,020 maç-dışarıda); kazancın tamamı silah teriminden, düello kapalı kontrol 0,696 → rota/geometri katkısı yok; desen oranları 0,51-0,57'ye sıkışık. Karar: ana dalda yalnız araç; olasılık / yüzde / kazanan yeşili hiçbir ekranda YOK; arenada izin = tek örnek akış animasyonu, sayısız, "Örnek akış: tahmin değil, betimleme. Sınamayı geçmedi." (metin Kawanagari, onay profesör). Olasılığa açma ölçütü: maç-dışarıda sınamada silah sınıfı + ELO tabanını %90 alt sınır > 0 ile geçmek, kalibrasyon eğimi [0,8; 1,2], rota/geometri katkısı alt sınır > 0. Sıradaki: peek/tutma + trade mekanizması (statistician-duel), bağımsız yeniden ölçüm (validation); ≥40 Dust2 maçı gerekli.
- **Taktik oluşturucu** (`/olustur`, `analysis/tactic_builder.py`, şema v8 `user_tactics`): oyuncu listesi izinli (kendisi, kabul etmiş takım üyeleri, kadro erişimi varsa sahibin çekirdeği), haritanın her noktasına yerleştirme, bölge = konum kayıtlarından 64 birimlik ızgara. Kartta doğrulanmış güç (`rating.rating_score`, %90 aralık, birleşik ELO) + hat kayıtları SADECE betimleyici (hat etkisi sınamayı geçmedi; "bu noktada daha güçlü" önerisi kaldırıldı). Rakip modu sırada.
- **Öneri takibi** (`analysis/tracking.py`): plan bir JSON (`plans/`): sahibi, harita, tarih, maddeler (metrik türü + parametre + hedef yönü). Kod oyuncu adı bilmez. Durum ancak plan sonrası ≥3 maçla yazılır; küçük fark (3 puan / %10) "değişim yok". İlk plan: `plans/dust2_cekirdek_2026-10-22.json` (tactician'ın çekirdek kadro planı; ajan çıktıları claude.ai dokümanında: "SquadMind — Ajan Çıktıları").
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
2. Faz 2b: desen / rol etiketlerini demodan doğrulama + öneri takibi (önce / sonra); Mirage ve Inferno demoları + bölge tanımları. Faz 3c: `squadmind scout <match_id>` (rakip scout). Takım raporu fonksiyonları (`team_rounds`, `t_round_patterns`, `ct_setups`) rakip için de kullanılabilir: rakip oyuncunun steam_id'si verilince onun takımını analiz eder.
3. T desen sınıflandırmasını birkaç round'da demodan izleyerek doğrula (özellikle "girilmedi" ve default eşiği).
4. Diğer haritalar için `analysis/maps.py` alan tanımı (şimdilik sadece Dust2).

### Bilinen teknik tuzaklar
- DuckDB 2.x: `.pl()` yok → `pl.from_arrow(con.cursor().sql(sql, params=...).to_arrow_table())`. Açık sonuç varken aynı bağlantıda yeni sorgu hata verir (ayrı `cursor()` kullan). `ON CONFLICT DO UPDATE` içinde `current_timestamp` yerine `now()`.
- Polars DataFrame'i DuckDB'ye yazmak için `pyarrow` gerekir.
- DuckDB 2.x relation'da `fetchone()` yok → `.fetchall()[0]`. `matches.map` değerleri `de_` önekli (`de_dust2`); CLI `--map dust2` kabul eder.
- awpy: `damages.dmg_health` kırpılmamış ham hasar (bkz. kararlar). `kills`/`smokes` ısınma round'unu da içerir (`round_windows` ile elenir); smoke atanı çoğunlukla boş gelir (`resolve_projectiles` ile `shots`'tan eşlenir); bölge adı ham alan `m_szLastPlaceName`.
- **Hesaplar (şema v11, `web/accounts.py`, `web/mailer.py`, 24.10.2026):** `accounts` (e-posta, argon2 karması) + `web_users.account_id / faceit_link` (`dogrulanmis` = FACEIT girişi, `dogrulanmamis` = nickname). Rotalar: `/kayit` (davet şart), `/giris`, `/hesap` (+ `/eposta`, `/sifre`, `/dogrula`, `/atla`), `/sifre-unuttum`, `/sifre-sifirla`. Oturum çerezi hâlâ steam_id taşır (eski çerezler çalışır; hesapsız kullanıcı `/ben`de `/hesap/eposta`ya yönlenir). FACEIT dönüşünde (`ensure_faceit_account`) hesap otomatik açılır / e-postayla eşleşene bağlanır (doğrulanmamış hesabın şifresi silinir: ön-ele geçirme). Giriş hız sınırı bellekte (5 hata / 15 dk), CSRF çift gönderim. SMTP (`SMTP_*`, `MAIL_FROM`) yoksa doğrulama / sıfırlama uçları 404. `squadmind accounts` salt-okunur liste (web açıkken DB kilitli).
