"""tests/test_pnl_ledger.py — rebuild_wallet_pnl 编排(拉 API -> 纯计算 -> upsert)。"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from engine.pnl_ledger import rebuild_wallet_pnl


def _utc_ts(y, mo, d, h=0):
    return int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp())


def test_rebuild_upserts_reward_and_realized_every_day():
    api = MagicMock()
    api.get_funder.return_value = "0xFUND"
    # REWARD 发放 UTC 2026-01-02 00:00 -> earning 日 2026-01-01
    api.get_activity.return_value = [
        {"type": "REWARD", "usdcSize": 7.0, "timestamp": _utc_ts(2026, 1, 2)},
    ]
    # 一个 asset:买10@0.1(北京01-02)、卖10@0.3(北京01-03,赚2)
    api.get_trades.return_value = [
        {
            "id": "t1",
            "match_time": str(_utc_ts(2026, 1, 2)),
            "trader_side": "MAKER",
            "asset_id": "A",
            "maker_orders": [
                {
                    "maker_address": "0xFUND",
                    "asset_id": "A",
                    "side": "BUY",
                    "matched_amount": "10",
                    "price": "0.1",
                    "fee_rate_bps": "",
                }
            ],
        },
        {
            "id": "t2",
            "match_time": str(_utc_ts(2026, 1, 3)),
            "trader_side": "MAKER",
            "asset_id": "A",
            "maker_orders": [
                {
                    "maker_address": "0xFUND",
                    "asset_id": "A",
                    "side": "SELL",
                    "matched_amount": "10",
                    "price": "0.3",
                    "fee_rate_bps": "",
                }
            ],
        },
    ]
    db = MagicMock()
    rebuild_wallet_pnl(api, db, "0xW", "2026-01-01", "2026-01-03")

    calls = {c.kwargs["date"]: c.kwargs for c in db.upsert_daily_pnl.call_args_list}
    # 每天都 upsert(含空日),共 3 天
    assert set(calls) == {"2026-01-01", "2026-01-02", "2026-01-03"}
    assert calls["2026-01-01"]["reward"] == 7.0
    assert calls["2026-01-01"]["sell_profit"] == 0.0
    assert abs(calls["2026-01-03"]["sell_profit"] - 2.0) < 1e-9
    assert calls["2026-01-02"]["reward"] == 0.0  # 空日也写


def test_rebuild_activity_uses_reward_rebate_redeem_types():
    api = MagicMock()
    api.get_funder.return_value = "0xFUND"
    api.get_activity.return_value = []
    api.get_trades.return_value = []
    db = MagicMock()
    rebuild_wallet_pnl(api, db, "2026-01-01", "2026-01-01", "2026-01-01")
    # 拉 activity 时限定类型(奖励/返佣/赎回)
    types = api.get_activity.call_args.kwargs.get("types")
    assert set(types) == {"REWARD", "MAKER_REBATE", "REDEEM"}


def test_confirmed_merge_is_realized_once_and_consumes_both_asset_queues():
    api = MagicMock()
    api.get_funder.return_value = "0xFUND"
    api.get_activity.return_value = []
    api.get_trades.return_value = []
    db = MagicMock()
    db.get_confirmed_merges.return_value = [
        {
            "confirmed_at": _utc_ts(2026, 1, 2),
            "realized_pnl": 1.2,
            "consumed_lots": [
                {"asset_id": "yes", "lots": [{"take": 12}]},
                {"asset_id": "no", "lots": [{"take": 12}]},
            ],
        }
    ]
    rebuild_wallet_pnl(api, db, "0xW", "2026-01-02", "2026-01-02")
    call = db.upsert_daily_pnl.call_args.kwargs
    assert call["sell_profit"] == 1.2
