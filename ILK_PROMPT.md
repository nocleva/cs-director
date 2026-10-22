# Claude Code'a Verilecek İlk Prompt

## Kurulum (prompt'tan önce)

| Adım | Ne yapılır | Komut / Yer |
|---|---|---|
| 1 | FACEIT Developer hesabı aç, **Server-side API key** oluştur | developers.faceit.com |
| 2 | Downloads API'ye başvur (onay ~30 gün sürer, bekleme süresinde manuel demo ile ilerlenir) | fce.gg/downloads-api-application |
| 3 | `uv` kur | aşağıdaki kod bloğu |
| 4 | Proje klasörünü aç, `CLAUDE.md`'yi içine koy | aşağıdaki kod bloğu |
| 5 | `.env` dosyasına API anahtarını yaz | aşağıdaki kod bloğu |
| 6 | Son 3-5 maçının demosunu FACEIT maç sayfasından indir, `data/demos/inbox/` içine at | FACEIT → Match Room → Watch demo |
| 7 | Claude Code'u başlat ve aşağıdaki prompt'u yapıştır | `claude` |

```bash
# 3) uv kurulumu (Windows PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 4) Proje klasörü
mkdir cs2-director
cd cs2-director
# CLAUDE.md dosyasını bu klasöre kopyala

# 5) API anahtarı (.env dosyası, git'e eklenmeyecek)
echo FACEIT_API_KEY=buraya_anahtarin > .env

# 6) Demo klasörü
mkdir -p data/demos/inbox

# 7) Claude Code'u başlat
claude
```

---

## Prompt (olduğu gibi kopyala)

```
CLAUDE.md dosyasını baştan sona oku; projenin vizyonu, mimarisi ve kuralları orada.

Bu oturumda Faz 0 ve Faz 1a'yı tamamlamanı istiyorum. Kod yazmaya başlamadan önce
plan modunda kısa bir uygulama planı çıkar, onayımı al, sonra uygula.

Görevler:
1. `uv` ile Python 3.11+ projesi kur. Bağımlılıklar: httpx, duckdb, polars, awpy,
   typer, python-dotenv, rich, pytest.
2. CLAUDE.md'deki dizin yapısını oluştur.
3. `src/director/faceit/client.py`: FACEIT Data API v4 istemcisi.
   - API anahtarını .env'deki FACEIT_API_KEY'den oku.
   - Disk cache (data/cache/, yanıtları JSON olarak sakla, maç detayları kalıcı,
     oyuncu profili 1 saat geçerli).
   - 429 yanıtında exponential backoff.
   - Fonksiyonlar: get_player_by_nickname, get_player_history (CS2, sayfalı),
     get_match_details, get_match_stats.
4. `src/director/db/schema.sql` ve bir init fonksiyonu: CLAUDE.md bölüm 5'teki
   tabloları DuckDB'de oluştur.
5. `src/director/ingest/matches.py`: verilen nickname için son N maçı çek;
   matches ve match_player_stats tablolarına yaz. Maç detayındaki demo URL'sini
   matches.demo_url alanına kaydet, demo_status = 'pending'. Aynı maç tekrar
   çekilirse kopya oluşturma (upsert).
6. `src/director/cli.py` (typer):
   - `director init-db`
   - `director fetch-player <nickname>` → profil, ELO, level yazdır
   - `director sync <nickname> --limit 30` → maçları DB'ye çek
   - `director summary <nickname>` → DB'den son maçların özet tablosu (rich ile):
     harita, skor, K/D, ADR, HS%, sonuç
7. Testler: API yanıtları için kaydedilmiş JSON fixture'larıyla client ve ingest
   testleri (gerçek API'ye test sırasında istek atılmasın).

Kurallar:
- Tüm kod tam ve çalışır olsun, satır içi Türkçe yorumlar ekle.
- Gerçek API'yi denemek için önce `director fetch-player <benim_nickim>` komutunu
  çalıştırmamı iste; yanıtın şeklini görmeden alan adı varsayma.
- FACEIT API yanıtındaki alan adlarını kesin bilmiyorsan, ilk gerçek yanıtı
  data/cache'ten okuyup modeli ona göre düzelt.
- Bitirince: neyin çalıştığını, nasıl test edeceğimi ve Faz 1b (demo parse)
  için önerdiğin sonraki adımı kısaca yaz.

Benim FACEIT nickname'im: <NICKNAME_BURAYA>
```

---

## Sonraki prompt'lar (sırasıyla)

| Sıra | Prompt özeti |
|---|---|
| 2 | "Faz 1b: `data/demos/inbox/` içindeki .dem/.dem.gz dosyalarını awpy ile parse et, CLAUDE.md bölüm 5 tablolarına yaz, işlenen demoyu processed/'a taşı. Demonun hangi FACEIT maçına ait olduğunu harita + oyuncu steam ID'leri + tarih ile eşleştir. Önce örnek demodaki gerçek kolonları yazdır, sonra şemayı ona göre bağla." |
| 3 | "Faz 1c: bölüm 6'daki oyuncu metriklerini ve rol sınıflandırmayı yaz. Her metriği bir demonun FACEIT scoreboard'u ile karşılaştırarak doğrula. `director report player <nick>` Markdown rapor üretsin." |
| 4 | "Faz 1d: `.claude/agents/director.md`, `scout.md`, `tactician.md` subagent dosyalarını CLAUDE.md bölüm 7'ye göre oluştur. Director, raporları ve DuckDB'yi okuyarak yorum yapsın; veri yetersizse söylesin." |
| 5 | "Faz 2a: T tarafı ilk 20 saniye pozisyonlarından taktik desenlerini kümele, desen başına kazanma oranı; ekonomi kararlarının sonuçları." |
