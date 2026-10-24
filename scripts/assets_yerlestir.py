"""assets/gelen/ içindeki Gemini görsellerini web statik klasörüne yerleştirir ve manifest.json yazar.

Kullanım:
    python scripts/gorsel_uret.py          # SVG seti (logo, rozet 1-10, avatar 1-8) -> static/img
    uv run --with pillow python scripts/assets_yerlestir.py

Davranış:
- giris-kapak.jpg -> 1920 ve 960 genişlikte WebP + JPEG (her biri <= 300 KB) üretilir (Pillow gerekir).
- Eski PNG adları (logo.png, kart-cerceve.png, rozet-N.png, avatar-N.png) gelirse yine kopyalanır.
- assets/gelen/referans/ yalnızca stil kaynağıdır, siteye KOPYALANMAZ.
- manifest.json: static/img içindeki tanınan dosyaların listesi (site yalnız bunları kullanır).
"""
from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GELEN = ROOT / "assets" / "gelen"
HEDEF = ROOT / "src" / "squadmind" / "web" / "static" / "img"
# Manifestte tanınan adlar (PROMPTLAR.md ile aynı)
IZINLI = re.compile(
    r"^(logo(-mark)?\.(png|svg)|kart-cerceve\.png|giris-kapak(-960|-1920)?\.(jpg|png|webp)"
    r"|rozet-(10|[1-9])\.(png|svg)|avatar-[1-8]\.(png|svg))$")
LIMIT = 300 * 1024


def kapak_uret() -> list[str]:
    """giris-kapak.jpg -> web boyutları. Kalite, dosya LIMIT'in altına inene kadar düşürülür."""
    kaynak = GELEN / "giris-kapak.jpg"
    if not kaynak.exists():
        return []
    from PIL import Image  # yalnız bu adımda gerekir
    img = Image.open(kaynak).convert("RGB")
    yazilan: list[str] = []
    for genislik in (1920, 960):
        im = img.resize((genislik, round(img.height * genislik / img.width)), Image.LANCZOS)
        for uzanti, fmt, kalite in (("webp", "WEBP", 82), ("jpg", "JPEG", 80)):
            q = kalite
            while True:
                buf = io.BytesIO()
                kw = {"quality": q, "method": 6} if fmt == "WEBP" else {"quality": q, "optimize": True, "progressive": True}
                im.save(buf, fmt, **kw)
                if buf.tell() <= LIMIT or q <= 40:
                    break
                q -= 4
            ad = f"giris-kapak-{genislik}.{uzanti}"
            (HEDEF / ad).write_bytes(buf.getvalue())
            yazilan.append(f"{ad} ({buf.tell() // 1024} KB, kalite {q})")
    return yazilan


def main() -> None:
    HEDEF.mkdir(parents=True, exist_ok=True)
    for f in sorted(GELEN.glob("*")):
        # ham kapak JPEG'i (2752 px) siteye konmaz; optimize edilen sürümler konur
        if f.is_file() and IZINLI.match(f.name) and not f.name.startswith("giris-kapak"):
            shutil.copy2(f, HEDEF / f.name)
    for satir in kapak_uret():
        print("kapak:", satir)
    liste = sorted(p.name for p in HEDEF.iterdir() if p.is_file() and IZINLI.match(p.name))
    (HEDEF / "manifest.json").write_text(json.dumps({"files": liste}, indent=2), encoding="utf-8")
    print(f"manifest: {len(liste)} dosya")


if __name__ == "__main__":
    main()
