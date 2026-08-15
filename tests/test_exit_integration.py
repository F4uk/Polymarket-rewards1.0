"""tests/test_exit_integration.py — 快速退出在 OrderMonitor/EngineManager 里的接缝。

验证:监控 check_exit 经路由接管持仓(动作落库、状态快照发布)、管理器重启恢复先对账
活动 Merge 操作;以及 Scanner/Rewards/Gap/Cliff/价格带/黑名单行为未被改动(回归锚点)。
"""

import time
from unittest.mock import MagicMock

from engine.exit_router import ExitRouter
from engine.exit_status import get_status, clear_snapshot
from engine.monitor import OrderMonitor
from models.database import Database

WALLET = "0xWallet"
CID = "0x" + "c1" * 32
YES = "0x" + "aa" * 32


class FakeAPI:
    def __init__(self):
        self.funder = "0xF"
        self.positions = []
        self.open_orders = []
        self.trades = []
        self.books = {}
        self.calls = []

    def get_funder(self):
        return self.funder

    def get_user_positions(self, user):
        return self.positions

    def get_open_orders(self):
        return list(self.open_orders)

    def get_orderbook(self, token):
        return self.books.get(token, {"bids": [], "asks": [], "tick_size": "0.01"})

    def get_orderbooks(self, tokens):
        return {t: self.get_orderbook(t) for t in tokens}

    def get_trades(self, params=None):
        return list(self.trades)

    def gamma_resolution_status(self, cids):
        return {}

    def get_neg_risk(self, token):
        return False

    def place_limit_sell(self, token, price, size, tick_size="0.01", neg_risk=None,
                         order_type="GTC", post_only=False):
        self.calls.append(("sell", token, price, size, order_type, post_only))
        oid = f"s{len(self.calls)}"
        self.open_orders.append({"id": oid, "asset_id": token, "side": "SELL",
                                 "price": price, "original_size": size, "size_matched": 0})
        return {"orderID": oid, "status": "live"}

    def cancel_orders(self, ids):
        self.calls.append(("cancel", ids))
        self.open_orders = [o for o in self.open_orders if o.get("id") not in ids]


def _make_monitor(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    tmpl = {
        "exit_maker_wait_sec": 60,
        "merge_enabled": False,
        "merge_min_shares": 1,
        "merge_min_advantage_usd": 0.01,
        "merge_confirm_timeout_sec": 120,
        "stop_loss_mode": "percent",
        "stop_loss_percent": 20,
        "theta_stop_cents": 5,
        "cooldown_minutes": 20,
    }
    db.save_template(db.get_default_template_id(), tmpl)
    api = FakeAPI()
    monitor = OrderMonitor(api, db, WALLET)
    router = ExitRouter(
        api, db, WALLET,
        cost_lots=monitor._cost_lots,
        sell_book=monitor._sell_book,
        status_add=monitor._status_add,
        record_action=monitor._record_action,
    )
    monitor.exit_router = router
    return db, api, monitor


def test_check_exit_routes_position_through_router(tmp_path):
    db, api, monitor = _make_monitor(tmp_path)
    now = time.time()
    api.trades.append(
        {"id": "t1", "market": CID, "match_time": now, "trader_side": "TAKER",
         "side": "BUY", "price": 0.55, "size": 100, "asset_id": YES, "maker_orders": []}
    )
    api.books[YES] = {"bids": [{"price": 0.50, "size": 200}],
                      "asks": [{"price": 0.60, "size": 200}], "tick_size": "0.01"}
    api.positions = [{"asset": YES, "conditionId": CID, "size": 100, "outcome": "Yes",
                      "curPrice": 0.5, "avgPrice": 0.55, "title": "M"}]
    monitor.begin_status_tick()
    monitor.check_exit()
    # 路由接管:挂了一张 Maker 卖单(上游 park-at-cost 不再执行)
    assert any(c[0] == "sell" and c[1] == YES for c in api.calls)
    actions = db.get_actions(WALLET)
    types = [a["action_type"] for a in actions]
    assert "exit_maker" in types
    assert "exit_rest" not in types  # 上游挂成本价卖单被快速退出取代
    assert "MAKER" in get_status(YES, CID)
    clear_snapshot()
    db.close()


def test_check_exit_falls_back_to_upstream_when_router_disabled(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    api = FakeAPI()
    monitor = OrderMonitor(api, db, WALLET)  # 无 exit_router -> 上游行为
    api.trades.append(
        {"id": "t1", "market": CID, "match_time": 1_000_000.0, "trader_side": "TAKER",
         "side": "BUY", "price": 0.55, "size": 100, "asset_id": YES, "maker_orders": []}
    )
    api.books[YES] = {"bids": [{"price": 0.50, "size": 200}],
                      "asks": [{"price": 0.60, "size": 200}], "tick_size": "0.01"}
    api.positions = [{"asset": YES, "conditionId": CID, "size": 100, "outcome": "Yes",
                      "curPrice": 0.5, "avgPrice": 0.55, "title": "M"}]
    monitor.begin_status_tick()
    monitor.check_exit()
    types = [a["action_type"] for a in db.get_actions(WALLET)]
    assert "exit_rest" in types  # 上游离场照旧
    db.close()


def test_manager_startup_recovery_reconciles_pending_merges(tmp_path):
    from engine.manager import EngineManager

    mgr = EngineManager.__new__(EngineManager)
    mgr.engines = {}
    worker = MagicMock()
    worker.wallet_address = WALLET
    router = MagicMock()
    worker.monitor.exit_router = router
    worker.monitor.init_watermark = MagicMock()
    worker.api.get_user_positions.return_value = [{"asset": YES}]
    worker.api.get_open_orders.return_value = []
    mgr.engines[WALLET] = worker
    mgr.startup_recovery()
    # 先对账(真实余额/订单/Relayer),再让任何新副作用发生
    router.reconcile_pending.assert_called_once_with([{"asset": YES}], [])
    worker.monitor.init_watermark.assert_called_once()


# --- 回归锚点:Scanner/Rewards/Gap/Cliff/价格带/黑名单行为未被改动 ---


def test_scanner_and_strategy_anchors_unchanged():
    from engine.strategy import reward_price_range
    from engine.laddering import compute_market_single_orders, has_cliff_below, max_gap_cents
    from engine.eligibility import recheck_resting_buy
    from engine.rewards import extract_max_spread, extract_daily_rate
    from engine.tiers import tier_for
    from engine.blacklist_ops import buy_order_ids_for_condition

    rmin, rmax = reward_price_range(0.30, 3)
    assert abs(rmin - 0.27) < 1e-9 and abs(rmax - 0.33) < 1e-9
    assert extract_max_spread([{"rewards_max_spread": 3}]) == 3
    assert extract_daily_rate([{"rewards_config": [{"rate_per_day": 12.5}]}]) == 12.5
    assert tier_for([], 100) is None
    # 悬崖否决:下沿下方 2 美分内无买档 -> 有悬崖
    assert has_cliff_below([{"price": 0.20, "size": 10}], 0.25, 2) is True
    assert has_cliff_below([{"price": 0.24, "size": 10}], 0.25, 2) is False
    # 最大断层
    assert max_gap_cents([{"price": 0.30, "size": 1}, {"price": 0.20, "size": 1}]) == 10
    # 黑名单辅助(纯函数,无 IO)
    orders = [{"id": "a", "side": "BUY", "market": CID},
              {"id": "b", "side": "SELL", "market": CID}]
    assert buy_order_ids_for_condition(orders, CID) == ["a"]
    # 资格复查签名仍在(内部行为由既有 test_eligibility 覆盖)
    assert callable(recheck_resting_buy)
    assert callable(compute_market_single_orders)
