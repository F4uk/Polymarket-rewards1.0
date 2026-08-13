"""Small, deterministic merge-first planning helpers.

This module deliberately knows nothing about HTTP, wallets, or SQLite.  It
only recognizes an ordinary same-condition binary pair; NegRisk conversion is
out of scope.
"""

from dataclasses import dataclass


EPSILON = 1e-9


@dataclass(frozen=True)
class MergePlan:
    condition_id: str
    yes_asset_id: str
    no_asset_id: str
    qty: float


def pair_quantity(yes_available: float, no_available: float, minimum: float = 0.0) -> float:
    """Return mergeable paired quantity without integer rounding."""
    qty = min(max(0.0, float(yes_available or 0)), max(0.0, float(no_available or 0)))
    return qty if qty + EPSILON >= float(minimum or 0) else 0.0


def ordinary_binary_plan(condition_id: str, positions: list[dict], minimum: float = 0.0) -> MergePlan | None:
    """Plan one merge from confirmed YES + NO position records.

    Exactly two complementary outcome labels are required.  Any ambiguous,
    NegRisk, malformed, or cross-condition representation safely yields no
    plan rather than guessing token semantics.
    """
    if not condition_id:
        return None
    outcomes = {}
    for p in positions or []:
        if str(p.get("conditionId", "")) != str(condition_id):
            continue
        if bool(p.get("negRisk") or p.get("neg_risk")):
            return None
        outcome = str(p.get("outcome", "")).strip().upper()
        asset = str(p.get("asset", ""))
        try:
            size = float(p.get("size", 0) or 0)
        except (TypeError, ValueError):
            continue
        if outcome in ("YES", "NO") and asset and size > 0:
            outcomes[outcome] = (asset, size)
    if set(outcomes) != {"YES", "NO"}:
        return None
    yes_asset, yes_qty = outcomes["YES"]
    no_asset, no_qty = outcomes["NO"]
    qty = pair_quantity(yes_qty, no_qty, minimum)
    return MergePlan(condition_id, yes_asset, no_asset, qty) if qty > 0 else None


def reserved_sell_quantity(open_orders: list[dict], asset_id: str) -> float:
    """Shares reserved by live SELLs, used only after the caller cancels them."""
    qty = 0.0
    for order in open_orders or []:
        if str(order.get("side", "")).upper() != "SELL" or str(order.get("asset_id", "")) != str(asset_id):
            continue
        try:
            original = float(order.get("original_size", order.get("size", 0)) or 0)
            matched = float(order.get("size_matched", 0) or 0)
            qty += max(0.0, original - matched)
        except (TypeError, ValueError):
            continue
    return qty
