"""V2 Inventory Exit Engine flows (spec §41-46) — real SQLite + fake API only.

No live CLOB / Relayer / chain: every funds path is a fake.  Two wallets in
the same condition are exercised to prove wallet isolation.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from engine.inventory_exit import InventoryExitEngine
from engine.manager import WalletWorker
from engine.monitor import OrderMonitor
from models.database import ActiveExitCycleExists, Database


CID = "0x" + "c" * 64
CID_B = "0x" + "b" * 64


def _tok(cid, yes_asset, no_asset):
    return {
        "tokens": [
            {"token_id": yes_asset, "outcome": "Yes"},
            {"token_id": no_asset, "outcome": "No"},
        ]
    }


class FakeAPI:
    """Deterministic CLOB/Data-API fake for one wallet."""

    def __init__(
        self,
        *,
        positions=None,
        books=None,
        tokens=None,
        signature_type=3,
        trading_enabled=True,
        cancel_blocks=None,
        cancel_sticky=None,
    ):
        self._positions = list(positions or [])
        self._books = dict(books or {})
        self._tokens = dict(tokens or {})
        self.signature_type = signature_type
        self.trading_enabled = trading_enabled
        self.trading_block_reason = "" if trading_enabled else "disabled in test"
        self.private_key = "0x" + "1" * 64
        self._open_orders = []
        self._cancelled = []
        self._placed_buys = []
        self._placed_sells = []
        self._placed_post_only_sells = []
        self._placed_market_sells = []
        self._placed_foks = []
        self._cancel_blocks = set(cancel_blocks or [])
        self._cancel_sticky = set(cancel_sticky or [])
        self._resolution = {}
        self._trades = {}
        # Provenance BUY fills seeded by _proven_fill; kept separate so test
        # assignments to _trades never wipe bot ownership evidence.
        self._bot_fills = {}

    # --- plumbing ---
    def get_funder(self):
        return "0xFunder"

    def get_user_positions(self, _funder):
        return [dict(p) for p in self._positions]

    def set_positions(self, positions):
        self._positions = [dict(p) for p in positions]

    def set_open_orders(self, orders):
        self._open_orders = [dict(o) for o in orders]

    def get_open_orders(self):
        return [dict(o) for o in self._open_orders]

    def get_orderbook(self, asset):
        return self._books.get(asset) or {"bids": [], "asks": [], "tick_size": "0.01"}

    def get_trades(self, params=None):
        return [t for values in self._bot_fills.values() for t in values] + [
            t for values in self._trades.values() for t in values
        ]

    def get_market(self, cid):
        return self._tokens.get(cid) or {"tokens": []}

    def gamma_resolution_status(self, cids):
        return {c: self._resolution.get(c) for c in (cids or [])}

    def get_balance(self):
        return self._balance

    _balance = 100.0

    # --- mutations (recorded, never live) ---
    def cancel_orders(self, ids):
        ids = list(ids or [])
        if any(i in self._cancel_blocks for i in ids):
            raise RuntimeError("cancel rejected in test")
        self._cancelled.extend(ids)
        if not any(i in self._cancel_sticky for i in ids):
            self._open_orders = [o for o in self._open_orders if o.get("id") not in ids]

    def place_limit_buy(self, token_id, price, size, tick_size="0.01", neg_risk=None):
        self._placed_buys.append((token_id, price, size))
        return {"orderID": f"bot-{len(self._placed_buys)}"}

    def place_limit_sell(self, token_id, price, size, tick_size="0.01", neg_risk=None):
        self._placed_sells.append((token_id, price, size))

    def place_post_only_sell(self, token_id, price, size, tick_size="0.01", neg_risk=None):
        self._placed_post_only_sells.append((token_id, price, size))

    def place_marketable_limit_sell(
        self, token_id, price, size, tick_size="0.01", neg_risk=None
    ):
        self._placed_market_sells.append((token_id, price, size))
        return {"makingAmount": float(size), "takingAmount": float(size) * float(price)}

    def place_complement_fok_buy(
        self, token_id, size, limit_price, tick_size="0.01", neg_risk=None
    ):
        self._placed_foks.append((token_id, size, limit_price))


def _db(tmp_path, template=None):
    db = Database(str(tmp_path / "test.db"))
    db.init()
    if template:
        db.save_template(db.get_default_template_id(), template)
    return db


def _fill(asset, size, price=0.30, market=CID, order_id="o1"):
    return {
        "trade_id": "t1",
        "order_id": order_id,
        "asset_id": asset,
        "price": price,
        "size": size,
        "market": market,
        "ts": 1000.0,
    }


def _proven_fill(db, api, asset, size, price=0.30, market=CID, order_id="o1", wallet="0xW"):
    """Record bot-order provenance AND its authoritative BUY fill, then return
    the fill event (fix pack 3: managed inventory is replayed from fills)."""
    db.record_bot_buy_order(wallet, order_id, market, asset)
    api._bot_fills.setdefault(market, []).append(
        # epoch-early ts: the provenance BUY must precede any later SELL fills
        # in the FIFO managed-inventory replay.
        _sell_fill(asset, size, price, ts=1.0, side="BUY", order_id=order_id)
    )
    return _fill(asset, size, price=price, market=market, order_id=order_id)


def _engine(db, api, *, costs=None, readiness=None, trades_provider=None):
    if readiness is None:
        readiness = lambda: (True, "READY")
    return InventoryExitEngine(
        api,
        db,
        "0xW",
        encryption_key=b"k" * 32,
        condition_lock=lambda cid: __import__("threading").Lock(),
        cost_provider=lambda asset, size, cid: (costs or {}).get(asset, (0.30, [])),
        book_provider=lambda asset: api.get_orderbook(asset),
        trades_provider=trades_provider or (lambda cid: api.get_trades()),
        readiness_checker=readiness,
    )


def _sell_fill(asset, qty, price, ts, side="SELL", order_id="o1"):
    """A maker-order style fill record (as returned by get_trades/extract_fills)."""
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


def _evidence_costs(api, fallback_cost=0.30):
    """Cost provider that only proves ownership when CLOB BUY fills exist."""
    from engine.fills import extract_fills

    def provider(asset, size, cid):
        fills = extract_fills(api.get_trades(), "0xFunder", asset)
        buys = [f for f in fills if f["side"] == "BUY"]
        if not buys:
            return None, []
        qty = sum(float(f["size"]) for f in buys)
        if qty < float(size) - 1e-6:
            return None, []
        return (
            fallback_cost,
            [
                {
                    "price": float(f["price"]),
                    "take": float(f["size"]),
                    "ts": float(f["ts"]),
                    "trade_id": f["trade_id"],
                }
                for f in buys
            ],
        )

    return provider


def _seed_bot_inventory(db, api, assets, cid=CID, ts=100.0):
    """Record bot orders + BUY fills proving bot ownership of ``assets``."""
    fills = []
    for i, (asset, qty, price) in enumerate(assets):
        oid = f"o-seed-{i}"
        db.record_bot_buy_order("0xW", oid, cid, asset)
        fills.append(_sell_fill(asset, qty, price, ts=ts, side="BUY", order_id=oid))
    api._bot_fills[cid] = fills


def _pos(side, size, asset, cid=CID):
    return {"conditionId": cid, "asset": asset, "outcome": side, "size": size}


def _book(bids, asks, tick="0.01"):
    return {
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "tick_size": tick,
    }


def _template_fast(**overrides):
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
    t.update(overrides)
    return t


@pytest.fixture
def merge_env(monkeypatch):
    """Enable the Type3 Merge funds path (read-only config resolution)."""
    monkeypatch.setenv("PMM_BUILDER_API_KEY", "k")
    monkeypatch.setenv("PMM_BUILDER_SECRET", "s")
    monkeypatch.setenv("PMM_BUILDER_PASSPHRASE", "p")
    return monkeypatch


def _market_api(yes_asset="yes", no_asset="no", cid=CID):
    return _tok(cid, yes_asset, no_asset)


# ---------------------------------------------------------------------------
# §41 A: fill -> maker SELL fills -> MAKER -> CLOSED
# ---------------------------------------------------------------------------


def test_flow_a_maker_exit(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={"no": _book([(0.28, 100)], [(0.29, 100)])},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    api.set_open_orders([])
    # complement ask expensive -> merge route loses -> maker window
    api._books["yes"] = _book([(0.28, 100)], [(0.75, 100)])
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.29))
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle is not None
    assert cycle["status"] == "ACTIVE"

    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # maker escape is GTC POST-ONLY, rests below cost (0.29 < 0.30) at best ask.
    assert api._placed_post_only_sells == [("no", 0.29, 50)]
    assert api._placed_sells == []
    assert api._placed_market_sells == []
    assert api._placed_foks == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["selected_route"] == "MAKER_WINDOW"
    # audit fix I: a rested maker intent has ZERO realized collateral
    legs = db.get_exit_legs(cycle["id"])
    maker_leg = next(l for l in legs if l["kind"] == "maker_sell")
    assert maker_leg["status"] == "rested"
    assert maker_leg["collateral"] == 0.0

    # maker sell filled -> inventory zero -> CLOSED as MAKER
    api._trades[CID] = [_sell_fill("no", 50, 0.29, ts=time.time() + 5)]
    api.set_positions([])
    eng.run_tick(open_orders=[], positions=[])
    assert db.get_active_exit_cycle("0xW", CID) is None
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])
    assert len(closed) == 1
    legs = db.get_exit_legs(closed[0]["id"])
    maker_leg = next(l for l in legs if l["kind"] == "maker_sell")
    assert maker_leg["status"] == "confirmed"
    assert maker_leg["exit_method"] == "MAKER"
    assert closed[0]["realized_recovered_collateral"] == pytest.approx(14.5)
    assert closed[0]["inventory_pnl"] == pytest.approx(14.5 - 50 * 0.29)
    assert closed[0]["error"] == ""


# ---------------------------------------------------------------------------
# §41 B: fill -> opposite reward BUY fills -> pair -> MERGE -> CLOSED
# ---------------------------------------------------------------------------


def test_flow_b_opposite_fill_pair_merge(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 20, "no")],
        books={},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.32, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 20, price=0.32))
    # opposite Reward BUY (also bot-placed) filled -> bot complete set
    eng.on_reward_fill(_proven_fill(db, api, "yes", 20, price=0.32, order_id="o2"))
    api.set_positions([_pos("NO", 20, "no"), _pos("YES", 20, "yes")])
    eng.run_tick(open_orders=[], positions=[_pos("NO", 20, "no"), _pos("YES", 20, "yes")])
    # Rule #2: complete set is Merge-first; no unilateral sell.
    assert api._placed_sells == []
    assert api._placed_post_only_sells == []
    assert api._placed_market_sells == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["selected_route"] == "MERGE_FIRST"
    assert cycle["paired_qty"] == 20

    # check_merges submits and confirms the Merge (simulated), inventory drops.
    op_id = db.create_merge_operation("0xW", "0xFunder", CID, "yes", "no", 20.0)
    db.update_merge_operation(op_id, "confirmed", "relayer-1", "0xh", realized_pnl=0.6)
    db.mark_merge_inventory_reconciled(op_id)
    api.set_positions([])
    eng.run_tick(open_orders=[], positions=[])
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])
    assert len(closed) == 1
    legs = db.get_exit_legs(closed[0]["id"])
    methods = {l["exit_method"] for l in legs if l["exit_method"]}
    assert methods == {"MERGE"}
    # both legs were bot reward buys: NO20@0.32 + YES20@0.32, merged back to 20
    assert closed[0]["inventory_pnl"] == pytest.approx(20 - 2 * 20 * 0.32, abs=1e-6)


# ---------------------------------------------------------------------------
# §41 C: fill -> FOK complement superior -> cancel opposite -> exact FOK -> Merge
# ---------------------------------------------------------------------------


def test_flow_c_fok_merge_full_sequence(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.19, 100)], [(0.20, 100)]),
            "yes": _book([(0.19, 100)], [(0.63, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    api.set_open_orders(
        [
            {"id": "b1", "side": "BUY", "asset_id": "yes", "original_size": 30, "size_matched": 0},
            {"id": "s1", "side": "SELL", "asset_id": "no", "original_size": 50, "size_matched": 0},
        ]
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 50, "no")])
    # 1) opposite Reward BUY and SELL reservation cancelled BEFORE the FOK
    assert set(api._cancelled) == {"b1", "s1"}
    # 2) exact protected FOK with the depth worst-case limit
    assert api._placed_foks == [("yes", 50, 0.63)]
    assert api._placed_market_sells == []
    # 3) durable planned merge persisted; barrier set; cycle CLOSING
    ops = db.get_unresolved_merges("0xW")
    assert len(ops) == 1 and ops[0]["status"] == "planned"
    assert "FOK accepted" in ops[0]["error"]
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["status"] == "CLOSING"
    legs = db.get_exit_legs(cycle["id"])
    assert any(l["kind"] == "complement_buy" for l in legs)

    # restart: a fresh engine must NOT resubmit the FOK or market-sell
    eng2 = _engine(db, api, costs={"no": (0.30, [])})
    api.set_positions([_pos("NO", 50, "no"), _pos("YES", 50, "yes")])
    eng2.run_tick(open_orders=[], positions=[_pos("NO", 50, "no"), _pos("YES", 50, "yes")])
    assert len(api._placed_foks) == 1
    assert api._placed_market_sells == []

    # Merge confirms -> inventory zero -> CLOSED as FOK+MERGE
    op = db.get_unresolved_merges("0xW")[0]
    db.update_merge_operation(op["id"], "confirmed", "relayer-1", "0xh", realized_pnl=0.6)
    db.mark_merge_inventory_reconciled(op["id"])
    api.set_positions([])
    eng2.run_tick(open_orders=[], positions=[])
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])
    assert len(closed) == 1
    methods = {l["exit_method"] for l in db.get_exit_legs(closed[0]["id"]) if l["exit_method"]}
    assert methods == {"FOK+MERGE"}


# ---------------------------------------------------------------------------
# §41 D: direct exit superior -> protected market SELL -> MARKET -> CLOSED
# ---------------------------------------------------------------------------


def test_flow_d_protected_direct_exit(tmp_path):
    db = _db(tmp_path, _template_fast(maker_exit_wait_sec=0))
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 50)], [(0.30, 100)]),
            "yes": _book([], [(0.78, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # protected FAK at the depth worst acceptable price, not uncontrolled FAK
    assert api._placed_market_sells == [("no", 0.28, 50)]
    assert api._placed_sells == []
    assert api._placed_foks == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["status"] == "CLOSING"
    assert cycle["selected_route"] == "MARKET"
    # audit fix I: the dispatched FAK leg is pending with ZERO realized value
    legs = db.get_exit_legs(cycle["id"])
    market_leg = next(l for l in legs if l["kind"] == "market_sell")
    assert market_leg["status"] == "pending"
    assert market_leg["collateral"] == 0.0

    api._trades[CID] = [_sell_fill("no", 50, 0.28, ts=time.time() + 5)]
    api.set_positions([])
    eng.run_tick(open_orders=[], positions=[])
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])[0]
    assert closed["realized_recovered_collateral"] == pytest.approx(14.0)
    assert closed["error"] == ""
    market_leg = next(l for l in db.get_exit_legs(closed["id"]) if l["kind"] == "market_sell")
    assert market_leg["exit_method"] == "MARKET"
    assert market_leg["status"] == "confirmed"


# ---------------------------------------------------------------------------
# §41 E: YES30/NO20 -> Merge20 -> residual YES10 -> MIXED
# ---------------------------------------------------------------------------


def test_flow_e_mixed_merge_then_maker(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("YES", 30, "yes"), _pos("NO", 20, "no")],
        books={
            "yes": _book([(0.40, 10)], [(0.42, 100)]),
            "no": _book([], [(0.68, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"yes": (0.30, [])})
    _seed_bot_inventory(db, api, [("yes", 30, 0.30), ("no", 20, 0.30)])
    # pre-V2 leftover adopted
    eng.run_tick(open_orders=[], positions=[_pos("YES", 30, "yes"), _pos("NO", 20, "no")])
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle is not None and cycle["trigger"] == "adopt_legacy"
    assert cycle["paired_qty"] == 20
    assert api._placed_sells == []  # pair untouched
    assert api._placed_post_only_sells == []

    # Merge 20 confirmed; residual YES 10 remains
    op_id = db.create_merge_operation("0xW", "0xFunder", CID, "yes", "no", 20.0)
    db.update_merge_operation(op_id, "confirmed", "relayer-1", "0xh", realized_pnl=0.6)
    db.mark_merge_inventory_reconciled(op_id)
    api.set_positions([_pos("YES", 10, "yes")])
    eng.run_tick(open_orders=[], positions=[_pos("YES", 10, "yes")])
    # residual YES 10: direct 4.0 vs merge 10-6.8=3.2 -> maker window
    assert api._placed_post_only_sells == [("yes", 0.42, 10)]

    api._trades[CID] = [_sell_fill("yes", 10, 0.42, ts=time.time() + 5)]
    api.set_positions([])
    eng.run_tick(open_orders=[], positions=[])
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])[0]
    methods = {l["exit_method"] for l in db.get_exit_legs(closed["id"]) if l["exit_method"]}
    assert methods == {"MERGE", "MAKER"}


# ---------------------------------------------------------------------------
# §41 F: partial maker exit -> residual recalculated
# ---------------------------------------------------------------------------


def test_flow_f_partial_maker_exit_recalculates(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 100)], [(0.29, 100)]),
            "yes": _book([], [(0.75, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    assert api._placed_post_only_sells == [("no", 0.29, 50)]

    # 30 filled, 20 remain: existing sell replaced with the new residual size
    api.set_positions([_pos("NO", 20, "no")])
    api.set_open_orders(
        [{"id": "s-old", "side": "SELL", "asset_id": "no", "original_size": 50, "size_matched": 30, "price": 0.29}]
    )
    api._trades[CID] = [_sell_fill("no", 30, 0.29, ts=time.time() + 5)]
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 20, "no")])
    # the still-resting sell (remaining 20 @ 0.29) matches the residual -> kept
    assert api._cancelled == []
    assert len(api._placed_post_only_sells) == 1
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["managed_qty"] == 20
    legs = db.get_exit_legs(cycle["id"])
    maker_leg = next(l for l in legs if l["kind"] == "maker_sell")
    assert maker_leg["status"] == "confirmed"
    assert maker_leg["qty"] == pytest.approx(30)  # only the confirmed portion


# ---------------------------------------------------------------------------
# §41 G / §43: lagged Data API -> no duplicate exit
# ---------------------------------------------------------------------------


def test_flow_g_lagged_data_api_no_duplicate_exit(tmp_path):
    db = _db(tmp_path, _template_fast(maker_exit_wait_sec=0))
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 50)], [(0.30, 100)]),
            "yes": _book([], [(0.78, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    assert len(api._placed_market_sells) == 1
    # Data API still shows the position (lag): the CLOSING guard suppresses a
    # second market sell.
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    assert len(api._placed_market_sells) == 1


# ---------------------------------------------------------------------------
# §42: churn protection
# ---------------------------------------------------------------------------


def test_churn_same_token_rebuy_blocked_and_opposite_capped(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 20, "no")],
        books={},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api)
    eng.on_reward_fill(_proven_fill(db, api, "no", 20, price=0.32))
    # same held token: blocked while the cycle owns residual
    authority = eng.authorize_placement(CID, "no", "NO", 50, [])
    assert authority["kind"] == "same_token_block" and authority["allowed"] is False
    # opposite token: scanner YES 50 capped to residual 20
    authority = eng.authorize_placement(CID, "yes", "YES", 50, [])
    assert authority["allowed"] is True
    assert authority["total_target"] == 20
    # TOTAL target semantics: open opposite 10 + proposal 50 -> target total 20
    open_orders = [
        {"id": "b1", "side": "BUY", "asset_id": "yes", "original_size": 10, "size_matched": 0}
    ]
    authority = eng.authorize_placement(CID, "yes", "YES", 50, open_orders)
    assert authority["total_target"] == 20
    # open already at cap -> target total stays 20 (reconcile keeps it)
    open_orders = [
        {"id": "b1", "side": "BUY", "asset_id": "yes", "original_size": 20, "size_matched": 0}
    ]
    authority = eng.authorize_placement(CID, "yes", "YES", 50, open_orders)
    assert authority["kind"] == "opposite_cap"
    assert authority["total_target"] == 20
    # residual 0 -> target total 0 (reconcile cancels all opposite BUYs)
    db.update_exit_cycle(
        db.get_active_exit_cycle("0xW", CID)["id"], managed_qty=0, paired_qty=0
    )
    authority = eng.authorize_placement(CID, "yes", "YES", 50, [])
    assert authority["total_target"] == 0


def test_churn_after_close_cooldown_still_applies(tmp_path):
    db = _db(tmp_path, _template_fast(maker_exit_wait_sec=0))
    api = FakeAPI(
        positions=[_pos("NO", 20, "no")],
        books={
            "no": _book([(0.28, 20)], [(0.30, 100)]),
            "yes": _book([], [(0.78, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 20, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 20, "no")])
    api.set_positions([])
    eng.run_tick(open_orders=[], positions=[])
    assert db.get_active_exit_cycle("0xW", CID) is None
    # cycle close does not bypass the cooldown mechanism (separate table).
    db.set_cooldown("0xW", CID, 20)
    assert db.is_in_cooldown("0xW", CID) is True
    # and the same token is placeable again once closed (cooldown is the gate).
    authority = eng.authorize_placement(CID, "no", "NO", 50, [])
    assert authority["allowed"] is True


# ---------------------------------------------------------------------------
# §43: races and isolation
# ---------------------------------------------------------------------------


def test_race_fok_withheld_when_cancellation_not_reconciled(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.19, 100)], [(0.20, 100)]),
            "yes": _book([], [(0.63, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
        cancel_blocks={"b1"},
    )
    api.set_open_orders(
        [{"id": "b1", "side": "BUY", "asset_id": "yes", "original_size": 30, "size_matched": 0}]
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 50, "no")])
    # cancellation rejected -> FOK must never be submitted
    assert api._placed_foks == []
    assert api._placed_market_sells == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["status"] == "BLOCKED"
    assert "cancellation" in cycle["error"]


def test_race_two_processes_same_condition_unique_cycle(tmp_path):
    db1 = _db(tmp_path, _template_fast())
    db2 = Database(str(tmp_path / "test.db"))  # same file, separate connection
    db2.init()
    api1 = FakeAPI(positions=[], books={}, tokens={"0x" + "c" * 64: _market_api()})
    api2 = FakeAPI(positions=[], books={}, tokens={"0x" + "c" * 64: _market_api()})
    eng1 = _engine(db1, api1)
    eng2 = _engine(db2, api2)
    eng1.on_reward_fill(_proven_fill(db1, api1, "no", 20))
    # A second process joining the same (wallet, condition) must JOIN the one
    # active cycle, never create a duplicate.
    eng2.on_reward_fill(_proven_fill(db2, api2, "no", 20))
    cycle = db2.get_active_exit_cycle("0xW", CID)
    assert cycle["initial_qty"] == 40
    assert len(db2.get_non_closed_exit_cycles("0xW")) == 1
    with pytest.raises(ActiveExitCycleExists):
        db2.create_exit_cycle("0xW", CID, trigger="reward_fill", qty=1)


def test_race_multiple_conditions_do_not_block_each_other(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 20, "no", CID), _pos("NO", 20, "no-b", CID_B)],
        books={
            "no": _book([(0.19, 100)], [(0.20, 100)]),
            "no-b": _book([(0.28, 100)], [(0.29, 100)]),
            "yes": _book([], [(0.63, 100)]),
            "yes-b": _book([], [(0.75, 100)]),
        },
        tokens={
            "0x" + "c" * 64: _tok(CID, "yes", "no"),
            "0x" + "b" * 64: _tok(CID_B, "yes-b", "no-b"),
        },
    )
    eng = _engine(db, api, costs={"no": (0.30, []), "no-b": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 20, market=CID))
    eng.on_reward_fill(_proven_fill(db, api, "no-b", 20, market=CID_B))
    # A goes FOK (CLOSING) while B takes the maker window; B must not be blocked.
    eng.run_tick(
        open_orders=[],
        positions=[_pos("NO", 20, "no", CID), _pos("NO", 20, "no-b", CID_B)],
    )
    assert api._placed_foks == [("yes", 20, 0.63)]
    assert api._placed_post_only_sells == [("no-b", 0.29, 20)]


def test_race_two_wallets_same_condition_isolated(tmp_path):
    db = _db(tmp_path, _template_fast())
    api_a = FakeAPI(positions=[], books={}, tokens={"0x" + "c" * 64: _market_api()})
    eng_a = _engine(db, api_a)
    eng_a.on_reward_fill(_proven_fill(db, api_a, "no", 20, price=0.32))
    # wallet B is not blocked by wallet A's cycle
    api_b = FakeAPI(positions=[], books={}, tokens={"0x" + "c" * 64: _market_api()})
    eng_b = InventoryExitEngine(
        api_b, db, "0xWB", encryption_key=b"k" * 32,
        condition_lock=lambda cid: __import__("threading").Lock(),
        cost_provider=lambda asset, size, cid: (0.30, []),
        book_provider=lambda asset: api_b.get_orderbook(asset),
    )
    eng_b.on_reward_fill(_proven_fill(db, api_b, "no", 20, price=0.32, wallet="0xWB"))
    cycles = db.get_non_closed_exit_cycles()
    assert {c["wallet"] for c in cycles} == {"0xW", "0xWB"}
    authority = eng_b.authorize_placement(CID, "no", "NO", 50, [])
    assert authority["kind"] == "same_token_block"  # B's own cycle blocks B
    # A's cycle does not leak into B's authority for the same condition
    authority = eng_a.authorize_placement(CID, "no", "NO", 50, [])
    assert authority["kind"] == "same_token_block"  # A's own cycle blocks A


def test_race_restart_after_merge_dispatch_uncertainty(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no"), _pos("YES", 50, "yes")],
        books={},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    op_id = db.create_merge_operation("0xW", "0xFunder", CID, "yes", "no", 50.0)
    db.update_merge_operation(
        op_id, "planned", error="relayer submission indeterminate: timeout"
    )
    _seed_bot_inventory(
        db, api, [("yes", 50, 0.30), ("no", 50, 0.30)]
    )
    eng = _engine(db, api)
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no"), _pos("YES", 50, "yes")])
    # unresolved planned merge -> CLOSING, no mutation of either leg
    assert api._placed_sells == []
    assert api._placed_market_sells == []
    assert api._placed_foks == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["status"] == "CLOSING"


# ---------------------------------------------------------------------------
# §45: low balance / resolution
# ---------------------------------------------------------------------------


def test_low_balance_merge_first_then_immediate_route(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast(low_balance_threshold_usd=5.0))
    api = FakeAPI(
        positions=[_pos("YES", 30, "yes"), _pos("NO", 20, "no")],
        books={
            "yes": _book([(0.40, 10)], [(0.42, 100)]),
            "no": _book([], [(0.68, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    api._balance = 3.0
    eng = _engine(db, api, costs={"yes": (0.30, [])})
    _seed_bot_inventory(db, api, [("yes", 30, 0.30), ("no", 20, 0.30)])
    eng.run_tick(open_orders=[], positions=[_pos("YES", 30, "yes"), _pos("NO", 20, "no")])
    # pair exists -> Merge-first even under low balance, never unilateral sell
    assert api._placed_market_sells == []
    assert api._placed_sells == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["selected_route"] == "MERGE_FIRST"


def test_low_balance_skips_maker_wait_for_residual(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast(low_balance_threshold_usd=5.0))
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 50)], [(0.30, 100)]),
            "yes": _book([], [(0.78, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    api._balance = 3.0
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # low balance: no 30s maker wait; immediate protected direct exit
    assert api._placed_market_sells == [("no", 0.28, 50)]


def test_resolution_immediate_safe_route_and_merge_first(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("YES", 30, "yes"), _pos("NO", 20, "no")],
        books={
            "yes": _book([(0.40, 10)], [(0.42, 100)]),
            "no": _book([], [(0.68, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    api._resolution[CID] = "resolved"
    eng = _engine(db, api, costs={"yes": (0.30, [])})
    _seed_bot_inventory(db, api, [("yes", 30, 0.30), ("no", 20, 0.30)])
    eng.run_tick(open_orders=[], positions=[_pos("YES", 30, "yes"), _pos("NO", 20, "no")])
    # resolution must not preempt the complete-set Merge
    assert api._placed_market_sells == []
    assert api._placed_sells == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["selected_route"] == "MERGE_FIRST"

    # one-sided residual at resolution: immediate route, no maker wait
    db2 = Database(str(tmp_path / "r2.db"))
    db2.init()
    db2.save_template(db2.get_default_template_id(), _template_fast())
    api2 = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 50)], [(0.30, 100)]),
            "yes": _book([], [(0.78, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    api2._resolution[CID] = "resolved"
    eng2 = _engine(db2, api2, costs={"no": (0.30, [])})
    eng2.on_reward_fill(_proven_fill(db2, api2, "no", 50, price=0.30))
    eng2.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    assert api2._placed_market_sells == [("no", 0.28, 50)]


# ---------------------------------------------------------------------------
# §44: Merge readiness gate
# ---------------------------------------------------------------------------


def test_merge_readiness_gate_blocks_type3_when_not_ready(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(positions=[], books={}, tokens={})
    eng = _engine(
        db, api, readiness=lambda: (False, "RELAYER_NOT_CONFIGURED")
    )
    ready, reason = eng.merge_runtime_ready()
    assert ready is False and reason == "RELAYER_NOT_CONFIGURED"
    # cached
    assert eng.merge_runtime_ready() == (False, "RELAYER_NOT_CONFIGURED")


def test_merge_readiness_gate_type1_type2_not_affected(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(positions=[], books={}, tokens={}, signature_type=2)
    eng = _engine(
        db, api, readiness=lambda: (False, "RELAYER_NOT_CONFIGURED")
    )
    ready, reason = eng.merge_runtime_ready()
    assert ready is True and reason == ""


def test_merge_readiness_gate_template_off_no_probe(tmp_path):
    db = _db(tmp_path, _template_fast(require_merge_ready_for_new_buys=False))
    api = FakeAPI(positions=[], books={}, tokens={})
    eng = _engine(
        db, api, readiness=lambda: (False, "RELAYER_NOT_CONFIGURED")
    )
    assert eng.merge_runtime_ready() == (True, "")


def test_manager_gate_pauses_new_buys_when_merge_not_ready(tmp_path):
    api = FakeAPI(positions=[], books={}, tokens={})
    api._books["yes"] = _book([(0.30, 300)], [(0.31, 1000)])
    tier = {
        "size": 100, "enabled": True, "shares": 150,
        "rule1_min_coeff": 0, "rule2_min_coeff": 0, "rule3_min_coeff": 0,
        "gap_high_coeff_sum_min": 20,
        "amount_value_table": [{"upper": 1.0, "value": 1}],
    }
    db = _db(tmp_path, _template_fast(size_tiers=[tier]))
    worker = WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5})

    class FakeEngine:
        def new_opening_gate_ready(self):
            return False, "RELAYER_AUTH_FAILED"

        def authorize_placement(self, *a, **kw):
            return {"kind": "ok", "allowed": True, "total_target": None, "reason": ""}

    worker.monitor = type("M", (), {"exit_engine": lambda self: FakeEngine()})()
    elig = [{
        "market_id": CID, "token_id": "yes", "outcome": "Yes",
        "market_name": "M", "rewards_min_size": 100, "rewards_max_spread": 6,
        "tick_size_str": "0.01", "neg_risk": False,
    }]
    worker.place_orders(elig)
    assert api._placed_buys == []

    class FakeEngineReady:
        def new_opening_gate_ready(self):
            return True, ""

        def authorize_placement(self, *a, **kw):
            return {"kind": "ok", "allowed": True, "total_target": None, "reason": ""}

    worker.monitor = type("M", (), {"exit_engine": lambda self: FakeEngineReady()})()
    worker.place_orders(elig)
    assert api._placed_buys != []


# ---------------------------------------------------------------------------
# §46: DB / restart
# ---------------------------------------------------------------------------


def test_db_migration_idempotent_and_preserves_existing(tmp_path):
    db = _db(tmp_path)
    db.add_wallet("0xW", "enc-blob", "0xF", signature_type=3)
    db.create_exit_cycle("0xW", CID, trigger="reward_fill", held_side="NO", held_asset_id="no", qty=20)
    # re-init on the same file must be idempotent and preserve rows
    db.init()
    assert db.get_active_exit_cycle("0xW", CID)["initial_qty"] == 20
    assert db.list_wallets()[0]["encrypted_key"] == "enc-blob"
    assert db.list_wallets()[0]["signature_type"] == 3


def test_cycle_and_legs_survive_restart(tmp_path):
    db1 = _db(tmp_path)
    cycle_id = db1.create_exit_cycle("0xW", CID, trigger="reward_fill", held_side="NO", held_asset_id="no", qty=20)
    db1.add_exit_leg(cycle_id, "0xW", CID, "reward_buy", asset_id="no", side="NO", qty=20, price=0.3, collateral=-6.0)
    db1.update_exit_cycle(cycle_id, status="CLOSING", error="restart test")
    db2 = Database(str(tmp_path / "test.db"))
    db2.init()
    cycle = db2.get_active_exit_cycle("0xW", CID)
    assert cycle is not None
    assert cycle["status"] == "CLOSING"
    assert len(db2.get_exit_legs(cycle_id)) == 1


def test_closed_cycle_not_reopened_by_stale_data_api(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[],
        books={},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api)
    cycle_id = db.create_exit_cycle("0xW", CID, trigger="reward_fill", held_side="NO", held_asset_id="no", qty=20)
    db.close_exit_cycle(cycle_id, closed_reason="test close", holding_duration_sec=10)
    # stale Data API still shows the old inventory: no adoption, no new cycle
    eng.run_tick(open_orders=[], positions=[_pos("NO", 20, "no")])
    assert db.get_active_exit_cycle("0xW", CID) is None
    assert len(db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])) == 1


def test_planned_fok_not_repeated_and_submitted_merge_not_repeated(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.19, 100)], [(0.20, 100)]),
            "yes": _book([], [(0.63, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    op_id = db.create_merge_operation("0xW", "0xFunder", CID, "yes", "no", 50.0)
    db.update_merge_operation(op_id, "planned", error="FOK accepted; awaiting confirmed Data API inventory")
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    assert api._placed_foks == []
    assert api._placed_market_sells == []
    # and a submitted merge blocks residual mutation
    db.update_merge_operation(op_id, "submitted", "relayer-9")
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    assert api._placed_foks == []
    assert api._placed_market_sells == []


def test_fail_closed_positions_unavailable_blocks(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(positions=[], books={}, tokens={})
    api.get_user_positions = lambda _f: (_ for _ in ()).throw(RuntimeError("network"))
    eng = _engine(db, api)
    db.create_exit_cycle("0xW", CID, trigger="reward_fill", held_side="NO", held_asset_id="no", qty=20)
    eng.run_tick(open_orders=[])
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["status"] == "BLOCKED"


# ---------------------------------------------------------------------------
# Audit fix pack (independent audit)
# ---------------------------------------------------------------------------


def test_audit_a_barrier_query_failure_blocks_all_funds_paths(tmp_path):
    db = _db(tmp_path, _template_fast(maker_exit_wait_sec=0))
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.19, 100)], [(0.20, 100)]),
            "yes": _book([], [(0.63, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    db.get_unresolved_merges = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("db down")
    )
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # unknown barrier state MUST NOT mean "no barrier"
    assert api._placed_foks == []
    assert api._placed_market_sells == []
    assert api._placed_post_only_sells == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["status"] == "BLOCKED"
    assert "barrier" in cycle["error"]


def test_audit_b_direct_cancel_error_blocks_no_fak(tmp_path):
    db = _db(tmp_path, _template_fast(maker_exit_wait_sec=0))
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 50)], [(0.30, 100)]),
            "yes": _book([], [(0.78, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
        cancel_blocks={"s1"},
    )
    api.set_open_orders(
        [{"id": "s1", "side": "SELL", "asset_id": "no", "original_size": 50, "size_matched": 0}]
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 50, "no")])
    assert api._placed_market_sells == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["status"] == "BLOCKED"
    assert "cancellation" in cycle["error"]


def test_audit_b_direct_cancel_not_reconciled_blocks_no_fak(tmp_path):
    db = _db(tmp_path, _template_fast(maker_exit_wait_sec=0))
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 50)], [(0.30, 100)]),
            "yes": _book([], [(0.78, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
        cancel_sticky={"s1"},
    )
    api.set_open_orders(
        [{"id": "s1", "side": "SELL", "asset_id": "no", "original_size": 50, "size_matched": 0}]
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 50, "no")])
    # cancel response returned but the SELL is still visible -> no FAK
    assert "s1" in api._cancelled
    assert api._placed_market_sells == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["status"] == "BLOCKED"
    assert "not reconciled" in cycle["error"]


def test_audit_b_direct_uses_fresh_residual_and_depth_after_cancel(tmp_path):
    db = _db(tmp_path, _template_fast(maker_exit_wait_sec=0))
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 50)], [(0.30, 100)]),
            "yes": _book([], [(0.78, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    calls = {"positions": 0, "books": 0}

    def positions_fn(_f):
        calls["positions"] += 1
        # inventory shrank during cancellation: the direct-exit refetch (the
        # only API-level read here) must see the FRESH residual 30
        return [dict(p) for p in (
            [_pos("NO", 30, "no")] if calls["positions"] == 1 else [_pos("NO", 50, "no")]
        )]

    def book_fn(asset):
        calls["books"] += 1
        # depth changed after cancellation: fresh read drops the best bid
        if calls["books"] >= 2:
            return _book([(0.25, 50)], [(0.30, 100)])
        return api._books.get(asset) or _book([], [])

    api.get_user_positions = positions_fn
    api.get_orderbook = book_fn
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # FAK uses the FRESH residual (30) and FRESH worst price (0.25)
    assert api._placed_market_sells == [("no", 0.25, 30)]


@pytest.mark.parametrize(
    "reason", ["RELAYER_AUTH_FAILED", "FUNDER_MISMATCH", "RELAYER_UNREACHABLE"]
)
def test_audit_c_no_complement_fok_when_merge_not_ready(tmp_path, merge_env, reason):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.19, 100)], [(0.20, 100)]),
            "yes": _book([], [(0.63, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(
        db, api, costs={"no": (0.30, [])},
        readiness=lambda: (False, reason),
    )
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # credentials exist (merge_env) but runtime readiness fails -> zero FOK
    assert api._placed_foks == []
    # falls back to the maker window (or protected direct), never complement
    assert api._placed_post_only_sells != [] or api._placed_market_sells != []


def test_audit_c_ready_route_executes_fok(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.19, 100)], [(0.20, 100)]),
            "yes": _book([], [(0.63, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    assert api._placed_foks == [("yes", 50, 0.63)]


def _elig_yes():
    return {
        "market_id": CID, "token_id": "yes", "outcome": "Yes",
        "market_name": "M", "rewards_min_size": 100, "rewards_max_spread": 6,
        "tick_size_str": "0.01", "neg_risk": False,
    }


def _tier():
    return {
        "size": 100, "enabled": True, "shares": 150,
        "rule1_min_coeff": 0, "rule2_min_coeff": 0, "rule3_min_coeff": 0,
        "gap_high_coeff_sum_min": 20,
        "amount_value_table": [{"upper": 1.0, "value": 1}],
    }


def _worker_with_engine(db, api, eng):
    worker = WalletWorker(api, db, "0xW", {"fill_check_interval_sec": 5})
    worker.monitor = type("M", (), {"exit_engine": lambda self: eng})()
    return worker


def test_audit_d_opposite_buys_reconciled_to_total_target(tmp_path):
    db = _db(tmp_path, _template_fast(size_tiers=[_tier()]))
    api = FakeAPI(
        positions=[_pos("NO", 20, "no")],
        books={"yes": _book([(0.30, 300)], [(0.31, 1000)])},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    api.set_open_orders(
        [{"id": "b1", "side": "BUY", "asset_id": "yes", "original_size": 30, "size_matched": 0, "price": 0.30}]
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 20, price=0.32))
    worker = _worker_with_engine(db, api, eng)
    worker.place_orders([_elig_yes()])
    # residual 20: the resting 30 must shrink to a TOTAL target of 20
    assert "b1" in api._cancelled
    placed = sum(s for _t, _p, s in api._placed_buys)
    assert placed <= 20
    # residual shrinks to 10: placement round reconciles down again
    api.set_open_orders(
        [{"id": "b2", "side": "BUY", "asset_id": "yes", "original_size": 20, "size_matched": 0, "price": 0.30}]
    )
    api._placed_buys = []
    api._cancelled = []
    db.update_exit_cycle(
        db.get_active_exit_cycle("0xW", CID)["id"], managed_qty=10, paired_qty=0
    )
    worker.place_orders([_elig_yes()])
    assert "b2" in api._cancelled
    placed = sum(s for _t, _p, s in api._placed_buys)
    assert placed <= 10
    # residual 0: all opposite Reward BUYs cancelled, none placed
    api.set_open_orders(
        [{"id": "b3", "side": "BUY", "asset_id": "yes", "original_size": 20, "size_matched": 0, "price": 0.30}]
    )
    api._placed_buys = []
    api._cancelled = []
    db.update_exit_cycle(
        db.get_active_exit_cycle("0xW", CID)["id"], managed_qty=0, paired_qty=0
    )
    worker.place_orders([_elig_yes()])
    assert "b3" in api._cancelled
    assert api._placed_buys == []


def test_audit_e_manual_position_never_mutated(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 20, "no")],
        books={"no": _book([(0.28, 100)], [(0.29, 100)])},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    # no CLOB trade evidence at all -> UNMANAGED, zero mutation
    eng = InventoryExitEngine(
        api, db, "0xW", encryption_key=b"k" * 32,
        condition_lock=lambda cid: __import__("threading").Lock(),
        cost_provider=_evidence_costs(api),
        book_provider=lambda asset: api.get_orderbook(asset),
        readiness_checker=lambda: (True, "READY"),
    )
    eng.run_tick(open_orders=[], positions=[_pos("NO", 20, "no")])
    assert db.get_active_exit_cycle("0xW", CID) is None
    assert api._placed_post_only_sells == []
    assert api._placed_market_sells == []
    assert api._placed_foks == []


def test_audit_e_mixed_manual_bot_claims_nothing(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 20, "no")],
        books={"no": _book([(0.28, 100)], [(0.29, 100)])},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    # bot fills prove only 10 of the 20 shares -> no adoption of the whole
    api._trades[CID] = [_sell_fill("no", 10, 0.30, ts=100, side="BUY")]
    eng = InventoryExitEngine(
        api, db, "0xW", encryption_key=b"k" * 32,
        condition_lock=lambda cid: __import__("threading").Lock(),
        cost_provider=_evidence_costs(api),
        book_provider=lambda asset: api.get_orderbook(asset),
        readiness_checker=lambda: (True, "READY"),
    )
    eng.run_tick(open_orders=[], positions=[_pos("NO", 20, "no")])
    assert db.get_active_exit_cycle("0xW", CID) is None
    assert api._placed_market_sells == []
    assert api._placed_foks == []


def test_audit_f_offline_fill_after_closed_cycle_recovered_once(tmp_path):
    db = _db(tmp_path, _template_fast())
    prior_id = db.create_exit_cycle(
        "0xW", CID, trigger="reward_fill", held_side="NO", held_asset_id="no", qty=20
    )
    close_ts = time.time()
    db.close_exit_cycle(
        prior_id, closed_reason="prior close",
        realized_recovered_collateral=5.8, inventory_pnl=0.0,
        holding_duration_sec=10,
    )
    db.update_exit_cycle(prior_id, closed_at=close_ts)
    api = FakeAPI(
        positions=[_pos("NO", 20, "no")],
        books={"no": _book([(0.28, 100)], [(0.29, 100)])},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    # new offline bot Reward BUY fill with evidence AFTER the previous close
    db.record_bot_buy_order("0xW", "o-offline", CID, "no")
    api._trades[CID] = [
        _sell_fill("no", 20, 0.30, ts=close_ts + 100, side="BUY", order_id="o-offline")
    ]
    eng = InventoryExitEngine(
        api, db, "0xW", encryption_key=b"k" * 32,
        condition_lock=lambda cid: __import__("threading").Lock(),
        cost_provider=_evidence_costs(api),
        book_provider=lambda asset: api.get_orderbook(asset),
        readiness_checker=lambda: (True, "READY"),
    )
    eng.run_tick(open_orders=[], positions=[_pos("NO", 20, "no")])
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle is not None
    assert cycle["trigger"] == "recovered_offline_fill"
    # exactly one active cycle; a second restart does not duplicate it
    eng2 = InventoryExitEngine(
        api, db, "0xW", encryption_key=b"k" * 32,
        condition_lock=lambda cid: __import__("threading").Lock(),
        cost_provider=_evidence_costs(api),
        book_provider=lambda asset: api.get_orderbook(asset),
        readiness_checker=lambda: (True, "READY"),
    )
    eng2.run_tick(open_orders=[], positions=[_pos("NO", 20, "no")])
    assert len(db.get_non_closed_exit_cycles("0xW")) == 1


def test_audit_g_type1_pair_does_not_freeze(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("YES", 20, "yes"), _pos("NO", 20, "no")],
        books={
            "yes": _book([(0.40, 20)], [(0.42, 100)]),
            "no": _book([(0.40, 20)], [(0.42, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
        signature_type=2,
    )
    eng = _engine(db, api, costs={"yes": (0.30, []), "no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "yes", 20, price=0.30))
    eng.on_reward_fill(_proven_fill(db, api, "no", 20, price=0.30, order_id="o2"))
    eng.run_tick(
        open_orders=[],
        positions=[_pos("YES", 20, "yes"), _pos("NO", 20, "no")],
    )
    # Type1: no automatic Merge -> both owned sides route through Maker/Market
    assert api._placed_foks == []
    assert sorted(api._placed_post_only_sells) == [
        ("no", 0.42, 20),
        ("yes", 0.42, 20),
    ]


def test_audit_g_type3_merge_disabled_pair_does_not_freeze(tmp_path):
    db = _db(tmp_path, _template_fast(merge_enabled=False))
    api = FakeAPI(
        positions=[_pos("YES", 20, "yes"), _pos("NO", 20, "no")],
        books={
            "yes": _book([(0.40, 20)], [(0.42, 100)]),
            "no": _book([(0.40, 20)], [(0.42, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"yes": (0.30, []), "no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "yes", 20, price=0.30))
    eng.on_reward_fill(_proven_fill(db, api, "no", 20, price=0.30, order_id="o2"))
    eng.run_tick(
        open_orders=[],
        positions=[_pos("YES", 20, "yes"), _pos("NO", 20, "no")],
    )
    assert sorted(api._placed_post_only_sells) == [
        ("no", 0.42, 20),
        ("yes", 0.42, 20),
    ]


def test_audit_g_dust_pair_below_minimum_does_not_noop_forever(tmp_path):
    db = _db(tmp_path, _template_fast(merge_min_shares=1))
    api = FakeAPI(
        positions=[_pos("YES", 0.5, "yes"), _pos("NO", 0.5, "no")],
        books={
            "yes": _book([(0.40, 10)], [(0.42, 100)]),
            "no": _book([(0.40, 10)], [(0.42, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"yes": (0.30, []), "no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "yes", 0.5, price=0.30))
    eng.on_reward_fill(_proven_fill(db, api, "no", 0.5, price=0.30, order_id="o2"))
    eng.run_tick(
        open_orders=[],
        positions=[_pos("YES", 0.5, "yes"), _pos("NO", 0.5, "no")],
    )
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["selected_route"] != "NOOP"
    assert sorted(api._placed_post_only_sells) == [
        ("no", 0.42, 0.5),
        ("yes", 0.42, 0.5),
    ]


def test_audit_i_rested_maker_then_market_counts_only_market(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 100)], [(0.29, 100)]),
            "yes": _book([], [(0.75, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    assert api._placed_post_only_sells == [("no", 0.29, 50)]
    # maker never filled; window times out -> protected MARKET exit
    cycle = db.get_active_exit_cycle("0xW", CID)
    db.update_exit_cycle(cycle["id"], opened_at=cycle["opened_at"] - 31)
    api.set_open_orders(
        [{"id": "s1", "side": "SELL", "asset_id": "no", "original_size": 50, "size_matched": 0, "price": 0.29}]
    )
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 50, "no")])
    assert api._placed_market_sells == [("no", 0.28, 50)]
    api._trades[CID] = [_sell_fill("no", 50, 0.28, ts=time.time() + 5)]
    api.set_positions([])
    eng.run_tick(open_orders=[], positions=[])
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])[0]
    # the rested maker intent never filled -> after the ledger grace it is
    # cancelled; the cycle must finalize as MARKET, never MIXED
    db.update_exit_cycle(closed["id"], closed_at=closed["closed_at"] - 301)
    eng.run_tick(open_orders=[], positions=[])
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])[0]
    assert closed["error"] == ""
    legs = db.get_exit_legs(closed["id"])
    maker_leg = next(l for l in legs if l["kind"] == "maker_sell")
    assert maker_leg["status"] == "cancelled"
    market_leg = next(l for l in legs if l["kind"] == "market_sell")
    assert market_leg["status"] == "confirmed"
    assert closed["realized_recovered_collateral"] == pytest.approx(14.0)
    methods = {
        l["exit_method"]
        for l in legs
        if l["exit_method"] and l["status"] in ("confirmed", "done")
    }
    assert methods == {"MARKET"}


# ---------------------------------------------------------------------------
# Fix pack 2 regressions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", ["RELAYER_AUTH_FAILED", "FUNDER_MISMATCH", "RELAYER_UNREACHABLE"]
)
def test_fix2_gate_split_opening_allowed_fok_forbidden(tmp_path, merge_env, reason):
    db = _db(tmp_path, _template_fast(require_merge_ready_for_new_buys=False))
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.19, 100)], [(0.20, 100)]),
            "yes": _book([], [(0.63, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(
        db, api, costs={"no": (0.30, [])},
        readiness=lambda: (False, reason),
    )
    # the new-opening gate may pass when the template disables it...
    assert eng.new_opening_gate_ready() == (True, "")
    # ...but execution readiness ALWAYS validates
    ready, exec_reason = eng.merge_execution_ready()
    assert ready is False and exec_reason == reason
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # complement FOK MUST remain forbidden
    assert api._placed_foks == []
    assert api._placed_post_only_sells != [] or api._placed_market_sells != []


def test_fix2_cap_cancel_raises_no_replacement(tmp_path):
    db = _db(tmp_path, _template_fast(size_tiers=[_tier()]))
    api = FakeAPI(
        positions=[_pos("NO", 10, "no")],
        books={"yes": _book([(0.30, 300)], [(0.31, 1000)])},
        tokens={"0x" + "c" * 64: _market_api()},
        cancel_blocks={"b1"},
    )
    api.set_open_orders(
        [{"id": "b1", "side": "BUY", "asset_id": "yes", "original_size": 20, "size_matched": 0, "price": 0.30}]
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 10, price=0.32))
    worker = _worker_with_engine(db, api, eng)
    worker.place_orders([_elig_yes()])
    # residual 10, open 20: cancel raises -> open stays 20, ZERO new BUY
    assert api._placed_buys == []
    assert db.get_active_exit_cycle("0xW", CID) is not None


def test_fix2_cap_cancel_not_reconciled_no_replacement(tmp_path):
    db = _db(tmp_path, _template_fast(size_tiers=[_tier()]))
    api = FakeAPI(
        positions=[_pos("NO", 10, "no")],
        books={"yes": _book([(0.30, 300)], [(0.31, 1000)])},
        tokens={"0x" + "c" * 64: _market_api()},
        cancel_sticky={"b1"},
    )
    api.set_open_orders(
        [{"id": "b1", "side": "BUY", "asset_id": "yes", "original_size": 20, "size_matched": 0, "price": 0.30}]
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 10, price=0.32))
    worker = _worker_with_engine(db, api, eng)
    worker.place_orders([_elig_yes()])
    # cancel response returned but b1 still visible -> ZERO new BUY
    assert "b1" in api._cancelled
    assert api._placed_buys == []


def test_fix2_cap_cancel_confirmed_replaces_down_to_residual(tmp_path):
    db = _db(tmp_path, _template_fast(size_tiers=[_tier()]))
    api = FakeAPI(
        positions=[_pos("NO", 10, "no")],
        books={"yes": _book([(0.30, 300)], [(0.31, 1000)])},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    api.set_open_orders(
        [{"id": "b1", "side": "BUY", "asset_id": "yes", "original_size": 20, "size_matched": 0, "price": 0.30}]
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 10, price=0.32))
    worker = _worker_with_engine(db, api, eng)
    worker.place_orders([_elig_yes()])
    assert "b1" in api._cancelled
    placed = sum(s for _t, _p, s in api._placed_buys)
    assert placed == 10


def test_fix2_manual_buy_same_funder_unmanaged(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 20, "no")],
        books={"no": _book([(0.28, 100)], [(0.29, 100)])},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    # a manual CLOB BUY fill from the same funder: order_id NOT in bot_buy_orders
    api._trades[CID] = [_sell_fill("no", 20, 0.30, ts=100, side="BUY", order_id="manual-1")]
    eng = _engine(db, api)
    # direct fill event (unproven) must not create a cycle
    eng.on_reward_fill(_fill("no", 20, price=0.30, order_id="manual-1"))
    assert db.get_active_exit_cycle("0xW", CID) is None
    # adoption must not claim it either
    eng.run_tick(open_orders=[], positions=[_pos("NO", 20, "no")])
    assert db.get_active_exit_cycle("0xW", CID) is None
    assert api._placed_market_sells == []
    assert api._placed_post_only_sells == []


def test_fix2_mixed_bot_manual_owns_at_most_bot_qty(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={"no": _book([(0.28, 100)], [(0.29, 100)])},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    # bot proves 20 of the 50 shares; the other 30 are manual
    db.record_bot_buy_order("0xW", "o-bot", CID, "no")
    api._trades[CID] = [
        _sell_fill("no", 20, 0.30, ts=100, side="BUY", order_id="o-bot"),
        _sell_fill("no", 30, 0.30, ts=200, side="BUY", order_id="manual-2"),
    ]
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle is not None
    owned = json.loads(cycle["owned_by_asset_json"])
    assert owned.get("no") == pytest.approx(20)
    # routing is capped to the proven 20
    assert api._placed_post_only_sells == [("no", 0.29, 20)]


def test_fix2_partial_maker_fill_remainder_two_stages(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 100)], [(0.29, 100)]),
            "yes": _book([], [(0.75, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    assert api._placed_post_only_sells == [("no", 0.29, 50)]
    # stage 1: 20 filled, 30 still resting
    api._trades[CID] = [_sell_fill("no", 20, 0.29, ts=time.time() + 5)]
    api.set_positions([_pos("NO", 30, "no")])
    api.set_open_orders(
        [{"id": "s1", "side": "SELL", "asset_id": "no", "original_size": 50, "size_matched": 20, "price": 0.29}]
    )
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 30, "no")])
    cycle = db.get_active_exit_cycle("0xW", CID)
    legs = db.get_exit_legs(cycle["id"])
    maker_legs = [l for l in legs if l["kind"] == "maker_sell"]
    assert maker_legs[0]["status"] == "confirmed" and maker_legs[0]["qty"] == 20
    assert any(l["status"] == "rested" and l["qty"] == 30 for l in maker_legs)
    # stage 2: the remaining 30 fills later
    api._trades[CID] = [
        _sell_fill("no", 20, 0.29, ts=time.time() + 4),
        _sell_fill("no", 30, 0.29, ts=time.time() + 10),
    ]
    api.set_positions([])
    api.set_open_orders([])
    eng.run_tick(open_orders=[], positions=[])
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])[0]
    assert closed["error"] == ""
    assert closed["realized_recovered_collateral"] == pytest.approx(14.5)
    legs = db.get_exit_legs(closed["id"])
    maker_qty = sum(l["qty"] for l in legs if l["kind"] == "maker_sell" and l["status"] == "confirmed")
    assert maker_qty == pytest.approx(50)
    methods = {
        l["exit_method"]
        for l in legs
        if l["exit_method"] and l["status"] in ("confirmed", "done")
    }
    assert methods == {"MAKER"}


def test_fix2_partial_maker_then_market_is_mixed(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 100)], [(0.29, 100)]),
            "yes": _book([], [(0.75, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # partial 20 -> remainder 30 still resting
    api._trades[CID] = [_sell_fill("no", 20, 0.29, ts=time.time() + 5)]
    api.set_positions([_pos("NO", 30, "no")])
    api.set_open_orders(
        [{"id": "s1", "side": "SELL", "asset_id": "no", "original_size": 50, "size_matched": 20, "price": 0.29}]
    )
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 30, "no")])
    # maker window times out -> protected MARKET for the remaining 30
    cycle = db.get_active_exit_cycle("0xW", CID)
    db.update_exit_cycle(cycle["id"], opened_at=cycle["opened_at"] - 31)
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 30, "no")])
    assert api._placed_market_sells == [("no", 0.28, 30)]
    api._trades[CID] = [
        _sell_fill("no", 20, 0.29, ts=time.time() + 4),
        _sell_fill("no", 30, 0.28, ts=time.time() + 10),
    ]
    api.set_positions([])
    api.set_open_orders([])
    eng.run_tick(open_orders=[], positions=[])
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])[0]
    db.update_exit_cycle(closed["id"], closed_at=closed["closed_at"] - 301)
    eng.run_tick(open_orders=[], positions=[])
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])[0]
    assert closed["error"] == ""
    # realized exactly Maker20 @ 0.29 + Market30 @ 0.28
    assert closed["realized_recovered_collateral"] == pytest.approx(20 * 0.29 + 30 * 0.28)
    methods = {
        l["exit_method"]
        for l in db.get_exit_legs(closed["id"])
        if l["exit_method"] and l["status"] in ("confirmed", "done")
    }
    assert methods == {"MAKER", "MARKET"}


def test_fix2_fok_complement_refines_to_actual_fill(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.19, 100)], [(0.20, 100)]),
            "yes": _book([(0.19, 100)], [(0.63, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    assert api._placed_foks == [("yes", 50, 0.63)]
    op = db.get_unresolved_merges("0xW")[0]
    # Merge confirms BEFORE the CLOB BUY fill converges
    db.update_merge_operation(op["id"], "confirmed", "relayer-1", "0xh", realized_pnl=0.6)
    db.mark_merge_inventory_reconciled(op["id"])
    api.set_positions([_pos("NO", 50, "no"), _pos("YES", 50, "yes")])
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no"), _pos("YES", 50, "yes")])
    cycle = db.get_active_exit_cycle("0xW", CID)
    complement = next(
        l for l in db.get_exit_legs(cycle["id"]) if l["kind"] == "complement_buy"
    )
    # still pending (worst limit is protection, not realized cost)
    assert complement["status"] == "pending"
    # actual fill .60 appears -> complement cost refines to .60
    api._trades[CID] = [
        _sell_fill("yes", 50, 0.60, ts=time.time() + 5, side="BUY")
    ]
    api.set_positions([])
    eng.run_tick(open_orders=[], positions=[])
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])[0]
    assert closed["error"] == ""
    complement = next(
        l for l in db.get_exit_legs(closed["id"]) if l["kind"] == "complement_buy"
    )
    assert complement["status"] == "confirmed"
    assert complement["price"] == pytest.approx(0.60)
    assert complement["collateral"] == pytest.approx(-30.0)


def test_fix2_held_assets_json_persists_and_collapses(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 30, "no")],
        books={},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, []), "yes": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 30, price=0.30))
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert json.loads(cycle["held_assets_json"]) == ["no"]
    # YES joins the cycle
    eng.on_reward_fill(_proven_fill(db, api, "yes", 30, price=0.30, order_id="o2"))
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert json.loads(cycle["held_assets_json"]) == ["no", "yes"]
    # Merge consumes the pair, leaving only NO 10
    op_id = db.create_merge_operation("0xW", "0xFunder", CID, "yes", "no", 20.0)
    db.update_merge_operation(op_id, "confirmed", "relayer-1", "0xh", realized_pnl=0.5)
    db.mark_merge_inventory_reconciled(op_id)
    api.set_positions([_pos("NO", 10, "no")])
    api._trades[CID] = []
    eng.run_tick(open_orders=[], positions=[_pos("NO", 10, "no")])
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert json.loads(cycle["held_assets_json"]) == ["no"]


def test_fix2_per_asset_closing_does_not_freeze_other_side(tmp_path):
    db = _db(tmp_path, _template_fast(merge_enabled=False))
    api = FakeAPI(
        positions=[_pos("YES", 20, "yes"), _pos("NO", 20, "no")],
        books={
            "yes": _book([(0.40, 20)], [(0.42, 100)]),
            "no": _book([(0.28, 20)], [(0.30, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"yes": (0.30, []), "no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "yes", 20, price=0.30))
    eng.on_reward_fill(_proven_fill(db, api, "no", 20, price=0.30, order_id="o2"))
    cycle = db.get_active_exit_cycle("0xW", CID)
    # age the window so both sides would route DIRECT
    db.update_exit_cycle(cycle["id"], opened_at=cycle["opened_at"] - 31)
    # mark YES as pending Data API convergence (per-asset mutation guard)
    eng._exit_pending[cycle["id"]] = {"yes": time.time() + 120}
    eng.run_tick(
        open_orders=[],
        positions=[_pos("YES", 20, "yes"), _pos("NO", 20, "no")],
    )
    # NO may continue its own safe route; YES must NOT duplicate
    assert api._placed_market_sells == [("no", 0.28, 20)]
    assert all(t != "yes" for t, _p, _s in api._placed_market_sells)
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["status"] == "CLOSING"


# ---------------------------------------------------------------------------
# Fix pack 3 regressions (final funds safety)
# ---------------------------------------------------------------------------


def test_fix3_mixed_same_token_no_oversell_and_closes_on_managed_zero(tmp_path):
    db = _db(tmp_path, _template_fast(maker_exit_wait_sec=0))
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 100)], [(0.30, 100)]),
            "yes": _book([], [(0.78, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    # bot NO20 + manual NO30 (wallet position NO50)
    db.record_bot_buy_order("0xW", "o-bot", CID, "no")
    api._bot_fills[CID] = [
        _sell_fill("no", 20, 0.30, ts=100, side="BUY", order_id="o-bot")
    ]
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle is not None
    assert cycle["managed_qty"] == pytest.approx(20)
    # direct SELL maximum = managed 20, never the wallet's 50
    assert api._placed_market_sells == [("no", 0.28, 20)]
    # bot SELL 20 confirms -> managed 0 -> cycle CLOSED, manual NO30 remains
    api._trades[CID] = [_sell_fill("no", 20, 0.28, ts=time.time() + 5)]
    api.set_positions([_pos("NO", 30, "no")])
    eng.run_tick(open_orders=[], positions=[_pos("NO", 30, "no")])
    assert db.get_active_exit_cycle("0xW", CID) is None
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])[0]
    assert closed["error"] == ""
    assert closed["realized_recovered_collateral"] == pytest.approx(5.6)
    # the manual 30 is never re-adopted or re-sold
    eng.run_tick(open_orders=[], positions=[_pos("NO", 30, "no")])
    assert db.get_active_exit_cycle("0xW", CID) is None
    assert api._placed_market_sells == [("no", 0.28, 20)]


def test_fix3_manual_opposite_side_is_never_auto_merged(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 20, "no"), _pos("YES", 20, "yes")],
        books={},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    # bot NO20 only; the YES20 is manual (no provenance)
    db.record_bot_buy_order("0xW", "o-bot", CID, "no")
    api._bot_fills[CID] = [
        _sell_fill("no", 20, 0.30, ts=100, side="BUY", order_id="o-bot")
    ]
    eng = _engine(db, api, costs={"no": (0.30, []), "yes": (0.30, [])})
    eng.run_tick(
        open_orders=[],
        positions=[_pos("NO", 20, "no"), _pos("YES", 20, "yes")],
    )
    cycle = db.get_active_exit_cycle("0xW", CID)
    # managed pair = 0: manual YES must never be merged with bot NO
    assert cycle["paired_qty"] == 0
    assert cycle["selected_route"] != "MERGE_FIRST"


def test_fix3_direct_fresh_qty_capped_to_managed(tmp_path):
    db = _db(tmp_path, _template_fast(maker_exit_wait_sec=0))
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 100)], [(0.30, 100)]),
            "yes": _book([], [(0.78, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    db.record_bot_buy_order("0xW", "o-bot", CID, "no")
    api._bot_fills[CID] = [
        _sell_fill("no", 20, 0.30, ts=100, side="BUY", order_id="o-bot")
    ]
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # FAK fresh qty = min(position 50, managed 20) = 20
    assert api._placed_market_sells == [("no", 0.28, 20)]


def test_fix3_fok_qty_capped_to_managed(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.19, 100)], [(0.20, 100)]),
            "yes": _book([], [(0.63, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    db.record_bot_buy_order("0xW", "o-bot", CID, "no")
    api._bot_fills[CID] = [
        _sell_fill("no", 20, 0.30, ts=100, side="BUY", order_id="o-bot")
    ]
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # complement FOK qty = managed 20, never the wallet's 50
    assert api._placed_foks == [("yes", 20, 0.63)]


def test_fix3_fresh_managed_residual_controls_opposite_cap(tmp_path):
    db = _db(tmp_path, _template_fast(size_tiers=[_tier()]))
    api = FakeAPI(
        positions=[_pos("NO", 20, "no")],
        books={"yes": _book([(0.30, 300)], [(0.31, 1000)])},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 20, price=0.32))
    # maker SELL fills 10 before the replacement round -> fresh managed 10
    api._trades[CID] = [_sell_fill("no", 10, 0.29, ts=time.time() + 5)]
    worker = _worker_with_engine(db, api, eng)
    worker.place_orders([_elig_yes()])
    placed = sum(s for _t, _p, s in api._placed_buys)
    assert placed == 10


def test_fix3_maker_recancel_cancel_raises_no_replacement(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 100)], [(0.29, 100)]),
            "yes": _book([], [(0.75, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
        cancel_blocks={"s-old"},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # repricing required (price mismatch) but the cancel raises
    api.set_open_orders(
        [{"id": "s-old", "side": "SELL", "asset_id": "no", "original_size": 50, "size_matched": 0, "price": 0.295}]
    )
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 50, "no")])
    # no replacement SELL
    assert len(api._placed_post_only_sells) == 1


def test_fix3_maker_recancel_unconfirmed_no_replacement(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 100)], [(0.29, 100)]),
            "yes": _book([], [(0.75, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
        cancel_sticky={"s-old"},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    api.set_open_orders(
        [{"id": "s-old", "side": "SELL", "asset_id": "no", "original_size": 50, "size_matched": 0, "price": 0.295}]
    )
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 50, "no")])
    assert "s-old" in api._cancelled
    assert len(api._placed_post_only_sells) == 1


def test_fix3_maker_recancel_confirmed_with_fills(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 50, "no")],
        books={
            "no": _book([(0.28, 100)], [(0.29, 100)]),
            "yes": _book([], [(0.75, 100)]),
        },
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # 20 filled during the rest; the still-resting 30 is repriced
    api._trades[CID] = [_sell_fill("no", 20, 0.29, ts=time.time() + 5)]
    api.set_positions([_pos("NO", 30, "no")])
    api.set_open_orders(
        [{"id": "s-old", "side": "SELL", "asset_id": "no", "original_size": 50, "size_matched": 20, "price": 0.295}]
    )
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 30, "no")])
    # old SELL cancelled and CONFIRMED cancelled before the replacement
    assert "s-old" in api._cancelled
    cycle = db.get_active_exit_cycle("0xW", CID)
    legs = db.get_exit_legs(cycle["id"])
    maker_legs = [l for l in legs if l["kind"] == "maker_sell"]
    confirmed = [l for l in maker_legs if l["status"] == "confirmed"]
    cancelled = [l for l in maker_legs if l["status"] == "cancelled"]
    assert sum(l["qty"] for l in confirmed) == pytest.approx(20)
    assert sum(l["qty"] for l in cancelled) == pytest.approx(30)
    # fresh managed residual 30 -> replacement POST-ONLY for 30
    assert api._placed_post_only_sells[-1] == ("no", 0.29, 30)


def test_fix3_provenance_persist_failure_cancels_placed_buy(tmp_path):
    db = _db(tmp_path, _template_fast(size_tiers=[_tier()]))
    api = FakeAPI(
        positions=[],
        books={"yes": _book([(0.30, 300)], [(0.31, 1000)])},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api)
    worker = _worker_with_engine(db, api, eng)
    db.record_bot_buy_order = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("disk full")
    )
    worker.place_orders([_elig_yes()])
    # the placed order was cancelled and confirmed; no further placement
    assert api._cancelled == ["bot-1"]
    assert len(api._placed_buys) == 1


def test_fix3_pure_bot_flow_merge_first(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("YES", 20, "yes"), _pos("NO", 20, "no")],
        books={},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"yes": (0.30, []), "no": (0.30, [])})
    eng.on_reward_fill(_proven_fill(db, api, "yes", 20, price=0.30))
    eng.on_reward_fill(_proven_fill(db, api, "no", 20, price=0.30, order_id="o2"))
    eng.run_tick(
        open_orders=[],
        positions=[_pos("YES", 20, "yes"), _pos("NO", 20, "no")],
    )
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["paired_qty"] == 20
    assert cycle["selected_route"] == "MERGE_FIRST"
    assert api._placed_post_only_sells == []


def test_fix3_check_merges_manual_opposite_never_merged(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 20, "no"), _pos("YES", 20, "yes")],
        books={},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    db.record_bot_buy_order("0xW", "o-bot", CID, "no")
    api._bot_fills[CID] = [
        _sell_fill("no", 20, 0.30, ts=100, side="BUY", order_id="o-bot")
    ]
    monitor = OrderMonitor(api, db, "0xW")
    client = MagicMock()
    client.submit_merge.return_value.transaction_id = "relayer-1"
    client.submit_merge.return_value.transaction_hash = "0xh"
    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda cid: __import__("threading").Lock())
    client.submit_merge.assert_not_called()
    assert db.get_unresolved_merges("0xW") == []


def test_fix3_check_merges_caps_to_managed_pair(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("YES", 50, "yes"), _pos("NO", 20, "no")],
        books={},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    # bot YES20 + bot NO20; the other YES30 is manual
    db.record_bot_buy_order("0xW", "o-y", CID, "yes")
    db.record_bot_buy_order("0xW", "o-n", CID, "no")
    api._bot_fills[CID] = [
        _sell_fill("yes", 20, 0.30, ts=100, side="BUY", order_id="o-y"),
        _sell_fill("no", 20, 0.30, ts=100, side="BUY", order_id="o-n"),
    ]
    monitor = OrderMonitor(api, db, "0xW")
    client = MagicMock()
    client.submit_merge.return_value.transaction_id = "relayer-1"
    client.submit_merge.return_value.transaction_hash = "0xh"
    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda cid: __import__("threading").Lock())
    client.submit_merge.assert_called_once_with("0xFunder", CID, 20.0)


def test_fix3_check_merges_pure_bot_merges(tmp_path, merge_env):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("YES", 20, "yes"), _pos("NO", 20, "no")],
        books={},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    db.record_bot_buy_order("0xW", "o-y", CID, "yes")
    db.record_bot_buy_order("0xW", "o-n", CID, "no")
    api._bot_fills[CID] = [
        _sell_fill("yes", 20, 0.30, ts=100, side="BUY", order_id="o-y"),
        _sell_fill("no", 20, 0.30, ts=100, side="BUY", order_id="o-n"),
    ]
    monitor = OrderMonitor(api, db, "0xW")
    client = MagicMock()
    client.submit_merge.return_value.transaction_id = "relayer-1"
    client.submit_merge.return_value.transaction_hash = "0xh"
    with patch.object(monitor, "_merge_client", return_value=client):
        monitor.check_merges(lambda cid: __import__("threading").Lock())
    client.submit_merge.assert_called_once_with("0xFunder", CID, 20.0)
