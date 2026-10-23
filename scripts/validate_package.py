"""WASM paketi doğrulaması: bugünkü awpy yolu ile paket yolunun çıktılarını karşılaştırır.

Her demo için:
  1. .dem.zst açılır (geçici klasöre; kaynak demoya dokunulmaz),
  2. `wasm/awpy-wasm/node_pack.js` ile WASM paketi üretilir (süre / bellek / boyut ölçülür),
  3. `parse_demo(dem)` ve `parse_package(paket)` çalıştırılır,
  4. `DemoTables`'ın HER tablosu karşılaştırılır: satır sayısı, sütunlar, dtype, değerler.

Karşılaştırma sınıfları (tablo başına):
  tam        satır sırası dahil birebir aynı
  sirasiz    aynı satır kümesi, yalnız sıra farklı (awpy'nin group_by sırası belirsiz olan tablolar)
  tolerans   sayısal değerler 1e-6 içinde aynı
  FARK       gerçek fark (ayrıntı yazılır)

Kullanım:
  python scripts/validate_package.py [--demos DIZIN_VEYA_DOSYA ...] [--limit N]
         [--workdir DIR] [--keep] [--self-check] [--json rapor.json]

DB'ye dokunmaz; tüm tablolar bellekte üretilir.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import fields
from pathlib import Path

import polars as pl
import zstandard

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from squadmind.parse.demo_parser import DemoTables, parse_demo  # noqa: E402
from squadmind.parse.package import RAW_TABLES, package_demo, parse_package  # noqa: E402

NODE_PACK = ROOT / "wasm" / "awpy-wasm" / "node_pack.js"
DEFAULT_DEMOS = Path.home() / "squadmind-data" / "demos" / "processed"
TOL = 1e-6


def compare_frames(a: pl.DataFrame | None, b: pl.DataFrame | None) -> tuple[str, str]:
    """İki tabloyu karşılaştırır; (sınıf, ayrıntı) döner."""
    if a is None or b is None:
        return ("tam", "") if a is None and b is None else ("FARK", "biri None")
    if a.columns != b.columns:
        return "FARK", f"sütunlar: {a.columns} != {b.columns}"
    if dict(a.schema) != dict(b.schema):
        diff = [f"{c}: {a.schema[c]} != {b.schema[c]}" for c in a.columns if a.schema[c] != b.schema[c]]
        return "FARK", "dtype: " + "; ".join(diff)
    if a.height != b.height:
        return "FARK", f"satır: {a.height} != {b.height}"
    if a.equals(b):
        return "tam", ""
    # Sıra farkı: tüm sütunlara göre sıralayıp tekrar dene
    try:
        sa, sb = a.sort(a.columns), b.sort(b.columns)
        if sa.equals(sb):
            return "sirasiz", ""
    except Exception:  # sıralanamayan tür varsa tolerans yoluna düş
        sa, sb = a, b
    # Sayısal tolerans
    try:
        from polars.testing import assert_frame_equal

        assert_frame_equal(sa, sb, check_exact=False, abs_tol=TOL, rel_tol=TOL)
        return "tolerans", ""
    except AssertionError as exc:
        return "FARK", str(exc).replace("\n", " ")[:300]


def compare_tables(a: DemoTables, b: DemoTables) -> dict[str, tuple[str, str]]:
    """DemoTables'ın tüm alanlarını karşılaştırır."""
    out: dict[str, tuple[str, str]] = {}
    for f in fields(DemoTables):
        va, vb = getattr(a, f.name), getattr(b, f.name)
        if isinstance(va, pl.DataFrame) or va is None:
            out[f.name] = compare_frames(va, vb)
        else:
            same = va == vb
            out[f.name] = ("tam", "") if same else ("FARK", f"{va!r} != {vb!r}")
    return out


def compare_raw(dem: Path, pkg: Path) -> dict[str, tuple[str, str]]:
    """awpy `Demo` ile `PackageDemo`'nun ham veri setlerini karşılaştırır (indirgenenler hariç)."""
    from awpy import Demo

    real, fake = Demo(dem), package_demo(pkg)
    out: dict[str, tuple[str, str]] = {}
    for name in RAW_TABLES:
        out[f"ham:{name}"] = compare_frames(getattr(real, name), getattr(fake, name))
    out["ham:snapshots"] = compare_frames(real.snapshots(seconds=2.0), fake.snapshots(seconds=2.0))
    return out


def decompress(src: Path, dst: Path) -> None:
    """`.dem.zst` → `.dem`"""
    with src.open("rb") as fi, dst.open("wb") as fo:
        zstandard.ZstdDecompressor().copy_stream(fi, fo)


def find_demos(args: list[str]) -> list[Path]:
    """Dosya ya da klasörlerden demo listesi."""
    roots = [Path(a) for a in args] or [DEFAULT_DEMOS]
    demos: list[Path] = []
    for r in roots:
        demos += sorted(r.glob("*.dem*")) if r.is_dir() else [r]
    return demos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--demos", nargs="*", default=[], help="demo dosyaları ya da klasörler")
    ap.add_argument("--limit", type=int, default=0, help="en fazla N demo")
    ap.add_argument("--workdir", default=str(ROOT / "data" / "tmp_validate"))
    ap.add_argument("--keep", action="store_true", help="açılmış .dem ve paketleri silme")
    ap.add_argument("--self-check", action="store_true",
                    help="parse_demo'yu iki kez çalıştırıp awpy'nin kendi belirsizliğini ölç")
    ap.add_argument("--json", default="", help="ayrıntılı raporu bu dosyaya yaz")
    args = ap.parse_args()

    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    demos = find_demos(args.demos)
    if args.limit:
        demos = demos[: args.limit]

    report: list[dict] = []
    for demo in demos:
        name = demo.name.removesuffix(".zst").removesuffix(".gz")
        dem = work / name
        pkg = work / (name + ".pkg.json.gz")
        print(f"\n== {demo.name}", flush=True)

        # 1) WASM paketi (Node)
        proc = subprocess.run(
            ["node", str(NODE_PACK), str(demo), str(pkg)],
            capture_output=True, text=True, encoding="utf-8",
        )
        if proc.returncode != 0:
            print("  WASM HATASI:", proc.stderr.strip()[:500])
            report.append({"demo": demo.name, "error": proc.stderr.strip()[:500]})
            continue
        m = json.loads(proc.stdout.strip().splitlines()[-1])
        print(f"  WASM {m['wasm_s']:.1f} sn · tepe bellek {m['max_rss_mb']} MB · "
              f"paket {m['json_bytes'] / 1e6:.2f} MB ham / {m['gzip_bytes'] / 1e6:.2f} MB gzip")

        # 2) awpy yolu (yerel, çok çekirdek) ve paket yolu
        if not dem.exists():
            decompress(demo, dem) if demo.suffix == ".zst" else dem.write_bytes(demo.read_bytes())
        t = time.perf_counter()
        ref = parse_demo(dem)
        t_ref = time.perf_counter() - t
        t = time.perf_counter()
        got = parse_package(pkg)
        t_pkg = time.perf_counter() - t

        result = compare_tables(ref, got)
        result.update(compare_raw(dem, pkg))
        entry: dict = {"demo": demo.name, "wasm": m, "t_awpy_s": t_ref, "t_package_s": t_pkg,
                       "tables": {k: {"sinif": v[0], "ayrinti": v[1]} for k, v in result.items()}}
        if args.self_check:
            ref2 = parse_demo(dem)
            entry["self_check"] = {k: v[0] for k, v in compare_tables(ref, ref2).items()}
        report.append(entry)

        print(f"  parse_demo {t_ref:.1f} sn · parse_package {t_pkg:.1f} sn")
        for k, (cls, detail) in result.items():
            rows = getattr(ref, k).height if isinstance(getattr(ref, k, None), pl.DataFrame) else "-"
            extra = f" [{detail}]" if detail else ""
            sc = (f"  (awpy kendi içinde: {entry['self_check'][k]})"
                  if args.self_check and k in entry["self_check"] else "")
            print(f"    {k:<13} {cls:<9} satır {rows}{extra}{sc}")

        if not args.keep:
            dem.unlink(missing_ok=True)
            pkg.unlink(missing_ok=True)

    # Özet: tablo başına sınıf sayıları
    ok = [r for r in report if "tables" in r]
    print(f"\n=== ÖZET: {len(ok)}/{len(report)} demo karşılaştırıldı")
    if ok:
        names = list(ok[0]["tables"])
        for n in names:
            counts: dict[str, int] = {}
            for r in ok:
                c = r["tables"][n]["sinif"]
                counts[c] = counts.get(c, 0) + 1
            print(f"  {n:<13} " + "  ".join(f"{c}={v}" for c, v in sorted(counts.items())))
        w = [r["wasm"] for r in ok]
        print(f"  WASM süre ort {sum(x['wasm_s'] for x in w) / len(w):.1f} sn "
              f"(maks {max(x['wasm_s'] for x in w):.1f}) · tepe bellek maks "
              f"{max(x['max_rss_mb'] for x in w)} MB · paket gzip ort "
              f"{sum(x['gzip_bytes'] for x in w) / len(w) / 1e6:.2f} MB "
              f"(maks {max(x['gzip_bytes'] for x in w) / 1e6:.2f}) · ham JSON maks "
              f"{max(x['json_bytes'] for x in w) / 1e6:.1f} MB")
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    bad = [r for r in ok if any(t["sinif"] == "FARK" for t in r["tables"].values())]
    return 1 if bad or len(ok) != len(report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
