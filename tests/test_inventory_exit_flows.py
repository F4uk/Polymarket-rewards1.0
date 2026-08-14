"""V2 Inventory Exit Engine flows (spec §41-46) — real SQLite + fake API only.

No live CLOB / Relayer / chain: every funds path is a fake.  Two wallets in
the same condition are exercised to prove wallet isolation.
"""

import time

import pytest

from engine.inventory_exit import InventoryExitEngine
from engine.manager import WalletWorker
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
        self._placed_market_sells = []
        self._placed_foks = []
        self._cancel_blocks = set(cancel_blocks or [])
        self._resolution = {}

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
        self._open_orders = [o for o in self._open_orders if o.get("id") not in ids]

    def place_limit_buy(self, token_id, price, size, tick_size="0.01", neg_risk=None):
        self._placed_buys.append((token_id, price, size))

    def place_limit_sell(self, token_id, price, size, tick_size="0.01", neg_risk=None):
        self._placed_sells.append((token_id, price, size))

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


def _fill(asset, size, price=0.30, market=CID):
    return {
        "trade_id": "t1",
        "order_id": "o1",
        "asset_id": asset,
        "price": price,
        "size": size,
        "market": market,
        "ts": 1000.0,
    }


def _engine(db, api, *, costs=None, readiness=None):
    return InventoryExitEngine(
        api,
        db,
        "0xW",
        encryption_key=b"k" * 32,
        condition_lock=lambda cid: __import__("threading").Lock(),
        cost_provider=lambda asset, size, cid: (costs or {}).get(asset, (0.30, [])),
        book_provider=lambda asset: api.get_orderbook(asset),
        readiness_checker=readiness,
    )


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
    eng.on_reward_fill(_fill("no", 50, price=0.29))
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle is not None
    assert cycle["status"] == "ACTIVE"

    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # maker escape rests below cost (0.29 < 0.30) at best ask, never crossing.
    assert api._placed_sells == [("no", 0.29, 50)]
    assert api._placed_market_sells == []
    assert api._placed_foks == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["selected_route"] == "MAKER_WINDOW"

    # maker sell filled -> inventory zero -> CLOSED as MAKER
    api.set_positions([])
    eng.run_tick(open_orders=[], positions=[])
    assert db.get_active_exit_cycle("0xW", CID) is None
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])
    assert len(closed) == 1
    assert db.get_exit_legs(closed[0]["id"])[0]["kind"] == "reward_buy"
    assert db.get_exit_legs(closed[0]["id"])[1]["exit_method"] == "MAKER"
    assert closed[0]["realized_recovered_collateral"] == pytest.approx(14.5)


# ---------------------------------------------------------------------------
# §41 B: fill -> opposite reward BUY fills -> pair -> MERGE -> CLOSED
# ---------------------------------------------------------------------------


def test_flow_b_opposite_fill_pair_merge(tmp_path):
    db = _db(tmp_path, _template_fast())
    api = FakeAPI(
        positions=[_pos("NO", 20, "no")],
        books={},
        tokens={"0x" + "c" * 64: _market_api()},
    )
    eng = _engine(db, api, costs={"no": (0.32, [])})
    eng.on_reward_fill(_fill("no", 20, price=0.32))
    # opposite Reward BUY filled -> complete set
    api.set_positions([_pos("NO", 20, "no"), _pos("YES", 20, "yes")])
    eng.run_tick(open_orders=[], positions=[_pos("NO", 20, "no"), _pos("YES", 20, "yes")])
    # Rule #2: complete set is Merge-first; no unilateral sell.
    assert api._placed_sells == []
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
    assert closed[0]["inventory_pnl"] == pytest.approx(20 - 20 * 0.32, abs=1e-6)


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
    eng.on_reward_fill(_fill("no", 50, price=0.30))
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
    eng.on_reward_fill(_fill("no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    # protected FAK at the depth worst acceptable price, not uncontrolled FAK
    assert api._placed_market_sells == [("no", 0.28, 50)]
    assert api._placed_sells == []
    assert api._placed_foks == []
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["status"] == "CLOSING"
    assert cycle["selected_route"] == "MARKET"

    api.set_positions([])
    eng.run_tick(open_orders=[], positions=[])
    closed = db.get_exit_cycles(wallet="0xW", statuses=["CLOSED"])[0]
    assert closed["realized_recovered_collateral"] == pytest.approx(14.0)
    assert db.get_exit_legs(closed["id"])[1]["exit_method"] == "MARKET"


# ---------------------------------------------------------------------------
# §41 E: YES30/NO20 -> Merge20 -> residual YES10 -> MIXED
# ---------------------------------------------------------------------------


def test_flow_e_mixed_merge_then_maker(tmp_path):
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
    # pre-V2 leftover adopted
    eng.run_tick(open_orders=[], positions=[_pos("YES", 30, "yes"), _pos("NO", 20, "no")])
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle is not None and cycle["trigger"] == "adopt_legacy"
    assert cycle["paired_qty"] == 20
    assert api._placed_sells == []  # pair untouched

    # Merge 20 confirmed; residual YES 10 remains
    op_id = db.create_merge_operation("0xW", "0xFunder", CID, "yes", "no", 20.0)
    db.update_merge_operation(op_id, "confirmed", "relayer-1", "0xh", realized_pnl=0.6)
    db.mark_merge_inventory_reconciled(op_id)
    api.set_positions([_pos("YES", 10, "yes")])
    eng.run_tick(open_orders=[], positions=[_pos("YES", 10, "yes")])
    # residual YES 10: direct 4.0 vs merge 10-6.8=3.2 -> maker window
    assert api._placed_sells == [("yes", 0.42, 10)]

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
    eng.on_reward_fill(_fill("no", 50, price=0.30))
    eng.run_tick(open_orders=[], positions=[_pos("NO", 50, "no")])
    assert api._placed_sells == [("no", 0.29, 50)]

    # 30 filled, 20 remain: existing sell replaced with the new residual size
    api.set_positions([_pos("NO", 20, "no")])
    api.set_open_orders(
        [{"id": "s-old", "side": "SELL", "asset_id": "no", "original_size": 50, "size_matched": 30}]
    )
    eng.run_tick(open_orders=api.get_open_orders(), positions=[_pos("NO", 20, "no")])
    assert api._cancelled == ["s-old"]
    assert api._placed_sells[-1] == ("no", 0.29, 20)
    cycle = db.get_active_exit_cycle("0xW", CID)
    assert cycle["managed_qty"] == 20


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
    eng.on_reward_fill(_fill("no", 50, price=0.30))
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
    eng.on_reward_fill(_fill("no", 20, price=0.32))
    # same held token: blocked while the cycle owns residual
    authority = eng.authorize_placement(CID, "no", "NO", 50, [])
    assert authority["kind"] == "same_token_block" and authority["allowed"] is False
    # opposite token: scanner YES 50 capped to residual 20
    authority = eng.authorize_placement(CID, "yes", "YES", 50, [])
    assert authority["allowed"] is True
    assert authority["effective_qty"] == 20
    # open opposite BUY 10 -> cap 10
    open_orders = [
        {"id": "b1", "side": "BUY", "asset_id": "yes", "original_size": 10, "size_matched": 0}
    ]
    authority = eng.authorize_placement(CID, "yes", "YES", 50, open_orders)
    assert authority["effective_qty"] == 10
    # open opposite BUY already at cap -> nothing more
    open_orders = [
        {"id": "b1", "side": "BUY", "asset_id": "yes", "original_size": 20, "size_matched": 0}
    ]
    authority = eng.authorize_placement(CID, "yes", "YES", 50, open_orders)
    assert authority["kind"] == "opposite_cap" and authority["allowed"] is False


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
    eng.on_reward_fill(_fill("no", 20, price=0.30))
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
    eng.on_reward_fill(_fill("no", 50, price=0.30))
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
    eng1.on_reward_fill(_fill("no", 20))
    # A second process joining the same (wallet, condition) must JOIN the one
    # active cycle, never create a duplicate.
    eng2.on_reward_fill(_fill("no", 20))
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
    eng.on_reward_fill(_fill("no", 20, market=CID))
    eng.on_reward_fill(_fill("no-b", 20, market=CID_B))
    # A goes FOK (CLOSING) while B takes the maker window; B must not be blocked.
    eng.run_tick(
        open_orders=[],
        positions=[_pos("NO", 20, "no", CID), _pos("NO", 20, "no-b", CID_B)],
    )
    assert api._placed_foks == [("yes", 20, 0.63)]
    assert api._placed_sells == [("no-b", 0.29, 20)]


def test_race_two_wallets_same_condition_isolated(tmp_path):
    db = _db(tmp_path, _template_fast())
    api_a = FakeAPI(positions=[], books={}, tokens={"0x" + "c" * 64: _market_api()})
    eng_a = _engine(db, api_a)
    eng_a.on_reward_fill(_fill("no", 20, price=0.32))
    # wallet B is not blocked by wallet A's cycle
    api_b = FakeAPI(positions=[], books={}, tokens={"0x" + "c" * 64: _market_api()})
    eng_b = InventoryExitEngine(
        api_b, db, "0xWB", encryption_key=b"k" * 32,
        condition_lock=lambda cid: __import__("threading").Lock(),
        cost_provider=lambda asset, size, cid: (0.30, []),
        book_provider=lambda asset: api_b.get_orderbook(asset),
    )
    eng_b.on_reward_fill(_fill("no", 20, price=0.32))
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


def test_low_balance_merge_first_then_immediate_route(tmp_path):
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
    eng.on_reward_fill(_fill("no", 50, price=0.30))
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
    eng2.on_reward_fill(_fill("no", 50, price=0.30))
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
        def merge_runtime_ready(self):
            return False, "RELAYER_AUTH_FAILED"

        def authorize_placement(self, *a, **kw):
            return {"kind": "ok", "allowed": True, "effective_qty": None, "reason": ""}

    worker.monitor = type("M", (), {"exit_engine": lambda self: FakeEngine()})()
    elig = [{
        "market_id": CID, "token_id": "yes", "outcome": "Yes",
        "market_name": "M", "rewards_min_size": 100, "rewards_max_spread": 6,
        "tick_size_str": "0.01", "neg_risk": False,
    }]
    worker.place_orders(elig)
    assert api._placed_buys == []

    class FakeEngineReady:
        def merge_runtime_ready(self):
            return True, ""

        def authorize_placement(self, *a, **kw):
            return {"kind": "ok", "allowed": True, "effective_qty": None, "reason": ""}

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
    eng.on_reward_fill(_fill("no", 50, price=0.30))
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
