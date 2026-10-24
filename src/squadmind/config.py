"""Proje genelindeki yollar ve ortam değişkenleri."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Proje kök dizini: src/squadmind/config.py → iki üst klasör
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# .env dosyasını proje kökünden yükle (komut hangi klasörden çalışırsa çalışsın)
load_dotenv(PROJECT_ROOT / ".env")

# Veri klasörleri (ortam değişkeniyle değiştirilebilir)
DATA_DIR = Path(os.getenv("SQUADMIND_DATA_DIR", PROJECT_ROOT / "data"))
CACHE_DIR = DATA_DIR / "cache"
DEMOS_INBOX = DATA_DIR / "demos" / "inbox"
DEMOS_PROCESSED = DATA_DIR / "demos" / "processed"
DEMOS_TMP = DATA_DIR / "demos" / "tmp"  # açılmış .dem dosyaları (işlem sonrası silinir)

# `squadmind watch` için izlenen tarayıcı indirme klasörü (Windows'ta "İndirilenler"
# klasörünün gerçek yolu Downloads'tur)
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", Path.home() / "Downloads"))

# Demo işlendikten (ve FACEIT ile doğrulandıktan) sonra orijinal dosya silinsin mi
AUTO_DELETE_DEMOS = os.getenv("SQUADMIND_AUTO_DELETE", "0").strip().lower() in ("1", "true", "evet")
DB_PATH = Path(os.getenv("SQUADMIND_DB", DATA_DIR / "squadmind.duckdb"))
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
# E-posta+şifre / nickname-davet girişi (varsayılan KAPALI: yalnız FACEIT ile giriş)
WEB_PASSWORD_LOGIN = os.getenv("WEB_PASSWORD_LOGIN", "0").strip().lower() in ("1", "true", "yes")
WEB_MAP = os.getenv("WEB_MAP", "de_dust2").strip() or None
# FACEIT ile giriş (FACEIT Connect): geliştirici portalındaki OAuth2 istemcisi. Boşsa
# nickname ile katılım kullanılır. Dönüş adresi: <WEB_BASE_URL>/giris/faceit/donus
FACEIT_CLIENT_ID = os.getenv("FACEIT_CLIENT_ID", "").strip()
FACEIT_CLIENT_SECRET = os.getenv("FACEIT_CLIENT_SECRET", "").strip()
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "https://app.squadmind.info").strip()
# FACEIT girişinde PKCE ve yetki adresi (bkz. web/faceit_auth.py)
FACEIT_PKCE = os.getenv("FACEIT_PKCE", "1").strip() in ("1", "true", "evet")
FACEIT_FLOW = os.getenv("FACEIT_FLOW", "code").strip() or "code"
FACEIT_AUTHORIZE_URL = os.getenv("FACEIT_AUTHORIZE_URL", "https://accounts.faceit.com").strip()
# FACEIT Downloads API ile "FACEIT'ten getir" ve DEMO_READY webhook'u. Yetki açılana kadar KAPALI
# (arayüzde görünmez, uç noktalar 404). Webhook gizli değeri ayrı: boşsa uç nokta 404.
FACEIT_DOWNLOADS_ENABLED = os.getenv("FACEIT_DOWNLOADS_ENABLED", "0").strip() in ("1", "true", "evet")
FACEIT_WEBHOOK_SECRET = os.getenv("FACEIT_WEBHOOK_SECRET", "").strip()
# App Studio'daki "Header Name" (değeri FACEIT_WEBHOOK_SECRET); sorgu parametresi ?gizli= de kabul edilir
FACEIT_WEBHOOK_HEADER = os.getenv("FACEIT_WEBHOOK_HEADER", "X-Squadmind-Webhook").strip()
# FACEIT girişinde istenen kapsam (e-posta ancak App Studio'da izin verilince eklenir)
# 24.10.2026: "email" istemciye tanımlı değil (FACEIT: "Invalid scope: email"), varsayılan e-postasız
FACEIT_SCOPE = os.getenv("FACEIT_SCOPE", "openid profile").strip() or "openid profile"
# E-posta gönderimi (doğrulama, şifre sıfırlama). SMTP_HOST ya da MAIL_FROM boşsa bu uçlar kapalıdır.
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
MAIL_FROM = os.getenv("MAIL_FROM", "").strip()
# Web'den gelen dosyaların parça parça birleştirildiği klasör
WEB_UPLOADS = DATA_DIR / "web_uploads"
