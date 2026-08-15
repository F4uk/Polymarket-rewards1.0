"""Fast-Exit V2 offline smoke demo (spec §54) — mocked/local data only.

Run from the repository root:

    .venv\\Scripts\\python.exe debug\\fast_exit_v2_smoke.py

Never touches a live CLOB / Relayer / chain: the API is a deterministic fake.
Scenarios demonstrated:
  1. NO 50 filled, MarketRecovery < FOKMergeRecovery  -> FOK+MERGE selected
  2. NO 50 filled, MarketRecovery > FOKMergeRecovery  -> maker window -> MARKET
  3. YES30/NO20 -> Merge20 -> residual YES10
  4. Relayer not ready -> Type3 new Reward BUY blocked
  5. same token active cycle -> new BUY blocked
  6. opposite scanner qty 50 while residual 20 -> effective qty 20
  7. history: FOK+MERGE / MARKET / MIXED labels render correctly
"""

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("PMM_BUILDER_API_KEY", "smoke-key")
os.environ.setdefault("PMM_BUILDER_SECRET", "smoke-secret")
os.environ.setdefault("PMM_BUILDER_PASSPHRASE", "smoke-passphrase")

from engine.inventory_exit import InventoryExitEngine, exit_method_label
from models.database import Database


CID = "0x" + "c" * 64


class FakeAPI:
    def __init__(self, books=None, tokens=None, signature_type=3):
        self._books = dict(books or {})
        self._tokens = dict(tokens or {})
        self.signature_type = signature_type
        self.trading_enabled = True
        self.private_key = "0x" + "1" * 64
        self.positions = []
        self.open_orders = []
        self.placed_buys = []
        self.placed_sells = []
        self.placed_post_only_sells = []
        self.placed_market = []
        self.placed_foks = []
        self.cancelled = []
        self.trades = {}

    def get_funder(self):
        return "0xFunder"

    def get_user_positions(self, _f):
        return [dict(p) for p in self.positions]

    def get_open_orders(self):
        return [dict(o) for o in self.open_orders]

    def get_orderbook(self, asset):
        return self._books.get(asset) or {"bids": [], "asks": [], "tick_size": "0.01"}

    def get_market(self, cid):
        return self._tokens.get(cid) or {"tokens": []}

    def gamma_resolution_status(self, cids):
        return {c: None for c in cids or []}

    def get_balance(self):
        return 100.0

    def cancel_orders(self, ids):
        self.cancelled += list(ids or [])
        self.open_orders = [o for o in self.open_orders if o.get("id") not in ids]

    def place_limit_buy(self, *a, **kw):
        self.placed_buys.append(a)

    def place_limit_sell(self, *a, **kw):
        self.placed_sells.append(a)

    def place_post_only_sell(self, *a, **kw):
        self.placed_post_only_sells.append(a)

    def place_marketable_limit_sell(self, token, price, size, **kw):
        self.placed_market.append((token, price, size))
        return {"makingAmount": float(size), "takingAmount": float(size) * float(price)}

    def place_complement_fok_buy(self, token, size, limit, **kw):
        self.placed_foks.append((token, size, limit))

    def get_trades(self, params=None):
        return [t for values in self.trades.values() for t in values]


def sell_fill(asset, qty, price, ts, side="SELL", order_id="o"):
    return {
        "trade_id": f"t-{asset}-{ts}",
        "maker_orders": [
            {
                "order_id": order_id,
                "maker_address": "0xfunder",
                "side": side,
                "asset_id": asset,
                "price": str(price),
                "matched_amount": str(qty),
            }
        ],
        "market": CID,
        "match_time": str(ts),
    }


def book(bids, asks):
    return {
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "tick_size": "0.01",
    }


def tokens():
    return {
        "tokens": [
            {"token_id": "yes", "outcome": "Yes"},
            {"token_id": "no", "outcome": "No"},
        ]
    }


def pos(side, size, asset):
    return {"conditionId": CID, "asset": asset, "outcome": side, "size": size}


def template(db, **over):
    t = {
        "fast_exit_enabled": True,
        "maker_exit_wait_sec": 30,
        "require_merge_ready_for_new_buys": True,
        "merge_enabled": True,
        "merge_min_shares": 1.0,
        "merge_advantage_min_usd": 0.01,
        "low_balance_threshold_usd": 0.0,
        "cliff_probe_cents": 0,
    }
    t.update(over)
    db.save_template(db.get_default_template_id(), t)


def engine(db, api, costs):
    return InventoryExitEngine(
        api, db, "0xW", encryption_key=b"k" * 32,
        condition_lock=lambda cid: threading.Lock(),
        cost_provider=lambda asset, size, cid: (costs.get(asset, 0.30), []),
        book_provider=lambda asset: api.get_orderbook(asset),
        readiness_checker=lambda: (True, "READY"),
    )


def fill(asset, size, price=0.30):
    return {
        "trade_id": "t", "order_id": "o", "asset_id": asset,
        "price": price, "size": size, "market": CID, "ts": 1.0,
    }


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main():
    tmp = tempfile.mkdtemp(prefix="fast-exit-smoke-")
    db = Database(os.path.join(tmp, "smoke.db"))
    db.init()

    # Scenario 1: FOK+MERGE economically superior -> selected immediately.
    template(db)
    api = FakeAPI(
        books={"no": book([(0.19, 100)], [(0.20, 100)]), "yes": book([], [(0.63, 100)])},
        tokens={"0x" + "c" * 64: tokens()},
    )
    api.positions = [pos("NO", 50, "no")]
    eng = engine(db, api, {"no": 0.30})
    db.record_bot_buy_order("0xW", "o", CID, "no")
    api.trades[CID] = [sell_fill("no", 50, 0.30, ts=1, side="BUY", order_id="o")]
    eng.on_reward_fill(fill("no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[pos("NO", 50, "no")])
    check(
        "1. FOK+MERGE selected when MarketRecovery < FOKMergeRecovery",
        api.placed_foks == [("yes", 50, 0.63)] and api.placed_market == [],
        f"foks={api.placed_foks}",
    )

    # Scenario 2: direct superior -> maker window -> timeout -> MARKET.
    db2 = Database(os.path.join(tmp, "s2.db"))
    db2.init()
    template(db2)
    api2 = FakeAPI(
        books={"no": book([(0.28, 100)], [(0.29, 100)]), "yes": book([], [(0.75, 100)])},
        tokens={"0x" + "c" * 64: tokens()},
    )
    api2.positions = [pos("NO", 50, "no")]
    eng2 = engine(db2, api2, {"no": 0.30})
    db2.record_bot_buy_order("0xW", "o", CID, "no")
    api2.trades[CID] = [sell_fill("no", 50, 0.30, ts=1, side="BUY", order_id="o")]
    eng2.on_reward_fill(fill("no", 50, price=0.30))
    eng2.run_tick(open_orders=[], positions=[pos("NO", 50, "no")])
    window_ok = api2.placed_post_only_sells == [("no", 0.29, 50)] and api2.placed_market == []
    # timeout: force the maker window to expire by aging the cycle.
    cycle = db2.get_active_exit_cycle("0xW", CID)
    db2.update_exit_cycle(cycle["id"], opened_at=cycle["opened_at"] - 31)
    eng2.run_tick(open_orders=[], positions=[pos("NO", 50, "no")])
    check(
        "2. maker window then protected MARKET on timeout",
        window_ok and api2.placed_market == [("no", 0.28, 50)],
        f"sells={api2.placed_sells} market={api2.placed_market}",
    )

    # Scenario 3: YES30/NO20 -> Merge20 -> residual YES10.
    db3 = Database(os.path.join(tmp, "s3.db"))
    db3.init()
    template(db3)
    api3 = FakeAPI(
        books={"yes": book([(0.40, 10)], [(0.42, 100)]), "no": book([], [(0.68, 100)])},
        tokens={"0x" + "c" * 64: tokens()},
    )
    api3.positions = [pos("YES", 30, "yes"), pos("NO", 20, "no")]
    eng3 = engine(db3, api3, {"yes": 0.30})
    # bot-order provenance for adoption (audit fix pack 2)
    db3.record_bot_buy_order("0xW", "o-y", CID, "yes")
    db3.record_bot_buy_order("0xW", "o-n", CID, "no")
    api3.trades[CID] = [
        sell_fill("yes", 30, 0.30, ts=100, side="BUY", order_id="o-y"),
        sell_fill("no", 20, 0.30, ts=100, side="BUY", order_id="o-n"),
    ]
    eng3.run_tick(open_orders=[], positions=[pos("YES", 30, "yes"), pos("NO", 20, "no")])
    cyc3 = db3.get_active_exit_cycle("0xW", CID)
    pair_ok = cyc3["paired_qty"] == 20 and api3.placed_sells == []
    op_id = db3.create_merge_operation("0xW", "0xFunder", CID, "yes", "no", 20.0)
    db3.update_merge_operation(op_id, "confirmed", "r1", "0xh", realized_pnl=0.6)
    db3.mark_merge_inventory_reconciled(op_id)
    api3.positions = [pos("YES", 10, "yes")]
    eng3.run_tick(open_orders=[], positions=[pos("YES", 10, "yes")])
    residual_ok = api3.placed_post_only_sells == [("yes", 0.42, 10)]
    api3.trades[CID] = [sell_fill("yes", 10, 0.42, ts=time.time() + 5)]
    api3.positions = []
    eng3.run_tick(open_orders=[], positions=[])
    closed3 = db3.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])[0]
    check(
        "3. YES30/NO20 -> Merge20 -> residual YES10 -> MIXED",
        pair_ok and residual_ok
        and exit_method_label(db3.get_exit_legs(closed3["id"])) == "MIXED",
        f"paired={cyc3['paired_qty']} residual_sell={api3.placed_sells}",
    )

    # Scenario 4: Relayer not ready -> Type3 new Reward BUY blocked.
    db4 = Database(os.path.join(tmp, "s4.db"))
    db4.init()
    template(db4)
    api4 = FakeAPI(books={}, tokens={})
    eng4 = InventoryExitEngine(
        api4, db4, "0xW", encryption_key=b"k" * 32,
        readiness_checker=lambda: (False, "RELAYER_NOT_CONFIGURED"),
    )
    ready, reason = eng4.merge_runtime_ready()
    check(
        "4. Merge 未就绪 -> Type3 新开仓暂停",
        ready is False and reason == "RELAYER_NOT_CONFIGURED",
        f"ready={ready} reason={reason}",
    )

    # Scenario 5 + 6: same-token block + opposite cap.
    api5 = FakeAPI(books={}, tokens={"0x" + "c" * 64: tokens()})
    eng5 = engine(db5 := Database(os.path.join(tmp, "s5.db")), api5, {})
    db5.init()
    template(db5)
    db5.record_bot_buy_order("0xW", "o", CID, "no")
    api5.trades[CID] = [sell_fill("no", 20, 0.30, ts=1, side="BUY", order_id="o")]
    eng5.on_reward_fill(fill("no", 20, price=0.32))
    same = eng5.authorize_placement(CID, "no", "NO", 50, [])
    check(
        "5. same token active cycle -> new BUY blocked",
        same["kind"] == "same_token_block" and same["allowed"] is False,
        same["reason"],
    )
    opp = eng5.authorize_placement(CID, "yes", "YES", 50, [])
    check(
        "6. opposite scanner qty 50 while residual 20 -> total target 20",
        opp["allowed"] is True and opp["total_target"] == 20,
        f"total_target={opp['total_target']}",
    )

    # Scenario 7: history labels render correctly.
    labels = [
        exit_method_label(
            [{"exit_method": "FOK+MERGE", "kind": "complement_buy", "status": "confirmed"}]
        ),
        exit_method_label(
            [{"exit_method": "MARKET", "kind": "market_sell", "status": "confirmed"}]
        ),
        exit_method_label(
            [
                {"exit_method": "MERGE", "kind": "merge", "status": "done"},
                {"exit_method": "MAKER", "kind": "maker_sell", "status": "confirmed"},
            ]
        ),
    ]
    check(
        "7. history labels FOK+MERGE / MARKET / MIXED",
        labels == ["FOK+MERGE", "MARKET", "MIXED"],
        str(labels),
    )

    failed = [r for r in RESULTS if not r[1]]
    print()
    print(f"SMOKE RESULT: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
