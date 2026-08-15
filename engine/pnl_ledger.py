"""engine/pnl_ledger.py — 台账编排（有 IO）:拉 API -> 纯计算 -> upsert daily_pnl。

奖励/返佣来自公开 /activity;卖出盈亏/手续费来自 get_trades(FIFO,逐 asset 合并);
结算盈亏 v1 先不计(REDEEM 无样本)。每天都写(含空日),使近几天未定稿的天每轮重算刷新。
"""

import logging
import time
from datetime import datetime, timedelta

from py_clob_client_v2.clob_types import TradeParams
from engine.fills import extract_fills
from engine.merge_accounting import merge_pnl_by_day
from engine.pnl import reward_rebate_by_day, realized_pnl_by_day, our_traded_assets, beijing_day

logger = logging.getLogger(__name__)


def _date_range(from_date, to_date):
    d = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    while d <= end:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def rebuild_wallet_pnl(api, db, wallet, from_date, to_date):
    """重算 [from_date, to_date] 每天的 daily_pnl 并 upsert（幂等覆盖）。

    每天都写(含空日),使近几天(奖励次日发放、成交流滞后)每轮重算刷新。
    """
    funder = api.get_funder()
    activity = api.get_activity(types=["REWARD", "MAKER_REBATE", "REDEEM"])
    rr = reward_rebate_by_day(activity)

    trades = api.get_trades(TradeParams(maker_address=funder))
    logger.info(
        "台账重算 %s [%s..%s]:activity=%d 笔、trades=%d 笔",
        wallet,
        from_date,
        to_date,
        len(activity),
        len(trades),
    )
    realized: dict = {}
    for asset in our_traded_assets(trades, funder):
        for d, v in realized_pnl_by_day(extract_fills(trades, funder, asset)).items():
            agg = realized.setdefault(d, {"sell_profit": 0.0, "loss": 0.0, "fee": 0.0})
            agg["sell_profit"] += v["sell_profit"]
            agg["loss"] += v["loss"]
            agg["fee"] += v["fee"]

    # Merge 盈亏:只累计 CONFIRMED 事件(READY/SUBMITTED/FAILED 贡献 0),按确认的
    # 北京时间归日。事件表是唯一事实来源 —— 重算多少次都只计一次(幂等)。
    merge_by_day = merge_pnl_by_day(
        db.get_confirmed_merge_events(wallet=wallet),
        beijing_day,
    )
    for d in _date_range(from_date, to_date):
        r = rr.get(d, {})
        z = realized.get(d, {})
        db.upsert_daily_pnl(
            wallet=wallet,
            date=d,
            reward=r.get("reward", 0.0),
            rebate=r.get("rebate", 0.0),
            sell_profit=z.get("sell_profit", 0.0),
            loss=z.get("loss", 0.0),
            fee=z.get("fee", 0.0),
            merge_pnl=merge_by_day.get(d, 0.0),
        )
