"""FACEIT verilerinin kaydı (şema v12): hesap e-postayla, anlık görüntüler, hassas alan ayıklama,
token saklanmaması, veri indirme, hesap silme. FACEIT sahte."""

from __future__ import annotations

import json

import pytest

from squadmind.db import connect, init_db
from squadmind.web import accounts as acc
from squadmind.web import faceit_data as fd
from tests.test_accounts import FakeMailer, FakeOAuth, build, csrf_of, faceit_login
from tests.test_ingest_demos import SELF

SECRET_TOKEN = "TOKEN-DEGERI-GIZLI-123"


class RichOAuth(FakeOAuth):
    """Hassas alanlı userinfo + id_token iddiaları + token üst verisi döner."""

    def fetch_user(self, code, verifier):
        user = super().fetch_user(code, verifier)
        user |= {"given_name": "Ali", "family_name": "Veli", "birthdate": "1990-05-01",
                 "locale": "tr", "nickname": "TestNick",
                 "_token_meta": {"scope": "openid profile email", "expires_in": 3600},
                 "_id_claims": {"sub": "p-self", "email": self.email, "family_name": "Veli"}}
        return user


@pytest.fixture
def site(tmp_path, make_client):
    oauth = RichOAuth()
    oauth.email, oauth.email_verified = "Mail@Example.com", True
    client, store = build(tmp_path, make_client, FakeMailer(), oauth)
    yield client, store
    store.close()


def dump(store) -> str:
    with store.cursor() as cur:
        rows = cur.execute("SELECT payload, coalesce(token_meta, '') FROM faceit_identity_snapshots").fetchall()
        accts = cur.execute("SELECT * FROM accounts").fetchall()
    return json.dumps([rows, accts], default=str)


def test_email_account_created_and_reused(site):
    client, store = site
    assert faceit_login(client).status_code == 303
    account, link = acc.account_of(store, SELF)
    assert account.email == "mail@example.com" and account.email_verified and link == acc.VERIFIED
    client.cookies.clear()
    faceit_login(client)
    again, _ = acc.account_of(store, SELF)
    assert again.account_id == account.account_id
    with store.cursor() as cur:
        assert cur.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1
        nick, cnt = cur.execute("SELECT faceit_nickname, login_count FROM accounts").fetchone()
    assert cnt == 2 and nick


def test_snapshots_written_once_per_content(site):
    client, store = site
    faceit_login(client)
    with store.cursor() as cur:
        first = cur.execute("SELECT source, verified FROM faceit_identity_snapshots ORDER BY source").fetchall()
    kinds = dict(first)
    assert {"userinfo", "id_token"} <= set(kinds)
    assert kinds["id_token"] is False and kinds["userinfo"] is True
    client.cookies.clear()
    faceit_login(client)                               # aynı içerik: yeni satır yok
    with store.cursor() as cur:
        assert cur.execute("SELECT count(*) FROM faceit_identity_snapshots").fetchone()[0] == len(first)


def test_sensitive_fields_not_stored_by_default(site):
    client, store = site
    faceit_login(client)
    text = dump(store)
    for bad in ("Ali", "Veli", "1990-05-01", "given_name", "family_name", "birthdate"):
        assert bad not in text
    assert '\\"age_18_plus\\": true' in text           # yalnız türetilmiş bayrak


def test_sensitive_flag_stores_fields(site):
    _, store = site
    acct = acc.create(store, None, None, False)
    info = {"guid": "p", "given_name": "Ali", "birthdate": "1990-05-01"}
    fd.record_login(store, acct, "p", info, None, None, {"infractions": {"a": 1}}, store_sensitive=True)
    text = dump(store)
    assert "Ali" in text and "1990-05-01" in text and "infractions" in text
    acct2 = acc.create(store, None, None, False)
    fd.record_login(store, acct2, "p", info, None, None, {"infractions": {"a": 1}})
    with store.cursor() as cur:
        rows = cur.execute("SELECT payload FROM faceit_identity_snapshots WHERE account_id = ?",
                           [acct2]).fetchall()
    assert all("Ali" not in r[0] and "infractions" not in r[0] for r in rows)


def test_scrub_nested_and_age():
    out = json.loads(fd._dump({"a": {"given_name": "x", "k": 1}, "infractions": {}, "birthdate": "2015-01-01"}))
    assert out == {"a": {"k": 1}, "age_18_plus": False}
    assert fd.age_18_plus("bozuk") is None


def test_token_values_never_stored(site):
    _, store = site
    acct = acc.create(store, None, None, False)
    fd.record_login(store, acct, "p", {"guid": "p", "access_token": SECRET_TOKEN},
                    {"id_token": SECRET_TOKEN, "sub": "p"}, {"scope": "x"}, {"refresh_token": SECRET_TOKEN})
    assert SECRET_TOKEN not in dump(store)


def test_my_data_only_own_and_requires_session(site):
    client, store = site
    assert client.get("/hesap/verilerim.json", follow_redirects=False).status_code == 401
    assert client.get("/hesap", follow_redirects=False).status_code == 303
    faceit_login(client)
    other = acc.create(store, "baska@example.com", None, True)
    fd.record_login(store, other, "q", {"guid": "q", "nickname": "Baskasi"}, None, None, None)
    r = client.get("/hesap/verilerim.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.json()["account"]["email"] == "mail@example.com"
    assert "Baskasi" not in r.text
    page = client.get("/hesap").text
    assert "FACEIT'ten alınan veriler" in page and "Baskasi" not in page


def test_delete_account(site):
    client, store = site
    faceit_login(client)
    assert client.get("/hesap/sil").status_code == 200
    token = csrf_of(client, "/hesap/sil")
    bad = client.post("/hesap/sil", data={"csrf": token, "onay": "evet"}, follow_redirects=False)
    assert bad.status_code == 400 and acc.account_of(store, SELF)
    nocsrf = client.post("/hesap/sil", data={"csrf": "x", "onay": "SIL"}, follow_redirects=False)
    assert nocsrf.status_code == 400
    ok = client.post("/hesap/sil", data={"csrf": token, "onay": "SIL"}, follow_redirects=False)
    assert ok.status_code == 303
    assert acc.account_of(store, SELF) is None and store.user(SELF) is None
    with store.cursor() as cur:
        assert cur.execute("SELECT count(*) FROM faceit_identity_snapshots").fetchone()[0] == 0
        assert cur.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0
    assert client.get("/hesap", follow_redirects=False).status_code == 303


def test_migration_idempotent(tmp_path):
    con = connect(tmp_path / "m.duckdb")
    init_db(con)
    init_db(con)
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'accounts'").fetchall()}
    assert {"faceit_nickname", "login_count", "token_meta"} <= cols
