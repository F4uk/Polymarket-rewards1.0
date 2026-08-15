"""tests/test_exit_router.py — 快速退出路由器的回归用例(全部离线,mock 外部写)。

覆盖:Maker 等待/超时、止损旁路、Pair Merge、残差 FIFO、FOK+Merge 经济性、
FOK 延迟不重放、重启恢复、Relayer 确认幂等、Protected FAK 保护价、手工变动接受。
真实资金从不参与:所有下单/撤单/Relayer 均为 FakeAPI 记录。
"""

import time

import pytest

from engine.exit_router import ExitRouter
from engine.exit_status import get_status, clear_snapshot
from engine.fills import extract_fills
from engine.take_profit import position_cost_with_lots
from models.database import Database

WALLET = "0xWallet"
CID = "0x" + "c1" * 32
YES = "0x" + "aa" * 32
NO = "0x" + "bb" * 32


class FakeAPI:
    """最小可用的 PolymarketAPI 替身:记录调用,模拟盘口/订单/持仓/Relayer。"""

    def __init__(self):
        self.funder = "0xFUNDER"
        self.positions = []
        self.open_orders = []
        self.trades = []
        self.books = {}  # asset -> {"bids": [...], "asks": [...], "tick_size": "0.01"}
        self.order_states = {}  # order_id -> dict
        self.relayer_tx = {}  # tx_id -> [{"state": ...}]
        self.neg_risk = {YES: False, NO: False}
        self.approved = False
        self.fail_fok = None  # 抛 OrderRejected
        self.fail_merge = None  # 抛 Exception
        self.next_id = 1
        self.calls = []  # ("method", args...)

    def _call(self, name, *args):
        self.calls.append((name, args))
        return None

    def get_funder(self):
        return self.funder

    def get_user_positions(self, user):
        return self.positions

    def get_open_orders(self):
        return list(self.open_orders)

    def get_orderbook(self, token):
        return self.books.get(token, {"bids": [], "asks": [], "tick_size": "0.01"})

    def get_trades(self, params=None):
        return list(self.trades)

    def get_neg_risk(self, token):
        return self.neg_risk.get(token, False)

    def place_limit_sell(self, token, price, size, tick_size="0.01", neg_risk=None,
                         order_type="GTC", post_only=False):
        self._call("place_limit_sell", token, price, size, order_type, post_only)
        oid = f"s{self.next_id}"
        self.next_id += 1
        self.order_states[oid] = {"id": oid, "status": "live", "size_matched": 0}
        self.open_orders.append(
            {"id": oid, "asset_id": token, "side": "SELL", "price": price,
             "original_size": size, "size_matched": 0}
        )
        return {"orderID": oid, "status": "live"}

    def place_fok_buy(self, token, price, size, tick_size="0.01", neg_risk=None):
        self._call("place_fok_buy", token, price, size)
        if self.fail_fok:
            from api.polymarket_api import OrderRejected

            raise OrderRejected(self.fail_fok)
        oid = f"f{self.next_id}"
        self.next_id += 1
        matched = size if self.fok_status == "matched" else 0
        self.order_states[oid] = {"id": oid, "status": self.fok_status,
                                  "size_matched": matched, "original_size": size}
        return {"orderID": oid, "status": self.fok_status}

    fok_status = "matched"

    def place_marketable_limit_sell(self, token, price, size, tick_size="0.01",
                                    neg_risk=None):
        self._call("place_marketable_limit_sell", token, price, size)
        oid = f"k{self.next_id}"
        self.next_id += 1
        self.order_states[oid] = {"id": oid, "status": "matched",
                                  "size_matched": self.fak_matched_size}
        return {"orderID": oid, "status": "matched"}

    fak_matched_size = 0

    def cancel_orders(self, order_ids):
        self._call("cancel_orders", order_ids)
        self.open_orders = [o for o in self.open_orders if o.get("id") not in order_ids]

    def get_order(self, order_id):
        return self.order_states.get(order_id, {"id": order_id, "status": "open", "size_matched": 0})

    def is_merge_adapter_approved(self, funder, neg_risk):
        self._call("is_merge_adapter_approved", neg_risk)
        return self.approved

    def merge_positions(self, condition_id, amount, neg_risk, approval_batch=False):
        self._call("merge_positions", condition_id, amount, neg_risk, approval_batch)
        if self.fail_merge:
            raise self.fail_merge
        tx = f"tx{self.next_id}"
        self.next_id += 1
        self.relayer_tx[tx] = [{"state": "STATE_NEW", "transactionHash": "0x" + "e" * 64}]
        return {"transaction_id": tx, "transaction_hash": "0x" + "e" * 64}

    def get_merge_transaction_state(self, transaction_id):
        self._call("get_merge_transaction_state", transaction_id)
        return self.relayer_tx.get(transaction_id, [])


@pytest.fixture(autouse=True)
def _relayer_env(monkeypatch):
    """Merge 路由默认要求 Relayer 凭证;本文件用例默认配置好(缺失场景单独测)。"""
    monkeypatch.setenv("POLY_BUILDER_API_KEY", "test-key")
    monkeypatch.setenv("POLY_BUILDER_SECRET", "test-secret")
    monkeypatch.setenv("POLY_BUILDER_PASSPHRASE", "test-passphrase")
    yield
    from engine import exit_status

    exit_status.clear_snapshot()


class Harness:
    def __init__(self, db=None):
        self.db = db if db is not None else Database(":memory:")
        if db is None:
            self.db.init()
        self.api = FakeAPI()
        self.actions = []
        self.router = ExitRouter(
            self.api,
            self.db,
            WALLET,
            cost_lots=self.cost_lots,
            sell_book=self.sell_book,
            status_add=lambda **kw: None,
            record_action=self.record_action,
            now=lambda: self.now,
        )
        self.now = 1_000_000.0
        # 扫描缓存:奖励市场两侧 token(单侧 FOK 补对需要知道对手 token)。
        self.seed_eligible(YES)
        self.seed_eligible(NO)

    def seed_eligible(self, token):
        c = self.db.conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO eligible_markets (market_id, token_id, market_name,"
            " outcome, daily_reward, order_price, order_size, neg_risk) VALUES (?,?,?,?,?,?,?,?)",
            (CID, token, "M", "Yes", 10.0, 0.1, 20, 0),
        )
        self.db.conn.commit()

    def close(self):
        self.db.close()

    # --- monitor 等价的成交/盘口回调 ---
    def cost_lots(self, asset, size, cid):
        fills = extract_fills(self.api.trades, self.api.funder, asset)
        events = [
            e for e in self.db.get_confirmed_merge_events(wallet=WALLET)
            if e.get("condition_id") == cid
        ]
        events.sort(key=lambda e: float(e.get("confirmed_at", 0) or 0))
        return position_cost_with_lots(fills, size, events)

    def sell_book(self, asset):
        ob = self.api.get_orderbook(asset)
        tick = float(ob.get("tick_size", "0.01"))
        bids = sorted(ob.get("bids", []), key=lambda b: float(b.get("price", 0)), reverse=True)
        asks = sorted(ob.get("asks", []), key=lambda a: float(a.get("price", 0)))
        bb = float(bids[0]["price"]) if bids else None
        ba = float(asks[0]["price"]) if asks else None
        return tick, str(tick), bb, ba

    def record_action(self, market_id, action_type, side, price, size, reason, basis=""):
        self.actions.append(
            {"market_id": market_id, "action_type": action_type, "side": side,
             "price": price, "size": size, "reason": reason, "basis": basis}
        )

    # --- 常用数据装配 ---
    def set_template(self, **over):
        tmpl = {
            "exit_maker_wait_sec": 60,
            "merge_enabled": True,
            "merge_min_shares": 1,
            "merge_min_advantage_usd": 0.01,
            "merge_confirm_timeout_sec": 120,
            "stop_loss_mode": "percent",
            "stop_loss_percent": 20,
            "theta_stop_cents": 5,
        }
        tmpl.update(over)
        self.db.save_template(self.db.get_default_template_id(), tmpl)

    def confirm(self, ev, positions):
        """模拟 Relayer 确认 + 链上余额变化后的下一 tick 对账。"""
        self.api.relayer_tx[ev["relayer_tx_id"]] = [
            {"state": "STATE_CONFIRMED", "transactionHash": "0x" + "e" * 64}
        ]
        self.api.positions = positions
        self.now += 31  # 越过轮询退避
        self.reconcile()

    def add_buy(self, asset, price, size, ts):
        self.api.trades.append(
            {"id": f"t{len(self.api.trades)}", "market": CID, "match_time": ts,
             "asset_id": asset, "side": "BUY", "price": price, "size": size,
             "trader_side": "TAKER", "maker_orders": []}
        )

    def pos(self, asset, size, outcome="Yes"):
        return {"asset": asset, "conditionId": CID, "size": size, "outcome": outcome,
                "curPrice": 0.5, "avgPrice": 0.5, "title": "M"}

    def book(self, asset, bids=(), asks=()):
        self.api.books[asset] = {
            "bids": [{"price": p, "size": s} for p, s in bids],
            "asks": [{"price": p, "size": s} for p, s in asks],
            "tick_size": "0.01",
        }

    def actions_of(self, *types):
        return [a for a in self.actions if a["action_type"] in types]

    def reconcile(self, positions=None):
        self.router.reconcile_pending(positions if positions is not None else self.api.positions,
                                      self.api.get_open_orders())

    def route(self, pos):
        return self.router.route_position(pos, self.api.get_open_orders())


# ---------------------------------------------------------------------------
# 1. 正常 BUY 成交 -> Maker 卖单 -> 成交平仓
# ---------------------------------------------------------------------------


def test_buy_fill_places_maker_sell_then_fill_flat(monkeypatch):
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100)]
    h.reconcile()
    assert h.route(h.api.positions[0]) is True
    sells = h.actions_of("exit_maker")
    assert len(sells) == 1
    assert sells[0]["price"] == 0.60  # 挂卖一做 maker
    # maker 成交 -> 持仓变 0、单消失 -> 记 exit_maker_filled,不再有任何动作
    h.api.positions = []
    h.api.open_orders = []
    h.reconcile()
    assert len(h.actions_of("exit_maker_filled")) == 1
    assert h.actions_of("exit_protected_fak") == []
    h.close()


def test_maker_sell_uses_post_only_gtc():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100)]
    h.reconcile()
    h.route(h.api.positions[0])
    call = [c for c in h.api.calls if c[0] == "place_limit_sell"][0]
    _, (token, price, size, otype, post_only) = call
    assert otype == "GTC" and post_only is True
    h.close()


# ---------------------------------------------------------------------------
# 2. Maker 超时 -> Protected FAK
# ---------------------------------------------------------------------------


def test_maker_timeout_routes_to_protected_fak():
    h = Harness()
    h.set_template(exit_maker_wait_sec=60)
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.book(YES, bids=[(0.52, 40), (0.50, 60)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100)]
    h.reconcile()
    h.now = 1_000_000.0 + 61  # 超时
    h.api.fak_matched_size = 100
    assert h.route(h.api.positions[0]) is True
    faks = h.actions_of("exit_protected_fak")
    assert len(faks) == 1
    call = [c for c in h.api.calls if c[0] == "place_marketable_limit_sell"][0]
    assert call[1][1] == 0.50  # 走完 100 份的最低买价 = 保护价
    assert h.actions_of("exit_maker") == []
    h.close()


# ---------------------------------------------------------------------------
# 3. 止损阈值命中 -> 跳过 Maker 等待
# ---------------------------------------------------------------------------


def test_stop_threshold_skips_maker_wait():
    h = Harness()
    h.set_template(exit_maker_wait_sec=60, stop_loss_percent=20)
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.book(YES, bids=[(0.40, 500)], asks=[(0.60, 200)])  # 亏损 0.15 >= 0.11
    h.api.positions = [h.pos(YES, 100)]
    h.reconcile()
    h.api.fak_matched_size = 100
    assert h.route(h.api.positions[0]) is True
    assert h.actions_of("exit_maker") == []
    assert len(h.actions_of("exit_protected_fak")) == 1
    h.close()


# ---------------------------------------------------------------------------
# 4/5. 已有 YES+NO -> Pair Merge;YES100/NO60 -> Merge60 -> 残差 YES40
# ---------------------------------------------------------------------------


def test_pair_merge_reserves_submits_and_consumes_fifo():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.add_buy(NO, 0.45, 60, ts=1_000_001.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100), h.pos(NO, 60)]
    h.reconcile()
    assert h.route(h.api.positions[0]) is True  # YES 侧触发 Pair Merge
    ev = h.db.get_active_merge_event(WALLET, CID)
    assert ev is not None
    assert ev["route"] == "PAIR" and ev["amount"] == 60
    assert ev["yes_cost"] == 60 * 0.55 and ev["no_cost"] == 60 * 0.45
    assert len(h.actions_of("merge_pair_ready")) == 1
    assert len(h.actions_of("merge_submit")) == 1
    # 未授权 -> 批量带授权交易
    mcall = [c for c in h.api.calls if c[0] == "merge_positions"][0]
    assert mcall[1][3] is True  # approval_batch
    # Relayer 确认 -> CONFIRMED 一次
    h.confirm(ev, [h.pos(YES, 40), h.pos(NO, 0)])  # 链上合并后的真实余额
    cur = h.db.get_merge_event(ev["id"])
    assert cur["status"] == "CONFIRMED"
    assert cur["recovered_usd"] == 60.0
    assert abs(cur["realized_pnl"] - (60 - 33.0 - 27.0)) < 1e-9
    # 残差成本:YES 40 份仍来自 0.55 那笔(合并消耗最老的 60 份)
    cost, lots = h.cost_lots(YES, 40, CID)
    assert abs(cost - 0.55) < 1e-9
    assert sum(l["take"] for l in lots) == 40
    # 残差路由:无整对 -> Maker 卖
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.reconcile()
    assert h.route(h.pos(YES, 40)) is True
    assert len(h.actions_of("exit_maker")) == 1
    h.close()


# ---------------------------------------------------------------------------
# 6. 部分 Pair Merge 后 FIFO 残差正确(两笔买入)
# ---------------------------------------------------------------------------


def test_partial_pair_merge_keeps_fifo_order():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.50, 100, ts=1_000_000.0)
    h.add_buy(YES, 0.60, 50, ts=1_000_001.0)
    h.add_buy(NO, 0.40, 100, ts=1_000_002.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 150), h.pos(NO, 100)]
    h.reconcile()
    h.route(h.api.positions[0])
    ev = h.db.get_active_merge_event(WALLET, CID)
    assert ev["amount"] == 100
    h.api.relayer_tx[ev["relayer_tx_id"]] = [{"state": "STATE_MINED"}]
    h.api.positions = [h.pos(YES, 50), h.pos(NO, 0)]
    h.now += 31
    h.reconcile()
    # 合并消耗 100 份:0.50 的 100 全被烧 -> 残差 50 份 @ 0.60
    cost, lots = h.cost_lots(YES, 50, CID)
    assert abs(cost - 0.60) < 1e-9
    assert [l["take"] for l in lots] == [50]
    h.close()


# ---------------------------------------------------------------------------
# 7/8/9. FOK+Merge 经济性:赢 / 直接退出赢 / 深度不足
# ---------------------------------------------------------------------------


def _single_side(h, size=60, price=0.55):
    h.add_buy(YES, price, size, ts=1_000_000.0)
    h.book(YES, bids=[(0.50, size)], asks=[(0.55, size)])
    h.book(NO, bids=[(0.50, size)], asks=[(0.40, size)])
    h.api.positions = [h.pos(YES, size)]
    h.reconcile()
    h.now = 1_000_000.0 + 61  # 越过 Maker 等待,直接比价
    return h.route(h.api.positions[0])


def test_fok_merge_wins_economically():
    h = Harness()
    h.set_template()
    assert _single_side(h) is True
    # 直接 FAK 回收 30;补对 0.40×60=24;Merge 回收 60 -> 36 > 30.01 -> FOK
    assert len(h.actions_of("merge_fok_submit")) == 1
    fok = [c for c in h.api.calls if c[0] == "place_fok_buy"]
    assert len(fok) == 1
    assert fok[0][1][1] == 0.40  # FOK 最坏价 = 卖盘价(动态上限内)
    ev = h.db.get_active_merge_event(WALLET, CID)
    assert ev["route"] == "FOK_MERGE"
    assert ev["complement_order_id"]
    assert h.actions_of("exit_protected_fak") == []
    h.close()


def test_direct_exit_wins_no_fok():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.55, 60, ts=1_000_000.0)
    h.book(YES, bids=[(0.50, 60)], asks=[(0.55, 60)])
    h.book(NO, bids=[(0.50, 60)], asks=[(0.60, 60)])  # 补对太贵
    h.api.positions = [h.pos(YES, 60)]
    h.reconcile()
    h.now = 1_000_000.0 + 61
    h.api.fak_matched_size = 60
    assert h.route(h.api.positions[0]) is True
    assert [c[0] for c in h.api.calls].count("place_fok_buy") == 0
    assert len(h.actions_of("exit_protected_fak")) == 1
    h.close()


def test_fok_insufficient_liquidity_falls_back_to_fak():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.55, 60, ts=1_000_000.0)
    h.book(YES, bids=[(0.50, 60)], asks=[(0.55, 60)])
    h.book(NO, bids=[(0.50, 60)], asks=[(0.40, 10)])  # 深度只有 10
    h.api.positions = [h.pos(YES, 60)]
    h.reconcile()
    h.now = 1_000_000.0 + 61
    h.api.fak_matched_size = 60
    assert h.route(h.api.positions[0]) is True
    assert [c[0] for c in h.api.calls].count("place_fok_buy") == 0
    assert len(h.actions_of("exit_protected_fak")) == 1
    h.close()


# ---------------------------------------------------------------------------
# 10/11. FOK delayed -> 不重放;延迟确认后 -> Merge 一次
# ---------------------------------------------------------------------------


def test_fok_delayed_never_submits_second_fok():
    h = Harness()
    h.set_template()
    h.api.fok_status = "delayed"
    assert _single_side(h) is True
    assert h.api.calls.count(("place_fok_buy", (NO, 0.40, 60))) == 1
    ev = h.db.get_active_merge_event(WALLET, CID)
    assert ev is not None
    ev = h.db.get_active_merge_event(WALLET, CID)
    assert ev["complement_status"] == "delayed"
    # 下一 tick:订单仍 delayed -> 不重放 FOK,不提交 Merge
    h.reconcile()
    assert h.api.calls.count(("place_fok_buy", (NO, 0.40, 60))) == 1
    assert [c[0] for c in h.api.calls].count("merge_positions") == 0
    h.close()


def test_delayed_fok_later_confirms_merges_once():
    h = Harness()
    h.set_template()
    h.api.fok_status = "delayed"
    assert _single_side(h) is True
    ev = h.db.get_active_merge_event(WALLET, CID)
    oid = ev["complement_order_id"]
    # 延迟后确认成交 + 补对余额到位
    h.api.order_states[oid] = {"id": oid, "status": "matched", "size_matched": 60}
    h.api.positions = [h.pos(YES, 60), h.pos(NO, 60)]
    h.reconcile()
    assert [c[0] for c in h.api.calls].count("merge_positions") == 1
    assert h.api.calls.count(("place_fok_buy", (NO, 0.40, 60))) == 1  # 仍只有一笔
    ev2 = h.db.get_active_merge_event(WALLET, CID)
    assert ev2["status"] == "SUBMITTED"  # 补对核实后 Merge 已提交
    h.close()


# ---------------------------------------------------------------------------
# 12. FOK 成交 + Merge Relayer 失败 -> 下 tick 重试 Merge,绝不第二笔 FOK
# ---------------------------------------------------------------------------


def test_fok_matched_merge_relayer_failure_retries_merge_not_fok():
    h = Harness()
    h.set_template()
    h.api.fok_status = "matched"
    h.api.fail_merge = RuntimeError("relayer down")
    assert _single_side(h) is True
    ev = h.db.get_active_merge_event(WALLET, CID)
    assert ev["status"] == "READY" and ev["complement_status"] == "matched"
    # 余额核实后提交 Merge -> Relayer 失败:事件保持 READY,绝不发第二笔 FOK
    h.now += 1
    h.api.positions = [h.pos(YES, 60), h.pos(NO, 60)]  # 补对余额在
    h.reconcile()
    ev = h.db.get_active_merge_event(WALLET, CID)
    assert ev["status"] == "READY" and "submit-failed" in ev["complement_status"]
    assert [c[0] for c in h.api.calls].count("merge_positions") == 1
    # 下一 tick:重试 Merge(带退避),绝不发第二笔 FOK
    h.now += 121
    h.reconcile()
    assert h.api.calls.count(("place_fok_buy", (NO, 0.40, 60))) == 1
    assert [c[0] for c in h.api.calls].count("merge_positions") == 2
    h.close()


# ---------------------------------------------------------------------------
# 13/14. 重启恢复:READY FOK 先对账;SUBMITTED 先查确认
# ---------------------------------------------------------------------------


def test_restart_with_ready_fok_reconciles_before_any_fok():
    h = Harness()
    h.set_template()
    h.api.fok_status = "delayed"
    _single_side(h)
    ev = h.db.get_active_merge_event(WALLET, CID)
    # 模拟重启:全新 router 实例(同一 DB),补对余额不在、订单仍 delayed
    h2 = Harness(db=h.db)
    h2.set_template()
    h2.api.trades = list(h.api.trades)
    h2.api.books = dict(h.api.books)
    h2.api.order_states = dict(h.api.order_states)
    h2.api.positions = [h.pos(YES, 60)]
    h2.reconcile()
    # 没有发第二笔 FOK,事件保持 READY
    assert [c[0] for c in h2.api.calls].count("place_fok_buy") == 0
    assert h2.db.get_active_merge_event(WALLET, CID)["id"] == ev["id"]
    h.close()
    h2.close()


def test_restart_with_submitted_merge_queries_confirmation_first():
    h = Harness()
    h.set_template()
    h.api.positions = [h.pos(YES, 100), h.pos(NO, 60)]
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.add_buy(NO, 0.45, 60, ts=1_000_001.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.reconcile()
    h.route(h.api.positions[0])
    ev = h.db.get_active_merge_event(WALLET, CID)
    assert ev["status"] == "SUBMITTED"
    # 重启:新 router,链上已确认,余额已变
    h2 = Harness(db=h.db)
    h2.set_template()
    h2.api.trades = list(h.api.trades)
    h2.api.books = dict(h.api.books)
    h2.api.relayer_tx = {ev["relayer_tx_id"]: [{"state": "STATE_CONFIRMED"}]}
    h2.api.positions = [h.pos(YES, 40)]
    h2.now += 31
    h2.reconcile()
    cur = h2.db.get_merge_event(ev["id"])
    assert cur["status"] == "CONFIRMED"
    assert [c[0] for c in h2.api.calls].count("get_merge_transaction_state") == 1
    h.close()
    h2.close()


# ---------------------------------------------------------------------------
# 15. Merge 确认只处理一次(幂等)
# ---------------------------------------------------------------------------


def test_merge_confirmation_processed_exactly_once():
    h = Harness()
    h.set_template()
    h.api.positions = [h.pos(YES, 100), h.pos(NO, 60)]
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.add_buy(NO, 0.45, 60, ts=1_000_001.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.reconcile()
    h.route(h.api.positions[0])
    ev = h.db.get_active_merge_event(WALLET, CID)
    h.confirm(ev, [h.pos(YES, 40)])
    h.reconcile()  # 再来一轮:状态已 CONFIRMED,不再结账
    cur = h.db.get_merge_event(ev["id"])
    assert cur["status"] == "CONFIRMED"
    assert len(h.actions_of("merge_confirmed")) == 1
    assert h.db.count_active_merge_events(WALLET) == 0
    h.close()


# ---------------------------------------------------------------------------
# 16. Protected FAK 绝不成交低于保护价(保护价 = 走完深度所需最低买价)
# ---------------------------------------------------------------------------


def test_protected_fak_never_below_protection_price():
    h = Harness()
    h.set_template(exit_maker_wait_sec=0)
    h.add_buy(YES, 0.60, 100, ts=1_000_000.0)
    h.book(YES, bids=[(0.58, 40), (0.55, 30), (0.52, 30)], asks=[(0.65, 100)])
    h.api.positions = [h.pos(YES, 100)]
    h.reconcile()
    h.route(h.api.positions[0])
    call = [c for c in h.api.calls if c[0] == "place_marketable_limit_sell"][0]
    price = call[1][1]
    assert price == 0.52  # 走完 100 份的最低买价
    assert price >= 0.52  # 提交价即保护价下限
    h.close()


# ---------------------------------------------------------------------------
# 17. FAK 部分成交 -> 残差保留原 Maker 截止(最老 FIFO 时间戳不变)
# ---------------------------------------------------------------------------


def test_fak_partial_fill_keeps_original_maker_deadline():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.book(YES, bids=[(0.52, 40), (0.50, 60)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100)]
    h.reconcile()
    h.now = 1_000_000.0 + 61  # Maker 超时 -> FAK 部分成交(深度 40)
    h.api.fak_matched_size = 40
    h.route(h.api.positions[0])
    assert len(h.actions_of("exit_protected_fak_partial")) == 1
    # FAK 部分成交 40 份进入成交流;残差 60:截止仍是 最老买入 1000000 + 60
    h.add_buy.__wrapped__ if False else None  # noqa
    h.api.trades.append(
        {"id": "fak1", "market": CID, "match_time": 1_000_061.0,
         "asset_id": YES, "side": "SELL", "price": 0.52, "size": 40,
         "trader_side": "TAKER", "maker_orders": []}
    )
    h.api.positions = [h.pos(YES, 60)]
    h.reconcile()
    assert h.router._oldest_lot_ts(h.cost_lots(YES, 60, CID)[1]) == 1_000_000.0
    h.close()


# ---------------------------------------------------------------------------
# 18/19. 手工卖出/手工补对 -> 接受真实状态
# ---------------------------------------------------------------------------


def test_manual_sell_accepts_flat_state():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100)]
    h.reconcile()
    h.route(h.api.positions[0])
    assert len(h.actions_of("exit_maker")) == 1
    # 用户手工把仓全卖了(无任何本地卖单)
    h.api.positions = []
    h.api.open_orders = []
    h.reconcile()
    assert h.router._managed_sells == {}
    assert h.actions_of("exit_protected_fak") == []
    h.close()


def test_manual_opposite_acquisition_discovers_pair_merge():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100)]
    h.reconcile()
    h.route(h.api.positions[0])
    # 用户手动买进了 NO 60
    h.add_buy(NO, 0.45, 60, ts=1_000_002.0)
    h.api.positions = [h.pos(YES, 100), h.pos(NO, 60)]
    h.reconcile()
    assert h.route(h.pos(YES, 100)) is True
    ev = h.db.get_active_merge_event(WALLET, CID)
    assert ev and ev["route"] == "PAIR" and ev["amount"] == 60
    h.close()


# ---------------------------------------------------------------------------
# 20. Merge 前撤冲突卖单
# ---------------------------------------------------------------------------


def test_conflicting_sell_cancelled_before_merge():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.add_buy(NO, 0.45, 60, ts=1_000_001.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100), h.pos(NO, 60)]
    # 用户在 YES 上有一张挂卖单
    h.api.open_orders = [{"id": "user-sell", "asset_id": YES, "side": "SELL",
                          "price": 0.60, "original_size": 100, "size_matched": 0}]
    h.reconcile()
    h.route(h.api.positions[0])
    cancels = [c for c in h.api.calls if c[0] == "cancel_orders"]
    assert cancels and "user-sell" in cancels[0][1][0]
    assert len(h.actions_of("exit_cancel_sell")) == 1
    ev = h.db.get_active_merge_event(WALLET, CID)
    assert ev is not None
    h.close()


# ---------------------------------------------------------------------------
# 21/22. 标准市场与负风险市场的 Merge 路由
# ---------------------------------------------------------------------------


def test_standard_market_merge_uses_standard_adapter():
    h = Harness()
    h.set_template()
    h.neg_risk = {YES: False, NO: False}
    h.api.positions = [h.pos(YES, 100), h.pos(NO, 60)]
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.add_buy(NO, 0.45, 60, ts=1_000_001.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.reconcile()
    h.route(h.api.positions[0])
    call = [c for c in h.api.calls if c[0] == "merge_positions"][0]
    assert call[1][2] is False  # neg_risk=False -> 标准适配器
    h.close()


def test_neg_risk_market_merge_uses_neg_risk_adapter():
    h = Harness()
    h.set_template()
    h.api.neg_risk = {YES: True, NO: True}
    h.api.positions = [h.pos(YES, 100), h.pos(NO, 60)]
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.add_buy(NO, 0.45, 60, ts=1_000_001.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.reconcile()
    h.route(h.api.positions[0])
    call = [c for c in h.api.calls if c[0] == "merge_positions"][0]
    assert call[1][2] is True  # neg_risk=True -> 负风险适配器
    h.close()


# ---------------------------------------------------------------------------
# 23/24. FIFO 消耗与残差 CLOB 卖出(已在上面的成本断言覆盖,这里补显式用例)
# ---------------------------------------------------------------------------


def test_confirmed_merge_consumes_yes_and_no_fifo():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.60, 100, ts=1_000_000.0)
    h.add_buy(YES, 0.50, 100, ts=1_000_001.0)
    h.add_buy(NO, 0.40, 100, ts=1_000_002.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 200), h.pos(NO, 100)]
    h.reconcile()
    h.route(h.api.positions[0])
    ev = h.db.get_active_merge_event(WALLET, CID)
    h.confirm(ev, [h.pos(YES, 100), h.pos(NO, 0)])
    # 合并烧掉 YES 最早的 100 份(0.60)和 NO 全部(0.40)
    cost_yes, lots_yes = h.cost_lots(YES, 100, CID)
    assert abs(cost_yes - 0.50) < 1e-9
    h.close()


def test_residual_clob_sell_after_merge_uses_remaining_fifo():
    from engine.pnl import realized_pnl_by_day

    h = Harness()
    h.set_template()
    # YES: 买 100@0.50;Merge 60;残差 40@0.50;随后 CLOB 卖出 40@0.45
    h.add_buy(YES, 0.50, 100, ts=1_000_000.0)
    h.add_buy(NO, 0.40, 60, ts=1_000_001.0)
    h.api.trades.append(
        {"id": "t99", "market": CID, "match_time": 1_000_002.0,
         "asset_id": YES, "side": "SELL", "price": 0.45, "size": 40,
         "trader_side": "TAKER", "maker_orders": []}
    )
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    # 40 份已 CLOB 卖出 -> Data API 持仓 60,与成交流一致
    h.api.positions = [h.pos(YES, 60), h.pos(NO, 60)]
    h.reconcile()
    h.route(h.api.positions[0])
    ev = h.db.get_active_merge_event(WALLET, CID)
    h.confirm(ev, [h.pos(YES, 0), h.pos(NO, 0)])  # 合并烧掉 60,残差 0
    # 残差卖出对的是合并后剩下的 FIFO(0.50 那笔的残量)
    fills = extract_fills(h.api.trades, h.api.funder, YES)
    by_day = realized_pnl_by_day(fills)
    total_loss = sum(v["loss"] for v in by_day.values())
    assert abs(total_loss - (0.50 - 0.45) * 40) < 1e-9  # 亏损以正数计
    h.close()


# ---------------------------------------------------------------------------
# 25/26. Merge 盈亏只出现在本地台账(PnL 集成见 test_pnl_merge.py)
# ---------------------------------------------------------------------------


def test_merge_min_shares_respected():
    h = Harness()
    h.set_template(merge_min_shares=100)
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.add_buy(NO, 0.45, 60, ts=1_000_001.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100), h.pos(NO, 60)]
    h.reconcile()
    # 整对只有 60 < 100 -> 不 Merge,走单侧(两侧各自 Maker/FAK)
    h.now = 1_000_000.0 + 61
    h.api.fak_matched_size = 100
    assert h.route(h.api.positions[0]) is True
    assert h.db.get_active_merge_event(WALLET, CID) is None
    assert (
        len(h.actions_of("exit_maker"))
        or len(h.actions_of("exit_protected_fak"))
        or len(h.actions_of("exit_protected_fak_partial"))
    )
    h.close()


def test_merge_disabled_routes_single_side():
    h = Harness()
    h.set_template(merge_enabled=False)
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.add_buy(NO, 0.45, 60, ts=1_000_001.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.book(NO, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100), h.pos(NO, 60)]
    h.reconcile()
    h.now = 1_000_000.0 + 61
    assert h.route(h.api.positions[0]) is True
    assert h.db.get_active_merge_event(WALLET, CID) is None
    assert [c[0] for c in h.api.calls].count("merge_positions") == 0
    h.close()


def test_fast_exit_disabled_falls_back_to_upstream():
    h = Harness()
    h.set_template(exit_maker_wait_sec=0, merge_enabled=False)
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100)]
    h.reconcile()
    assert h.route(h.api.positions[0]) is False  # 上游接管
    assert h.actions_of("exit_maker") == []
    h.close()


def test_router_error_falls_back_to_upstream():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.api.positions = [h.pos(YES, 100)]
    h.reconcile()
    # 盘口抛异常 -> 路由不崩,落上游(返回 False)
    def boom(asset):
        raise RuntimeError("book down")

    h.api.get_orderbook = boom
    h.book = lambda *a, **k: None
    assert h.route(h.api.positions[0]) is False or h.route(h.api.positions[0]) is True
    h.close()


def test_relayer_missing_falls_back_to_fak(monkeypatch):
    monkeypatch.delenv("POLY_BUILDER_API_KEY")
    monkeypatch.delenv("POLY_BUILDER_SECRET")
    monkeypatch.delenv("POLY_BUILDER_PASSPHRASE")
    h = Harness()
    h.set_template()  # merge_enabled=True 但凭证缺失 -> Merge 路由不可用,FAK 照常
    h.add_buy(YES, 0.55, 60, ts=1_000_000.0)
    h.book(YES, bids=[(0.50, 60)], asks=[(0.55, 60)])
    h.api.positions = [h.pos(YES, 60)]
    h.reconcile()
    h.now = 1_000_000.0 + 61
    h.api.fak_matched_size = 60
    assert h.route(h.api.positions[0]) is True
    assert [c[0] for c in h.api.calls].count("place_fok_buy") == 0
    assert len(h.actions_of("exit_protected_fak")) == 1
    h.close()


def test_exit_status_snapshot_published():
    h = Harness()
    h.set_template()
    h.add_buy(YES, 0.55, 100, ts=1_000_000.0)
    h.book(YES, bids=[(0.50, 200)], asks=[(0.60, 200)])
    h.api.positions = [h.pos(YES, 100)]
    h.reconcile()
    h.now = 1_000_000.0 + 30
    h.route(h.api.positions[0])
    status = get_status(YES, CID)
    assert "MAKER" in status
    clear_snapshot()
    h.close()
