"""Read-only preflight tests — all Relayer/CLOB calls are mocked."""

import time
from unittest.mock import MagicMock, patch

import pytest

from api.relayer_config import (
    FUNDER_MISMATCH,
    NOT_TYPE3,
    READY,
    RELAYER_AUTH_FAILED,
    RELAYER_UNREACHABLE,
    TEMPLATE_DISABLED,
    encrypt_credential_trio,
    run_wallet_preflight,
)
from models.database import Database
from utils.crypto import derive_key

KEY = derive_key("test-password-123", b"testsalt16bytes!")
FUNDER = "0x" + "f" * 40
WALLET = "0xWallet"


class FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code

    def json(self):
        return {"error": "mock"}

    @property
    def text(self):
        return "mock error"


def _db(tmp_path, funder=FUNDER, merge_enabled=True, sig=3):
    database = Database(str(tmp_path / "preflight.db"))
    database.init()
    database.add_wallet(WALLET, "enc", funder=funder, signature_type=sig)
    database.save_relayer_credentials(
        *encrypt_credential_trio(
            "test-builder-key", "test-secret-not-real", "test-passphrase-not-real", KEY
        )
    )
    if not merge_enabled:
        database.save_template(
            database.get_default_template_id(), {"merge_enabled": False}
        )
    return database


def _client(expected=FUNDER, nonce={"nonce": 7}, deployed=True):
    client = MagicMock()
    client.get_expected_deposit_wallet.return_value = expected
    client.signer.address.return_value = "0xEOA"
    client.get_nonce.return_value = nonce
    client.get_deployed.return_value = deployed
    return client


def _wallet(funder=FUNDER, sig=3):
    return {
        "address": WALLET,
        "funder": funder,
        "signature_type": sig,
        "enabled": 1,
        "encrypted_key": "enc",
    }


def test_preflight_ready_on_valid_type3(tmp_path):
    db = _db(tmp_path)
    try:
        client = _client()
        with patch(
            "py_builder_relayer_client.client.RelayClient",
            MagicMock(return_value=client),
        ) as cls:
            result = run_wallet_preflight(
                _wallet(), "0x" + "1" * 64, db=db, encryption_key=KEY
            )
        assert result.ready is True
        assert result.reason == READY
        cls.assert_called_once()
        client.get_expected_deposit_wallet.assert_called_once()
        client.get_nonce.assert_called_once()
    finally:
        db.close()


def test_preflight_funder_mismatch(tmp_path):
    db = _db(tmp_path)
    try:
        client = _client()
        with patch(
            "py_builder_relayer_client.client.RelayClient",
            MagicMock(return_value=client),
        ):
            result = run_wallet_preflight(
                _wallet(funder="0x" + "a" * 40),
                "0x" + "1" * 64,
                db=db,
                encryption_key=KEY,
            )
        assert result.ready is False
        assert result.reason == FUNDER_MISMATCH
        # The nonce query must not run once the funder check fails.
        client.get_nonce.assert_not_called()
    finally:
        db.close()


def test_preflight_empty_funder_is_mismatch(tmp_path):
    db = _db(tmp_path)
    try:
        with patch(
            "py_builder_relayer_client.client.RelayClient",
            MagicMock(return_value=_client()),
        ):
            result = run_wallet_preflight(
                _wallet(funder=""), "0x" + "1" * 64, db=db, encryption_key=KEY
            )
        assert result.reason == FUNDER_MISMATCH
    finally:
        db.close()


def test_preflight_bad_auth(tmp_path):
    from py_builder_relayer_client.exceptions import RelayerApiException

    db = _db(tmp_path)
    try:
        client = _client()
        client.get_nonce.side_effect = RelayerApiException(FakeResp(401))
        with patch(
            "py_builder_relayer_client.client.RelayClient",
            MagicMock(return_value=client),
        ):
            result = run_wallet_preflight(
                _wallet(), "0x" + "1" * 64, db=db, encryption_key=KEY
            )
        assert result.ready is False
        assert result.reason == RELAYER_AUTH_FAILED
        assert "401" in result.detail
    finally:
        db.close()


def test_preflight_timeout_is_unreachable(tmp_path):
    db = _db(tmp_path)
    try:
        client = _client()

        def hang(*args, **kwargs):
            time.sleep(1.0)
            return FUNDER

        client.get_expected_deposit_wallet.side_effect = hang
        with patch(
            "py_builder_relayer_client.client.RelayClient",
            MagicMock(return_value=client),
        ):
            result = run_wallet_preflight(
                _wallet(), "0x" + "1" * 64, db=db, encryption_key=KEY, timeout=0.2
            )
        assert result.ready is False
        assert result.reason == RELAYER_UNREACHABLE
        assert "超时" in result.detail
    finally:
        db.close()


def test_preflight_network_error_is_unreachable(tmp_path):
    from py_builder_relayer_client.exceptions import RelayerApiException

    db = _db(tmp_path)
    try:
        client = _client()
        client.get_nonce.side_effect = RelayerApiException(
            error_msg="Request exception!"
        )
        with patch(
            "py_builder_relayer_client.client.RelayClient",
            MagicMock(return_value=client),
        ):
            result = run_wallet_preflight(
                _wallet(), "0x" + "1" * 64, db=db, encryption_key=KEY
            )
        assert result.ready is False
        assert result.reason == RELAYER_UNREACHABLE
    finally:
        db.close()


def test_preflight_non_type3(tmp_path):
    db = _db(tmp_path, sig=2)
    try:
        with patch(
            "py_builder_relayer_client.client.RelayClient",
            MagicMock(return_value=_client()),
        ) as cls:
            result = run_wallet_preflight(
                _wallet(sig=2), "0x" + "1" * 64, db=db, encryption_key=KEY
            )
        assert result.reason == NOT_TYPE3
        cls.assert_not_called()
    finally:
        db.close()


def test_preflight_template_disabled(tmp_path):
    db = _db(tmp_path, merge_enabled=False)
    try:
        with patch(
            "py_builder_relayer_client.client.RelayClient",
            MagicMock(return_value=_client()),
        ) as cls:
            result = run_wallet_preflight(
                _wallet(), "0x" + "1" * 64, db=db, encryption_key=KEY
            )
        assert result.reason == TEMPLATE_DISABLED
        cls.assert_not_called()
    finally:
        db.close()


def test_preflight_trading_disabled(tmp_path):
    db = _db(tmp_path)
    try:
        with patch(
            "py_builder_relayer_client.client.RelayClient",
            MagicMock(return_value=_client()),
        ) as cls:
            result = run_wallet_preflight(
                _wallet(), "0x" + "1" * 64, db=db, encryption_key=KEY,
                trading_enabled=False,
            )
        assert result.reason == "TRADING_DISABLED"
        cls.assert_not_called()
    finally:
        db.close()


def test_preflight_mutation_methods_never_called(tmp_path):
    """The preflight must be strictly read-only."""
    db = _db(tmp_path)
    try:
        client = _client()
        with patch(
            "py_builder_relayer_client.client.RelayClient",
            MagicMock(return_value=client),
        ):
            run_wallet_preflight(
                _wallet(), "0x" + "1" * 64, db=db, encryption_key=KEY
            )
        client.execute_deposit_wallet_batch.assert_not_called()
        client.execute.assert_not_called()
        # No WALLET-CREATE / deploy path is reachable from the preflight.
        for call in client.method_calls:
            assert "execute_deposit_wallet_batch" not in call[0]
            assert "wallet_create" not in call[0].lower()
    finally:
        db.close()


def test_preflight_missing_credentials_is_not_configured(tmp_path):
    db = Database(str(tmp_path / "nocfg.db"))
    db.init()
    try:
        db.add_wallet(WALLET, "enc", funder=FUNDER, signature_type=3)
        with patch(
            "py_builder_relayer_client.client.RelayClient",
            MagicMock(return_value=_client()),
        ) as cls:
            result = run_wallet_preflight(
                _wallet(), "0x" + "1" * 64, db=db, encryption_key=KEY
            )
        assert result.reason == "RELAYER_NOT_CONFIGURED"
        cls.assert_not_called()
    finally:
        db.close()
