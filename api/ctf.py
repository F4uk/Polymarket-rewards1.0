"""Narrow Polymarket CTF V2 merge adapter.

The target and ABI are copied from Polymarket's current official
``ctf-exchange-v2`` deployment/source, not inferred from a legacy exchange.
Only the standard same-condition binary adapter entrypoint is exposed.
"""

import os
import time

from eth_abi import encode
from eth_utils import is_address, keccak, to_checksum_address
from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import DepositWalletCall, TransactionType
from py_builder_signing_sdk.config import BuilderConfig
from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds


CHAIN_ID = 137
# Official ctf-exchange-v2 Polygon ordinary CTF adapter deployment.
CTF_COLLATERAL_ADAPTER = "0xADa100874d00e3331D00F2007a9c336a65009718"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
MERGE_SIGNATURE = "mergePositions(address,bytes32,bytes32,uint256[],uint256)"
APPROVAL_SIGNATURE = "setApprovalForAll(address,bool)"


class RelayerUnavailable(RuntimeError):
    """Raised before any transaction request when required credentials are absent."""


def relayer_configured(env=os.environ) -> bool:
    return all(
        (env.get(key) or "").strip()
        for key in ("PMM_RELAYER_URL", "PMM_BUILDER_API_KEY", "PMM_BUILDER_SECRET", "PMM_BUILDER_PASSPHRASE")
    )


def merge_calldata(condition_id: str, amount: float) -> str:
    """Encode CtfCollateralAdapter.mergePositions for a six-decimal pUSD amount."""
    if not str(condition_id).startswith("0x") or len(str(condition_id)) != 66:
        raise ValueError("condition_id must be bytes32 hex")
    raw_amount = int(round(float(amount) * 1_000_000))
    if raw_amount <= 0:
        raise ValueError("merge amount must be positive")
    selector = keccak(text=MERGE_SIGNATURE)[:4]
    args = encode(
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        ["0x0000000000000000000000000000000000000000", bytes(32), bytes.fromhex(condition_id[2:]), [1, 2], raw_amount],
    )
    # The adapter retains the legacy parameters for compatibility but ignores
    # collateral and parent/partition internally; zero + [1,2] documents the
    # ordinary binary intent and matches its public signature.
    return "0x" + (selector + args).hex()


def merge_approval_calldata() -> str:
    """Allow the official adapter to transfer CTF shares from the Deposit Wallet.

    ``CtfCollateralAdapter.mergePositions`` uses ERC-1155 ``safeTransferFrom``
    to pull the complete set. This idempotent approval is executed by the
    Deposit Wallet in the same relayer batch before every merge.
    """
    selector = keccak(text=APPROVAL_SIGNATURE)[:4]
    args = encode(["address", "bool"], [to_checksum_address(CTF_COLLATERAL_ADAPTER), True])
    return "0x" + (selector + args).hex()


class Type3MergeClient:
    """Official Relayer WALLET-batch execution for a POLY_1271 deposit wallet."""

    def __init__(self, private_key: str, *, env=os.environ, relay_client_cls=RelayClient):
        if not relayer_configured(env):
            raise RelayerUnavailable("Relayer credentials are not configured")
        creds = BuilderApiKeyCreds(
            key=env["PMM_BUILDER_API_KEY"],
            secret=env["PMM_BUILDER_SECRET"],
            passphrase=env["PMM_BUILDER_PASSPHRASE"],
        )
        self.client = relay_client_cls(
            env["PMM_RELAYER_URL"],
            CHAIN_ID,
            private_key=private_key,
            builder_config=BuilderConfig(local_builder_creds=creds),
            rpc_url=(env.get("PMM_POLYGON_RPC_URL") or None),
        )

    def expected_deposit_wallet(self) -> str:
        return self.client.get_expected_deposit_wallet()

    def submit_merge(self, funder: str, condition_id: str, qty: float):
        expected = self.expected_deposit_wallet()
        if not is_address(str(funder)):
            raise RelayerUnavailable("Type3 funder is not a valid Deposit Wallet address")
        funder = to_checksum_address(funder)
        if expected.lower() != funder.lower():
            raise RelayerUnavailable("Type3 funder does not match expected Deposit Wallet")
        nonce_payload = self.client.get_nonce(self.client.signer.address(), TransactionType.WALLET.value)
        if not nonce_payload or nonce_payload.get("nonce") is None:
            raise RelayerUnavailable("Relayer did not provide a Deposit Wallet nonce")
        approval = DepositWalletCall(
            target=to_checksum_address(CONDITIONAL_TOKENS), value="0", data=merge_approval_calldata()
        )
        call = DepositWalletCall(
            target=to_checksum_address(CTF_COLLATERAL_ADAPTER), value="0", data=merge_calldata(condition_id, qty)
        )
        # Short future deadline prevents a stale signed batch from being usable indefinitely.
        return self.client.execute_deposit_wallet_batch(
            [approval, call], funder, str(nonce_payload["nonce"]), str(int(time.time()) + 300)
        )

    def get_transaction(self, relayer_id: str):
        return self.client.get_transaction(relayer_id)
