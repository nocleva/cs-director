"""FACEIT ile giriş (FACEIT Connect: OAuth2 yetki kodu + PKCE).

Uç noktalar FACEIT'in OpenID keşif belgesinden
(https://api.faceit.com/auth/v1/openid_configuration): istemci kimliği / gizli anahtarı
FACEIT geliştirici portalında (App Studio → OAuth2 Client) oluşturulur, dönüş adresi
orada `<WEB_BASE_URL>/giris/faceit/donus` olarak kayıtlı olmalı. Kullanıcı bilgisinden
sadece FACEIT oyuncu kimliği (guid) alınır; nickname, Steam hesabı ve seviye Data
API'den gelir. Böylece kullanıcı nickname yazmaz, başkasının adıyla katılamaz.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

# Keşif belgesindeki yetki adresi. Eski "FACEIT Connect" sayfası (/accounts) kodu veriyor ama
# PKCE bilgisini kayda geçirmiyor gibi: token adımı 401 (23.10.2026). FACEIT_AUTHORIZE_URL ile değişir.
AUTHORIZE_URL = "https://accounts.faceit.com"
TOKEN_URL = "https://api.faceit.com/auth/v1/oauth/token"
USERINFO_URL = "https://api.faceit.com/auth/v1/resources/userinfo"
CALLBACK_PATH = "/giris/faceit/donus"

log = logging.getLogger("squadmind.web.faceit")


class FaceitAuthError(Exception):
    """FACEIT girişi tamamlanamadı (kullanıcıya gösterilecek mesajla)."""


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge): S256, RFC 7636."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@dataclass
class FaceitOAuth:
    client_id: str
    client_secret: str          # "Authorization Code with PKCE" istemcisinde boş olabilir
    base_url: str               # ör. https://app.squadmind.info (dönüş adresi bundan)
    timeout: float = 15.0
    # PKCE: FACEIT'in "Authorization Code with PKCE" istemcisi token adımında doğrulayıcı istiyor
    # (doğrulayıcısız istek doğru gizli anahtarla da 401); FACEIT_PKCE=0 ile kapatılır
    pkce: bool = True
    authorize_base: str = AUTHORIZE_URL
    # Akış: "implicit" (FACEIT erişim anahtarını dönüş adresinin # kısmında verir; token adımı
    # yok) ya da "code" (yetki kodu + token adımı). 23.10.2026: code akışında token adımı her
    # biçimde 401 verdiği için varsayılan implicit; FACEIT_FLOW ile değişir
    flow: str = "implicit"

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.base_url)

    @property
    def redirect_uri(self) -> str:
        return self.base_url.rstrip("/") + CALLBACK_PATH

    def authorize_url(self, state: str, challenge: str) -> str:
        """Kullanıcının yönlendirileceği FACEIT giriş sayfası."""
        params = {
            "response_type": "code" if self.flow == "code" else "token",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            # FACEIT: true = giriş sonrası açılan sayfayı dönüş adresine yönlendir (false hata verir)
            "redirect_popup": "true",
            "scope": "openid profile",
            "state": state,
        }
        if self.flow == "code" and self.pkce:
            params |= {"code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{self.authorize_base}?{urlencode(params)}"

    def _token(self, http: httpx.Client, form: dict[str, str]) -> httpx.Response:
        """Token isteği: gizli anahtar varsa client_secret_basic, yoksa kimlik formda."""
        if self.client_secret:
            return http.post(TOKEN_URL, auth=(self.client_id, self.client_secret), data=form)
        return http.post(TOKEN_URL, data={**form, "client_id": self.client_id})

    def _exchange(self, http: httpx.Client, code: str, verifier: str) -> httpx.Response:
        """Kod → token (standart: client_secret_basic + code_verifier + redirect_uri).

        23.10.2026: FACEIT bu istemciyle gerçek kodu her biçimde 401 ile reddetti (6 biçim
        denendi: secret Basic / gövdede, PKCE'li / PKCE'siz, redirect'li / redirect'siz).
        Sahte kodla aynı istek 400 (kimlik tanınıyor). FACEIT desteğine soruldu; cevap gelene
        kadar giriş davet koduyla da yapılabilir.
        """
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri}
        if self.pkce:
            form["code_verifier"] = verifier
        tok = self._token(http, form)
        if tok.status_code != 200:
            log.warning("FACEIT token yanıtı %s: %s (kod uzunluğu %d)", tok.status_code,
                        tok.headers.get("www-authenticate"), len(code))
            if tok.status_code == 401:
                # 401 iki şey olabilir: istemci kimliği / secret reddedildi ya da kod bu
                # istemciye ait değil. Sahte kodlu kontrol isteği ikisini ayırır
                log.warning("FACEIT kimlik kontrolü: %s", self.check_credentials(http))
        return tok

    def check_credentials(self, http: httpx.Client | None = None) -> str:
        """İstemci kimliği + secret FACEIT'te geçerli mi? Sahte kodla token isteği atar.

        FACEIT token uç noktası (Spring, realm "oauth2/client") kimliği kabul ederse sahte
        kod için 400 (invalid_grant), kimliği reddederse 401 döner. 23.10.2026 ölçümü:
        doğrulayıcısız istek doğru secret'la da 401, bu yüzden istekte PKCE doğrulayıcısı
        da var. Dönen metin günlüğe yazılır (secret içermez).
        """
        form = {"grant_type": "authorization_code", "code": "squadmind-kontrol",
                "redirect_uri": self.redirect_uri,
                "code_verifier": secrets.token_urlsafe(48)}
        try:
            if http is None:
                with httpx.Client(timeout=self.timeout) as own:
                    r = self._token(own, form)
            else:
                r = self._token(http, form)
        except httpx.HTTPError as exc:
            return f"ulaşılamadı ({exc})"
        if r.status_code == 400:
            return "400: kimlik geçerli (sahte kod reddedildi) → sorun gerçek kodda"
        if r.status_code == 401:
            return "401: istemci kimliği / secret reddedildi → App Studio'da secret'ı yenileyip .env'ye yaz"
        return f"{r.status_code}: beklenmeyen yanıt"

    def fetch_user(self, code: str, verifier: str) -> dict[str, Any]:
        """Yetki kodu → erişim anahtarı → kullanıcı bilgisi (en az `guid`)."""
        try:
            with httpx.Client(timeout=self.timeout) as http:
                tok = self._exchange(http, code, verifier)
                if tok.status_code != 200:
                    raise FaceitAuthError(f"FACEIT giriş anahtarı verilmedi ({tok.status_code}).")
                access = tok.json().get("access_token")
        except httpx.HTTPError as exc:
            raise FaceitAuthError(f"FACEIT'e ulaşılamadı: {exc}") from exc
        return self.user_from_token(access)

    def user_from_token(self, access_token: str) -> dict[str, Any]:
        """Erişim anahtarı → kullanıcı bilgisi (en az `guid`). Anahtar FACEIT'e sorularak
        doğrulanır: sahte ya da süresi dolmuş anahtar kimlik vermez."""
        if not access_token:
            raise FaceitAuthError("FACEIT erişim anahtarı gelmedi.")
        try:
            with httpx.Client(timeout=self.timeout) as http:
                info = http.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as exc:
            raise FaceitAuthError(f"FACEIT'e ulaşılamadı: {exc}") from exc
        if info.status_code != 200:
            log.warning("FACEIT userinfo yanıtı %s", info.status_code)
            raise FaceitAuthError(f"FACEIT kullanıcı bilgisi alınamadı ({info.status_code}).")
        user = info.json()
        if not user.get("guid"):
            raise FaceitAuthError("FACEIT kullanıcı kimliği gelmedi.")
        return user
