"""Düello animasyonu modülü: API, sonuç dürüstlüğü, Node duman testi."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "src/squadmind/web/static/js/sm-duel-anim.js"


def test_module_exports_playduel():
    src = MOD.read_text(encoding="utf-8")
    assert re.search(r"export function playDuel\(", src)
    assert len(src.encode()) < 16_000


def test_no_probability_logic_or_untouched_random():
    # Modül olasılık hesaplamaz / göstermez; rastgelelik yalnız tohumlu PRNG
    src = MOD.read_text(encoding="utf-8").lower()
    for bad in ("prob", "winner", "percent", "math.random"):
        assert bad not in src, bad
    assert "tofixed" not in src  # sayı biçimleme (yüzde gösterimi) yok


def test_outcome_option_present():
    src = MOD.read_text(encoding="utf-8")
    assert "outcome" in src and "'a'" in src and "'b'" in src


def test_demo_page_exists():
    html = (ROOT / "src/squadmind/web/static/anim-demo.html").read_text(encoding="utf-8")
    assert "sm-duel-anim.js" in html and "outcome" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node yok")
def test_node_syntax_and_smoke():
    r = subprocess.run(["node", "--check", str(MOD)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run(["node", str(ROOT / "tests/js/duel_anim_smoke.mjs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
