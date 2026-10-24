"""Gemini API ile görsel üretir (marka / atmosfer parçaları: logo, kart çerçevesi, rozet, kapak).

Kullanım:
    python scripts/gen_image.py "<prompt>" assets/gelen/kart_cerceve.png [--model gemini-3.1-flash-image]
        [--aspect 3:4]

Anahtar yalnız .env'deki GEMINI_API_KEY'den okunur (koda / komut satırına yazılmaz). Çıktı PNG;
model birden çok görsel dönerse _2, _3 ekiyle kaydedilir. Telif / marka kuralı: CS2 karakteri,
Valve harita görseli, FACEIT logosu, gerçek silah modeli ÜRETTİRME (CLAUDE.md, Kawanagari rehberi).
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def generate(prompt: str, out: Path, model: str, aspect: str | None) -> list[Path]:
    """Prompt'u gönderir, dönen görselleri `out` (ve _2, _3 …) olarak kaydeder."""
    load_dotenv()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit(".env içinde GEMINI_API_KEY yok")
    config: dict = {"responseModalities": ["IMAGE"]}
    if aspect:
        config["imageConfig"] = {"aspectRatio": aspect}
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config}
    r = httpx.post(API.format(model=model), json=body, headers={"x-goog-api-key": key}, timeout=180)
    if r.status_code != 200:
        # Hata gövdesi anahtar içermez; kısaltılmış yazdır
        raise SystemExit(f"Gemini {r.status_code}: {r.text[:400]}")
    saved: list[Path] = []
    for cand in r.json().get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            data = (part.get("inlineData") or part.get("inline_data") or {}).get("data")
            if not data:
                continue
            path = out if not saved else out.with_name(f"{out.stem}_{len(saved) + 1}{out.suffix}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(data))
            saved.append(path)
    if not saved:
        raise SystemExit(f"Görsel dönmedi: {r.text[:400]}")
    return saved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prompt")
    ap.add_argument("out", type=Path)
    ap.add_argument("--model", default="gemini-3.1-flash-image")
    ap.add_argument("--aspect", default=None, help="ör. 1:1, 3:4, 16:9")
    a = ap.parse_args()
    for p in generate(a.prompt, a.out, a.model, a.aspect):
        print(p)


if __name__ == "__main__":
    sys.exit(main())
