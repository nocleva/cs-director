"""FACEIT Data API v4 için ince istemci: disk cache + rate limit + 429 backoff."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from director.config import CACHE_DIR, get_api_key

BASE_URL = "https://open.faceit.com/data/v4"

# Cache süreleri (saniye). None = kalıcı (biten maçın verisi değişmez)
PROFILE_TTL: float | None = 3600  # oyuncu profili 1 saat geçerli
HISTORY_TTL: float | None = 600  # maç geçmişi 10 dk (yeni maç gelebilir)
PERMANENT: float | None = None

# FACEIT history endpoint'i tek istekte en fazla 100 kayıt döner
HISTORY_PAGE_MAX = 100


class FaceitAPIError(Exception):
    """FACEIT API'den başarısız yanıt geldiğinde fırlatılır."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"FACEIT API hatası {status_code}: {message}")
        self.status_code = status_code


class FaceitNotFound(FaceitAPIError):
    """404: oyuncu / maç / istatistik bulunamadı."""


class FaceitClient:
    """FACEIT Data API v4 istemcisi.

    Tüm GET yanıtları `cache_dir` altında JSON olarak saklanır; böylece aynı veri
    için API'ye tekrar gidilmez ve gerçek yanıtların şekli diskten incelenebilir.
    """

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: Path = CACHE_DIR,
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        min_interval: float = 0.15,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Anahtar verilmediyse .env'den oku (koda asla yazılmaz)
        self.api_key = api_key or get_api_key()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.min_interval = min_interval
        self._sleep = sleep
        self._last_request_at = 0.0
        # Gerçek ağa çıkan istek sayısı (testlerde cache'i doğrulamak için)
        self.request_count = 0
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            timeout=30.0,
            transport=transport,
        )

    # --- yaşam döngüsü -------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> FaceitClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- cache ---------------------------------------------------------------

    def cache_path(self, path: str, params: dict[str, Any] | None = None) -> Path:
        """İstek için okunabilir + benzersiz cache dosya yolu üretir."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = path + "?" + urlencode(sorted(params.items()))
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        # Dosya adında sadece güvenli karakterler kalsın
        slug = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")[:80]
        return self.cache_dir / f"{slug}__{digest}.json"

    def _read_cache(self, file: Path, ttl: float | None) -> Any | None:
        if not file.exists():
            return None
        # TTL varsa dosyanın yaşına bak; süresi geçmişse yok say
        if ttl is not None and time.time() - file.stat().st_mtime > ttl:
            return None
        try:
            return json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Bozuk cache dosyası: API'den yeniden çekilecek
            return None

    def _write_cache(self, file: Path, data: Any) -> None:
        # Önce geçici dosyaya yaz, sonra taşı: yarım yazılmış cache oluşmasın
        tmp = file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(file)

    # --- HTTP ----------------------------------------------------------------

    def _throttle(self) -> None:
        """İstekler arasında en az `min_interval` saniye bekle (nazik rate limit)."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            self._sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _backoff_delay(self, response: httpx.Response, attempt: int) -> float:
        """Retry-After başlığı varsa ona uy, yoksa üstel bekleme (1, 2, 4, 8...)."""
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), self.backoff_max)
            except ValueError:
                pass
        return min(self.backoff_base * (2**attempt), self.backoff_max)

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        ttl: float | None,
        use_cache: bool = True,
    ) -> Any:
        """Cache'li GET. 429 ve 5xx yanıtlarında exponential backoff ile tekrar dener."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        file = self.cache_path(path, params)

        if use_cache:
            cached = self._read_cache(file, ttl)
            if cached is not None:
                return cached

        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.request_count += 1
            response = self._http.get(path, params=params)

            # Geçici hatalar: bekle ve tekrar dene
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    self._sleep(self._backoff_delay(response, attempt))
                    continue
                break

            if response.status_code == 404:
                raise FaceitNotFound(404, f"{path} bulunamadı")
            if response.status_code >= 400:
                raise FaceitAPIError(response.status_code, response.text[:300])

            data = response.json()
            self._write_cache(file, data)
            return data

        status = response.status_code if response is not None else -1
        raise FaceitAPIError(status, f"{path}: {self.max_retries} denemeden sonra vazgeçildi")

    # --- endpoint'ler --------------------------------------------------------

    def get_player_by_nickname(self, nickname: str, game: str = "cs2") -> dict[str, Any]:
        """Nickname ile oyuncu profili (ELO, level, steam id). 1 saat cache'lenir."""
        return self._get("/players", {"nickname": nickname, "game": game}, ttl=PROFILE_TTL)

    def get_player(self, player_id: str) -> dict[str, Any]:
        """FACEIT player_id ile oyuncu profili. 1 saat cache'lenir."""
        return self._get(f"/players/{player_id}", ttl=PROFILE_TTL)

    def get_player_history(
        self,
        player_id: str,
        limit: int = 30,
        game: str = "cs2",
        page_size: int = HISTORY_PAGE_MAX,
    ) -> list[dict[str, Any]]:
        """Oyuncunun son `limit` maçını sayfalayarak döner (en yeni önce)."""
        page_size = max(1, min(page_size, HISTORY_PAGE_MAX))
        items: list[dict[str, Any]] = []
        offset = 0
        while len(items) < limit:
            want = min(page_size, limit - len(items))
            page = self._get(
                f"/players/{player_id}/history",
                {"game": game, "offset": offset, "limit": want},
                ttl=HISTORY_TTL,
            )
            batch = page.get("items") or []
            items.extend(batch)
            # İstenenden az geldiyse geçmişin sonuna ulaştık
            if len(batch) < want:
                break
            offset += len(batch)
        return items[:limit]

    def get_match_details(self, match_id: str) -> dict[str, Any]:
        """Maç detayı (takımlar, kadro, skor, harita, demo URL'si)."""
        data = self._get(f"/matches/{match_id}", ttl=PERMANENT)
        # Bitmemiş maçı kalıcı cache'te tutma: sonuç/demo daha sonra gelecek
        if str(data.get("status", "")).upper() != "FINISHED":
            self.cache_path(f"/matches/{match_id}").unlink(missing_ok=True)
        return data

    def get_match_stats(self, match_id: str) -> dict[str, Any]:
        """Maç istatistikleri (oyuncu başı K/D, ADR, HS% ...). Kalıcı cache."""
        return self._get(f"/matches/{match_id}/stats", ttl=PERMANENT)
