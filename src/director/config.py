"""Proje genelindeki yollar ve ortam değişkenleri."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Proje kök dizini: src/director/config.py → iki üst klasör
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# .env dosyasını proje kökünden yükle (komut hangi klasörden çalışırsa çalışsın)
load_dotenv(PROJECT_ROOT / ".env")

# Veri klasörleri (ortam değişkeniyle değiştirilebilir)
DATA_DIR = Path(os.getenv("DIRECTOR_DATA_DIR", PROJECT_ROOT / "data"))
CACHE_DIR = DATA_DIR / "cache"
DEMOS_INBOX = DATA_DIR / "demos" / "inbox"
DEMOS_PROCESSED = DATA_DIR / "demos" / "processed"
DEMOS_TMP = DATA_DIR / "demos" / "tmp"  # açılmış .dem dosyaları (işlem sonrası silinir)

# `director watch` için izlenen tarayıcı indirme klasörü (Windows'ta "İndirilenler"
# klasörünün gerçek yolu Downloads'tur)
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", Path.home() / "Downloads"))

# Demo işlendikten (ve FACEIT ile doğrulandıktan) sonra orijinal dosya silinsin mi
AUTO_DELETE_DEMOS = os.getenv("DIRECTOR_AUTO_DELETE", "0").strip().lower() in ("1", "true", "evet")
DB_PATH = Path(os.getenv("DIRECTOR_DB", DATA_DIR / "director.duckdb"))
REPORTS_DIR = PROJECT_ROOT / "reports"

# .env.example'daki yer tutucu; gerçek anahtar sanılmasın diye reddediyoruz
_PLACEHOLDER_KEY = "buraya_anahtarin"


def get_api_key() -> str:
    """FACEIT API anahtarını sadece ortamdan / .env'den okur."""
    key = os.getenv("FACEIT_API_KEY", "").strip()
    if not key or key == _PLACEHOLDER_KEY:
        raise RuntimeError(
            "FACEIT_API_KEY bulunamadı. Proje kökündeki .env dosyasına "
            "FACEIT_API_KEY=<server-side anahtarın> satırını ekle."
        )
    return key

# --- Web (Faz 3a: takım demo ekibi) ---------------------------------------------
# Davet kodu: linkte ?davet=<kod>; boşsa web arayüzü katılıma kapalıdır
WEB_INVITE_CODE = os.getenv("WEB_INVITE_CODE", "").strip()
# Oturum çerezini imzalayan gizli anahtar (boşsa her başlatmada rastgele: oturumlar düşer)
WEB_SECRET = os.getenv("WEB_SECRET", "").strip()
# Her oyuncudan istenen demo sayısı (ilerleme çubuğu)
WEB_REQUIRED_DEMOS = int(os.getenv("WEB_REQUIRED_DEMOS", "10"))
# Takım raporu bu oyuncunun takımı için üretilir; analiz bu haritayla sınırlı
WEB_TEAM_OWNER = os.getenv("WEB_TEAM_OWNER", "").strip()
WEB_MAP = os.getenv("WEB_MAP", "de_dust2").strip() or None
# Web'den gelen dosyaların parça parça birleştirildiği klasör
WEB_UPLOADS = DATA_DIR / "web_uploads"
