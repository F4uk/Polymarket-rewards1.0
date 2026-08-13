"""Offline identity tests for py-clob-client-v2 authentication and orders.

These tests build headers and signed order payloads locally.  They never call
the CLOB, relayer, RPC, or submit an order.
"""

import base64
from unittest.mock import MagicMock, patch

import pytest
from py_clob_client_v2.clob_types import (
    ApiCreds,
    CreateOrderOptions,
    OrderArgs,
    RequestArgs,
)
from py_clob_client_v2.headers.headers import (
    create_level_1_headers,
    create_level_2_headers,
)
from py_clob_client_v2.order_builder.builder import OrderBuilder
from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
from py_clob_client_v2.signer import Signer

from api.polymarket_api import CHAIN_ID, POLYMARKET_HOST, PolymarketAPI


PRIVATE_KEY = "0x" + "1" * 64
FUNDER = "0x" + "2" * 40


@patch("api.polymarket_api.expected_type3_deposit_wallet", return_value=FUNDER)
@patch("api.polymarket_api.ClobClient")
def test_type3_reuses_eoa_credentials_with_explicit_deposit_wallet(
    mock_clob, _expected_wallet
):
    credentials = object()
    temporary = MagicMock()
    temporary.create_or_derive_api_key.return_value = credentials
    temporary.get_address.return_value = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
    authenticated = MagicMock()
    mock_clob.side_effect = [temporary, authenticated]

    api = PolymarketAPI(PRIVATE_KEY, signature_type=3, funder=FUNDER)

    first, second = mock_clob.call_args_list
    assert first.kwargs == {
        "host": POLYMARKET_HOST,
        "key": PRIVATE_KEY,
        "chain_id": CHAIN_ID,
    }
    assert second.kwargs == {
        "host": POLYMARKET_HOST,
        "key": PRIVATE_KEY,
        "chain_id": CHAIN_ID,
        "creds": credentials,
        "signature_type": 3,
        "funder": FUNDER,
    }
    temporary.create_or_derive_api_key.assert_called_once_with()
    assert api.client is authenticated
    assert api.trading_enabled is True


def test_l1_l2_headers_identify_eoa_and_l2_uses_supplied_credentials():
    signer = Signer(PRIVATE_KEY, CHAIN_ID)
    credentials = ApiCreds(
        api_key="offline-api-key",
        api_secret=base64.urlsafe_b64encode(b"offline-secret").decode(),
        api_passphrase="offline-passphrase",
    )

    l1 = create_level_1_headers(signer, nonce=7, timestamp=1_700_000_000)
    l2 = create_level_2_headers(
        signer,
        credentials,
        RequestArgs(method="POST", request_path="/order", body={"offline": True}),
        timestamp=1_700_000_000,
    )

    assert l1["POLY_ADDRESS"] == signer.address()
    assert l1["POLY_NONCE"] == "7"
    assert l1["POLY_SIGNATURE"].startswith("0x")
    assert l2["POLY_ADDRESS"] == signer.address()
    assert l2["POLY_API_KEY"] == credentials.api_key
    assert l2["POLY_PASSPHRASE"] == credentials.api_passphrase
    assert l2["POLY_SIGNATURE"]
    assert FUNDER not in l1.values()
    assert FUNDER not in l2.values()


def _build_offline_order(signature_type: SignatureTypeV2):
    signer = Signer(PRIVATE_KEY, CHAIN_ID)
    return signer, OrderBuilder(
        signer, signature_type=signature_type, funder=FUNDER
    ).build_order(
        OrderArgs(token_id="123", price=0.4, size=5.0, side="BUY"),
        CreateOrderOptions(tick_size="0.01", neg_risk=False),
        version=2,
    )


def test_type3_order_uses_deposit_wallet_maker_signer_and_wrapped_signature():
    _signer, order = _build_offline_order(SignatureTypeV2.POLY_1271)

    assert order.maker == FUNDER
    assert order.signer == FUNDER
    assert order.signatureType == SignatureTypeV2.POLY_1271
    assert order.signature.startswith("0x")
    # A normal ECDSA signature is 65 bytes (132 hex characters including 0x).
    # Type3 carries the longer Deposit Wallet ERC-7739/EIP-1271 wrapper.
    assert len(order.signature) > 132


@pytest.mark.parametrize(
    "signature_type",
    [SignatureTypeV2.POLY_PROXY, SignatureTypeV2.POLY_GNOSIS_SAFE],
)
def test_legacy_type1_and_type2_keep_eoa_signer(signature_type):
    signer, order = _build_offline_order(signature_type)

    assert order.maker == FUNDER
    assert order.signer == signer.address()
    assert order.signatureType == signature_type
    assert len(order.signature) == 132
