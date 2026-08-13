"""Relayer/Builder credentials - encrypted-at-rest storage, resolution, preflight.

This module owns *configuration*, never funds logic:

- ``RelayerRuntimeConfig`` is the explicit credentials object injected into
  ``Type3MergeClient``; its repr is redacted and it never touches ``os.environ``.
- Credentials are encrypted with the login-derived ``encryption_key`` (the same
  key that protects wallet private keys) and stored in the dedicated
  ``relayer_credentials`` single-row table. Plaintext secrets never reach
  SQLite, logs, settings JSON or response bodies.
- ``run_wallet_preflight`` is strictly read-only: it derives the expected
  Deposit Wallet (RPC eth_call/eth_getCode reads), compares it with the stored
  funder and performs an authenticated nonce query. It never executes a
  Deposit Wallet batch, never deploys a wallet and never places an order.

Config source precedence (documented, tested):

1. encrypted DB credentials (decryptable with the current login key)
2. environment credentials (PMM_BUILDER_* / PMM_POLYGON_RPC_URL)
3. unavailable

ENV secrets are never copied into the database. The Relayer URL defaults to
the current official Polymarket Relayer; ``PMM_RELAYER_URL`` overrides it but
its absence never makes Merge unavailable.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from utils.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Current official Polymarket Relayer (checked against docs.polymarket.com and
# the installed py-builder-relayer-client examples; the SDK strips the trailing
# slash internally).
RELAYER_DEFAULT_URL = "https://relayer-v2.polymarket.com/"

ENV_URL_KEY = "PMM_RELAYER_URL"
ENV_API_KEY_KEY = "PMM_BUILDER_API_KEY"
ENV_SECRET_KEY = "PMM_BUILDER_SECRET"
ENV_PASSPHRASE_KEY = "PMM_BUILDER_PASSPHRASE"
ENV_RPC_URL_KEY = "PMM_POLYGON_RPC_URL"

# Preflight / availability reason codes. These are stable identifiers the UI
# maps to Chinese copy; they are also safe to persist as test metadata.
NOT_TYPE3 = "NOT_TYPE3"
TEMPLATE_DISABLED = "TEMPLATE_DISABLED"
TRADING_DISABLED = "TRADING_DISABLED"
FUNDER_MISMATCH = "FUNDER_MISMATCH"
RELAYER_NOT_CONFIGURED = "RELAYER_NOT_CONFIGURED"
RELAYER_NOT_TESTED = "RELAYER_NOT_TESTED"
RELAYER_AUTH_FAILED = "RELAYER_AUTH_FAILED"
RELAYER_UNREACHABLE = "RELAYER_UNREACHABLE"
DEPOSIT_WALLET_VALIDATION_FAILED = "DEPOSIT_WALLET_VALIDATION_FAILED"
KEY_UNAVAILABLE = "KEY_UNAVAILABLE"
READY = "READY"

REASON_CN = {
    NOT_TYPE3: "Type1/Type2 自动 Merge 不适用",
    TEMPLATE_DISABLED: "当前模板关闭自动 Merge",
    TRADING_DISABLED: "CLOB 交易被禁用",
    FUNDER_MISMATCH: "Deposit Wallet 地址校验失败",
    RELAYER_NOT_CONFIGURED: "未配置 Relayer 授权",
    RELAYER_NOT_TESTED: "已配置，未验证",
    RELAYER_AUTH_FAILED: "Relayer 凭据验证失败",
    RELAYER_UNREACHABLE: "Relayer 连接失败",
    DEPOSIT_WALLET_VALIDATION_FAILED: "Deposit Wallet 校验失败",
    KEY_UNAVAILABLE: "未登录，无法解密凭据",
    READY: "Merge 可用",
}

# Credential-level failures: these invalidate the *global* credential trio.
# KEY_UNAVAILABLE is a local key/decryption problem, not a credential problem,
# so it never blocks saving a new credential trio.
_CREDENTIAL_LEVEL_FAILURES = frozenset(
    {
        RELAYER_NOT_CONFIGURED,
        RELAYER_AUTH_FAILED,
        RELAYER_UNREACHABLE,
        DEPOSIT_WALLET_VALIDATION_FAILED,
    }
)

PREFLIGHT_TIMEOUT_SEC = 15.0


# --------------------------------------------------------------------------
# Runtime config object
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RelayerRuntimeConfig:
    """Explicit credentials/config handed to ``Type3MergeClient``.

    Never derived from ``os.environ`` inside the client; never logged.
    """

    url: str
    builder_key: str
    builder_secret: str
    builder_passphrase: str
    rpc_url: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(
            (self.builder_key or "").strip()
            and (self.builder_secret or "").strip()
            and (self.builder_passphrase or "").strip()
        )

    def __repr__(self) -> str:
        # Redacted: Builder secrets must never appear in logs/exceptions.
        return (
            "RelayerRuntimeConfig(url=%r, builder_key=<redacted>, "
            "builder_secret=<redacted>, builder_passphrase=<redacted>, rpc_url=%r)"
            % (self.url, self.rpc_url)
        )


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return RELAYER_DEFAULT_URL
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def env_relayer_configured(env) -> bool:
    """True when the ENV credential trio is complete (URL defaults)."""
    return all(
        (env.get(k) or "").strip()
        for k in (ENV_API_KEY_KEY, ENV_SECRET_KEY, ENV_PASSPHRASE_KEY)
    )


def resolve_relayer_config(encrypted_config=None, env=os.environ) -> Optional[RelayerRuntimeConfig]:
    """Resolve a runtime config from an already-decrypted dict or from ENV.

    ``encrypted_config`` may be a dict with keys ``builder_api_key``,
    ``builder_secret``, ``builder_passphrase`` and optional ``relayer_url`` /
    ``rpc_url`` (e.g. freshly decrypted DB values). When ``None``, falls back
    to the ENV credentials. Returns ``None`` when neither source is complete.
    """
    if encrypted_config is not None:
        api_key = str(encrypted_config.get("builder_api_key") or "").strip()
        secret = str(encrypted_config.get("builder_secret") or "").strip()
        passphrase = str(encrypted_config.get("builder_passphrase") or "").strip()
        if api_key and secret and passphrase:
            url = _normalize_url(
                encrypted_config.get("relayer_url") or env.get(ENV_URL_KEY) or RELAYER_DEFAULT_URL
            )
            rpc_url = encrypted_config.get("rpc_url") or env.get(ENV_RPC_URL_KEY) or None
            return RelayerRuntimeConfig(
                url=url,
                builder_key=api_key,
                builder_secret=secret,
                builder_passphrase=passphrase,
                rpc_url=rpc_url,
            )
    if env_relayer_configured(env):
        return RelayerRuntimeConfig(
            url=_normalize_url(env.get(ENV_URL_KEY) or RELAYER_DEFAULT_URL),
            builder_key=str(env.get(ENV_API_KEY_KEY) or "").strip(),
            builder_secret=str(env.get(ENV_SECRET_KEY) or "").strip(),
            builder_passphrase=str(env.get(ENV_PASSPHRASE_KEY) or "").strip(),
            rpc_url=env.get(ENV_RPC_URL_KEY) or None,
        )
    return None


def load_relayer_runtime_config(
    db, encryption_key, env=os.environ
) -> Optional[RelayerRuntimeConfig]:
    """Load the active runtime config: encrypted DB first, ENV fallback.

    Before login ``encryption_key`` is ``None`` and the DB row cannot be
    decrypted - this is expected behavior; ENV still works, and after login the
    DB credentials become available again. ENV secrets are never written to
    the database.
    """
    if db is not None and encryption_key is not None:
        row = db.get_relayer_credentials()
        if row:
            try:
                api_key = decrypt(row["encrypted_api_key"], encryption_key)
                secret = decrypt(row["encrypted_secret"], encryption_key)
                passphrase = decrypt(row["encrypted_passphrase"], encryption_key)
            except Exception:
                logger.warning(
                    "stored Relayer credentials could not be decrypted; falling back to ENV"
                )
            else:
                return resolve_relayer_config(
                    {
                        "builder_api_key": api_key,
                        "builder_secret": secret,
                        "builder_passphrase": passphrase,
                    },
                    env=env,
                )
    return resolve_relayer_config(None, env=env)


# --------------------------------------------------------------------------
# Encrypted storage helpers (thin wrappers over utils.crypto)
# --------------------------------------------------------------------------


def encrypt_credential_trio(api_key: str, secret: str, passphrase: str, encryption_key: bytes):
    """Return the three encrypted blobs; all inputs must be non-empty strings."""
    for value, label in (
        (api_key, "Builder API Key"),
        (secret, "Builder Secret"),
        (passphrase, "Builder Passphrase"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s 不能为空" % label)
    return (
        encrypt(api_key, encryption_key),
        encrypt(secret, encryption_key),
        encrypt(passphrase, encryption_key),
    )


def decrypt_credential_trio(
    encrypted_api_key, encrypted_secret, encrypted_passphrase, encryption_key: bytes
):
    """Decrypt the trio; raises on wrong key / corrupt data (caller falls back)."""
    return (
        decrypt(encrypted_api_key, encryption_key),
        decrypt(encrypted_secret, encryption_key),
        decrypt(encrypted_passphrase, encryption_key),
    )


# --------------------------------------------------------------------------
# Read-only preflight
# --------------------------------------------------------------------------


class PreflightResult:
    """Per-wallet read-only preflight outcome (safe to persist for display)."""

    def __init__(
        self,
        wallet: str,
        ready: bool,
        reason: str,
        *,
        tested_at: float = None,
        deposit_wallet_deployed: Optional[bool] = None,
        detail: str = "",
    ):
        self.wallet = wallet
        self.ready = bool(ready)
        self.reason = reason
        self.tested_at = time.time() if tested_at is None else tested_at
        self.deposit_wallet_deployed = deposit_wallet_deployed
        self.detail = detail

    @property
    def reason_cn(self) -> str:
        return REASON_CN.get(self.reason, self.reason)

    def to_dict(self) -> dict:
        return {
            "wallet": self.wallet,
            "ready": self.ready,
            "reason": self.reason,
            "reason_cn": self.reason_cn,
            "tested_at": self.tested_at,
            "deposit_wallet_deployed": self.deposit_wallet_deployed,
            "detail": self.detail,
        }


def _bounded(fn, timeout: float):
    """Run ``fn`` under a timeout (the SDK's ``get()`` has no timeout)."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        return future.result(timeout=timeout)


def run_wallet_preflight(
    wallet: dict,
    private_key: str,
    *,
    db,
    encryption_key,
    env=os.environ,
    client_cls=None,
    trading_enabled: bool = True,
    relayer_config_override=None,
    timeout: float = PREFLIGHT_TIMEOUT_SEC,
) -> PreflightResult:
    """Run the strictly read-only credential/funder preflight for one wallet.

    Order of checks (fail-closed):
    A. credential structure is resolvable (DB then ENV)
    B. RelayClient initializes with the explicit config
    C. ``get_expected_deposit_wallet()`` (RPC reads only)
    D. expected == stored funder
    E. authenticated nonce query succeeds
    F. CLOB ``trading_enabled`` remains true

    Never mutates chain/CLOB state. Never calls:
    ``execute_deposit_wallet_batch`` / ``deploy_deposit_wallet`` /
    ``submit_merge`` / order placement.
    """
    from py_builder_relayer_client.client import RelayClient
    from py_builder_relayer_client.models import TransactionType
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    address = wallet.get("address", "")
    if int(wallet.get("signature_type", -1)) != 3:
        return PreflightResult(address, False, NOT_TYPE3)
    if not trading_enabled:
        return PreflightResult(
            address,
            False,
            TRADING_DISABLED,
            detail=wallet.get("trading_block_reason") or "",
        )
    try:
        template = db.get_template_for(address) if db is not None else {}
    except Exception:
        template = {}
    if not template.get("merge_enabled", True):
        return PreflightResult(address, False, TEMPLATE_DISABLED)
    config = (
        relayer_config_override
        if relayer_config_override is not None
        else load_relayer_runtime_config(db, encryption_key, env=env)
    )
    if config is None or not config.configured:
        return PreflightResult(address, False, RELAYER_NOT_CONFIGURED)

    client_cls = client_cls or RelayClient

    def _network_phase():
        creds = BuilderApiKeyCreds(
            key=config.builder_key,
            secret=config.builder_secret,
            passphrase=config.builder_passphrase,
        )
        client = client_cls(
            config.url,
            137,
            private_key=private_key,
            builder_config=BuilderConfig(local_builder_creds=creds),
            rpc_url=config.rpc_url,
        )
        expected = client.get_expected_deposit_wallet()
        funder = str(wallet.get("funder") or "").strip()
        if not funder or expected.lower() != funder.lower():
            return PreflightResult(address, False, FUNDER_MISMATCH)
        nonce_payload = client.get_nonce(
            client.signer.address(), TransactionType.WALLET.value
        )
        if not nonce_payload or nonce_payload.get("nonce") is None:
            return PreflightResult(
                address, False, RELAYER_UNREACHABLE, detail="Relayer 未返回有效 nonce"
            )
        deployed = None
        try:
            deployed = bool(client.get_deployed(funder, TransactionType.WALLET.value))
        except Exception:
            deployed = None  # display-only metadata; never blocks READY
        return PreflightResult(address, True, READY, deposit_wallet_deployed=deployed)

    try:
        return _bounded(_network_phase, timeout)
    except TimeoutError:
        return PreflightResult(
            address, False, RELAYER_UNREACHABLE, detail="Relayer 请求超时"
        )
    except Exception as exc:
        reason, detail = _classify_exception(exc)
        return PreflightResult(address, False, reason, detail=detail)


def _classify_exception(exc: Exception):
    """Map SDK/network exceptions to a reason + sanitized detail (no secrets)."""
    try:
        from py_builder_relayer_client.exceptions import RelayerApiException
    except Exception:
        RelayerApiException = ()
    if isinstance(exc, RelayerApiException):
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return RELAYER_AUTH_FAILED, "Relayer 拒绝了凭据（HTTP %s）" % status
        return RELAYER_UNREACHABLE, "Relayer 请求失败（HTTP %s）" % (status or "网络错误")
    if isinstance(exc, (ConnectionError, OSError)):
        return RELAYER_UNREACHABLE, "Relayer 网络连接失败"
    # Any other failure while deriving/validating the Deposit Wallet
    # (RPC errors, SDK contract-config issues, ...) - safe default.
    return DEPOSIT_WALLET_VALIDATION_FAILED, "Deposit Wallet 校验不可用"


# --------------------------------------------------------------------------
# Per-wallet status aggregation for the UI (capability vs template enablement)
# --------------------------------------------------------------------------


def wallet_merge_status(
    wallet: dict,
    template: dict,
    relayer_configured: bool,
    test_result: Optional[dict],
) -> dict:
    """Combine capability (Type3 + validated funder + relayer + trading) with
    the template's ``merge_enabled`` choice into the UI contract:
    ``merge_capable`` / ``merge_enabled`` / ``merge_status`` / ``merge_reason``.
    """
    is_type3 = int(wallet.get("signature_type", -1)) == 3
    funder = str(wallet.get("funder") or "").strip()
    # None means "unknown yet" (API not built) — only an explicit False is a
    # disabled state; the read-only preflight re-checks the real value.
    trading_enabled = wallet.get("trading_enabled")
    trading_blocked = trading_enabled is False
    merge_enabled = bool(template.get("merge_enabled", True))

    capability = is_type3 and not trading_blocked and relayer_configured and bool(funder)
    if not is_type3:
        status, reason = "not_ready", NOT_TYPE3
    elif not merge_enabled:
        status, reason = "template_disabled", TEMPLATE_DISABLED
    elif trading_blocked:
        status, reason = "not_ready", TRADING_DISABLED
    elif not relayer_configured:
        status, reason = "not_ready", RELAYER_NOT_CONFIGURED
    elif not funder:
        status, reason = "not_ready", FUNDER_MISMATCH
    elif test_result and isinstance(test_result, dict) and test_result.get("last_test_ok"):
        status, reason = "ready", READY
    elif test_result and isinstance(test_result, dict):
        stored = test_result.get("last_test_reason") or RELAYER_AUTH_FAILED
        status, reason = "not_ready", stored if stored in REASON_CN else RELAYER_AUTH_FAILED
    else:
        status, reason = "not_ready", RELAYER_NOT_TESTED

    return {
        "merge_capable": capability,
        "merge_enabled": merge_enabled,
        "merge_status": status,
        "merge_reason": reason,
        "merge_reason_cn": REASON_CN.get(reason, reason),
        "merge_available": status == "ready",
        "deposit_wallet_deployed": (
            test_result.get("deposit_wallet_deployed")
            if test_result and isinstance(test_result, dict)
            else None
        ),
        "merge_last_test_at": (
            test_result.get("last_test_at")
            if test_result and isinstance(test_result, dict)
            else None
        ),
        "merge_last_test_ok": (
            test_result.get("last_test_ok")
            if test_result and isinstance(test_result, dict)
            else None
        ),
    }


def preflight_all_wallets(
    db,
    encryption_key,
    env=os.environ,
    private_key_for=None,
    trading_enabled_for=None,
    config_override=None,
    enabled_only=True,
) -> list:
    """Run the read-only preflight for every Type3 wallet.

    ``private_key_for`` / ``trading_enabled_for`` are callables ``(wallet) ->
    value`` injected by the web layer (which owns key decryption / API state);
    when omitted, keys are decrypted here with the login key.
    """
    if db is None:
        return []
    results = []
    for wallet in db.list_wallets():
        if int(wallet.get("signature_type", -1)) != 3:
            continue
        if enabled_only and not wallet.get("enabled", 1):
            continue
        if private_key_for is not None:
            private_key = private_key_for(wallet)
            if private_key is None:
                results.append(
                    PreflightResult(wallet["address"], False, KEY_UNAVAILABLE)
                )
                continue
        else:
            try:
                private_key = decrypt(wallet["encrypted_key"], encryption_key)
            except Exception:
                results.append(
                    PreflightResult(wallet["address"], False, KEY_UNAVAILABLE)
                )
                continue
        trading = True
        if trading_enabled_for is not None:
            trading = bool(trading_enabled_for(wallet))
        results.append(
            run_wallet_preflight(
                wallet,
                private_key,
                db=db,
                encryption_key=encryption_key,
                env=env,
                trading_enabled=trading,
                relayer_config_override=config_override,
            )
        )
    return results


def relayer_status_metadata(db, encryption_key, env=os.environ) -> dict:
    """Non-secret metadata for GET /api/relayer-config."""
    try:
        row = db.get_relayer_credentials() if db is not None else None
    except Exception:
        row = None
    db_configured = bool(row and encryption_key is not None)
    env_configured = env_relayer_configured(env)
    if db_configured:
        source = "encrypted_db"
        configured = True
    elif env_configured:
        source = "env"
        configured = True
    else:
        source = "unavailable"
        configured = False
    config = load_relayer_runtime_config(db, encryption_key, env=env)
    url = config.url if config is not None else (
        _normalize_url(env.get(ENV_URL_KEY) or RELAYER_DEFAULT_URL)
    )
    try:
        results = db.get_relayer_test_results() if db is not None else {}
    except Exception:
        results = {}
    if not isinstance(results, dict):
        results = {}
    any_ok = any(r.get("last_test_ok") for r in results.values())
    any_fail = bool(results) and not any_ok
    last_test_at = max(
        (r.get("last_test_at") or 0 for r in results.values()), default=None
    )
    reason = None
    if any_fail:
        reason = next(
            (
                r.get("last_test_reason")
                for r in results.values()
                if r.get("last_test_reason")
            ),
            None,
        )
    return {
        "configured": configured,
        "source": source,
        "relayer_url": url,
        "relayer_url_source": "env" if env.get(ENV_URL_KEY) else "default",
        "auth_mode": "builder",
        "last_test_ok": bool(any_ok) if (any_ok or any_fail) else None,
        "last_test_at": last_test_at,
        "last_test_reason": None if any_ok else reason,
        "reason": None if any_ok else reason,
    }
