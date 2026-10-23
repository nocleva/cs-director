"""Hesaplar: e-posta + şifre, FACEIT bağlantısı, hız sınırı.

Model: `accounts` (e-posta, şifre karması) ve `web_users` (FACEIT oyuncusu). Bir FACEIT hesabı
(web_users satırı) en fazla bir hesaba bağlıdır (`account_id` tek kolon); bir hesapta en fazla
bir FACEIT vardır (`link_user` kontrol eder). `faceit_link`: 'dogrulanmis' (FACEIT girişiyle
kanıtlandı) ya da 'dogrulanmamis' (nickname yazıldı, sahipliği kanıtlanmadı).
"""

from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from squadmind.web.store import Store

try:  # tercih: argon2id; yoksa scrypt (standart kütüphane)
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError
    _PH: PasswordHasher | None = PasswordHasher()
except ImportError:  # pragma: no cover - argon2-cffi bağımlılıktır, yedek yol
    _PH = None

VERIFIED, UNVERIFIED = "dogrulanmis", "dogrulanmamis"
MIN_PASSWORD = 10
MAX_PASSWORD = 200  # çok uzun girişle işlemciyi yormayı engeller
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AccountError(Exception):
    """Kullanıcıya gösterilecek hesap hatası."""


@dataclass
class Account:
    account_id: str
    email: str | None
    password_hash: str | None
    email_verified: bool
    created_at: datetime | None = None
    last_login_at: datetime | None = None


_COLS = "account_id, email, password_hash, coalesce(email_verified, false), created_at, last_login_at"


# --- e-posta ve şifre ----------------------------------------------------------------

def normalize_email(raw: str) -> str:
    """Küçük harf + kırpılmış; geçersizse AccountError."""
    email = (raw or "").strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise AccountError("Geçerli bir e-posta adresi yaz.")
    return email


def check_password_policy(password: str, email: str | None = None) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Şifre en az {MIN_PASSWORD} karakter olmalı.")
    if len(password) > MAX_PASSWORD:
        raise AccountError(f"Şifre en fazla {MAX_PASSWORD} karakter olabilir.")
    if email and password.strip().lower() == email:
        raise AccountError("Şifre e-posta adresinle aynı olamaz.")


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    if _PH is not None:
        return _PH.hash(password)
    salt = os.urandom(16)  # yedek biçim: scrypt$<tuz b64>$<karma b64>
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(_scrypt(password, salt)).decode()


def verify_password(password: str, stored: str | None) -> bool:
    """Sabit zamanlı doğrulama; karma yoksa / bozuksa yanlış."""
    if not stored:
        return False
    if stored.startswith("scrypt$"):
        try:
            _, salt_b64, h_b64 = stored.split("$")
            return hmac.compare_digest(_scrypt(password, base64.b64decode(salt_b64)),
                                       base64.b64decode(h_b64))
        except (ValueError, TypeError):
            return False
    if _PH is None:
        return False
    try:
        return _PH.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


# Var olmayan e-postada da aynı işi yapıp süre farkından hesap varlığı sızmasın
DUMMY_HASH = hash_password("kukla-sifre-zamanlama")


def reset_fingerprint(password_hash: str | None) -> str:
    """Şifre sıfırlama bağlantısını tek kullanımlık yapar: karma değişince parmak izi değişir."""
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:16]


# --- veritabanı ------------------------------------------------------------------------

def _row(row: Any) -> Account | None:
    return Account(*row) if row else None


def by_email(store: Store, email: str) -> Account | None:
    with store.cursor() as cur:
        return _row(cur.execute(f"SELECT {_COLS} FROM accounts WHERE email = ?", [email]).fetchone())


def by_id(store: Store, account_id: str) -> Account | None:
    with store.cursor() as cur:
        return _row(cur.execute(f"SELECT {_COLS} FROM accounts WHERE account_id = ?",
                                [account_id]).fetchone())


def account_of(store: Store, steam_id: str) -> tuple[Account, str | None] | None:
    """Oyuncunun bağlı olduğu hesap ve bağlantı durumu."""
    with store.cursor() as cur:
        row = cur.execute(
            "SELECT a.account_id, a.email, a.password_hash, coalesce(a.email_verified, false), "
            "a.created_at, a.last_login_at, w.faceit_link FROM web_users w "
            "JOIN accounts a ON a.account_id = w.account_id WHERE w.steam_id = ?",
            [steam_id]).fetchone()
    return (Account(*row[:6]), row[6]) if row else None


def steam_of(store: Store, account_id: str) -> str | None:
    """Hesaba bağlı FACEIT oyuncusunun steam_id'si."""
    with store.cursor() as cur:
        row = cur.execute("SELECT steam_id FROM web_users WHERE account_id = ?",
                          [account_id]).fetchone()
    return row[0] if row else None


def create(store: Store, email: str | None, password_hash: str | None, verified: bool) -> str:
    """Yeni hesap; e-posta zaten kayıtlıysa AccountError."""
    account_id = uuid.uuid4().hex
    with store.cursor() as cur:
        if email and cur.execute("SELECT 1 FROM accounts WHERE email = ?", [email]).fetchone():
            raise AccountError("Bu e-posta ile zaten bir hesap var.")
        cur.execute("INSERT INTO accounts (account_id, email, password_hash, email_verified) "
                    "VALUES (?, ?, ?, ?)", [account_id, email, password_hash, verified])
    return account_id


def link_user(store: Store, account_id: str, steam_id: str, link: str) -> None:
    """FACEIT oyuncusunu hesaba bağlar. Bir FACEIT en fazla bir hesapta, bir hesap en fazla
    bir FACEIT'te: aksi AccountError. Bağlantı 'dogrulanmis' iken geriye düşmez."""
    with store.cursor() as cur:
        row = cur.execute("SELECT account_id, faceit_link FROM web_users WHERE steam_id = ?",
                          [steam_id]).fetchone()
        if row is None:
            raise AccountError("FACEIT oyuncusu sitede kayıtlı değil.")
        if row[0] and row[0] != account_id:
            raise AccountError("Bu FACEIT hesabı başka bir hesaba bağlı.")
        other = cur.execute("SELECT steam_id FROM web_users WHERE account_id = ? AND steam_id <> ?",
                            [account_id, steam_id]).fetchone()
        if other:
            raise AccountError("Bu hesapta zaten başka bir FACEIT hesabı bağlı.")
        if row[1] == VERIFIED:
            link = VERIFIED
        cur.execute("UPDATE web_users SET account_id = ?, faceit_link = ? WHERE steam_id = ?",
                    [account_id, link, steam_id])


def set_password(store: Store, account_id: str, password_hash: str) -> None:
    with store.cursor() as cur:
        cur.execute("UPDATE accounts SET password_hash = ? WHERE account_id = ?",
                    [password_hash, account_id])


def set_email(store: Store, account_id: str, email: str, verified: bool) -> None:
    with store.cursor() as cur:
        if cur.execute("SELECT 1 FROM accounts WHERE email = ? AND account_id <> ?",
                       [email, account_id]).fetchone():
            raise AccountError("Bu e-posta ile zaten bir hesap var.")
        cur.execute("UPDATE accounts SET email = ?, email_verified = ? WHERE account_id = ?",
                    [email, verified, account_id])


def mark_verified(store: Store, account_id: str) -> None:
    with store.cursor() as cur:
        cur.execute("UPDATE accounts SET email_verified = true WHERE account_id = ?", [account_id])


def touch_login(store: Store, account_id: str) -> None:
    with store.cursor() as cur:
        cur.execute("UPDATE accounts SET last_login_at = now() WHERE account_id = ?", [account_id])


def ensure_faceit_account(store: Store, steam_id: str, email: str | None) -> str | None:
    """FACEIT girişi (kimlik doğrulanmış) sonrası hesabı hazırlar; hesap kimliğini döner.

    - Oyuncu zaten bir hesaba bağlıysa: bağlantı 'dogrulanmis' olur; hesapta e-posta yoksa
      FACEIT e-postası eklenir.
    - Değilse ve FACEIT e-postası varsa: o e-postayla hesap varsa ona bağlanır (FACEIT
      e-postası doğrulanmış sayılır), yoksa yeni hesap açılır (şifresiz, doğrulanmış).
    - E-posta gelmediyse e-postasız hesap açılır; kullanıcı sonra e-posta ekler.
    Ön-ele geçirme önlemi: e-posta doğrulanmamış bir hesaba bağlanırken şifre silinir (e-postayı
    önceden kaydeden başkası, gerçek sahibin hesabına şifreyle girmeye devam edemesin).
    """
    existing = account_of(store, steam_id)
    if existing:
        acct, _ = existing
        link_user(store, acct.account_id, steam_id, VERIFIED)
        if email and not acct.email:
            try:
                set_email(store, acct.account_id, email, True)
            except AccountError:
                pass  # e-posta başka hesapta: hesap e-postasız kalır
        elif email and acct.email == email and not acct.email_verified:
            mark_verified(store, acct.account_id)
        return acct.account_id
    target = by_email(store, email) if email else None
    if target is not None and steam_of(store, target.account_id) is None:
        if not target.email_verified:
            with store.cursor() as cur:
                cur.execute("UPDATE accounts SET password_hash = NULL WHERE account_id = ?",
                            [target.account_id])
        mark_verified(store, target.account_id)
        link_user(store, target.account_id, steam_id, VERIFIED)
        return target.account_id
    # e-posta başka bir FACEIT'e bağlı hesaptaysa e-posta kullanılmaz (bir e-posta bir hesap)
    account_id = create(store, None if target is not None else email, None,
                        bool(email) and target is None)
    link_user(store, account_id, steam_id, VERIFIED)
    return account_id


def list_accounts(con: Any) -> list[tuple]:
    """CLI için: (e-posta, doğrulandı, nickname, bağlantı, kayıt, son giriş)."""
    return con.execute(
        "SELECT a.email, coalesce(a.email_verified, false), w.nickname, w.faceit_link, "
        "a.created_at, a.last_login_at FROM accounts a LEFT JOIN web_users w "
        "ON w.account_id = a.account_id ORDER BY a.created_at").fetchall()


# --- hız sınırı --------------------------------------------------------------------------

class LoginLimiter:
    """Bellekte basit hız sınırı: pencere içinde en fazla `limit` hatalı deneme.

    Anahtar (e-posta, IP) ve ayrıca yalnız e-posta (IP değiştirerek kaçmayı zorlaştırır).
    Süreç yeniden başlayınca sıfırlanır; tek süreçli sunucu için yeterli.
    """

    def __init__(self, limit: int = 5, window: float = 900.0, clock=time.monotonic) -> None:
        self.limit, self.window, self._clock = limit, window, clock
        self._fails: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> collections.deque[float]:
        q = self._fails.setdefault(key, collections.deque())
        now = self._clock()
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, email: str, ip: str) -> bool:
        with self._lock:
            return (len(self._recent(f"{email}|{ip}")) >= self.limit
                    or len(self._recent(email)) >= self.limit * 4)

    def fail(self, email: str, ip: str) -> None:
        with self._lock:
            now = self._clock()
            self._recent(f"{email}|{ip}").append(now)
            self._recent(email).append(now)
            if len(self._fails) > 5000:  # bellek şişmesin: süresi dolanları at
                for k in [k for k, q in self._fails.items() if not q or now - q[-1] > self.window]:
                    del self._fails[k]

    def reset(self, email: str, ip: str) -> None:
        with self._lock:
            self._fails.pop(f"{email}|{ip}", None)
