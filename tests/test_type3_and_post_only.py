"""Type3 fail-closed and order semantic tests; all CLOB/Relayer work is mocked."""

from unittest.mock import MagicMock, patch

import pytest
from eth_utils import keccak
from py_clob_client_v2.clob_types import OrderType

from api.ctf import (
    APPROVAL_SIGNATURE,
    CONDITIONAL_TOKENS,
    CTF_COLLATERAL_ADAPTER,
    RelayerUnavailable,
    Type3MergeClient,
    merge_approval_calldata,
    relayer_configured,
)
from api.polymarket_api import OrderRejected, PolymarketAPI


def _api(mock_clob, *, signature_type=2, funder=None):
    client = MagicMock()
    client.get_address.return_value = "0xEOA"
    mock_clob.return_value = client
    return PolymarketAPI("0x" + "1" * 64, signature_type=signature_type, funder=funder), client


@patch("api.polymarket_api.derive_deposit_address", return_value="0xSafe")
@patch("api.polymarket_api.ClobClient")
def test_reward_buy_is_gtc_post_only(mock_clob, _safe):
    api, client = _api(mock_clob)
    api.place_limit_buy("token", 0.3, 10)
    assert client.create_and_post_order.call_args.args[2] == OrderType.GTC
    assert client.create_and_post_order.call_args.kwargs["post_only"] is True


@patch("api.polymarket_api.derive_deposit_address", return_value="0xSafe")
@patch("api.polymarket_api.ClobClient")
def test_v2_maker_escape_sell_is_gtc_post_only(mock_clob, _safe):
    # Audit fix H: the V2 maker escape SELL must be GTC + POST-ONLY so it can
    # never accidentally become a taker order.
    api, client = _api(mock_clob)
    api.place_post_only_sell("token", 0.29, 10)
    assert client.create_and_post_order.call_args.args[2] == OrderType.GTC
    assert client.create_and_post_order.call_args.kwargs["post_only"] is True
    args = client.create_and_post_order.call_args.args[0]
    assert args.side == "SELL"


@patch("api.polymarket_api.derive_deposit_address", return_value="0xSafe")
@patch("api.polymarket_api.ClobClient")
def test_v2_maker_escape_sell_runs_inside_wallet_proxy(mock_clob, _safe):
    # Fix pack 2: place_post_only_sell must receive the same per-wallet proxy
    # context as every other wallet network method.
    from api.proxy import current_proxy

    client = MagicMock()
    client.get_address.return_value = "0xEOA"
    mock_clob.return_value = client
    api = PolymarketAPI(
        "0x" + "1" * 64, signature_type=2, funder=None, proxy="h:1000:u:p"
    )
    seen = {}

    def record(*a, **kw):
        seen["proxy"] = current_proxy.get()
        return {"success": True, "orderID": "o-1"}

    client.create_and_post_order.side_effect = record
    api.place_post_only_sell("token", 0.29, 10)
    assert seen["proxy"] == "http://u:p@h:1000"


@patch("api.polymarket_api.derive_deposit_address", return_value="0xSafe")
@patch("api.polymarket_api.ClobClient")
def test_rejected_post_only_does_not_fallback(mock_clob, _safe):
    api, client = _api(mock_clob)
    client.create_and_post_order.return_value = {"success": False, "errorMsg": "crosses"}
    with pytest.raises(OrderRejected):
        api.place_limit_buy("token", 0.3, 10)
    assert client.create_and_post_order.call_count == 1
    client.create_and_post_market_order.assert_not_called()


@patch("api.polymarket_api.expected_type3_deposit_wallet", return_value="0xExpected")
@patch("api.polymarket_api.derive_deposit_address", return_value="0xSafe")
@patch("api.polymarket_api.ClobClient")
def test_type3_expected_funder_allows_trading(mock_clob, _safe, _expected):
    api, _ = _api(mock_clob, signature_type=3, funder="0xexpected")
    assert api.trading_enabled is True


@patch("api.polymarket_api.expected_type3_deposit_wallet", return_value="0xExpected")
@patch("api.polymarket_api.derive_deposit_address", return_value="0xSafe")
@patch("api.polymarket_api.ClobClient")
def test_type3_mismatch_fails_closed(mock_clob, _safe, _expected):
    api, client = _api(mock_clob, signature_type=3, funder="0xWrong")
    assert api.trading_enabled is False
    with pytest.raises(OrderRejected):
        api.place_limit_buy("token", 0.3, 10)
    client.create_and_post_order.assert_not_called()


@patch("api.polymarket_api.derive_deposit_address", return_value="0xSafe")
@patch("api.polymarket_api.ClobClient")
def test_type1_and_type2_do_not_use_type3_validation(mock_clob, _safe):
    one, _ = _api(mock_clob, signature_type=1, funder="0xProxy")
    two, _ = _api(mock_clob, signature_type=2, funder="0xSafe")
    assert one.trading_enabled and two.trading_enabled


def test_missing_relayer_configuration_is_safe_and_does_not_construct_client():
    env = {
        "PMM_RELAYER_URL": "https://relayer.example",
        "PMM_BUILDER_API_KEY": "",
        "PMM_BUILDER_SECRET": "",
        "PMM_BUILDER_PASSPHRASE": "",
    }
    assert relayer_configured(env) is False
    with pytest.raises(RelayerUnavailable):
        Type3MergeClient("0x" + "1" * 64, env=env)


def test_type3_merge_batches_ctf_approval_before_adapter_call():
    env = {
        "PMM_RELAYER_URL": "https://relayer.example",
        "PMM_BUILDER_API_KEY": "test-key",
        "PMM_BUILDER_SECRET": "test-secret",
        "PMM_BUILDER_PASSPHRASE": "test-passphrase",
    }
    relayer = MagicMock()
    relayer.get_expected_deposit_wallet.return_value = "0xf000000000000000000000000000000000000001"
    relayer.signer.address.return_value = "0xEOA"
    relayer.get_nonce.return_value = {"nonce": 7}
    client = Type3MergeClient("0x" + "1" * 64, env=env, relay_client_cls=MagicMock(return_value=relayer))

    client.submit_merge("0xf000000000000000000000000000000000000001", "0x" + "c" * 64, 12.5)

    calls = relayer.execute_deposit_wallet_batch.call_args.args[0]
    assert len(calls) == 2
    assert calls[0].target == CONDITIONAL_TOKENS
    assert calls[0].data == merge_approval_calldata()
    assert calls[0].data[:10] == "0x" + keccak(text=APPROVAL_SIGNATURE)[:4].hex()
    assert calls[1].target == CTF_COLLATERAL_ADAPTER


@patch("api.polymarket_api.derive_deposit_address", return_value="0xSafe")
@patch("api.polymarket_api.ClobClient")
def test_urgent_complement_is_bounded_fok_buy(mock_clob, _safe):
    api, client = _api(mock_clob)
    api.place_complement_fok_buy("token", 12.5, 0.42, tick_size="0.01")
    order_args = client.create_and_post_market_order.call_args.args[0]
    assert order_args.amount == pytest.approx(5.25)
    assert order_args.price == pytest.approx(0.42)
    assert client.create_and_post_market_order.call_args.args[2] == OrderType.FOK
