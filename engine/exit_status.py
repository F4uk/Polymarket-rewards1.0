# engine/exit_status.py
"""快速退出/Merge 的轻量状态快照与文案(纯计算)。

退出路由每 tick 把已经算好的结果写进这里(线程安全,进程内),「挂单与持仓」页的
「退出状态」列和仪表盘「退出处理中」只读这份快照 —— 不在 UI 刷新时重复请求盘口/
成交,也不暴露任何敏感值。快照键:asset_id(单侧)与 condition_id(配对/合并)。
"""

import threading

_LOCK = threading.Lock()
# wallet -> {"assets": {asset_id: str}, "conditions": {condition_id: str},
#            "active": int, "ts": float}
_SNAPSHOTS: dict = {}


def set_snapshot(wallet: str, assets: dict, conditions: dict, active: int, ts: float) -> None:
    with _LOCK:
        _SNAPSHOTS[wallet] = {
            "assets": dict(assets or {}),
            "conditions": dict(conditions or {}),
            "active": int(active or 0),
            "ts": float(ts or 0),
        }


def get_status(asset_id: str, condition_id: str) -> str:
    """某持仓的退出状态文案;查不到 -> '—'。"""
    with _LOCK:
        for s in _SNAPSHOTS.values():
            if asset_id and asset_id in s["assets"]:
                return s["assets"][asset_id]
            if condition_id and condition_id in s["conditions"]:
                return s["conditions"][condition_id]
    return "—"


def count_active() -> int:
    """全钱包「退出处理中」操作数(Maker 退出 + READY/SUBMITTED Merge)。"""
    with _LOCK:
        return sum(s["active"] for s in _SNAPSHOTS.values())


def clear_snapshot() -> None:
    with _LOCK:
        _SNAPSHOTS.clear()


# --- 文案 ---

def maker_wait_text(remaining_sec: float) -> str:
    """Maker 等待倒计时文案;<=0 表示已到截止。"""
    if remaining_sec is None:
        return "MAKER"
    r = int(remaining_sec)
    return "MAKER · 剩余%ds" % max(0, r) if r > 0 else "MAKER · 到期"


def merge_status_text(route: str, status: str, complement_status: str = "") -> str:
    """Merge 操作的状态文案。route: PAIR / FOK_MERGE;status: READY/SUBMITTED/..."""
    r = str(route or "")
    s = str(status or "")
    if r == "PAIR":
        if s == "READY":
            return "PAIR MERGE · READY"
        if s == "SUBMITTED":
            return "MERGE · SUBMITTED"
        if s == "CONFIRMED":
            return "MERGE · CONFIRMED"
        if s == "FAILED":
            return "MERGE FAILED"
        return f"MERGE · {s}"
    # FOK_MERGE
    if s == "READY":
        if complement_status in ("delayed", "pending"):
            return "FOK 补对·延迟确认"
        if complement_status in ("matched", "filled"):
            return "FOK 已成交·待MERGE"
        return "FOK 补对"
    if s == "SUBMITTED":
        return "MERGE · SUBMITTED"
    if s == "CONFIRMED":
        return "MERGE · CONFIRMED"
    if s == "FAILED":
        return "MERGE FAILED"
    return f"MERGE · {s}"
