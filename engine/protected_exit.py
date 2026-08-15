# engine/protected_exit.py
"""受保护 FAK / Maker 卖单 / FOK+Merge 路由的纯报价与规划(无 IO)。

全部函数都是纯计算:输入盘口行 [{price, size}] 与份额,输出计划。盘口行须已按
价格排序(bids 降序 / asks 升序)。价格天然在 tick 上(来自 CLOB 盘口),此处不再
做 tick 对齐;需要钳位的调用方自己对齐。
"""

from engine.merge import merge_recovery_usd


def _num(v, default=0.0) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return default


def bid_walk(bids: list, size: float) -> dict:
    """从买一往下走深度,求 size 份的最坏成交价与逐档可成交量。

    返回 {"levels": [(price, take), ...], "executable": 可成交量, "worst_price": 最低可成交价}。
    size 小于等于 0 -> executable=0、worst_price=None。盘口为空/全零同样 None。
    """
    size = float(size or 0)
    if size <= 0:
        return {"levels": [], "executable": 0.0, "worst_price": None}
    levels = []
    executable = 0.0
    worst = None
    remaining = size
    for b in bids or []:
        price = _num(b.get("price"))
        qty = _num(b.get("size"))
        if price <= 0 or qty <= 0:
            continue
        take = min(qty, remaining)
        levels.append((price, take))
        executable += take
        worst = price
        remaining -= take
        if remaining <= 1e-9:
            break
    if not levels:
        return {"levels": [], "executable": 0.0, "worst_price": None}
    return {"levels": levels, "executable": executable, "worst_price": worst}


def protected_fak_plan(bids: list, size: float) -> dict | None:
    """Protected FAK 卖单计划:最坏价 = 走完 size 深度所需的最低买价。

    FAK 限价单保证绝不成交在保护价以下(盘口瞬时假跌也卖不穿)。若当前深度不足以
    全量成交,可成交量 < size —— 部分成交可接受,剩余交给下个 tick。盘口无效时
    返回 None(调用方不挂单)。
    """
    walk = bid_walk(bids, size)
    if walk["worst_price"] is None:
        return None
    recovery = sum(p * t for p, t in walk["levels"])
    return {
        "worst_price": walk["worst_price"],
        "executable": walk["executable"],
        "expected_recovery": recovery,
        "levels": walk["levels"],
        "full": walk["executable"] >= size - 1e-9,
    }


def maker_sell_price(asks: list, best_bid: float) -> float | None:
    """Maker 卖单挂价:当前有效卖一;卖盘无效时回退买一(绝不自己砸穿)。

    快速退出的 Maker 卖单允许小额做市亏损(为回收资金),不设「不低于成本」硬底;
    只要求价格有效(>0 且 <1)。book 无效返回 None,调用方本轮不挂、下轮重算。
    """
    for a in asks or []:
        price = _num(a.get("price"))
        if 0 < price < 1:
            return price
    if best_bid is not None and best_bid > 0:
        return best_bid
    return None


def quote_direct_exit(bids: list, size: float) -> dict | None:
    """DIRECT 路线报价:Protected FAK 的期望回收。"""
    return protected_fak_plan(bids, size)


def quote_merge_route(
    complement_asks: list,
    size: float,
    direct_recovery: float,
    merge_min_advantage_usd: float,
    merge_min_shares: float,
) -> dict:
    """MERGE 路线报价:FOK 补对 + Merge 的可行性、FOK 最坏价、期望回收。

    FOK 最坏价是**动态推导**的(不设静态 merge_max_buy_price):
        补对成交后 Merge 回收 = size × $1;
        选 Merge 当且仅当 回收 − 补对成本 > direct_recovery + merge_min_advantage_usd
        即 补对价 p < 1 − (direct_recovery + advantage) / size。
    要求补对侧 ask 深度在「≤ 该最坏价」的档位上足以成交全部 size(FOK 全成或全不成,
    深度不足直接判不可行,绝不提交会吃不满的 FOK)。返回值:
      feasible: bool; fok_worst_price: float|None; expected_cost: float;
      expected_recovery: float(=size); reason: str。
    """
    size = float(size or 0)
    adv = float(merge_min_advantage_usd or 0)
    min_shares = float(merge_min_shares or 0)
    if size <= 0:
        return {"feasible": False, "fok_worst_price": None, "expected_cost": 0.0,
                "expected_recovery": 0.0, "reason": "size<=0"}
    if size < min_shares:
        return {"feasible": False, "fok_worst_price": None, "expected_cost": 0.0,
                "expected_recovery": 0.0, "reason": f"低于 merge_min_shares({min_shares:g})"}
    direct = float(direct_recovery or 0)
    bound = 1.0 - (direct + adv) / size
    if bound <= 0:
        return {"feasible": False, "fok_worst_price": None, "expected_cost": 0.0,
                "expected_recovery": 0.0,
                "reason": f"直接退出已更优或等价(bound={bound:.4f}<=0)"}
    # 找补对 ask 深度:价格 <= bound 的档位累加,须覆盖全部 size。
    levels = []
    cost = 0.0
    remaining = size
    worst = None
    for a in complement_asks or []:
        price = _num(a.get("price"))
        qty = _num(a.get("size"))
        if price <= 0 or qty <= 0 or price > bound:
            continue
        take = min(qty, remaining)
        levels.append((price, take))
        cost += price * take
        worst = price
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return {"feasible": False, "fok_worst_price": None, "expected_cost": 0.0,
                "expected_recovery": 0.0,
                "reason": f"补对侧深度不足(≤{bound:.4f} 只有 {size-remaining:g}/{size:g})"}
    return {
        "feasible": True,
        "fok_worst_price": worst,
        "expected_cost": cost,
        "expected_recovery": merge_recovery_usd(size),
        "levels": levels,
        "reason": f"FOK 最坏价 {worst:.4f}(上限 {bound:.4f}),补对成本 {cost:.4f}",
    }
