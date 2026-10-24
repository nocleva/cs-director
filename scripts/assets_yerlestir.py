"""assets/gelen/ içindeki Gemini görsellerini web statik klasörüne kopyalar ve manifest.json yazar.

Kullanım: python scripts/assets_yerlestir.py
Dosya adları assets/PROMPTLAR.md'de. Tanımsız adlar atlanır (yanlış dosya sitede görünmesin).
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GELEN = ROOT / "assets" / "gelen"
HEDEF = ROOT / "src" / "squadmind" / "web" / "static" / "img"
# Beklenen adlar (PROMPTLAR.md ile aynı)
IZINLI = re.compile(r"^(logo\.png|kart-cerceve\.png|giris-kapak\.(jpg|png)|rozet-(10|[1-9])\.png|avatar-[1-8]\.png)$")


def main() -> None:
    HEDEF.mkdir(parents=True, exist_ok=True)
    kopyalanan: list[str] = []
    atlanan: list[str] = []
    for f in sorted(GELEN.glob("*")):
        if f.is_file() and IZINLI.match(f.name):
            shutil.copy2(f, HEDEF / f.name)          # izinli ad: statik klasöre kopyala
            kopyalanan.append(f.name)
        elif f.is_file() and f.name != ".gitkeep":
            atlanan.append(f.name)
    (HEDEF / "manifest.json").write_text(json.dumps({"files": kopyalanan}, indent=2), encoding="utf-8")
    print(f"{len(kopyalanan)} dosya kopyalandı: {', '.join(kopyalanan) or '-'}")
    if atlanan:
        print("Adı tanınmadığı için atlandı:", ", ".join(atlanan))


if __name__ == "__main__":
    main()
