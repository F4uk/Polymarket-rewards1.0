"""API contract for /api/relayer-config — privacy, auth, atomicity."""

from unittest.mock import MagicMock, patch

import pytest

import web.routes as routes
from api.relayer_config import (
    RELAYER_AUTH_FAILED,
    RELAYER_DEFAULT_URL,
    PreflightResult,
    encrypt_credential_trio,
    wallet_merge_status,
)
from models.database import Database
from utils.crypto import derive_key, decrypt

KEY = derive_key("test-password-123", b"testsalt16bytes!")

SECRETS = (
    "test-builder-key",
    "test-secret-not-real",
    "test-passphrase-not-real",
)


def _client_logged_in(logged_in=True):
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = logged_in
    return client


def _db(tmp_path, name="api.db"):
    database = Database(str(tmp_path / name))
    database.init()
    return database


def _setup(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "manager", None)
    monkeypatch.setattr(routes, "encryption_key", KEY)
    return db


def _save_trio(db, api_key=SECRETS[0], secret=SECRETS[1], passphrase=SECRETS[2]):
    db.save_relayer_credentials(*encrypt_credential_trio(api_key, secret, passphrase, KEY))


# --- GET: metadata only, never secrets ---


def test_get_returns_non_secret_metadata_only(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    try:
        _save_trio(db)
        resp = _client_logged_in().get("/api/relayer-config")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["configured"] is True
        assert body["source"] == "encrypted_db"
        assert body["auth_mode"] == "builder"
        assert body["relayer_url"] == RELAYER_DEFAULT_URL
        text = resp.get_data(as_text=True)
        for secret in SECRETS:
            assert secret not in text
        for forbidden in ("builder_api_key", "builder_secret", "builder_passphrase", "ciphertext"):
            assert forbidden not in body
    finally:
        db.close()


def test_get_unconfigured(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    try:
        body = _client_logged_in().get("/api/relayer-config").get_json()
        assert body["configured"] is False
        assert body["source"] == "unavailable"
        assert body["last_test_ok"] is None
    finally:
        db.close()


def test_get_env_source(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("PMM_BUILDER_API_KEY", "env-key")
    monkeypatch.setenv("PMM_BUILDER_SECRET", "env-secret")
    monkeypatch.setenv("PMM_BUILDER_PASSPHRASE", "env-pass")
    try:
        body = _client_logged_in().get("/api/relayer-config").get_json()
        assert body["configured"] is True
        assert body["source"] == "env"
    finally:
        db.close()


def test_get_requires_login(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    resp = _client_logged_in(logged_in=False).get("/api/relayer-config")
    assert resp.status_code in (302, 401)


# --- POST: validation + atomicity ---


def test_post_requires_login(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    resp = _client_logged_in(logged_in=False).post(
        "/api/relayer-config", json={"builder_api_key": "k", "builder_secret": "s", "builder_passphrase": "p"}
    )
    assert resp.status_code in (302, 401)


def test_post_invalid_payload_400(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    try:
        for payload in (
            {},
            {"builder_api_key": "k"},
            {"builder_api_key": "k", "builder_secret": "s"},
            {"builder_api_key": " ", "builder_secret": " ", "builder_passphrase": " "},
        ):
            resp = _client_logged_in().post("/api/relayer-config", json=payload)
            assert resp.status_code == 400
            assert "error" in resp.get_json()
        assert db.get_relayer_credentials() is None
    finally:
        db.close()


def test_post_saves_encrypted_trio_without_wallets(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    try:
        resp = _client_logged_in().post(
            "/api/relayer-config",
            json={"builder_api_key": SECRETS[0], "builder_secret": SECRETS[1], "builder_passphrase": SECRETS[2]},
        )
        assert resp.status_code == 200
        row = db.get_relayer_credentials()
        assert row is not None
        assert decrypt(row["encrypted_api_key"], KEY) == SECRETS[0]
        assert decrypt(row["encrypted_secret"], KEY) == SECRETS[1]
        assert decrypt(row["encrypted_passphrase"], KEY) == SECRETS[2]
        text = resp.get_data(as_text=True)
        for secret in SECRETS:
            assert secret not in text
    finally:
        db.close()


def test_post_failed_validation_keeps_old_trio(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    try:
        _save_trio(db)
        failure = PreflightResult("0xWallet", False, RELAYER_AUTH_FAILED)
        monkeypatch.setattr(routes, "_preflight_wallets", lambda **kw: [failure])
        resp = _client_logged_in().post(
            "/api/relayer-config",
            json={"builder_api_key": "new-key", "builder_secret": "new-secret", "builder_passphrase": "new-pass"},
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert "旧配置保持不变" in body["error"]
        assert body["results"][0]["reason"] == RELAYER_AUTH_FAILED
        row = db.get_relayer_credentials()
        assert decrypt(row["encrypted_api_key"], KEY) == SECRETS[0]
        assert decrypt(row["encrypted_secret"], KEY) == SECRETS[1]
        assert decrypt(row["encrypted_passphrase"], KEY) == SECRETS[2]
    finally:
        db.close()


def test_post_validated_save_persists_results(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    try:
        db.add_wallet("0xWallet", "enc", funder="0x" + "f" * 40, signature_type=3)
        ok = PreflightResult("0xWallet", True, "READY")
        monkeypatch.setattr(routes, "_preflight_wallets", lambda **kw: [ok])
        resp = _client_logged_in().post(
            "/api/relayer-config",
            json={"builder_api_key": SECRETS[0], "builder_secret": SECRETS[1], "builder_passphrase": SECRETS[2]},
        )
        assert resp.status_code == 200
        results = db.get_relayer_test_results()
        assert results["0xWallet"]["last_test_ok"] is True
        assert results["0xWallet"]["last_test_reason"] == "READY"
    finally:
        db.close()


def test_post_without_test_skips_preflight(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    try:
        resp = _client_logged_in().post(
            "/api/relayer-config",
            json={
                "builder_api_key": SECRETS[0],
                "builder_secret": SECRETS[1],
                "builder_passphrase": SECRETS[2],
                "test": False,
            },
        )
        assert resp.status_code == 200
        assert db.get_relayer_test_results() == {}
    finally:
        db.close()


# --- POST /api/relayer-config/test (re-test) ---


def test_retest_runs_and_persists(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    try:
        db.add_wallet("0xWallet", "enc", funder="0x" + "f" * 40, signature_type=3)
        ok = PreflightResult("0xWallet", True, "READY")
        monkeypatch.setattr(routes, "_preflight_wallets", lambda **kw: [ok])
        resp = _client_logged_in().post("/api/relayer-config/test")
        assert resp.status_code == 200
        assert resp.get_json()["results"][0]["reason"] == "READY"
        assert db.get_relayer_test_results()["0xWallet"]["last_test_ok"] is True
    finally:
        db.close()


# --- DELETE ---


def test_delete_requires_login(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    resp = _client_logged_in(logged_in=False).delete("/api/relayer-config")
    assert resp.status_code in (302, 401)


def test_delete_removes_db_creds_and_falls_back_to_env(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("PMM_BUILDER_API_KEY", "env-key")
    monkeypatch.setenv("PMM_BUILDER_SECRET", "env-secret")
    monkeypatch.setenv("PMM_BUILDER_PASSPHRASE", "env-pass")
    try:
        _save_trio(db)
        db.save_relayer_test_result("0xWallet", True, "READY")
        resp = _client_logged_in().delete("/api/relayer-config")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["source"] == "env"
        assert body["configured"] is True
        assert db.get_relayer_credentials() is None
        assert db.get_relayer_test_results() == {}
        # Wallets untouched.
        assert db.list_wallets() == []
    finally:
        db.close()


def test_delete_without_env_is_unconfigured(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    try:
        _save_trio(db)
        body = _client_logged_in().delete("/api/relayer-config").get_json()
        assert body["source"] == "unavailable"
        assert body["configured"] is False
    finally:
        db.close()


# --- wallet_merge_status aggregation (capability vs template) ---


def _w(sig=3, funder="0x" + "f" * 40, trading=True, enabled=1):
    return {
        "address": "0xWallet",
        "funder": funder,
        "signature_type": sig,
        "trading_enabled": trading,
        "enabled": enabled,
    }


def test_merge_status_ready():
    s = wallet_merge_status(_w(), {"merge_enabled": True}, True, {"last_test_ok": True, "last_test_reason": "READY"})
    assert s["merge_capable"] is True
    assert s["merge_status"] == "ready"
    assert s["merge_reason"] == "READY"
    assert s["merge_available"] is True


def test_merge_status_template_disabled_shows_separate_state():
    s = wallet_merge_status(_w(), {"merge_enabled": False}, True, {"last_test_ok": True})
    assert s["merge_capable"] is True
    assert s["merge_status"] == "template_disabled"
    assert s["merge_reason"] == "TEMPLATE_DISABLED"
    assert s["merge_available"] is False


def test_merge_status_not_type3():
    s = wallet_merge_status(_w(sig=2), {"merge_enabled": True}, True, None)
    assert s["merge_capable"] is False
    assert s["merge_status"] == "not_ready"
    assert s["merge_reason"] == "NOT_TYPE3"


def test_merge_status_not_configured():
    s = wallet_merge_status(_w(), {"merge_enabled": True}, False, None)
    assert s["merge_capable"] is False
    assert s["merge_reason"] == "RELAYER_NOT_CONFIGURED"


def test_merge_status_configured_untested():
    s = wallet_merge_status(_w(), {"merge_enabled": True}, True, None)
    assert s["merge_capable"] is True
    assert s["merge_reason"] == "RELAYER_NOT_TESTED"


def test_merge_status_trading_disabled():
    s = wallet_merge_status(_w(trading=False), {"merge_enabled": True}, True, None)
    assert s["merge_capable"] is False
    assert s["merge_reason"] == "TRADING_DISABLED"


def test_merge_status_unknown_trading_not_blocked():
    w = _w()
    w["trading_enabled"] = None
    s = wallet_merge_status(w, {"merge_enabled": True}, True, None)
    assert s["merge_capable"] is True
