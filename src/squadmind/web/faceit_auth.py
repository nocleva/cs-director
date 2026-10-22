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

AUTHORIZE_URL = "https://accounts.faceit.com/accounts"
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
    # PKCE: gizli anahtarlı istemcide zorunlu değil. FACEIT gerçek kodla doğrulayıcıyı
    # 401 ile reddetti (23.10.2026), bu yüzden varsayılan kapalı; FACEIT_PKCE=1 ile açılır
    pkce: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.base_url)

    @property
    def redirect_uri(self) -> str:
        return self.base_url.rstrip("/") + CALLBACK_PATH

    def authorize_url(self, state: str, challenge: str) -> str:
        """Kullanıcının yönlendirileceği FACEIT giriş sayfası."""
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            # FACEIT: true = giriş sonrası açılan sayfayı dönüş adresine yönlendir (false hata verir)
            "redirect_popup": "true",
            "scope": "openid profile",
            "state": state,
        }
        if self.pkce:
            params |= {"code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def _token(self, http: httpx.Client, form: dict[str, str]) -> httpx.Response:
        """Token isteği: gizli anahtar varsa client_secret_basic, yoksa kimlik formda."""
        if self.client_secret:
            return http.post(TOKEN_URL, auth=(self.client_id, self.client_secret), data=form)
        return http.post(TOKEN_URL, data={**form, "client_id": self.client_id})

    def fetch_user(self, code: str, verifier: str) -> dict[str, Any]:
        """Yetki kodu → erişim anahtarı → kullanıcı bilgisi (en az `guid`)."""
        try:
            with httpx.Client(timeout=self.timeout) as http:
                form = {"grant_type": "authorization_code", "code": code,
                        "redirect_uri": self.redirect_uri}
                if self.pkce:
                    form["code_verifier"] = verifier
                tok = self._token(http, form)
                if tok.status_code != 200:
                    # Sebep günlükte görünsün (gizli bilgi içermez: kod tek kullanımlık, süresi dolmuş)
                    log.warning("FACEIT token yanıtı %s: %s %s", tok.status_code,
                                tok.headers.get("www-authenticate"), tok.text[:300])
                    raise FaceitAuthError(f"FACEIT giriş anahtarı verilmedi ({tok.status_code}).")
                access = tok.json().get("access_token")
                info = http.get(USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
                if info.status_code != 200:
                    raise FaceitAuthError(f"FACEIT kullanıcı bilgisi alınamadı ({info.status_code}).")
                user = info.json()
        except httpx.HTTPError as exc:
            raise FaceitAuthError(f"FACEIT'e ulaşılamadı: {exc}") from exc
        if not user.get("guid"):
            raise FaceitAuthError("FACEIT kullanıcı kimliği gelmedi.")
        return user
