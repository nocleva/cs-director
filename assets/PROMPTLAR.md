# SquadMind görsel paketi: Gemini prompt'ları

Üretim: gemini.google.com (elle). Çıktıları `assets/gelen/` klasörüne **tam bu dosya adlarıyla** koy, sonra:

```
python scripts/assets_yerlestir.py
```

Betik dosyaları `src/squadmind/web/static/img/` altına kopyalar ve `manifest.json` yazar; site manifestte
olanı otomatik kullanır, olmayanın yerinde CSS/SVG yer tutucu kalır (hiçbir şey bozulmaz, tek tek eklenebilir).

## Ortak stil (her prompt'un başına ekle)

> Original anime-inspired game UI illustration meets clean modern flat design. Bold clean outlines, angular
> chamfered shapes, subtle halftone dots and diagonal speed lines, limited palette: ember orange (#FF6A1F),
> night ink (#0D1018 to #1B2133), violet (#8B6CFF), cyan (#38E1D0), white highlights. Crisp vector look,
> high contrast, readable at small sizes. Competitive tactical shooter vibe, not tied to any existing franchise.

## Ortak "İÇERMEMELİ" listesi (her prompt'a ekle)

> Do NOT include: FACEIT logo or its flame/chevron mark, Valve / Counter-Strike / CS2 logos, any existing game
> character or operator, real weapon models or recognizable gun silhouettes (AK-47, AWP, M4, Desert Eagle),
> any text, letters, numbers or watermarks (unless the prompt says so), photographs of real people, copyrighted
> mascots.

---

## 1. Logo
- **Dosya:** `logo.png` | **Boyut:** 1024x1024, kare | **Arka plan:** ŞEFFAF (PNG alfa)
- **Kullanım:** üst çubuk markası (28 px, sol üst), giriş sayfası başlığı. Küçük boyutta okunmalı.
- **Prompt:** `[Ortak stil] A minimalist emblem mark for "SquadMind": an angular bracket / chevron shape formed by two
  interlocking slashes, one ember orange and one violet, suggesting a tactical map arrow and a mind spark. Single
  centered symbol, thick strokes, simple enough to read at 28 px, transparent background, no text. [İÇERMEMELİ]`

## 2. Kart çerçevesi (boş)
- **Dosya:** `kart-cerceve.png` | **Boyut:** 750x1050 (5:7 dikey) | **Arka plan:** ŞEFFAF, ortası tamamen boş
- **Kullanım:** oyuncu kartının üstüne bindirilir (Kartlar, Ben, Arena yan paneli). Kart içeriği çerçevenin
  içinde okunmalı: iç alan düz, çerçeve ince olmalı (kenarlardan en çok %6).
- **Prompt:** `[Ortak stil] An empty trading-card frame, portrait 5:7, thin ornate border with two chamfered corners
  (top-right and bottom-left cut diagonally), subtle orange-to-cyan gradient edge light, tiny corner ticks and a
  slim notch at the top center for a banner. Interior is fully transparent and empty. Transparent background
  outside the frame. [İÇERMEMELİ]`

## 3. Seviye rozeti seti (1-10)
- **Dosyalar:** `rozet-1.png` ... `rozet-10.png` | **Boyut:** 512x512 | **Arka plan:** ŞEFFAF
- **Kullanım:** kart afişi (46 px), arena VS plakaları (40 px). Görsel emblemin tamamının yerine geçer; bu yüzden
  seviye rakamı rozetin İÇİNDE olmalı (tek metin istisnası).
- **Renk bandı (seviye rengi, FACEIT'in kendi logosu değil, sadece derece rengi):** 1 gri; 2-3 yeşil; 4-7 sarı;
  8-9 turuncu; 10 kırmızı (kırmızı olan en parlak, en süslü).
- **Prompt (her seviye için N ve renk değiştir):** `[Ortak stil] A hexagonal shield rank badge, tier N of 10, main
  color COLOR. Higher tiers get more wings, spikes and glow; tier 1 is plain and tier 10 is the most ornate with a
  small crown-like crest. A single bold numeral N centered inside the badge (only this numeral). Transparent
  background, consistent shape and size across the set. [İÇERMEMELİ: sayı dışında yazı]`
- **İpucu:** önce 1, 5 ve 10'u üret, tutarlıysa diğerlerini aynı sohbette iste.

## 4. Giriş kapak görseli
- **Dosya:** `giris-kapak.jpg` (ya da `.png`) | **Boyut:** 2400x1350 (16:9) | **Arka plan:** opak
- **Kullanım:** giriş sayfasında harita panelinin arkasında (%55 saydamlıkla). Sol-orta boş kalmalı, ana öğeler sağda.
  Mobilde ortadan kırpılır: önemli şey merkezde olsun.
- **Prompt:** `[Ortak stil] Wide cinematic key art: two abstract silhouetted tactical players facing each other across
  a stylized top-down desert-city map made of glowing outlines, a large diagonal VS slash between them, ember
  orange vs cyan rim lights, speed lines, halftone sky at dusk, lots of dark negative space. Players are generic
  faceless silhouettes with visors, no uniforms or logos, no weapons (empty hands). [İÇERMEMELİ]`

## 5. Soyut oyuncu avatarı seti
- **Dosyalar:** `avatar-1.png` ... `avatar-8.png` | **Boyut:** 512x512 | **Arka plan:** ŞEFFAF (altıgen kesim, kenar boşluğu %8)
- **Kullanım:** kart afişi (58 px), arena VS plakaları (46 px). Oyuncu adından türeyen sayıyla biri seçilir; seviyeyle
  ilgisi yoktur.
- **Prompt:** `[Ortak stil] A set of 8 distinct abstract player avatars, each inside a hexagon: a faceless helmeted
  silhouette with a glowing visor line, each with a different color pair and geometric pattern behind it (stripes,
  triangles, dots, rings, waves, grid, chevrons, sunburst). Same style, same framing, centered bust, transparent
  background outside the hexagon, generate one image per avatar. [İÇERMEMELİ: gerçek yüz, silah]`

---

## Kabul listesi (görseli siteye koymadan önce)
- Logo / marka / karakter / silah benzerliği yok (FACEIT alevi, CS ikonları, bilinen operatör yüzleri).
- İçinde metin yok (rozette yalnız seviye rakamı).
- Şeffaf istenenlerde gerçekten alfa var (köşe pikselleri saydam).
- 28 px ve 46 px'e küçültünce okunuyor.
