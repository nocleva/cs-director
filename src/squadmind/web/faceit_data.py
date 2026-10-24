"""FACEIT'ten alınan verilerin kaydı (şema v12): her girişte ham yanıtların anlık görüntüsü.

Saklanan: userinfo yanıtı, id_token iddiaları (imza doğrulanmadığı için verified=false),
Data API oyuncu yanıtı. Erişim / yenileme / id token'ın kendisi SAKLANMAZ; yalnız `token_meta`
(kapsam, süre, alınma zamanı). İçerik değişmediyse yeni satır eklenmez.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from squadmind.web.store import Store

SOURCES = ("userinfo", "id_token", "data_api_player")
# Anlık görüntüye asla girmemesi gereken alan adları (savunma: yanıtta anahtar olursa elenir)
_SECRET_KEYS = {"access_token", "refresh_token", "id_token", "token", "client_secret"}


# KVKK / GDPR: varsayılan olarak SAKLANMAYAN kişisel alanlar (FACEIT_STORE_SENSITIVE=1 ile açılır)
SENSITIVE_KEYS = {"given_name", "family_name", "birthdate", "infractions"}


def _scrub(value: Any, drop: set[str]) -> Any:
    """Anahtar benzeri ve (istenirse) hassas alanları iç içe olanlar dahil siler."""
    if isinstance(value, dict):
        return {k: _scrub(v, drop) for k, v in value.items() if str(k).lower() not in drop}
    if isinstance(value, list):
        return [_scrub(v, drop) for v in value]
    return value


def age_18_plus(birthdate: Any, today: date | None = None) -> bool | None:
    """Doğum tarihinden (YYYY-MM-DD) 18 yaş üstü mü; çözülemezse None. Tarihin kendisi saklanmaz."""
    try:
        born = date.fromisoformat(str(birthdate)[:10])
    except ValueError:
        return None
    t = today or date.today()
    return (t.year - born.year - ((t.month, t.day) < (born.month, born.day))) >= 18


def _dump(payload: dict, store_sensitive: bool = False) -> str:
    drop = set(_SECRET_KEYS) | (set() if store_sensitive else SENSITIVE_KEYS)
    clean = _scrub(payload, drop)
    if not store_sensitive and isinstance(payload, dict) and "birthdate" in payload:
        clean["age_18_plus"] = age_18_plus(payload.get("birthdate"))   # yalnız türetilmiş bayrak
    return json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)


def _first(*values: Any) -> str | None:
    for v in values:
        if v not in (None, ""):
            return str(v)
    return None


def record_login(store: Store, account_id: str, faceit_id: str, userinfo: dict,
                 id_claims: dict | None, token_meta: dict | None, profile: dict | None,
                 store_sensitive: bool = False) -> int:
    """Girişte anlık görüntüleri yazar, hesabın son değerlerini ve giriş sayısını günceller.

    Dönen sayı: yeni eklenen satır adedi (içeriği değişmeyenler eklenmez)."""
    meta_text = json.dumps(token_meta, ensure_ascii=False, sort_keys=True) if token_meta else None
    items = [("userinfo", userinfo, True), ("id_token", id_claims, False),
             ("data_api_player", profile, True)]
    added = 0
    with store.cursor() as cur:
        for source, payload, verified in items:
            if not payload:
                continue
            text = _dump(payload, store_sensitive)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            last = cur.execute(
                "SELECT content_hash FROM faceit_identity_snapshots WHERE account_id = ? AND source = ? "
                "ORDER BY taken_at DESC, last_seen_at DESC LIMIT 1", [account_id, source]).fetchone()
            if last and last[0] == digest:
                cur.execute("UPDATE faceit_identity_snapshots SET last_seen_at = now() "
                            "WHERE account_id = ? AND source = ? AND content_hash = ?",
                            [account_id, source, digest])
                continue
            cur.execute(
                "INSERT INTO faceit_identity_snapshots (account_id, faceit_id, taken_at, last_seen_at, "
                "source, payload, content_hash, verified, token_meta) VALUES (?, ?, now(), now(), ?, ?, ?, ?, ?)",
                [account_id, faceit_id, source, text, digest, verified, meta_text])
            added += 1
        prof = profile or {}
        cur.execute(
            "UPDATE accounts SET faceit_nickname = coalesce(?, faceit_nickname), "
            "faceit_avatar = coalesce(?, faceit_avatar), faceit_country = coalesce(?, faceit_country), "
            "faceit_locale = coalesce(?, faceit_locale), token_meta = coalesce(?, token_meta), "
            "login_count = coalesce(login_count, 0) + 1, last_login_at = now() WHERE account_id = ?",
            [_first(prof.get("nickname"), userinfo.get("nickname")),
             _first(prof.get("avatar"), userinfo.get("picture")),
             _first(prof.get("country"), userinfo.get("country")),
             _first(userinfo.get("locale"), prof.get("language")),
             meta_text, account_id])
        # E-posta doğrulaması FACEIT'ten geliyorsa onu yansıt (yalnız hesabın e-postası FACEIT'inkiyle aynıysa)
        if userinfo.get("email_verified") is True and userinfo.get("email"):
            cur.execute("UPDATE accounts SET email_verified = true WHERE account_id = ? AND email = ?",
                        [account_id, str(userinfo["email"]).strip().lower()])
    return added


def latest_snapshots(store: Store, account_id: str) -> list[dict[str, Any]]:
    """Kaynak başına en son anlık görüntü (payload çözülmüş)."""
    with store.cursor() as cur:
        rows = cur.execute(
            "SELECT source, taken_at, last_seen_at, verified, payload, token_meta "
            "FROM faceit_identity_snapshots WHERE account_id = ? ORDER BY taken_at DESC, last_seen_at DESC",
            [account_id]).fetchall()
    out, seen = [], set()
    for source, taken, seen_at, verified, payload, meta in rows:
        if source in seen:
            continue
        seen.add(source)
        out.append({"source": source, "taken_at": taken, "last_seen_at": seen_at,
                    "verified": verified, "payload": json.loads(payload),
                    "token_meta": json.loads(meta) if meta else None})
    return sorted(out, key=lambda s: SOURCES.index(s["source"]) if s["source"] in SOURCES else 9)


def delete_account(store: Store, account_id: str) -> None:
    """Hesabı, FACEIT bağlantısını (web_users satırı) ve anlık görüntüleri siler. Maç / demo
    istatistik tabloları kalır (üçüncü kişilerin verisini de içerir)."""
    with store.cursor() as cur:
        cur.execute("DELETE FROM faceit_identity_snapshots WHERE account_id = ?", [account_id])
        cur.execute("DELETE FROM web_users WHERE account_id = ?", [account_id])
        cur.execute("DELETE FROM accounts WHERE account_id = ?", [account_id])


def export_account(store: Store, account_id: str) -> dict[str, Any]:
    """Kullanıcının kendi verisi (JSON indirme): hesap alanları + tüm anlık görüntüler."""
    with store.cursor() as cur:
        acct = cur.execute(
            "SELECT email, email_verified, created_at, last_login_at, login_count, faceit_nickname, "
            "faceit_avatar, faceit_country, faceit_locale, token_meta FROM accounts WHERE account_id = ?",
            [account_id]).fetchone()
        snaps = cur.execute(
            "SELECT source, taken_at, last_seen_at, verified, payload, token_meta "
            "FROM faceit_identity_snapshots WHERE account_id = ? ORDER BY taken_at", [account_id]).fetchall()
    keys = ("email", "email_verified", "created_at", "last_login_at", "login_count", "faceit_nickname",
            "faceit_avatar", "faceit_country", "faceit_locale", "token_meta")
    account = dict(zip(keys, acct or [None] * len(keys)))
    if account.get("token_meta"):
        account["token_meta"] = json.loads(account["token_meta"])
    return {
        "account": account,
        "faceit_snapshots": [
            {"source": s, "taken_at": t, "last_seen_at": ls, "verified": v, "payload": json.loads(p),
             "token_meta": json.loads(m) if m else None} for s, t, ls, v, p, m in snaps],
    }


def display_fields(payload: dict) -> list[tuple[str, str]]:
    """Sayfada gösterim: (alan, değer); iç içe değerler kısaltılmış JSON."""
    rows = []
    for key in sorted(payload):
        v = payload[key]
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        rows.append((str(key), text if len(text) <= 160 else text[:157] + "..."))
    return rows
