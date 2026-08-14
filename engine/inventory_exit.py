"""engine/inventory_exit.py — Merge-First Fast-Exit V2 (post-fill inventory engine).

Reward BUY fills are inventory-risk events, not directional investments.  This
module owns everything that happens after a fill, for one wallet:

- durable exit-cycle lifecycle (ACTIVE / CLOSING / BLOCKED / CLOSED) keyed by
  (wallet, condition_id), persisted in ``inventory_exit_cycles``;
- Rule #1: same held token Reward BUY is blocked while the cycle owns residual;
- opposite Reward BUY is capped to the unpaired residual;
- Rule #2: existing complete sets always Merge first (executed by
  ``engine/monitor.py`` check_merges; this engine never sells a pair);
- one-sided residual routes: immediate FOK+Merge when its recovery beats
  protected direct exit by ``merge_advantage_min_usd``; otherwise a bounded
  maker escape window (``maker_exit_wait_sec``); on timeout / emergency /
  low-balance / resolution, the best *protected* immediate route, never an
  uncontrolled market sell;
- fail-closed BLOCKED handling for unknown positions/orders/FOK/Merge outcomes.

The module deliberately stays narrow: pure economics live in
``engine/exit_router.py``, pairing/planning in ``engine/merge.py`` and funds
execution in ``api/ctf.py`` + ``api/polymarket_api.py``.
"""

from __future__ import annotations

import logging
import time

from engine.exit_router import executable_limit_price, executable_value
from engine.merge import ordinary_binary_plan
from engine.positions import condition_key, positions_by_condition
from engine.take_profit import (
    ceil_to_tick,
    effective_theta_stop,
    market_fill_price,
    plan_take_profit,
)
from models.database import ActiveExitCycleExists

logger = logging.getLogger(__name__)

# Cycle statuses (only four, per design; activity lives in legs).
CYCLE_ACTIVE = "ACTIVE"
CYCLE_CLOSING = "CLOSING"
CYCLE_BLOCKED = "BLOCKED"
CYCLE_CLOSED = "CLOSED"

# Exit method labels (UI + ledger).
EXIT_METHODS = frozenset({"MERGE", "FOK+MERGE", "MAKER", "MARKET"})

# Leg kinds.
LEG_REWARD_BUY = "reward_buy"
LEG_COMPLEMENT_BUY = "complement_buy"
LEG_MERGE = "merge"
LEG_MAKER_SELL = "maker_sell"
LEG_MARKET_SELL = "market_sell"

# A Merge runtime probe is read-only (expected wallet + nonce) but costs a
# Relayer round trip; cache it per wallet for a short TTL.  Never once per
# token/order.
MERGE_READINESS_TTL_SEC = 60.0

# Protected direct exit: FAK marketable limit SELL.  Its limit is the last
# consumed bid level (worst acceptable price derived from depth).
EPSILON = 1e-9
# How long a CLOSING cycle waits for Data API inventory convergence before a
# stale CLOSING (e.g. failed FOK with no barrier) is re-routed.
CLOSING_GRACE_SEC = 120.0


# ---------------------------------------------------------------------------
# Pure economics (deterministic, fully unit-tested)
# ---------------------------------------------------------------------------


def direct_recovery(bids: list[dict], qty: float):
    """(depth-weighted proceeds, worst acceptable price) or (None, None).

    ``best_bid * qty`` is deliberately NOT used unless the best bid alone
    contains the entire qty — executable_value walks the full bid depth.
    """
    if float(qty or 0) <= 0:
        return 0.0, None
    value = executable_value(bids or [], qty)
    if value is None:
        return None, None
    return value, executable_limit_price(bids or [], qty)


def complement_merge_recovery(
    qty: float,
    complement_asks: list[dict],
    collateral_per_set: float = 1.0,
    fee_buffer: float = 0.0,
):
    """(recovery, worst_case_limit) or (None, None) for a qty complement buy + Merge.

    Recovery = qty * collateral_per_set - qty * limit - fee_buffer, where
    ``limit`` is the actual worst-case price bound of the signed FOK request
    (the last ask level consumed by the full qty) — never an optimistic best
    ask.  Insufficient complement depth -> (None, None): do not FOK.
    """
    qty = float(qty or 0)
    if qty <= 0:
        return 0.0, 0.0
    limit = executable_limit_price(complement_asks or [], qty)
    if limit is None:
        return None, None
    recovery = qty * float(collateral_per_set) - qty * limit - float(fee_buffer or 0)
    return recovery, limit


def merge_advantage(merge_value, direct_value):
    """MergeAdvantage = ComplementMergeRecovery - DirectRecovery, or None."""
    if merge_value is None or direct_value is None:
        return None
    return merge_value - direct_value


def choose_residual_route(
    *,
    qty: float,
    direct_value,
    direct_worst,
    merge_value,
    margin: float = 0.01,
    wait_sec: float = 30,
    elapsed: float = 0.0,
    urgent: bool = False,
    merge_capable: bool = True,
) -> dict:
    """Select the V2 route for one-sided residual ``qty``.

    Rules (documented in docs/FAST_EXIT_V2_DESIGN.md):
    1. FOK+Merge may execute immediately whenever
       ComplementMergeRecovery >= DirectRecovery + margin (or DirectRecovery
       is unavailable but the protected Merge route is executable).  It does
       NOT wait for the maker window.
    2. Otherwise, when not urgent and the window has not elapsed, rest in the
       maker escape window (the caller places a maker SELL / capped opposite
       BUY).
    3. Urgent (emergency / low-balance / resolution) or timeout: choose the
       protected direct exit; when it is not executable -> BLOCKED (the caller
       must not invent execution).
    """
    qty = float(qty or 0)
    if qty <= 0:
        return {"route": "NOOP", "reason": "no residual inventory", "window_remaining": None}
    merge_wins = bool(
        merge_capable
        and merge_value is not None
        and (direct_value is None or merge_value >= float(direct_value) + float(margin))
    )
    if merge_wins:
        return {
            "route": "FOK_MERGE",
            "reason": "FOK+Merge recovers at least the advantage margin more than direct exit",
            "window_remaining": None,
        }
    window_expired = float(wait_sec) <= 0 or float(elapsed) >= float(wait_sec)
    if urgent or window_expired:
        if direct_worst is not None and direct_value is not None:
            return {
                "route": "DIRECT",
                "reason": "protected direct exit selected after immediate comparison",
                "window_remaining": None,
            }
        return {
            "route": "BLOCKED",
            "reason": "neither protected route is safely executable",
            "window_remaining": None,
        }
    return {
        "route": "MAKER_WINDOW",
        "reason": "maker escape window until timeout",
        "window_remaining": max(0.0, float(wait_sec) - float(elapsed)),
    }


def maker_escape_price(best_bid, best_ask, tick):
    """Deterministic maker-only SELL price — cost is NOT a floor.

    A resting SELL must sit strictly above the best bid so it can never become
    an accidental taker order.  The natural maker price is ``best_ask``; when
    the ask side is missing or degenerate (best_ask <= best_bid), use the
    smallest tick above the best bid.  V2 fast inventory exit may rest below
    the original cost; cost stays an accounting/emergency metric only.
    """
    if best_bid is None or best_bid <= 0 or tick is None or float(tick) <= 0:
        return None
    floor = ceil_to_tick(float(best_bid) + float(tick), float(tick))
    if best_ask is not None and float(best_ask) > float(best_bid):
        return float(best_ask)
    return floor


def effective_opposite_buy_qty(unpaired_residual, open_buy_qty, proposed_qty):
    """Cap an opposite Reward BUY to the unpaired residual minus open qty."""
    residual = max(0.0, float(unpaired_residual or 0))
    open_qty = max(0.0, float(open_buy_qty or 0))
    cap = max(0.0, residual - open_qty)
    return max(0.0, min(float(proposed_qty or 0), cap))


def exit_method_label(legs: list[dict]) -> str:
    """MERGE / FOK+MERGE / MAKER / MARKET / MIXED from a cycle's exit legs."""
    methods = {
        str(leg.get("exit_method") or "")
        for leg in legs or []
        if leg.get("exit_method") in EXIT_METHODS
    }
    if not methods:
        return ""
    return next(iter(methods)) if len(methods) == 1 else "MIXED"


def open_buy_qty(open_orders: list[dict], token_id: str) -> float:
    """Unfilled resting BUY quantity on one token."""
    qty = 0.0
    for order in open_orders or []:
        if str(order.get("side", "")).upper() != "BUY":
            continue
        if str(order.get("asset_id", "")) != str(token_id):
            continue
        try:
            qty += float(order.get("original_size", order.get("size", 0)) or 0) - float(
                order.get("size_matched", 0) or 0
            )
        except (TypeError, ValueError):
            continue
    return qty


# ---------------------------------------------------------------------------
# Inventory Exit Engine (one instance per wallet)
# ---------------------------------------------------------------------------


class InventoryExitEngine:
    """Owns post-fill inventory for one wallet; final placement authority."""

    def __init__(
        self,
        api,
        db,
        wallet_address: str,
        encryption_key: bytes = None,
        condition_lock=None,
        cost_provider=None,
        book_provider=None,
        status_add=None,
        record_action=None,
        readiness_checker=None,
    ):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address
        self.encryption_key = encryption_key
        self._condition_lock = condition_lock
        # (asset_id, size, condition_id) -> (cost|None, lots); injected by the
        # monitor to reuse its per-tick cached CLOB cost reconstruction.
        self._cost_provider = cost_provider
        # (asset_id) -> raw orderbook dict|None; injected by the monitor to
        # reuse the per-tick prefetch cache.
        self._book_provider = book_provider
        self._status_add = status_add or (lambda **fields: None)
        self._record_action = record_action or (
            lambda *a, **kw: None
        )
        self._readiness_checker = readiness_checker
        self._readiness_cache = (0.0, True, "")
        # condition_key -> until; accepted/indeterminate FOK barrier (V2-owned).
        self._pending_fok_until: dict[str, float] = {}
        self._liquidate_cooldown_until: float = 0.0
        self._token_cache: dict[str, list] = {}
        self._last_position_count: int | None = None

    # -- helpers ------------------------------------------------------------

    def _try_condition_lock(self, condition_id):
        if not self._condition_lock:
            return None
        lock = self._condition_lock(condition_id)
        return lock if lock.acquire(blocking=False) else False

    @staticmethod
    def _release_condition_lock(lock):
        if lock:
            lock.release()

    def _template(self) -> dict:
        try:
            return self.db.get_template_for(self.wallet_address)
        except Exception as exc:
            logger.warning("template unavailable for %s: %s", self.wallet_address, exc)
            return {}

    def _token_outcome(self, asset_id: str, condition_id: str) -> str:
        """YES/NO label for an asset in a condition ('' when unknown)."""
        tokens = self._market_tokens(condition_id)
        for token_id, outcome in tokens:
            if str(token_id) == str(asset_id):
                return outcome
        return ""

    def _opposite_token(self, condition_id: str, asset_id: str):
        """Complement YES/NO token, or None for non-ordinary/ambiguous markets."""
        ours = self._token_outcome(asset_id, condition_id)
        if ours not in ("YES", "NO"):
            return None
        for token_id, outcome in self._market_tokens(condition_id):
            if outcome in ("YES", "NO") and outcome != ours:
                return token_id
        return None

    def _market_tokens(self, condition_id: str) -> list:
        if condition_id in self._token_cache:
            return self._token_cache[condition_id]
        parsed: list = []
        try:
            market = self.api.get_market(condition_id)
            for token in market.get("tokens") or market.get("outcomes") or []:
                if not isinstance(token, dict):
                    continue
                token_id = token.get("token_id") or token.get("tokenId") or token.get("id")
                outcome = str(token.get("outcome") or token.get("name") or "").upper()
                if token_id:
                    parsed.append((str(token_id), outcome))
        except Exception as exc:
            logger.warning("get_market(%s) failed for exit engine: %s", condition_id, exc)
        self._token_cache[condition_id] = parsed
        return parsed

    def _book(self, asset_id: str):
        if self._book_provider is not None:
            try:
                return self._book_provider(asset_id)
            except Exception:
                return None
        try:
            return self.api.get_orderbook(asset_id)
        except Exception as exc:
            logger.warning("orderbook %s failed (exit engine): %s", asset_id, exc)
            return None

    def _cost(self, asset_id: str, size: float, condition_id: str):
        if self._cost_provider is not None:
            try:
                return self._cost_provider(asset_id, size, condition_id)
            except Exception as exc:
                logger.warning("cost provider failed %s: %s", asset_id, exc)
                return None, []
        return None, []

    def _log_route(self, cid: str, side: str, qty: float, age: float, direct, merge,
                   advantage, remaining, route: str, reason: str) -> None:
        logger.info(
            "[exit-v2] wallet=%s cond=%s side=%s qty=%g age=%.1fs direct=%s merge=%s "
            "advantage=%s maker_left=%s route=%s reason=%s",
            str(self.wallet_address)[:8],
            str(cid)[:10],
            side,
            qty,
            age,
            "n/a" if direct is None else f"{direct:.4f}",
            "n/a" if merge is None else f"{merge:.4f}",
            "n/a" if advantage is None else f"{advantage:.4f}",
            "n/a" if remaining is None else f"{remaining:.0f}s",
            route,
            reason,
        )

    # -- cycle lifecycle ------------------------------------------------------

    def on_reward_fill(self, ev: dict) -> None:
        """Create or join the (wallet, condition) exit cycle for a Reward BUY fill."""
        cid = ev.get("market", "")
        asset_id = ev.get("asset_id", "")
        size = float(ev.get("size", 0) or 0)
        price = float(ev.get("price", 0) or 0)
        if not cid or size <= 0:
            return
        try:
            outcome = self._token_outcome(asset_id, cid)
            existing = self.db.get_active_exit_cycle(self.wallet_address, cid)
            if existing:
                cycle_id = existing["id"]
                self.db.update_exit_cycle(
                    cycle_id,
                    status=CYCLE_ACTIVE,
                    managed_qty=float(existing.get("managed_qty", 0) or 0) + size,
                    initial_qty=float(existing.get("initial_qty", 0) or 0) + size,
                    held_side=outcome or existing.get("held_side", ""),
                    held_asset_id=asset_id or existing.get("held_asset_id", ""),
                    error="",
                )
            else:
                cycle_id = self.db.create_exit_cycle(
                    self.wallet_address,
                    cid,
                    trigger="reward_fill",
                    held_side=outcome,
                    held_asset_id=asset_id,
                    qty=size,
                )
            self.db.add_exit_leg(
                cycle_id,
                self.wallet_address,
                cid,
                LEG_REWARD_BUY,
                asset_id=asset_id,
                side=outcome,
                qty=size,
                price=price,
                collateral=-(size * price),
                order_id=str(ev.get("order_id") or ""),
                note=f"reward fill trade={ev.get('trade_id', '')}",
            )
            self._status_add(
                market=cid,
                side="买入",
                price=f"{price:.4f}",
                size=str(size),
                matched=str(size),
                stage="库存退出",
                action="周期 ACTIVE",
                detail=f"Reward BUY 成交 {size:g} 份 -> 库存退出周期（join={bool(existing)}）",
            )
        except ActiveExitCycleExists:
            logger.info("exit cycle already active for %s/%s", self.wallet_address[:8], cid)
        except Exception as exc:
            logger.error("on_reward_fill cycle ledger failed %s: %s", cid, exc)

    def _reconcile_cycle_inventory(self, cycle: dict, group: list[dict]) -> dict:
        """Update cycle managed inventory from confirmed positions.

        Returns a per-side summary: {yes_qty, no_qty, paired, residual_side,
        residual_asset, residual_qty, total}.
        """
        yes_qty = no_qty = 0.0
        for pos in group or []:
            try:
                size = float(pos.get("size", 0) or 0)
            except (TypeError, ValueError):
                continue
            if size <= 0:
                continue
            outcome = str(pos.get("outcome", "")).strip().upper()
            if outcome == "YES":
                yes_qty += size
            elif outcome == "NO":
                no_qty += size
        minimum = float(self._template().get("merge_min_shares", 1) or 0)
        plan = ordinary_binary_plan(cycle["condition_id"], group or [], minimum)
        paired = float(plan.qty) if plan else 0.0
        residual_side, residual_asset, residual_qty = "", "", 0.0
        if yes_qty > no_qty + EPSILON:
            residual_side = "YES"
            residual_qty = yes_qty - paired
            residual_asset = next(
                (p.get("asset", "") for p in group if str(p.get("outcome", "")).strip().upper() == "YES"),
                "",
            )
        elif no_qty > yes_qty + EPSILON:
            residual_side = "NO"
            residual_qty = no_qty - paired
            residual_asset = next(
                (p.get("asset", "") for p in group if str(p.get("outcome", "")).strip().upper() == "NO"),
                "",
            )
        return {
            "yes_qty": yes_qty,
            "no_qty": no_qty,
            "paired": paired,
            "residual_side": residual_side,
            "residual_asset": residual_asset,
            "residual_qty": residual_qty,
            "total": yes_qty + no_qty,
        }

    def run_tick(
        self,
        open_orders=None,
        low_balance: bool = False,
        positions: list | None = None,
    ) -> None:
        """One monitor-tick pass for all managed cycles of this wallet."""
        tmpl = self._template()
        if not tmpl.get("fast_exit_enabled", True):
            return  # legacy mode owns post-fill inventory
        self._token_cache = {}
        if positions is None:
            try:
                positions = self.api.get_user_positions(self.api.get_funder())
            except Exception as exc:
                logger.warning("[exit-v2] positions unavailable, cycles BLOCKED: %s", exc)
                for cycle in self.db.get_non_closed_exit_cycles(self.wallet_address):
                    if cycle["status"] != CYCLE_CLOSING:
                        self.db.update_exit_cycle(
                            cycle["id"], status=CYCLE_BLOCKED,
                            error="positions unavailable",
                        )
                return
        if open_orders is None:
            try:
                open_orders = self.api.get_open_orders()
            except Exception as exc:
                logger.warning("[exit-v2] open orders unavailable: %s", exc)
                for cycle in self.db.get_non_closed_exit_cycles(self.wallet_address):
                    if cycle["status"] != CYCLE_CLOSING:
                        self.db.update_exit_cycle(
                            cycle["id"], status=CYCLE_BLOCKED,
                            error="open orders unavailable; no funds mutation",
                        )
                return
        by_condition = positions_by_condition(positions)
        if not low_balance:
            low_balance = self._low_balance_triggered(tmpl)
        if low_balance:
            # Mirror V1: after a low-balance recovery, cool down so on-chain
            # collateral settlement can catch up before re-scanning.
            self._liquidate_cooldown_until = time.time() + 60
        resolving = self._resolving_conditions(by_condition)
        self._adopt_legacy_inventory(by_condition, open_orders)
        cycles = self.db.get_non_closed_exit_cycles(self.wallet_address)
        self._last_position_count = sum(
            len(group) for group in by_condition.values()
        )
        for cycle in cycles:
            try:
                self._route_cycle(
                    cycle,
                    by_condition.get(cycle["condition_id"], []),
                    open_orders or [],
                    resolving,
                    low_balance,
                    tmpl,
                )
            except Exception as exc:
                logger.exception("[exit-v2] cycle %s failed: %s", cycle["id"], exc)

    def _resolving_conditions(self, by_condition) -> set:
        cids = [cid for cid in by_condition if cid]
        if not cids:
            return set()
        try:
            status_map = self.api.gamma_resolution_status(cids)
        except Exception as exc:
            logger.warning("[exit-v2] gamma status unavailable (fail-open): %s", exc)
            return set()
        from engine.resolution import in_resolution

        return {c for c in cids if in_resolution(status_map.get(c))}

    def _low_balance_triggered(self, tmpl: dict) -> bool:
        threshold = float(tmpl.get("low_balance_threshold_usd", 0) or 0)
        if threshold <= 0:
            return False
        if time.time() < self._liquidate_cooldown_until:
            return False
        try:
            balance = float(self.api.get_balance() or 0)
        except Exception as exc:
            logger.warning("[exit-v2] get_balance failed: %s", exc)
            return False
        return balance < threshold

    def _adopt_legacy_inventory(self, by_condition, open_orders) -> None:
        """Create cycles for pre-V2 leftover inventory (migration) only.

        Any existing cycle row (including CLOSED) suppresses adoption: a
        CLOSED cycle is never reopened from stale Data API rows.  Genuine new
        managed inventory always arrives through on_reward_fill, which creates
        a brand-new cycle.
        """
        if not by_condition:
            return
        try:
            existing = {
                condition_key(cid)
                for cid in self.db.get_exit_cycle_condition_keys(self.wallet_address)
            }
        except Exception:
            return
        for cid, group in by_condition.items():
            if condition_key(cid) in existing:
                continue
            summary = self._reconcile_cycle_inventory(
                {"condition_id": cid}, group
            )
            if summary["total"] <= 0:
                continue
            try:
                self.db.create_exit_cycle(
                    self.wallet_address,
                    cid,
                    trigger="adopt_legacy",
                    held_side=summary["residual_side"],
                    held_asset_id=summary["residual_asset"],
                    qty=summary["residual_qty"] or summary["total"],
                    paired_qty=summary["paired"],
                )
                self._record_action(
                    cid, "exit_cycle_opened", "-", -1, summary["total"],
                    "V2 接管存量持仓，开启库存退出周期",
                    f"adopt_legacy residual={summary['residual_qty']:g} paired={summary['paired']:g}",
                )
            except ActiveExitCycleExists:
                continue

    def _route_cycle(
        self, cycle, group, open_orders, resolving: set, low_balance: bool, tmpl: dict
    ) -> None:
        cid = cycle["condition_id"]
        now = time.time()
        key = condition_key(cid)
        self._record_confirmed_merge_legs(cycle)
        # Funds-moving Merge machinery owns this condition: never mutate
        # residual while a planned/submitted Merge, an accepted/indeterminate
        # FOK, or a confirmed-but-unreconciled Merge is in flight.
        try:
            unresolved = {
                condition_key(op["condition_id"])
                for op in self.db.get_unresolved_merges(self.wallet_address)
                if op.get("condition_id")
            }
            inventory_barriers = {
                condition_key(op["condition_id"])
                for op in self.db.get_merge_inventory_barriers(self.wallet_address)
                if op.get("condition_id")
            }
        except Exception as exc:
            logger.warning("[exit-v2] merge barrier query failed: %s", exc)
            unresolved, inventory_barriers = set(), set()
        pending_fok = {
            k for k, until in self._pending_fok_until.items() if until > now
        }
        if key in unresolved or key in inventory_barriers or key in pending_fok:
            self.db.update_exit_cycle(cycle["id"], status=CYCLE_CLOSING, error="")
            self._status_add(
                market=cid, side="卖出", price="-", size="-", matched="-",
                stage="库存退出", action="CLOSING·等待合并确认",
                detail="未决 Merge/FOK 阻止并行库存变更",
            )
            return

        summary = self._reconcile_cycle_inventory(cycle, group)
        self.db.update_exit_cycle(
            cycle["id"],
            managed_qty=summary["total"],
            paired_qty=summary["paired"],
            held_side=summary["residual_side"] or cycle.get("held_side", ""),
            held_asset_id=summary["residual_asset"] or cycle.get("held_asset_id", ""),
        )

        if summary["total"] <= EPSILON:
            self._close_cycle(cycle, reason="inventory returned to zero")
            return
        if cycle.get("status") == CYCLE_CLOSING:
            # A direct exit / FOK was dispatched this cycle; wait for Data API
            # convergence before any further mutation.  A stale CLOSING with no
            # barrier and no inventory movement is re-routed after the grace.
            stale = now - float(cycle.get("updated_at", now) or now) > CLOSING_GRACE_SEC
            if not stale:
                self._status_add(
                    market=cid, side=summary["residual_side"], price="-",
                    size=str(summary["residual_qty"]), matched="-",
                    stage="库存退出", action="CLOSING·等待库存收敛",
                    detail="上一笔退出变更待 Data API 确认，禁止重复退出",
                )
                return
            self.db.update_exit_cycle(cycle["id"], status=CYCLE_ACTIVE, error="")
        if summary["paired"] > 0:
            # Rule #2: existing complete sets always Merge first.  check_merges
            # runs earlier in the tick; never sell either leg of a pair.
            self.db.update_exit_cycle(
                cycle["id"], status=CYCLE_ACTIVE, selected_route="MERGE_FIRST", error=""
            )
            self._status_add(
                market=cid, side="-", price="-", size=str(summary["paired"]),
                matched="-", stage="库存退出", action="等待合并(Merge-first)",
                detail=f"完整集合 {summary['paired']:g} 份先 Merge，残差 {summary['residual_qty']:g}",
            )
            return

        residual_qty = summary["residual_qty"]
        if residual_qty <= EPSILON:
            self.db.update_exit_cycle(cycle["id"], status=CYCLE_ACTIVE, selected_route="NOOP")
            return
        side = summary["residual_side"]
        asset = summary["residual_asset"]
        if not asset:
            asset = cycle.get("held_asset_id", "")
        cost, lots = self._cost(asset, residual_qty, cid)
        if cost is None or cost <= 0:
            self.db.update_exit_cycle(
                cycle["id"], status=CYCLE_BLOCKED, error="cost basis unavailable"
            )
            self._status_add(
                market=cid, side=side, price="-", size=str(residual_qty), matched="-",
                stage="库存退出", action="⚠️BLOCKED·成本未知",
                detail="get_trades 无法重建成本，不做库存变更，下 tick 重试",
            )
            return

        book = self._book(asset) or {}
        bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        tick = float(book.get("tick_size", "0.01") or 0.01)
        tick_str = str(book.get("tick_size", "0.01") or "0.01")

        direct, direct_worst = direct_recovery(bids, residual_qty)
        complement = self._opposite_token(cid, asset)
        merge_capable = self._merge_capable(tmpl)
        merge_value = merge_limit = None
        comp_asks = []
        if merge_capable and complement:
            comp_book = self._book(complement)
            if comp_book:
                comp_asks = sorted(
                    comp_book.get("asks", []), key=lambda x: float(x["price"])
                )
                merge_value, merge_limit = complement_merge_recovery(
                    residual_qty, comp_asks
                )
        advantage = merge_advantage(merge_value, direct)

        stop_mode = tmpl.get("stop_loss_mode", "percent")
        stop_percent = tmpl.get("stop_loss_percent", 20)
        stop_cents = tmpl.get("theta_stop_cents", 5)
        theta_stop = effective_theta_stop(cost, stop_mode, stop_percent, stop_cents)
        emergency = (
            theta_stop is not None
            and best_bid is not None
            and (cost - best_bid) >= theta_stop
        )
        urgent = bool(low_balance or key in resolving or emergency)
        if (
            tmpl.get("take_profit_mode", "maker") == "market"
            and cost is not None
            and best_bid is not None
            and cost < best_bid
        ):
            urgent = True
        wait_sec = float(tmpl.get("maker_exit_wait_sec", 30) or 0)
        opened_at = float(cycle.get("opened_at", now) or now)
        elapsed = now - opened_at
        decision = choose_residual_route(
            qty=residual_qty,
            direct_value=direct,
            direct_worst=direct_worst,
            merge_value=merge_value,
            margin=float(tmpl.get("merge_advantage_min_usd", 0.01) or 0),
            wait_sec=wait_sec,
            elapsed=elapsed,
            urgent=urgent,
            merge_capable=merge_capable,
        )
        self._log_route(
            cid, side, residual_qty, now - opened_at, direct, merge_value,
            advantage, decision.get("window_remaining"), decision["route"],
            decision["reason"],
        )
        self.db.update_exit_cycle(
            cycle["id"],
            direct_recovery=direct,
            merge_recovery=merge_value,
            advantage=advantage,
            selected_route=decision["route"],
            error="",
            status=(
                CYCLE_CLOSING
                if decision["route"] in ("FOK_MERGE", "DIRECT")
                else CYCLE_ACTIVE
            ),
        )

        if decision["route"] == "FOK_MERGE":
            self._execute_fok_merge(
                cid, asset, complement, residual_qty, merge_limit, tick_str,
                condition_locked=False,
            )
            return
        if decision["route"] == "DIRECT":
            self._execute_protected_direct(
                cid, asset, residual_qty, direct_worst, tick, tick_str,
                best_bid, cost, lots,
            )
            return
        if decision["route"] == "BLOCKED":
            self.db.update_exit_cycle(
                cycle["id"], status=CYCLE_BLOCKED,
                error="no protected route safely executable",
            )
            self._status_add(
                market=cid, side=side, price="-", size=str(residual_qty), matched="-",
                stage="库存退出", action="⚠️BLOCKED·无可执行保护路线",
                detail=(
                    f"直接回收={'不可用' if direct is None else '%.4f' % direct} "
                    f"Merge回收={'不可用' if merge_value is None else '%.4f' % merge_value}"
                ),
            )
            return

        # MAKER_WINDOW: bounded maker escape.
        window_until = now + wait_sec
        self.db.update_exit_cycle(
            cycle["id"],
            status=CYCLE_ACTIVE,
            selected_route="MAKER_WINDOW",
            maker_window_until=window_until,
            direct_recovery=direct,
            merge_recovery=merge_value,
            advantage=advantage,
        )
        self._rest_maker_sell(
            cid, asset, residual_qty, best_bid, best_ask, tick, tick_str, cost,
            open_orders, window_until, side,
        )

    def _record_confirmed_merge_legs(self, cycle: dict) -> None:
        """Record durable MERGE legs from confirmed merge operations.

        A confirmed Merge for the cycle's condition is inventory consumption:
        it returns ``qty`` collateral per set.  Attributed as FOK+MERGE when
        this cycle previously bought a complement leg, else MERGE.
        """
        try:
            ops = self.db.get_confirmed_merges(self.wallet_address)
            legs = self.db.get_exit_legs(cycle["id"])
        except Exception as exc:
            logger.warning("[exit-v2] confirmed merge legs unavailable: %s", exc)
            return
        recorded = {
            str(leg.get("order_id"))
            for leg in legs
            if leg.get("kind") == LEG_MERGE
        }
        has_complement = any(
            leg.get("kind") == LEG_COMPLEMENT_BUY for leg in legs
        )
        opened = float(cycle.get("opened_at", 0) or 0)
        for op in ops:
            cid = op.get("condition_id", "")
            if condition_key(cid) != condition_key(cycle["condition_id"]):
                continue
            if float(op.get("confirmed_at", 0) or 0) < opened:
                continue  # merged before this cycle existed
            op_id = str(op.get("id"))
            if op_id in recorded:
                continue
            qty = float(op.get("requested_qty", 0) or 0)
            if qty <= 0:
                continue
            method = "FOK+MERGE" if has_complement else "MERGE"
            try:
                self.db.add_exit_leg(
                    cycle["id"], self.wallet_address, cid, LEG_MERGE,
                    asset_id="", side="", qty=qty, price=1.0,
                    collateral=qty, exit_method=method, order_id=op_id,
                    relayer_id=str(op.get("relayer_id", "") or ""),
                    status="done",
                    note=f"confirmed merge op {op_id} pnl={op.get('realized_pnl')}",
                    created_at=float(op.get("confirmed_at", 0) or 0) or None,
                )
            except Exception as exc:
                logger.warning("[exit-v2] merge leg record failed %s: %s", op_id, exc)

    # -- route execution -------------------------------------------------------

    def _merge_capable(self, tmpl: dict) -> bool:
        """True when the automatic-Merge funds path is eligible for this wallet."""
        if not tmpl.get("merge_enabled", True):
            return False
        if int(getattr(self.api, "signature_type", -1)) != 3:
            return False
        if not getattr(self.api, "trading_enabled", True):
            return False
        try:
            from api.relayer_config import load_relayer_runtime_config

            config = load_relayer_runtime_config(self.db, self.encryption_key)
            return config is not None and config.configured
        except Exception:
            return False

    def _execute_fok_merge(
        self, cid, held_asset, complement, qty, limit_price, tick_str,
        *, condition_locked=False,
    ):
        """Protected complement FOK + Merge with the full race-protection sequence.

        Order (spec §18): lock -> CLOSING -> cancel opposite Reward BUY ->
        cancel SELL reservations -> confirm cancellations by refetch -> refetch
        positions -> exact missing qty -> refetch complement book -> recompute
        economics -> persist plan -> submit exact protected FOK.  A stale
        quantity is never used; without confirmed cancellations no FOK happens.
        """
        mutation_lock = None if condition_locked else self._try_condition_lock(cid)
        if mutation_lock is False:
            return
        try:
            try:
                open_orders = self.api.get_open_orders()
            except Exception as exc:
                logger.warning("[exit-v2] FOK pre-check open orders failed: %s", exc)
                self.db.update_exit_cycle(
                    self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                    status=CYCLE_BLOCKED, error="open orders unavailable before FOK",
                )
                return
            cancel_ids = [
                o["id"]
                for o in open_orders
                if o.get("id")
                and (
                    (
                        str(o.get("side", "")).upper() == "BUY"
                        and str(o.get("asset_id", "")) == str(complement)
                    )
                    or (
                        str(o.get("side", "")).upper() == "SELL"
                        and str(o.get("asset_id", "")) in (str(held_asset), str(complement))
                    )
                )
            ]
            if cancel_ids:
                try:
                    self.api.cancel_orders(cancel_ids)
                except Exception as exc:
                    logger.warning("[exit-v2] FOK pre-cancel failed: %s", exc)
                    self.db.update_exit_cycle(
                        self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                        status=CYCLE_BLOCKED, error="cancellation failed before FOK",
                    )
                    return
                self._record_action(
                    cid, "fok_pre_cancel", "-", -1, qty,
                    "FOK 前置：撤对侧 Reward BUY 与冲突 SELL 预约",
                    f"cancel {len(cancel_ids)} orders",
                )
            try:
                refreshed_orders = self.api.get_open_orders()
            except Exception as exc:
                logger.warning("[exit-v2] FOK cancel confirmation failed: %s", exc)
                self.db.update_exit_cycle(
                    self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                    status=CYCLE_BLOCKED, error="cancellation confirmation unavailable",
                )
                return
            conflicts = [
                o["id"]
                for o in refreshed_orders
                if o.get("id")
                and (
                    (
                        str(o.get("side", "")).upper() == "BUY"
                        and str(o.get("asset_id", "")) == str(complement)
                    )
                    or (
                        str(o.get("side", "")).upper() == "SELL"
                        and str(o.get("asset_id", "")) in (str(held_asset), str(complement))
                    )
                )
            ]
            if conflicts:
                self.db.update_exit_cycle(
                    self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                    status=CYCLE_BLOCKED,
                    error="cancellation not reconciled; FOK withheld",
                )
                logger.warning(
                    "[exit-v2] FOK withheld %s: cancellations not reconciled (%s)",
                    cid, conflicts,
                )
                return
            try:
                positions = self.api.get_user_positions(self.api.get_funder())
            except Exception as exc:
                logger.warning("[exit-v2] FOK positions refetch failed: %s", exc)
                self.db.update_exit_cycle(
                    self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                    status=CYCLE_BLOCKED, error="positions unavailable before FOK",
                )
                return
            fresh = self._reconcile_cycle_inventory(
                {"condition_id": cid}, positions_by_condition(positions).get(cid, [])
            )
            residual = fresh["residual_qty"]
            if residual <= EPSILON:
                cycle_row = self.db.get_active_exit_cycle(self.wallet_address, cid)
                if cycle_row:
                    self.db.update_exit_cycle(
                        cycle_row["id"], status=CYCLE_ACTIVE, selected_route="",
                    )
                return  # inventory changed; next tick reconciles
            comp_book = self._book(complement) or {}
            comp_asks = sorted(
                comp_book.get("asks", []), key=lambda x: float(x["price"])
            )
            merge_value, fresh_limit = complement_merge_recovery(residual, comp_asks)
            if fresh_limit is None or merge_value is None:
                cycle_row = self.db.get_active_exit_cycle(self.wallet_address, cid)
                if cycle_row:
                    self.db.update_exit_cycle(
                        cycle_row["id"], status=CYCLE_ACTIVE, selected_route="",
                    )
                self._status_add(
                    market=cid, side="-", price="-", size=str(residual), matched="-",
                    stage="库存退出", action="FOK 取消（补边深度不足）",
                    detail="补边盘口深度不足，不提交 FOK",
                )
                return
            held_book = self._book(held_asset) or {}
            held_bids = sorted(
                held_book.get("bids", []), key=lambda x: float(x["price"]), reverse=True
            )
            fresh_direct, _ = direct_recovery(held_bids, residual)
            margin = float(self._template().get("merge_advantage_min_usd", 0.01) or 0)
            if fresh_direct is not None and merge_value < fresh_direct + margin:
                # Economics changed after cancellation: abort without mutation.
                cycle_row = self.db.get_active_exit_cycle(self.wallet_address, cid)
                if cycle_row:
                    self.db.update_exit_cycle(
                        cycle_row["id"], status=CYCLE_ACTIVE, selected_route="",
                    )
                self._status_add(
                    market=cid, side="-", price="-", size=str(residual), matched="-",
                    stage="库存退出", action="FOK 取消（经济学变化）",
                    detail="撤单后重算不再满足优势下限，回退路由",
                )
                return
            try:
                op_id = self.db.create_merge_operation(
                    self.wallet_address, self.api.get_funder(), cid,
                    held_asset, complement, residual,
                )
            except Exception as exc:
                from models.database import ActiveMergeOperationExists

                if isinstance(exc, ActiveMergeOperationExists):
                    self.db.update_exit_cycle(
                        self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                        status=CYCLE_CLOSING,
                        error="Merge operation already active for condition",
                    )
                return
                logger.error("[exit-v2] pre-FOK merge plan persist failed %s: %s", cid, exc)
                return
            try:
                self.api.place_complement_fok_buy(
                    complement, residual, fresh_limit,
                    tick_size=comp_book.get("tick_size", tick_str), neg_risk=None,
                )
            except Exception as exc:
                from api.polymarket_api import OrderRejected

                if isinstance(exc, OrderRejected):
                    self.db.update_merge_operation(op_id, "failed", error=f"FOK unfilled/rejected: {exc}")
                    self._pending_fok_until.pop(condition_key(cid), None)
                    cycle_row = self.db.get_active_exit_cycle(self.wallet_address, cid)
                    if cycle_row:
                        self.db.update_exit_cycle(
                            cycle_row["id"], status=CYCLE_ACTIVE,
                            error="", selected_route="",
                        )
                    self._record_action(
                        cid, "exit_fok_failed", "BUY", -1, residual,
                        f"FOK 补边被拒：{exc}", "no FOK inventory assumed; route fallback",
                    )
                    return
                # Transport uncertainty: never assume failure and market-sell.
                self.db.update_merge_operation(
                    op_id, "planned",
                    error=f"FOK submission indeterminate; awaiting inventory: {exc}",
                )
                self._pending_fok_until[condition_key(cid)] = time.time() + 30
                self._record_action(
                    cid, "exit_fok_indeterminate", "BUY", -1, residual,
                    "FOK 传输结果未知；等待库存确认，禁止并行卖出",
                    str(exc),
                )
                self.db.update_exit_cycle(
                    self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                    status=CYCLE_CLOSING, error="FOK outcome unknown",
                )
                return
            self.db.update_merge_operation(
                op_id, "planned", error="FOK accepted; awaiting confirmed Data API inventory"
            )
            self._pending_fok_until[condition_key(cid)] = time.time() + 30
            self.db.add_exit_leg(
                self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                self.wallet_address, cid, LEG_COMPLEMENT_BUY,
                asset_id=complement, side="", qty=residual, price=fresh_limit,
                collateral=-(residual * fresh_limit),
                exit_method="FOK+MERGE", status="pending",
                note=f"exact protected FOK limit={fresh_limit:.4f}",
            )
            self._record_action(
                cid, "exit_fok_submitted", "BUY", -1, residual,
                "FOK 补边已提交；等待库存确认后 Merge",
                f"limit={fresh_limit:.4f} all-or-nothing protected buy",
            )
            self._status_add(
                market=cid, side="买入", price=f"{fresh_limit:.4f}", size=str(residual),
                matched="-", stage="库存退出", action="FOK+MERGE 已提交",
                detail="等待对侧库存确认",
            )
        finally:
            self._release_condition_lock(mutation_lock)

    def _execute_protected_direct(
        self, cid, asset, qty, worst_price, tick, tick_str, best_bid, cost, lots
    ):
        """Protected direct market exit: FAK marketable limit SELL bounded by
        the depth-derived worst acceptable price.  Never uncontrolled slippage."""
        mutation_lock = self._try_condition_lock(cid)
        if mutation_lock is False:
            return
        try:
            if worst_price is None:
                self.db.update_exit_cycle(
                    self.db.get_active_exit_cycle(self.wallet_address, cid)["id"],
                    status=CYCLE_BLOCKED, error="no depth-derived worst price",
                )
                return
            try:
                open_orders = self.api.get_open_orders()
                sell_ids = [
                    o["id"] for o in open_orders
                    if o.get("side") == "SELL" and o.get("asset_id") == asset and o.get("id")
                ]
                if sell_ids:
                    self.api.cancel_orders(sell_ids)
                    self._record_action(
                        cid, "exit_cancel_sell", "-", -1, qty,
                        "受保护直卖前撤掉在挂卖单", f"cancel {len(sell_ids)} SELL",
                    )
            except Exception as exc:
                logger.warning("[exit-v2] pre-direct sell cancel failed: %s", exc)
            resp = self.api.place_marketable_limit_sell(
                asset, worst_price, qty, tick_size=tick_str, neg_risk=None
            )
            fill = market_fill_price(resp, best_bid, worst_price)
            cycle = self.db.get_active_exit_cycle(self.wallet_address, cid)
            if cycle:
                self.db.add_exit_leg(
                    cycle["id"], self.wallet_address, cid, LEG_MARKET_SELL,
                    asset_id=asset, side="SELL", qty=qty, price=fill,
                    collateral=qty * fill, exit_method="MARKET",
                    order_id=str((resp or {}).get("orderID", "")) if isinstance(resp, dict) else "",
                    note=f"protected FAK worst={worst_price:.4f}",
                )
                self.db.update_exit_cycle(
                    cycle["id"], status=CYCLE_CLOSING,
                    selected_route="MARKET", error="",
                )
            self._record_action(
                cid, "exit_market_protected", "卖出", fill, qty,
                "受保护直卖：深度最差价上界 FAK",
                f"worst={worst_price:.4f} fill≈{fill:.4f}",
            )
            self._status_add(
                market=cid, side="卖出", price=f"{fill:.4f}", size=str(qty), matched="-",
                stage="库存退出", action="MARKET·受保护直卖",
                detail=f"worst={worst_price:.4f} fill≈{fill:.4f}",
            )
        except Exception as exc:
            logger.error("[exit-v2] protected direct exit failed %s: %s", asset, exc)
            cycle_row = self.db.get_active_exit_cycle(self.wallet_address, cid)
            if cycle_row:
                self.db.update_exit_cycle(
                    cycle_row["id"], status=CYCLE_ACTIVE,
                    error=f"protected direct exit failed: {exc}",
                )
            self._status_add(
                market=cid, side="卖出", price="-", size=str(qty), matched="-",
                stage="库存退出", action="⚠️直卖失败·保留",
                detail=f"受保护直卖被拒：{exc}",
            )
        finally:
            self._release_condition_lock(mutation_lock)

    def _rest_maker_sell(
        self, cid, asset, qty, best_bid, best_ask, tick, tick_str, cost,
        open_orders, window_until, side,
    ):
        """Rest one maker SELL for the escape window (cost is not a floor)."""
        price = maker_escape_price(best_bid, best_ask, tick)
        if price is None:
            self._status_add(
                market=cid, side=side, price="-", size=str(qty), matched="-",
                stage="库存退出", action="MAKER 窗口（无盘口不挂）",
                detail="无买盘/无有效 tick，本 tick 不挂卖单",
            )
            return
        sells = [
            o for o in open_orders
            if o.get("side") == "SELL" and o.get("asset_id") == asset
        ]
        plan = plan_take_profit(qty, price, tick, sells)
        if plan["action"] in ("noop", "keep"):
            self._status_add(
                market=cid, side=side, price=f"{price:.4f}", size=str(qty), matched="-",
                stage="库存退出", action="MAKER 窗口·挂单保持",
                detail=f"成本{cost:.4f} 挂卖{price:.4f}（可低于成本，不穿价）",
            )
            return
        mutation_lock = self._try_condition_lock(cid)
        if mutation_lock is False:
            return
        try:
            if plan["cancel_ids"]:
                try:
                    self.api.cancel_orders(plan["cancel_ids"])
                    self._record_action(
                        cid, "exit_recancel", "-", -1, qty,
                        "撤与持仓不符的旧卖单，改挂 MAKER 逃生价", f"cancel {len(plan['cancel_ids'])} SELL",
                    )
                except Exception as exc:
                    logger.warning("[exit-v2] maker recancel failed: %s", exc)
                    return
            self.api.place_limit_sell(asset, price, qty, tick_size=tick_str, neg_risk=None)
            cycle = self.db.get_active_exit_cycle(self.wallet_address, cid)
            if cycle:
                self.db.add_exit_leg(
                    cycle["id"], self.wallet_address, cid, LEG_MAKER_SELL,
                    asset_id=asset, side="SELL", qty=qty, price=price,
                    collateral=qty * price, exit_method="MAKER", status="rested",
                    note=f"maker escape below-cost allowed, cost={cost:.4f}",
                )
                self.db.update_exit_cycle(
                    cycle["id"], selected_route="MAKER_WINDOW",
                    maker_window_until=window_until,
                )
            self._record_action(
                cid, "exit_maker_rest", "卖出", price, qty,
                f"MAKER 逃生窗口挂卖单（成本 {cost:.4f}，可低于成本，绝不穿价）",
                f"maker_escape_price={price:.4f} bid={best_bid} ask={best_ask}",
            )
            self._status_add(
                market=cid, side=side, price=f"{price:.4f}", size=str(qty), matched="-",
                stage="库存退出", action="MAKER 窗口·挂卖单",
                detail=f"成本{cost:.4f} 挂卖{price:.4f}",
            )
        except Exception as exc:
            logger.error("[exit-v2] maker rest failed %s: %s", asset, exc)
            self._status_add(
                market=cid, side=side, price=f"{price:.4f}", size=str(qty), matched="-",
                stage="库存退出", action="⚠️MAKER 挂单失败",
                detail=f"{exc}",
            )
        finally:
            self._release_condition_lock(mutation_lock)

    def _close_cycle(self, cycle: dict, reason: str) -> None:
        """Close a cycle and finalize its economic summary from legs."""
        try:
            legs = self.db.get_exit_legs(cycle["id"])
            collateral = sum(float(l.get("collateral", 0) or 0) for l in legs)
            buy_collateral = sum(
                float(l.get("collateral", 0) or 0)
                for l in legs
                if l.get("kind") == LEG_REWARD_BUY
            )
            recovered = collateral - buy_collateral
            duration = time.time() - float(cycle.get("opened_at", time.time()) or time.time())
            method = exit_method_label(legs)
            self.db.close_exit_cycle(
                cycle["id"],
                closed_reason=reason,
                realized_recovered_collateral=recovered,
                inventory_pnl=collateral,
                holding_duration_sec=max(0.0, duration),
            )
            self._record_action(
                cycle["condition_id"], "exit_cycle_closed", "-", -1,
                float(cycle.get("initial_qty", 0) or 0),
                f"库存退出周期关闭（{method or '—'}）：回收 ${recovered:.4f} 库存损益 ${collateral:.4f}",
                f"reason={reason} duration={duration:.0f}s legs={len(legs)}",
            )
            logger.info(
                "[exit-v2] cycle %s closed wallet=%s cond=%s method=%s recovery=%.4f pnl=%.4f",
                cycle["id"], str(self.wallet_address)[:8], str(cycle["condition_id"])[:10],
                method or "-", recovered, collateral,
            )
            # Cancel any lingering stale SELL so inventory and orders converge.
            try:
                open_orders = self.api.get_open_orders()
                stale = [
                    o["id"] for o in open_orders
                    if o.get("side") == "SELL" and o.get("asset_id") in (
                        cycle.get("held_asset_id", ""),
                    ) and o.get("id")
                ]
                if stale:
                    self.api.cancel_orders(stale)
                    self._record_action(
                        cycle["condition_id"], "exit_cancel_sell", "-", -1, 0,
                        "周期关闭：撤掉残留过期卖单",
                        f"cancel {len(stale)} stale SELL",
                    )
            except Exception as exc:
                logger.warning("[exit-v2] stale SELL cleanup failed: %s", exc)
        except Exception as exc:
            logger.error("[exit-v2] close cycle %s failed: %s", cycle["id"], exc)

    # -- placement authority ---------------------------------------------------

    def authorize_placement(
        self, condition_id: str, token_id: str, outcome: str, proposed_qty: float,
        open_orders: list[dict],
    ) -> dict:
        """Final authority over Reward BUY placement for one token.

        Returns {kind, allowed, effective_qty, reason}:
        - kind="same_token_block": held token — cancel resting buys, place none.
        - kind="opposite_cap": opposite token at/near cap — keep resting buys,
          add none (or clamp to effective_qty when partially allowed).
        - kind="ok": normal placement.
        Scanner/laddering proposes; this engine clamps or rejects.
        """
        try:
            cycle = self.db.get_active_exit_cycle(self.wallet_address, condition_id)
        except Exception as exc:
            logger.warning("[exit-v2] placement authority cycle lookup failed: %s", exc)
            return {
                "kind": "same_token_block", "allowed": False, "effective_qty": 0.0,
                "reason": "退出周期查询失败，fail-closed",
            }
        if not isinstance(cycle, dict):
            return {
                "kind": "ok", "allowed": True,
                "effective_qty": float(proposed_qty or 0), "reason": "",
            }
        managed = float(cycle.get("managed_qty", 0) or 0)
        held_asset = str(cycle.get("held_asset_id", "") or "")
        held_side = str(cycle.get("held_side", "") or "").upper()
        if managed <= EPSILON:
            return {
                "kind": "ok", "allowed": True,
                "effective_qty": float(proposed_qty or 0), "reason": "",
            }
        # Rule #1: same held token is blocked while the cycle owns residual.
        if str(token_id) == held_asset or (held_side and held_side == str(outcome or "").upper()):
            return {
                "kind": "same_token_block", "allowed": False,
                "effective_qty": 0.0,
                "reason": "库存退出中：同侧持仓未退出，禁止重挂 Reward BUY（防填-亏-再买 churn）",
            }
        # Opposite Reward BUY allowed but capped to the unpaired residual.
        open_qty = open_buy_qty(open_orders, token_id)
        effective = effective_opposite_buy_qty(managed, open_qty, proposed_qty)
        if effective <= EPSILON:
            return {
                "kind": "opposite_cap", "allowed": False,
                "effective_qty": 0.0,
                "reason": f"对侧 Reward BUY 已达残差上限（残差 {managed:g} - 在挂 {open_qty:g}）",
            }
        return {
            "kind": "ok", "allowed": True,
            "effective_qty": effective,
            "reason": f"对侧 Reward BUY 限制为残差 {managed:g} - 在挂 {open_qty:g} = {effective:g}",
        }

    # -- Merge runtime readiness (new-BUY gate) ---------------------------------

    def merge_runtime_ready(self) -> tuple[bool, str]:
        """(ready, reason) for automatic-Merge runtime capability, short-cached.

        The gate is only meaningful for Type3 wallets whose template enables
        fast_exit + merge + require_merge_ready_for_new_buys; Type1/Type2 and
        gate-off templates return (True, '').
        """
        tmpl = self._template()
        if not (
            tmpl.get("fast_exit_enabled", True)
            and tmpl.get("merge_enabled", True)
            and tmpl.get("require_merge_ready_for_new_buys", True)
        ):
            return True, ""
        if int(getattr(self.api, "signature_type", -1)) != 3:
            return True, ""
        now = time.time()
        cached_at, cached_ready, cached_reason = self._readiness_cache
        if now - cached_at < MERGE_READINESS_TTL_SEC:
            return cached_ready, cached_reason
        if not getattr(self.api, "trading_enabled", True):
            result = (False, "TRADING_DISABLED")
        elif self._readiness_checker is not None:
            try:
                result = tuple(self._readiness_checker())
            except Exception as exc:
                logger.warning("[exit-v2] readiness checker failed: %s", exc)
                result = (False, "RELAYER_UNREACHABLE")
        else:
            result = self._probe_merge_runtime()
        self._readiness_cache = (now, bool(result[0]), str(result[1]))
        return bool(result[0]), str(result[1])

    def _probe_merge_runtime(self) -> tuple[bool, str]:
        """Read-only runtime probe (expected Deposit Wallet + authenticated nonce)."""
        try:
            from api.relayer_config import run_wallet_preflight

            wallet = {
                "address": self.wallet_address,
                "signature_type": getattr(self.api, "signature_type", 3),
                "funder": self.api.get_funder() or "",
                "trading_block_reason": getattr(self.api, "trading_block_reason", ""),
            }
            result = run_wallet_preflight(
                wallet,
                getattr(self.api, "private_key", None),
                db=self.db,
                encryption_key=self.encryption_key,
                trading_enabled=getattr(self.api, "trading_enabled", True),
                timeout=10.0,
            )
            return result.ready, result.reason
        except Exception as exc:
            logger.warning("[exit-v2] merge runtime probe failed: %s", exc)
            return False, "RELAYER_UNREACHABLE"
