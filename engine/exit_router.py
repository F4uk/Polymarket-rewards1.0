"""Pure economic router for residual inventory after merge-first handling."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExitRoute:
    action: str
    qty: float
    direct_value: float | None
    merge_value: float | None
    advantage: float | None
    reason: str


def executable_value(levels: list[dict], qty: float) -> float | None:
    """Depth-weighted proceeds/cost; None means insufficient executable depth."""
    remaining, value = float(qty or 0), 0.0
    if remaining <= 0:
        return 0.0
    for level in levels or []:
        try:
            price, size = float(level["price"]), float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        take = min(remaining, max(0.0, size))
        value += take * price
        remaining -= take
        if remaining <= 1e-9:
            return value
    return None


def executable_limit_price(levels: list[dict], qty: float) -> float | None:
    """Worst price consumed by a full-depth quote, or None if it cannot fill.

    The CLOB V2 market-buy API needs a price limit as well as the collateral
    amount.  Returning the final consumed ask keeps the FOK bounded to the
    exact depth used by ``executable_value`` instead of allowing a stale or
    far-away level to inflate the purchase limit.
    """
    remaining, limit = float(qty or 0), None
    if remaining <= 0:
        return None
    for level in levels or []:
        try:
            price, size = float(level["price"]), float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        take = min(remaining, max(0.0, size))
        if take <= 0:
            continue
        limit = price
        remaining -= take
        if remaining <= 1e-9:
            return limit
    return None


def route_residual(*, qty, paired_qty=0.0, severe_loss=False, direct_levels=None, complement_asks=None, fee_buffer=0.0, safety_margin=0.01, collateral_per_set=1.0) -> ExitRoute:
    qty = float(qty or 0)
    if qty <= 0:
        return ExitRoute("noop", 0.0, None, None, None, "no residual inventory")
    if float(paired_qty or 0) > 0:
        return ExitRoute("merge_existing", min(qty, float(paired_qty)), None, None, None, "confirmed complete sets exist")
    if not severe_loss:
        return ExitRoute("wait_dual_path", qty, None, None, None, "normal residual: maker sell plus opposite reward quote")
    direct = executable_value(direct_levels or [], qty)
    complement_cost = executable_value(complement_asks or [], qty)
    merge_value = None if complement_cost is None else qty * float(collateral_per_set) - complement_cost - float(fee_buffer or 0)
    if direct is None:
        if merge_value is not None:
            return ExitRoute(
                "buy_complement_and_merge",
                qty,
                None,
                merge_value,
                None,
                "direct exit lacks depth; protected merge route is executable",
            )
        return ExitRoute("noop", qty, None, None, None, "insufficient direct and complement depth")
    if merge_value is None:
        return ExitRoute("direct_sell", qty, direct, None, None, "insufficient complement depth")
    advantage = merge_value - direct
    if advantage > float(safety_margin or 0):
        return ExitRoute("buy_complement_and_merge", qty, direct, merge_value, advantage, "merge route exceeds safety margin")
    return ExitRoute("direct_sell", qty, direct, merge_value, advantage, "direct route wins after safety margin")
