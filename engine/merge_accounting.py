# engine/merge_accounting.py
"""Merge 的 FIFO 消耗与盈亏聚合(纯计算)。

Merge 在 CLOB 之外烧掉 YES+NO,get_trades 里看不到 —— 重建持仓成本时必须把
CONFIRMED 的 Merge 当作「库存消耗事件」按 FIFO 从买仓队列里扣掉,否则重建数量
与 Data API 实际持仓对不上(成本算不出 -> 残仓裸奔)。消费按事件幂等:同一事件
只扣一次,重算多少次结果都相同。

真正把 merge 事件并入成本重建的入口是 take_profit.position_cost_with_lots 的
merge_events 参数(见该函数);本模块只提供纯的消耗计算与按日聚合。
"""

from engine.merge import merge_realized_pnl


def consume_fifo_cost(lots: list, qty: float):
    """按 FIFO 从 lots(最早在前)消耗 qty 份,返回 (消耗成本, 剩余 lots)。

    lots 形如 position_cost_with_lots 的剩余构成:{"price", "take"(剩余持仓量),
    "ts", "trade_id"}。qty 超过持仓总量时返回 (None, 原样 lots) —— 调用方应视作
    成本不可重建(成交流滞后/外部变动),不要把缺口的成本瞎凑出来。
    """
    qty = float(qty or 0)
    if qty <= 0:
        return 0.0, list(lots)
    remaining = list(lots)
    total = sum(float(l.get("take", 0) or 0) for l in remaining)
    if total + 1e-9 < qty:
        return None, list(lots)
    cost = 0.0
    need = qty
    for lot in remaining:
        take = float(lot.get("take", 0) or 0)
        if take <= 0:
            continue
        used = min(take, need)
        cost += float(lot.get("price", 0) or 0) * used
        lot["take"] = take - used
        need -= used
        if need <= 1e-9:
            break
    kept = [l for l in remaining if float(l.get("take", 0) or 0) > 1e-9]
    return cost, kept


def merge_event_pnl_from_lots(
    amount: float,
    yes_lots: list,
    no_lots: list,
    fees_usd: float = 0.0,
):
    """按两边的剩余 FIFO lots 计算一笔 CONFIRMED Merge 的已实现盈亏。

    返回 (realized_pnl, yes_consumed_cost, no_consumed_cost, ok):
      ok=False 表示某侧成本重建不完整(缺口),realized_pnl 按 0 计并由调用方记录原因。
    """
    yes_cost, _ = consume_fifo_cost(yes_lots, amount)
    no_cost, _ = consume_fifo_cost(no_lots, amount)
    if yes_cost is None or no_cost is None:
        return 0.0, 0.0, 0.0, False
    return (
        merge_realized_pnl(amount, yes_cost, no_cost, fees_usd),
        yes_cost,
        no_cost,
        True,
    )


def merge_pnl_by_day(events: list, beijing_day) -> dict:
    """CONFIRMED Merge 事件 -> {北京日: merge_pnl 合计}(幂等:每事件只计一次)。

    events 来自 db.get_confirmed_merge_events(已按 confirmed_at 排序);beijing_day
    为 engine.pnl.beijing_day 或等价函数。只有 CONFIRMED 事件计入;READY/SUBMITTED/
    FAILED 贡献 0(调用方根本不会把它们传进来)。
    """
    out: dict = {}
    for ev in events or []:
        d = beijing_day(float(ev.get("confirmed_at", 0) or 0))
        out[d] = out.get(d, 0.0) + float(ev.get("realized_pnl", 0) or 0)
    return out


def pair_amount(yes_size: float, no_size: float) -> float:
    """可 Merge 的整对份数 = min(YES, NO)(实际余额为准,≥0)。"""
    return max(0.0, min(float(yes_size or 0), float(no_size or 0)))
