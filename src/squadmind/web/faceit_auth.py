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

# Keşif belgesindeki yetki adresi (FACEIT Connect; sayfa /accounts'a gider ve onayı
# accounts.faceit.com/api/v1/authorize'a sorar). FACEIT_AUTHORIZE_URL ile değişir.
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
    # PKCE (S256): App Studio'daki tek istemci türü "Authorization Code with PKCE". FACEIT Connect
    # sayfası code_challenge'ı onay isteğine (GET/POST /api/v1/authorize) aynen iletir; Auth.js'in
    # FACEIT sağlayıcısı da PKCE + client_secret_basic kullanır. FACEIT_PKCE=0 ile kapatılır
    pkce: bool = True
    authorize_base: str = AUTHORIZE_URL
    # Akış: "code" (yetki kodu + token adımı, standart) ya da "implicit" (anahtar dönüş adresinin
    # # kısmında). 24.10.2026: eski 401'lerin çoğu geçersiz secret'tandı; varsayılan code
    flow: str = "code"
    # İstenen kapsam. 24.10.2026 kök neden: "email" bu istemciye tanımlı değil; FACEIT Connect
    # sayfası /api/v1/authorize'dan 400 {"message":"Invalid scope: email"} alıyor ve hatayı
    # yalnız Sentry'ye yazıp ekranda hiçbir şey göstermiyor ("Continue as …" tepkisiz).
    # E-posta gerekirse istemciye izin verildikten sonra FACEIT_SCOPE ile eklenir.
    scope: str = "openid profile"

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
            "scope": self.scope,
            "state": state,
        }
        # FACEIT Connect, PKCE türündeki istemcide akıştan bağımsız challenge istiyor
        # (24.10: implicit + challenge'sız → onay tepkisiz, büyük olasılıkla pkce_required)
        if self.pkce:
            params |= {"code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{self.authorize_base}?{urlencode(params)}"

    def _token(self, http: httpx.Client, form: dict[str, str]) -> httpx.Response:
        """Token isteği: gizli anahtar varsa client_secret_basic, yoksa kimlik formda."""
        if self.client_secret:
            return http.post(TOKEN_URL, auth=(self.client_id, self.client_secret), data=form)
        return http.post(TOKEN_URL, data={**form, "client_id": self.client_id})

    def _exchange(self, http: httpx.Client, code: str, verifier: str) -> httpx.Response:
        """Kod → token: client_secret_basic + redirect_uri (+ PKCE açıksa code_verifier).

        Biçim Auth.js'in çalışan FACEIT sağlayıcısıyla aynı (Basic başlık, gövdede grant_type /
        code / redirect_uri / code_verifier). PKCE'li istek 401 alırsa bir kez doğrulayıcısız
        denenir (23.10: doğrulayıcı varken FACEIT secret'a bakmıyor gibiydi); fazlası yok, çünkü
        art arda başarısız token istekleri istemciyi geçici kilitliyor.
        """
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri}
        if not self.pkce:
            tok = self._token(http, form)
            if tok.status_code != 200:
                self._log_token_failure(tok, len(code), "PKCE'siz")
            return tok
        # 24.10.2026 (geçerli secret ile): doğrulayıcısız → 400 "PKCE mismatch" (secret geçerli);
        # doğrulayıcılı her biçim (Basic / gövde / public) → 401. FACEIT'in token sunucusu (Spring)
        # PKCE doğrulamasını istemci kimliği adımında yapar ve uyuşmazlığı 401 döndürür → sorun
        # kimlik değil, doğrulayıcı. Olası neden: Connect sayfası code_challenge_method'u iletmiyor
        # ve sunucu "plain" varsayıyor (doğrulayıcı = challenge). Aynı kodla sırayla: S256
        # doğrulayıcısı → plain (challenge). Kod 401'den sonra geçerli kalıyor (gözlendi).
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        # İlk deneme passport-faceit (amber-gg, 2022) biçimi: Basic başlık + gövdede client_id ve
        # client_secret (passport-oauth2 ikisini birlikte gönderir) — buna doğrulayıcı eklenir
        body_ids = {"client_id": self.client_id, "client_secret": self.client_secret}
        tok = None
        for kind, v, extra in (("passport-faceit biçimi + S256", verifier, body_ids),
                               ("Basic + S256 doğrulayıcı", verifier, {}),
                               ("Basic + plain (challenge)", challenge, {})):
            tok = self._token(http, {**form, **extra, "code_verifier": v})
            if tok.status_code == 200:
                log.info("FACEIT token alındı (%s)", kind)
                return tok
            self._log_token_failure(tok, len(code), kind)
            if tok.status_code not in (400, 401):
                break
        return tok

    @staticmethod
    def _log_token_failure(tok: httpx.Response, code_len: int, kind: str) -> None:
        """Token hatasını günlüğe yazar: durum, WWW-Authenticate ve FACEIT hata gövdesinin başı
        (gövde secret içermez; kodun kendisi yazılmaz, yalnız uzunluğu)."""
        log.warning("FACEIT token yanıtı (%s) %s: %s %s (kod uzunluğu %d)", kind, tok.status_code,
                    tok.headers.get("www-authenticate"), " ".join(tok.text[:200].split()), code_len)

    def check_credentials(self, http: httpx.Client | None = None) -> str:
        # Dikkat (23.10.2026): FACEIT kısa sürede birkaç başarısız token isteğinden sonra
        # istemciyi bir süre kilitliyor (doğru istek de 401). Bu kontrolü seyrek çalıştır;
        # sunucu açılışında ve giriş hatasında otomatik çalışmaz.
        """İstemci kimliği + secret FACEIT'te geçerli mi? Sahte kodla token isteği atar.

        FACEIT token uç noktası (Spring, realm "oauth2/client") kimliği kabul ederse sahte
        kod için 400 (invalid_grant), kimliği reddederse 401 döner. Dönen metin günlüğe
        yazılır (secret içermez).
        """
        # 23.10.2026 (yeni secret ile): doğrulayıcı gönderilince FACEIT secret'a bakmadan PKCE
        # yoluna sapıyor (sahte kodda doğru / yanlış secret ikisi de aynı yanıtı alıyordu); bu
        # yüzden kontrol doğrulayıcısız: doğru secret → 400, yanlış → 401
        # Sahte kod gerçek kodlar gibi kısa ve harf (tire içeren uzun kod kimlikten bağımsız 401 alıyor)
        form = {"grant_type": "authorization_code", "code": "kontrl",
                "redirect_uri": self.redirect_uri}
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
            return ("401: secret reddedildi ya da FACEIT istemciyi geçici kilitledi (kısa sürede çok "
                    "başarısız istek; bekleyip tekrar dene)")
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
