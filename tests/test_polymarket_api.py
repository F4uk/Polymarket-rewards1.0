"""tests/test_polymarket_api.py — PolymarketAPI 构造时的 L1 凭证派生。"""

import pytest
from unittest.mock import MagicMock, patch

from api.polymarket_api import (
    PolymarketAPI,
    pick_funded_sig_type,
    resolve_signature_type,
)


@patch("api.polymarket_api.derive_deposit_address", return_value="0xFunder")
@patch("api.polymarket_api.ClobClient")
def test_init_creates_api_creds_for_new_wallet(mock_clob, _mock_derive):
    """新钱包从没在 Polymarket 创建过 API key,derive 会取不到。

    构造必须走 create_or_derive_api_key(先 create,失败再 derive),否则
    新导入的浏览器钱包会报 'Could not derive api key!'。
    """
    temp = MagicMock()
    temp.get_address.return_value = "0xEOA"
    mock_clob.return_value = temp  # 两次 ClobClient(...) 都返回同一个 mock

    PolymarketAPI("0xprivkey")

    temp.create_or_derive_api_key.assert_called_once()
    # derive-only 路径对新钱包会失败,不能再单独依赖它。
    temp.derive_api_key.assert_not_called()


class TestResolveSignatureType:
    def test_blank_funder_is_safe(self):
        assert resolve_signature_type("0xSafe", None) == 2
        assert resolve_signature_type("0xSafe", "") == 2

    def test_funder_equals_derived_safe_is_safe(self):
        # 大小写不敏感比较
        assert resolve_signature_type("0xAbCdef", "0xabcdef") == 2

    def test_funder_differs_is_proxy(self):
        # 填的存款地址 ≠ 派生 Safe → Polymarket Proxy 账户
        assert resolve_signature_type("0xSafe", "0x8CfA") == 1


class TestPickFundedSigType:
    def test_picks_sig_with_balance(self):
        # POLY_1271(智能合约钱包)账户:只有 sig=3 有钱
        assert pick_funded_sig_type({0: "0", 1: "0", 2: "0", 3: "10000000"}) == 3

    def test_all_zero_returns_none(self):
        assert pick_funded_sig_type({0: "0", 1: "0", 2: "0", 3: "0"}) is None

    def test_picks_safe_when_funded_there(self):
        assert pick_funded_sig_type({0: "0", 2: "5000000"}) == 2

    def test_ignores_error_entries(self):
        assert pick_funded_sig_type({1: "ERR boom", 3: "10000000"}) == 3


@patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
@patch("api.polymarket_api.ClobClient")
def test_init_passes_signature_type_and_funder(mock_clob, _mock_derive):
    temp = MagicMock()
    temp.get_address.return_value = "0xEOA"
    mock_clob.return_value = temp

    PolymarketAPI("0xpk", signature_type=1, funder="0xPROXY")

    # 第二次构造(带 L2 鉴权的正式 client)必须用传入的 sig 和 funder
    full_call = mock_clob.call_args_list[1]
    assert full_call.kwargs["signature_type"] == 1
    assert full_call.kwargs["funder"] == "0xPROXY"


@patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
@patch("api.polymarket_api.ClobClient")
def test_get_balance_uses_instance_signature_type(mock_clob, _mock_derive):
    temp = MagicMock()
    temp.get_address.return_value = "0xEOA"
    temp.get_balance_allowance.return_value = {"balance": "10000000"}
    mock_clob.return_value = temp

    api = PolymarketAPI("0xpk", signature_type=1, funder="0xPROXY")
    bal = api.get_balance()

    params = temp.get_balance_allowance.call_args.args[0]
    assert params.signature_type == 1
    assert bal == 10.0


def _api_with_mock_client(mock_clob):
    """Build a PolymarketAPI whose .client is the shared mock."""
    temp = MagicMock()
    temp.get_address.return_value = "0xEOA"
    mock_clob.return_value = temp
    return PolymarketAPI("0xpk", signature_type=2), temp


class TestNegRiskAutoResolve:
    """卖单未显式指定 neg_risk 时,必须传 None 让底层客户端按 token 自查真实
    neg_risk(client.create_*_order 仅在 options.neg_risk is None 时才回退到
    get_neg_risk(token_id))。写死 False 会让负风险市场的止盈/止损卖单签到错误的
    交易所合约、被拒,导致该持仓既挂不出止盈卖单、止损也平不掉仓(2026-06-02 事故)。"""

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_limit_sell_defaults_neg_risk_to_none(self, mock_clob, _mock_derive):
        api, client = _api_with_mock_client(mock_clob)
        api.place_limit_sell("tok1", 0.30, 100, tick_size="0.01")
        options = client.create_and_post_order.call_args.args[1]
        assert options.neg_risk is None

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_market_sell_defaults_neg_risk_to_none(self, mock_clob, _mock_derive):
        api, client = _api_with_mock_client(mock_clob)
        api.place_market_sell("tok1", 1000)
        options = client.create_and_post_market_order.call_args.args[1]
        assert options.neg_risk is None

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_limit_buy_defaults_neg_risk_to_none(self, mock_clob, _mock_derive):
        api, client = _api_with_mock_client(mock_clob)
        api.place_limit_buy("tok1", 0.30, 100, tick_size="0.01")
        options = client.create_and_post_order.call_args.args[1]
        assert options.neg_risk is None

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_explicit_neg_risk_true_is_honored_on_sell(self, mock_clob, _mock_derive):
        api, client = _api_with_mock_client(mock_clob)
        api.place_limit_sell("tok1", 0.30, 100, neg_risk=True)
        options = client.create_and_post_order.call_args.args[1]
        assert options.neg_risk is True

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_explicit_neg_risk_false_is_honored_on_buy(self, mock_clob, _mock_derive):
        api, client = _api_with_mock_client(mock_clob)
        api.place_limit_buy("tok1", 0.30, 100, neg_risk=False)
        options = client.create_and_post_order.call_args.args[1]
        assert options.neg_risk is False


class TestMarketSellUsesFAK:
    """止损市价单应为 FAK(fill-and-kill,能成交多少先成交多少),而非 FOK
    (fill-or-kill,整笔成不了就全杀)——否则持仓 > 买一档深度时止损永远打不出去
    (2026-06-02 审计 F3)。"""

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_market_sell_is_fak_not_fok(self, mock_clob, _mock_derive):
        from py_clob_client_v2.clob_types import OrderType

        api, client = _api_with_mock_client(mock_clob)
        api.place_market_sell("tok1", 1000)
        order_type = client.create_and_post_market_order.call_args.args[2]
        assert order_type == OrderType.FAK


class TestOrderResponseChecked:
    """下单封装必须检查交易所应用层响应:HTTP 200 但 success=False 或 FOK/FAK
    status='unmatched'(未成交)时要抛错,否则 monitor 会把没挂上的单/没卖出的仓
    当成已成交,记下幻影止损成交、留下假'已挂卖单'记录(2026-06-02 审计 F2)。"""

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_limit_sell_raises_on_success_false(self, mock_clob, _mock_derive):
        from api.polymarket_api import OrderRejected

        api, client = _api_with_mock_client(mock_clob)
        client.create_and_post_order.return_value = {
            "success": False,
            "errorMsg": "not enough balance",
        }
        with pytest.raises(OrderRejected):
            api.place_limit_sell("tok1", 0.30, 100)

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_market_sell_raises_on_unmatched(self, mock_clob, _mock_derive):
        from api.polymarket_api import OrderRejected

        api, client = _api_with_mock_client(mock_clob)
        client.create_and_post_market_order.return_value = {
            "success": True,
            "status": "unmatched",
        }
        with pytest.raises(OrderRejected):
            api.place_market_sell("tok1", 1000)

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_limit_buy_ok_on_live_status(self, mock_clob, _mock_derive):
        api, client = _api_with_mock_client(mock_clob)
        client.create_and_post_order.return_value = {
            "success": True,
            "status": "live",
            "orderID": "0xabc",
        }
        res = api.place_limit_buy("tok1", 0.30, 100)
        assert res["orderID"] == "0xabc"

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_market_sell_ok_on_matched(self, mock_clob, _mock_derive):
        api, client = _api_with_mock_client(mock_clob)
        client.create_and_post_market_order.return_value = {
            "success": True,
            "status": "matched",
        }
        res = api.place_market_sell("tok1", 1000)
        assert res["status"] == "matched"


class TestCancelResponseObserved:
    """批量撤单的应用层失败(HTTP 200 但 not_canceled 非空 / success=False)不能静默
    吞掉:至少 WARNING 记录,便于诊断"幻影撤单→重复挂单"(F5)。不抛异常(调用方各有
    兜底,自愈靠下一轮对账)。"""

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_cancel_orders_warns_on_app_level_failure(
        self, mock_clob, _mock_derive, caplog
    ):
        import logging

        api, client = _api_with_mock_client(mock_clob)
        client.cancel_orders.return_value = {
            "canceled": [],
            "not_canceled": {"o1": "x"},
        }
        with caplog.at_level(logging.WARNING):
            res = api.cancel_orders(["o1"])
        assert res == {"canceled": [], "not_canceled": {"o1": "x"}}  # 原样返回、不抛
        assert any("未撤" in r.message for r in caplog.records)

    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_cancel_orders_silent_on_clean_success(
        self, mock_clob, _mock_derive, caplog
    ):
        import logging

        api, client = _api_with_mock_client(mock_clob)
        client.cancel_orders.return_value = {"canceled": ["o1"], "not_canceled": {}}
        with caplog.at_level(logging.WARNING):
            api.cancel_orders(["o1"])
        assert not any("未撤" in r.message for r in caplog.records)


class TestPositionsPagination:
    """get_user_positions 需传 limit 抬高单页上限,避免持仓被服务端默认页大小静默截断
    导致漏离场/止损(F7)。"""

    @patch("api.proxy.requests")
    @patch("api.polymarket_api.derive_deposit_address", return_value="0xDerivedSafe")
    @patch("api.polymarket_api.ClobClient")
    def test_get_user_positions_sends_limit(
        self, mock_clob, _mock_derive, mock_requests
    ):
        api, client = _api_with_mock_client(mock_clob)
        resp = MagicMock()
        resp.json.return_value = []
        mock_requests.get.return_value = resp
        api.get_user_positions("0xfunder")
        params = mock_requests.get.call_args.kwargs["params"]
        assert params.get("user") == "0xfunder"
        assert params.get("limit") == 500
        assert params.get("sizeThreshold") == 0


class TestGetRewardsForMarketNoPartialPayload:
    """分页中途出错必须返回 `[]`,不能返回已取到的半份数据。

    调用方把返回的项求和当成市场的每日奖励(`extract_daily_rate`),半份数据算出来的
    是个「看着合理但偏小」的奖励额,足以让正在赚奖励的在挂买单被 Step3 判成跌破
    `min_reward_usd` 而撤掉 —— 正是「绝不 fail-close」这条不变量禁止的结果。
    """

    def _page(self, items, next_cursor):
        resp = MagicMock()
        resp.json.return_value = {"data": items, "next_cursor": next_cursor}
        return resp

    @patch("api.polymarket_api.http_get")
    def test_error_mid_pagination_returns_empty_not_partial(self, mock_get):
        item = {"rewards_config": [{"rate_per_day": 100}]}
        mock_get.side_effect = [
            self._page([item], "NEXT"),  # 第一页成功
            RuntimeError("connection reset"),  # 第二页炸
        ]
        assert PolymarketAPI.get_rewards_for_market("0xcid") == []

    @patch("api.polymarket_api.http_get")
    def test_error_on_first_page_returns_empty(self, mock_get):
        mock_get.side_effect = RuntimeError("timeout")
        assert PolymarketAPI.get_rewards_for_market("0xcid") == []

    @patch("api.polymarket_api.http_get")
    def test_full_pagination_returns_all_items(self, mock_get):
        a = {"rewards_config": [{"rate_per_day": 100}]}
        b = {"rewards_config": [{"rate_per_day": 20}]}
        mock_get.side_effect = [self._page([a], "NEXT"), self._page([b], "LTE=")]
        assert PolymarketAPI.get_rewards_for_market("0xcid") == [a, b]


class TestPerWalletProxy:
    @patch("api.polymarket_api.ClobClient")
    @patch("api.polymarket_api.derive_deposit_address", return_value="0xS")
    def test_proxy_url_parsed_from_init(self, _mock_derive, mock_clob):
        temp = MagicMock()
        temp.get_address.return_value = "0xE"
        mock_clob.return_value = temp
        api = PolymarketAPI("0xpk", proxy="h:1000:u:p")
        assert api.proxy_url == "http://u:p@h:1000"

    @patch("api.polymarket_api.ClobClient")
    @patch("api.polymarket_api.derive_deposit_address", return_value="0xS")
    def test_no_proxy_is_none(self, _mock_derive, mock_clob):
        temp = MagicMock()
        temp.get_address.return_value = "0xE"
        mock_clob.return_value = temp
        assert PolymarketAPI("0xpk").proxy_url is None

    @patch("api.proxy.requests.get")
    @patch("api.polymarket_api.ClobClient")
    @patch("api.polymarket_api.derive_deposit_address", return_value="0xS")
    def test_get_user_positions_routes_through_wallet_proxy(
        self, _mock_derive, mock_clob, mock_get
    ):
        temp = MagicMock()
        temp.get_address.return_value = "0xE"
        mock_clob.return_value = temp
        resp = MagicMock()
        resp.json.return_value = []
        mock_get.return_value = resp
        api = PolymarketAPI("0xpk", proxy="h:1000:u:p")
        api.get_user_positions("0xfunder")
        assert mock_get.call_args.kwargs["proxies"] == {
            "http": "http://u:p@h:1000",
            "https": "http://u:p@h:1000",
        }
