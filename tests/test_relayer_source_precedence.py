"""Config source precedence: encrypted DB first, ENV fallback, never copied."""

from api.relayer_config import (
    ENV_API_KEY_KEY,
    ENV_PASSPHRASE_KEY,
    ENV_RPC_URL_KEY,
    ENV_SECRET_KEY,
    ENV_URL_KEY,
    RELAYER_DEFAULT_URL,
    env_relayer_configured,
    load_relayer_runtime_config,
    resolve_relayer_config,
)
from api.ctf import RelayerUnavailable, Type3MergeClient, relayer_configured
from models.database import Database
from utils.crypto import derive_key

KEY = derive_key("test-password-123", b"testsalt16bytes!")

ENV_FULL = {
    ENV_API_KEY_KEY: "env-builder-key",
    ENV_SECRET_KEY: "env-secret-not-real",
    ENV_PASSPHRASE_KEY: "env-passphrase-not-real",
    ENV_URL_KEY: "https://relayer.example",
    ENV_RPC_URL_KEY: "https://rpc.example",
}

ENV_NO_URL = {k: v for k, v in ENV_FULL.items() if k != ENV_URL_KEY}


def _db_with_creds(tmp_path):
    from api.relayer_config import encrypt_credential_trio

    database = Database(str(tmp_path / "prec.db"))
    database.init()
    database.save_relayer_credentials(
        *encrypt_credential_trio(
            "db-builder-key", "db-secret-not-real", "db-passphrase-not-real", KEY
        )
    )
    return database


def test_db_only_uses_db_config(tmp_path):
    db = _db_with_creds(tmp_path)
    try:
        config = load_relayer_runtime_config(db, KEY, env={})
        assert config is not None
        assert config.builder_key == "db-builder-key"
        assert config.builder_secret == "db-secret-not-real"
        assert config.builder_passphrase == "db-passphrase-not-real"
        # No URL anywhere -> official default.
        assert config.url == RELAYER_DEFAULT_URL
    finally:
        db.close()


def test_env_only_uses_env(tmp_path):
    db = Database(str(tmp_path / "no.db"))
    db.init()
    try:
        config = load_relayer_runtime_config(db, KEY, env=dict(ENV_FULL))
        assert config is not None
        assert config.builder_key == "env-builder-key"
        assert config.url == "https://relayer.example"
        assert config.rpc_url == "https://rpc.example"
    finally:
        db.close()


def test_both_db_wins(tmp_path):
    db = _db_with_creds(tmp_path)
    try:
        config = load_relayer_runtime_config(db, KEY, env=dict(ENV_FULL))
        assert config.builder_key == "db-builder-key"
        assert config.builder_secret == "db-secret-not-real"
        # URL still honors the ENV override even when creds come from DB.
        assert config.url == "https://relayer.example"
    finally:
        db.close()


def test_neither_unavailable(tmp_path):
    db = Database(str(tmp_path / "none.db"))
    db.init()
    try:
        assert load_relayer_runtime_config(db, KEY, env={}) is None
        assert load_relayer_runtime_config(None, KEY, env={}) is None
    finally:
        db.close()


def test_delete_db_while_env_exists_falls_back(tmp_path):
    db = _db_with_creds(tmp_path)
    try:
        db.delete_relayer_credentials()
        config = load_relayer_runtime_config(db, KEY, env=dict(ENV_FULL))
        assert config is not None
        assert config.builder_key == "env-builder-key"
    finally:
        db.close()


def test_no_migration_copies_env_into_db(tmp_path):
    db = _db_with_creds(tmp_path)
    try:
        load_relayer_runtime_config(db, KEY, env=dict(ENV_FULL))
        row = db.get_relayer_credentials()
        from utils.crypto import decrypt

        assert decrypt(row["encrypted_api_key"], KEY) == "db-builder-key"
        # ENV values were never written anywhere.
        raw = (tmp_path / "prec.db").read_bytes()
        assert b"env-builder-key" not in raw
        assert b"env-secret-not-real" not in raw
    finally:
        db.close()


def test_not_logged_in_db_unavailable_but_env_works(tmp_path):
    db = _db_with_creds(tmp_path)
    try:
        # encryption_key None == not logged in yet.
        config = load_relayer_runtime_config(db, None, env=dict(ENV_FULL))
        assert config is not None
        assert config.builder_key == "env-builder-key"
        assert load_relayer_runtime_config(db, None, env={}) is None
    finally:
        db.close()


def test_env_without_url_still_configured():
    assert env_relayer_configured(ENV_NO_URL) is True
    assert relayer_configured(ENV_NO_URL) is True
    config = resolve_relayer_config(None, env=ENV_NO_URL)
    assert config.url == RELAYER_DEFAULT_URL


def test_env_missing_any_credential_not_configured():
    for key in (ENV_API_KEY_KEY, ENV_SECRET_KEY, ENV_PASSPHRASE_KEY):
        env = dict(ENV_FULL)
        env.pop(key)
        assert env_relayer_configured(env) is False


def test_type3_client_uses_injected_config():
    """The funds path accepts an explicit config object (no env mutation)."""
    from api.relayer_config import RelayerRuntimeConfig

    relayer = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    config = RelayerRuntimeConfig(
        url="https://relayer.example",
        builder_key="test-builder-key",
        builder_secret="test-secret-not-real",
        builder_passphrase="test-passphrase-not-real",
    )
    client = Type3MergeClient(
        "0x" + "1" * 64,
        relayer_config=config,
        relay_client_cls=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
            return_value=relayer
        ),
    )
    assert client.client is relayer


def test_type3_client_env_path_still_works():
    relayer = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    client = Type3MergeClient(
        "0x" + "1" * 64,
        env=dict(ENV_NO_URL),
        relay_client_cls=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
            return_value=relayer
        ),
    )
    assert client.client is relayer


def test_type3_client_missing_env_raises_cleanly():
    import pytest

    with pytest.raises(RelayerUnavailable):
        Type3MergeClient("0x" + "1" * 64, env={})


def test_engine_merge_client_resolves_encrypted_db_config(tmp_path):
    """OrderMonitor._merge_client() loads DB credentials through the login key."""
    from unittest.mock import MagicMock, patch

    import engine.monitor
    from engine.monitor import OrderMonitor

    db = _db_with_creds(tmp_path)
    try:
        api = MagicMock()
        api.signature_type = 3
        api.private_key = "0x" + "1" * 64
        monitor = OrderMonitor(api, db, "0xWallet", encryption_key=KEY)
        with patch.object(engine.monitor, "Type3MergeClient") as cls:
            client = monitor._merge_client()
        assert client is cls.return_value
        assert cls.call_args.args[0] == "0x" + "1" * 64
        config = cls.call_args.kwargs["relayer_config"]
        assert config.url == RELAYER_DEFAULT_URL
        assert config.builder_key == "db-builder-key"
        assert config.builder_secret == "db-secret-not-real"
        assert config.builder_passphrase == "db-passphrase-not-real"
    finally:
        db.close()


def test_engine_merge_client_raises_when_unconfigured(tmp_path):
    from unittest.mock import MagicMock

    from engine.monitor import OrderMonitor

    db = Database(str(tmp_path / "unconf.db"))
    db.init()
    try:
        api = MagicMock()
        api.signature_type = 3
        api.private_key = "0x" + "1" * 64
        monitor = OrderMonitor(api, db, "0xWallet", encryption_key=KEY)
        import pytest

        with pytest.raises(RelayerUnavailable):
            monitor._merge_client()
    finally:
        db.close()
