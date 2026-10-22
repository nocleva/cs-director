# SquadMind — Tasarım brifi (tasarım yapay zekâsı için temel)

## Ürün
SquadMind, FACEIT'te birlikte oynayan CS2 takımları için bir **takım yönetimi** uygulaması. Takımın kendi maç demolarını analiz edip şunları söyler: kim nerede iyi, nereleri tutmalıyız, gücümüz ve zayıflığımız nerede, kime destek gerekli, maç öncesi round round hazırlık planı. Her öneri veriye dayanır ve örneklemiyle gösterilir ("%20 (10/50)").

- **Kullanıcılar:** 4-5 kişilik FACEIT premade takımı (level 7-10 oyuncular), takımın kaptanı / IGL. Türkçe arayüz; oyun terimleri İngilizce kalır (entry, trade, retake, eco, smoke, flash, long, short).
- **Kullanım anı:** Maç öncesi (hazırlık, 5-10 dk), maç sonrası (rapor), hafta içi (gelişim takibi). Maç sırasında kullanılmaz.
- **Adres:** app.squadmind.info (masaüstü öncelikli, telefonda da okunabilir olmalı).

## Ekranlar
1. **Katıl:** Davet linkiyle gelinir; tek alan: FACEIT nickname. Uyarı: alt çizgi / büyük harf önemli.
2. **Sayfam:** Demo yükleme (sürükle-bırak, çoklu, yüzde ilerleme), yüklemelerin durumu (yükleniyor / sırada / işleniyor / tamam / reddedildi + neden), hedef ilerlemesi ("10 demodan 4'ü").
3. **Hazırlık (ana ekran, ürünün kalbi):** Maç hazırlık planı. Round sekmeleri: R1 pistol → R2 kazanırsak / kaybedersek → Ekonomi (full / force / eco) → Orta oyun → Retake. Her sekmede **taktik tahtası + oynatıcı + oyuncu inceleyicisi**. T / CT anahtarı. Rakip gücüne göre varyant (rakip ELO yüksek / denk / düşük).
4. **Raporum:** Oyuncunun kendi analizi: güçlü / zayıf yönler (lobiye göre), silah ve bölge, kill değeri, "sonraki maç için tek odak".
5. **Takım:** Kim nerede iyi (alan × oyuncu sıralaması), güç farkımız (biz vs rakip), kime destek gerekli, uyum (kim kimi trade ediyor, kim kiminle yakın oynuyor), T desenleri, CT dizilişleri, ekonomi kararları.
6. **Takip:** Planın kuralları önce / sonra: "iyileşti / kötüleşti / değişim yok / veri bekleniyor".

## Kalbi: taktik tahtası + oynatıcı + anlatım (kesin, korunacak)
- **Harita:** Gerçek oyun haritasının kuş bakışı çizimi (oyunun navmesh verisinden kendi çizimimiz). Duvar konturu, 3 yükseklik tonu (alt geçitler koyu, siteler açık), hedef site hafif vurgulu, bölge adları (Long A, Short, Catwalk, Mid Doors, CT Spawn…). Kod tarafından SVG olarak üretilir; tasarım renkleri ve stilleri belirler.
- **Katmanlar:** Oyuncu tokenları (numaralı daireler, rol rengi), rotalar (kesik çizgi + ok, aynı yoldan gidenler paralel şerit), utility (smoke gerçek boyutta daire, flash yıldız / parlama, molly), atış yayları (noktalı), rakip spotları (görülme yüzdesiyle halka, ör. "%30").
- **Oynatma:** Oyuncular saniye saniye rotalarında ilerler, smoke zamanı gelince açılır, flash patlar. Oynat / durdur, zaman çubuğu (utility anları işaretli), 1× / 0,5×.
- **Oyuncuya tıklayınca (kullanıcının en çok beğendiği kısım):** "Şu an" (oynatmayla güncellenir: "0:12 · Long A'ya ilk peek"), "Görev", "Eğer… ise" dalları: koşul (kalın) → ne yapacak → veriden dayanak (küçük, gri). Örnek: *"İlk peek'te ölürsen → _squ1rtle_ 2 sn içinde trade eder, ağırlık short'a kayar. (Long'daki 10 yalnız ölümün hiçbiri trade edilmemiş.)"*

## Örnek gerçek içerik (taslaklarda kullan)
- Kadro: DeloneCS (entry, long), _squ1rtle_ (trade, B anchor), nicorett3 (support, bomba), Nocleva (AWP / Scout, mid / short), 5. oyuncu (esnek).
- R1 pistol "Hızlı A": 0:06 Xbox smoke · 0:08 long flash · 0:14 short flash + CT smoke · 0:18 long ve short birlikte giriş. Geçmiş: pistol rush %57 (8/14); kazanırsak R2 20/20, kaybedersek force rush 6/7.
- Rakip spotları: A'nın CT girişi %30, short altı %22, Mid Doors %21. Retake A %20 (10/50).
- Takım vs rakip: T opening %52 / %42, CT opening %58 / %48, trade %16 / %16.

## Tasarım yönü: öğrendiklerimiz
- **Beğenilen:** Taktik ekranı hissi (oyunun sert doğası), koyu tema, tek güçlü vurgu rengi (turuncu) + sakin ikinci renk (turkuaz / camgöbeği) + rakip için kırmızı. Sadelik ve asillik (FACEIT'in temiz koyu arayüzü, İskandinav sadeliği). Gerçek harita üzerinde yüksek kaliteli çizim. Anlatımın netliği.
- **İstenmeyen:** Bir markanın kopyası gibi görünmek ("Apple çakması" oldu). Ucuz "gamer" klişeleri (neon parlama, aşırı HUD süsü). Logo / marka renklerinin birebir alınması (FACEIT ile ileride iş birliği hedefi var: onların yanında yabancı durmamalı ama taklit de etmemeli).
- **Okunurluk:** Rakamlar hizalı (tabular), örneklem her oranın yanında, az örnek "az örnek" diye işaretli. Yorum değil veri; yorumu "Eğer… ise" ve "Sonraki maç için tek odak" taşır.

## Teknik sınırlar (geliştirme tarafı)
- Web: Python (FastAPI) + sunucu tarafı HTML şablonları; az JavaScript (oynatıcı, yükleme). React gerekmez.
- Taktik tahtası SVG olarak üretilir; tasarımdan istenen: harita renkleri, çizgi kalınlıkları, token / utility / spot görünümü (tema değişkenleri olarak).
- Koyu tema öncelikli; telefon genişliğinde sağ panel alta geçer.
- Erişilebilirlik: metin kontrastı 4.5:1, gerçek düğmeler, renk tek başına anlam taşımasın (T / destek ayrımı aynı zamanda numara ve şekille).

## Tasarım yapay zekâsından beklenen çıktı
1. **Tasarım dili:** Renk değişkenleri (zemin katmanları, metin, vurgu, rol renkleri, durum renkleri), yazı tipleri ve ölçek, boşluk ölçeği, köşe yarıçapları, çizgi kalınlıkları.
2. **Taktik tahtası stili:** Harita zemin / duvar / hedef site / etiket renkleri; token, rota, smoke, flash, molly, rakip spotu görünümleri; oynatma sırasında hareket (smoke açılışı, flash patlaması).
3. **Ekranlar:** Yukarıdaki 6 ekranın masaüstü tasarımı; Hazırlık ekranının telefon görünümü.
4. **Bileşenler:** Round sekmeleri, T / CT anahtarı, oynatma kontrolü, oyuncu inceleyicisi ("Şu an / Görev / Eğer…"), metrik satırı (değer + lobi + örneklem), durum rozetleri, yükleme kartı.
5. **Durumlar:** Boş (henüz demo yok), yükleniyor, az örnek uyarısı, hata.
