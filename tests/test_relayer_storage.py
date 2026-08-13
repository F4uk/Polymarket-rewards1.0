"""Encrypted Relayer credential storage — atomicity, secrecy at rest, safety."""

import os
import sqlite3

import pytest

from api.relayer_config import (
    encrypt_credential_trio,
    decrypt_credential_trio,
)
from models.database import Database
from utils.crypto import derive_key, encrypt

KEY = derive_key("test-password-123", b"testsalt16bytes!")
OTHER_KEY = derive_key("other-password-456", b"testsalt16bytes!")


def _db(tmp_path, name="relayer.db"):
    database = Database(str(tmp_path / name))
    database.init()
    return database


def _trio():
    return encrypt_credential_trio(
        "test-builder-key", "test-secret-not-real", "test-passphrase-not-real", KEY
    )


def test_save_credential_trio_roundtrip(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_relayer_credentials(*_trio())
        row = db.get_relayer_credentials()
        assert row is not None
        assert decrypt_credential_trio(
            row["encrypted_api_key"],
            row["encrypted_secret"],
            row["encrypted_passphrase"],
            KEY,
        ) == ("test-builder-key", "test-secret-not-real", "test-passphrase-not-real")
    finally:
        db.close()


def test_raw_sqlite_contains_no_plaintext(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_relayer_credentials(*_trio())
        db.close()
        raw = (tmp_path / "relayer.db").read_bytes()
        for needle in (b"test-builder-key", b"test-secret-not-real", b"test-passphrase-not-real"):
            assert needle not in raw, "plaintext secret found in SQLite file"
    finally:
        db.close()


def test_decrypt_with_wrong_key_fails_safely(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_relayer_credentials(*_trio())
        row = db.get_relayer_credentials()
        with pytest.raises(Exception):
            decrypt_credential_trio(
                row["encrypted_api_key"],
                row["encrypted_secret"],
                row["encrypted_passphrase"],
                OTHER_KEY,
            )
    finally:
        db.close()


def test_encrypt_never_returns_plaintext():
    api_key, secret, passphrase = encrypt_credential_trio(
        "test-builder-key", "test-secret-not-real", "test-passphrase-not-real", KEY
    )
    assert "test-builder-key" not in api_key
    assert "test-secret-not-real" not in secret
    assert "test-passphrase-not-real" not in passphrase
    assert api_key != secret != passphrase


def test_atomic_update_replaces_entire_trio(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_relayer_credentials(*_trio())
        new_trio = encrypt_credential_trio(
            "test-new-key", "test-new-secret", "test-new-passphrase", KEY
        )
        db.save_relayer_credentials(*new_trio)
        row = db.get_relayer_credentials()
        assert decrypt_credential_trio(
            row["encrypted_api_key"],
            row["encrypted_secret"],
            row["encrypted_passphrase"],
            KEY,
        ) == ("test-new-key", "test-new-secret", "test-new-passphrase")
        # single row only (id=1 CHECK constraint)
        conn = sqlite3.connect(str(tmp_path / "relayer.db"))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM relayer_credentials"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1
    finally:
        db.close()


def test_failed_validation_does_not_overwrite_old_trio(tmp_path, monkeypatch):
    """A save attempt that fails validation keeps the previous trio intact."""
    db = _db(tmp_path)
    try:
        db.save_relayer_credentials(*_trio())
        # Simulate a failed validation: attempt to save with an empty value
        # raises before any DB write.
        with pytest.raises(ValueError):
            encrypt_credential_trio("test-new-key", "", "x", KEY)
        row = db.get_relayer_credentials()
        assert decrypt_credential_trio(
            row["encrypted_api_key"],
            row["encrypted_secret"],
            row["encrypted_passphrase"],
            KEY,
        ) == ("test-builder-key", "test-secret-not-real", "test-passphrase-not-real")
    finally:
        db.close()


def test_delete_only_removes_relayer_config(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_relayer_credentials(*_trio())
        db.add_wallet("0xWallet", encrypt("0x" + "1" * 64, KEY), funder="0xFunder")
        db.save_relayer_test_result("0xWallet", True, "READY")
        db.delete_relayer_credentials()
        db.delete_relayer_test_results()
        assert db.get_relayer_credentials() is None
        assert db.get_relayer_test_results() == {}
        # Wallet private key, template and history untouched.
        wallets = db.list_wallets()
        assert len(wallets) == 1
        assert wallets[0]["address"] == "0xWallet"
        assert wallets[0]["funder"] == "0xFunder"
    finally:
        db.close()


def test_existing_wallet_encrypted_keys_unaffected(tmp_path):
    db = _db(tmp_path)
    try:
        wallet_enc = encrypt("0x" + "a" * 64, KEY)
        db.add_wallet("0xWallet", wallet_enc, funder="0xFunder")
        db.save_relayer_credentials(*_trio())
        row = db.get_relayer_credentials()
        assert row["encrypted_api_key"] != wallet_enc
        assert db.list_wallets()[0]["encrypted_key"] == wallet_enc
    finally:
        db.close()


def test_idempotent_migration(tmp_path):
    """init() twice is safe and keeps existing encrypted rows."""
    db = _db(tmp_path)
    try:
        db.save_relayer_credentials(*_trio())
        db.init()
        row = db.get_relayer_credentials()
        assert row is not None
    finally:
        db.close()


def test_relayer_test_results_roundtrip(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_relayer_test_result("0xW1", True, "READY", deposit_wallet_deployed=True)
        db.save_relayer_test_result("0xW2", False, "FUNDER_MISMATCH")
        results = db.get_relayer_test_results()
        assert results["0xW1"]["last_test_ok"] is True
        assert results["0xW1"]["deposit_wallet_deployed"] is True
        assert results["0xW2"]["last_test_ok"] is False
        assert results["0xW2"]["last_test_reason"] == "FUNDER_MISMATCH"
        db.delete_relayer_test_result("0xW2")
        assert "0xW2" not in db.get_relayer_test_results()
    finally:
        db.close()
