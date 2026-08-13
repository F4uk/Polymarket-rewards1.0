"""tests/test_database.py"""

import os
import pytest
from models.database import Database


def test_settings_page_engine_strategy_split_roundtrip(tmp_path):
    """/api/settings 的拆分机制:引擎键存 settings、策略键存默认模板,GET 合并取回。"""
    from config import ENGINE_DEFAULTS, TEMPLATE_DEFAULTS

    database = Database(str(tmp_path / "split.db"))
    database.init()
    try:
        data = {"scan_interval_sec": 90, "min_reward_usd": 250.0}
        engine = {k: v for k, v in data.items() if k in ENGINE_DEFAULTS}
        strategy = {k: v for k, v in data.items() if k in TEMPLATE_DEFAULTS}
        database.save_settings(engine)
        database.save_template(database.get_default_template_id(), strategy)
        merged = dict(database.get_settings())
        merged.update(database.get_template(database.get_default_template_id()))
        assert merged["scan_interval_sec"] == 90
        assert merged["min_reward_usd"] == 250.0
        # 策略键没有泄漏进引擎级 settings
        assert "stop_loss_pct" not in database.get_settings()
    finally:
        database.close()


def test_config_split_engine_and_template_defaults():
    from config import ENGINE_DEFAULTS, TEMPLATE_DEFAULTS, DEFAULTS

    assert set(ENGINE_DEFAULTS) == {
        "scan_interval_sec",
        "fill_check_interval_sec",
        "cooldown_minutes",
        "rewards_cache_ttl_sec",
        "discovery_interval_sec",
        "reward_scan_max_pages",
    }
    assert "excluded_categories" not in TEMPLATE_DEFAULTS
    assert TEMPLATE_DEFAULTS["include_other"] is True
    assert "sports" not in TEMPLATE_DEFAULTS["included_categories"]
    assert "politics" in TEMPLATE_DEFAULTS["included_categories"]
    assert TEMPLATE_DEFAULTS["min_reward_usd"] == 100.0
    assert "stop_loss_pct" not in TEMPLATE_DEFAULTS
    # 向后兼容:DEFAULTS 仍是两者合并(get_settings 在最后一个任务前仍用它)
    assert DEFAULTS["scan_interval_sec"] == 30
    assert DEFAULTS["min_reward_usd"] == 100.0
    assert set(ENGINE_DEFAULTS) & set(TEMPLATE_DEFAULTS) == set()
    assert ENGINE_DEFAULTS["discovery_interval_sec"] == 14400


def test_template_defaults_has_exposure_keys():
    from config import TEMPLATE_DEFAULTS

    assert TEMPLATE_DEFAULTS["max_exposure_usd"] == 250
    assert TEMPLATE_DEFAULTS["max_exposure_shares"] == 500
    assert TEMPLATE_DEFAULTS["max_concurrent_markets"] == 10
    assert TEMPLATE_DEFAULTS["cliff_probe_cents"] == 2
    assert "order_size_mode" not in TEMPLATE_DEFAULTS
    assert "order_size_custom_usd" not in TEMPLATE_DEFAULTS
    assert "max_buy_orders_per_wallet" not in TEMPLATE_DEFAULTS
    # laddering / 单份奖励阈值 已删:确保不再回归默认值
    for k in (
        "tier_rules",
        "placement_mode",
        "min_price_double_cents",
        "tier_match_var",
        "per_share_reward_enabled",
        "per_share_reward_thresholds",
    ):
        assert k not in TEMPLATE_DEFAULTS


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    database.init()
    yield database
    database.close()


class TestSettings:
    def test_get_default_settings_engine_only(self, db):
        settings = db.get_settings()
        assert settings["scan_interval_sec"] == 30
        assert settings["cooldown_minutes"] == 20
        assert "stop_loss_pct" not in settings
        assert "min_reward_usd" not in settings

    def test_save_and_load_engine_settings(self, db):
        db.save_settings({"scan_interval_sec": 60})
        assert db.get_settings()["scan_interval_sec"] == 60

    def test_save_password_hash(self, db):
        db.save_password("hashed_pw", b"salt_bytes")
        pw_hash, salt = db.get_password()
        assert pw_hash == "hashed_pw"
        assert salt == b"salt_bytes"


class TestSettingsToTemplateMigration:
    def _make_legacy_state(self, db):
        """模拟老库:templates 空,settings 里有策略键+引擎键。"""
        import json as _json

        c = db.conn.cursor()
        c.execute("DELETE FROM template_settings")
        c.execute("DELETE FROM templates")
        c.execute("DELETE FROM settings")
        legacy = {
            "stop_loss_pct": 10.0,
            "min_reward_usd": 200.0,
            "order_size_mode": "balance",
            "scan_interval_sec": 45,
        }
        for k, v in legacy.items():
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)", (k, _json.dumps(v))
            )
        db.conn.commit()

    def test_strategy_keys_move_to_default_template(self, db):
        self._make_legacy_state(db)
        db._migrate()
        t = db.get_template(db.get_default_template_id())
        assert t["min_reward_usd"] == 200.0
        # order_size_mode was retired (SP2); migration no longer moves it
        # stop_loss_pct was retired (SP3 T5); migration no longer moves it

    def test_engine_keys_stay_in_settings(self, db):
        self._make_legacy_state(db)
        db._migrate()
        assert db.get_settings()["scan_interval_sec"] == 45

    def test_strategy_keys_removed_from_settings(self, db):
        self._make_legacy_state(db)
        db._migrate()
        c = db.conn.cursor()
        c.execute("SELECT key FROM settings")
        keys = {row["key"] for row in c.fetchall()}
        # min_reward_usd is still in TEMPLATE_DEFAULTS → migrated out of settings
        assert "min_reward_usd" not in keys
        # stop_loss_pct retired (SP3 T5): no longer in TEMPLATE_DEFAULTS,
        # so migration leaves it stranded in settings (harmless; nothing reads it)

    def test_migration_idempotent(self, db):
        self._make_legacy_state(db)
        db._migrate()
        db._migrate()
        assert len([t for t in db.list_templates() if t["name"] == "默认"]) == 1
        assert db.get_template(db.get_default_template_id())["min_reward_usd"] == 200.0

    def test_fresh_install_no_copy(self, db):
        t = db.get_template(db.get_default_template_id())
        assert t["min_reward_usd"] == 100.0  # 默认值,非迁移值


class TestWallets:
    def test_add_and_list_wallets(self, db):
        db.add_wallet("0xABC", "encrypted_key_1")
        db.add_wallet("0xDEF", "encrypted_key_2")
        wallets = db.list_wallets()
        assert len(wallets) == 2
        assert wallets[0]["address"] == "0xABC"

    def test_remove_wallet(self, db):
        db.add_wallet("0xABC", "encrypted_key_1")
        db.remove_wallet("0xABC")
        assert len(db.list_wallets()) == 0

    def test_toggle_wallet(self, db):
        db.add_wallet("0xABC", "encrypted_key_1")
        db.toggle_wallet("0xABC", enabled=False)
        wallet = db.list_wallets()[0]
        assert wallet["enabled"] == 0

    def test_signature_type_defaults_to_2(self, db):
        db.add_wallet("0xABC", "encrypted_key_1")
        assert db.list_wallets()[0]["signature_type"] == 2

    def test_signature_type_round_trips(self, db):
        db.add_wallet("0xPROXY", "enc", funder="0xfund", signature_type=1)
        w = db.list_wallets()[0]
        assert w["signature_type"] == 1
        assert w["funder"] == "0xfund"

    def test_proxy_defaults_to_empty(self, db):
        db.add_wallet("0xABC", "enc")
        assert db.list_wallets()[0]["proxy"] == ""

    def test_add_wallet_stores_proxy(self, db):
        db.add_wallet("0xABC", "enc", proxy="gate.kookeey.info:1000:u:p")
        assert db.list_wallets()[0]["proxy"] == "gate.kookeey.info:1000:u:p"

    def test_set_wallet_proxy_updates(self, db):
        db.add_wallet("0xABC", "enc")
        db.set_wallet_proxy("0xABC", "h:2000:x:y")
        assert db.list_wallets()[0]["proxy"] == "h:2000:x:y"

    def test_migration_adds_signature_type_to_old_db(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "old.db")
        # 模拟老库:wallets 表没有 signature_type 列
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE wallets (address TEXT PRIMARY KEY, encrypted_key TEXT NOT NULL,"
            " funder TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,"
            " created_at REAL NOT NULL DEFAULT (strftime('%s','now')))"
        )
        conn.execute(
            "INSERT INTO wallets (address, encrypted_key) VALUES ('0xOLD', 'enc')"
        )
        conn.commit()
        conn.close()

        database = Database(db_path)
        database.init()  # 应迁移补列,默认 2
        try:
            w = database.list_wallets()[0]
            assert w["address"] == "0xOLD"
            assert w["signature_type"] == 2
        finally:
            database.close()

    def test_remark_defaults_to_empty(self, db):
        db.add_wallet("0xABC", "enc")
        assert db.list_wallets()[0]["remark"] == ""

    def test_add_wallet_stores_remark(self, db):
        db.add_wallet("0xABC", "enc", remark="主号")
        assert db.list_wallets()[0]["remark"] == "主号"

    def test_set_wallet_remark_updates(self, db):
        db.add_wallet("0xABC", "enc")
        db.set_wallet_remark("0xABC", "小号2")
        assert db.list_wallets()[0]["remark"] == "小号2"

    def test_migration_adds_remark_to_old_db(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "old.db")
        # 模拟老库:wallets 表没有 remark 列
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE wallets (address TEXT PRIMARY KEY, encrypted_key TEXT NOT NULL,"
            " funder TEXT NOT NULL DEFAULT '', signature_type INTEGER NOT NULL DEFAULT 2,"
            " proxy TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,"
            " created_at REAL NOT NULL DEFAULT (strftime('%s','now')))"
        )
        conn.execute(
            "INSERT INTO wallets (address, encrypted_key) VALUES ('0xOLD', 'enc')"
        )
        conn.commit()
        conn.close()

        database = Database(db_path)
        database.init()  # 应迁移补 remark 列,默认 ''
        try:
            w = database.list_wallets()[0]
            assert w["address"] == "0xOLD"
            assert w["remark"] == ""
            database.set_wallet_remark("0xOLD", "回填")
            assert database.list_wallets()[0]["remark"] == "回填"
        finally:
            database.close()


class TestOrders:
    def test_record_and_get_buy_order(self, db):
        db.record_order(
            wallet="0xABC",
            market_id="mkt1",
            token_id="tok1",
            market_name="Test Market",
            side="buy",
            order_id="ord1",
            price=0.25,
            size=1000,
            status="open",
        )
        orders = db.get_open_orders("0xABC")
        assert len(orders) == 1
        assert orders[0]["order_id"] == "ord1"

    def test_update_order_status(self, db):
        db.record_order(
            wallet="0xABC",
            market_id="mkt1",
            token_id="tok1",
            market_name="Test Market",
            side="buy",
            order_id="ord1",
            price=0.25,
            size=1000,
            status="open",
        )
        db.update_order_status("ord1", "filled")
        orders = db.get_open_orders("0xABC")
        assert len(orders) == 0

    def test_record_position(self, db):
        db.record_position(
            wallet="0xABC",
            market_id="mkt1",
            token_id="tok1",
            market_name="Test Market",
            buy_price=0.25,
            size=1000,
            sell_order_id="sell1",
        )
        positions = db.get_positions("0xABC")
        assert len(positions) == 1
        assert positions[0]["buy_price"] == 0.25

    def test_record_trade_history(self, db):
        db.record_trade(
            wallet="0xABC",
            market_id="mkt1",
            market_name="Test Market",
            side="buy",
            price=0.25,
            size=1000,
            pnl=0.0,
        )
        trades = db.get_trade_history()
        assert len(trades) == 1

    def test_cooldown(self, db):
        db.set_cooldown("0xABC", "mkt1", minutes=20)
        assert db.is_in_cooldown("0xABC", "mkt1") is True

    def test_side_pause_and_merge_ledger_are_persistent(self, db):
        db.set_side_pause("0xABC", "mkt1", "yes", 20)
        assert db.is_side_paused("0xABC", "mkt1", "yes") is True
        assert db.is_side_paused("0xABC", "mkt1", "no") is False
        op = db.create_merge_operation("0xABC", "0xF", "mkt1", "yes", "no", 12.5)
        assert db.get_unresolved_merges("0xABC")[0]["id"] == op
        db.update_merge_operation(op, "submitted", relayer_id="rid", tx_hash="0xtx")
        db.update_merge_operation(
            op, "confirmed", realized_pnl=1.25,
            consumed_lots=[{"asset_id": "yes", "lots": [{"price": 0.3, "take": 12.5}]}],
        )
        confirmed = db.get_confirmed_merges("0xABC")
        assert confirmed[0]["realized_pnl"] == 1.25
        assert confirmed[0]["consumed_lots"][0]["asset_id"] == "yes"


class TestActions:
    def test_record_and_get_action(self, db):
        db.record_action(
            wallet="0xABC",
            market_id="mkt1",
            action_type="take_profit_sell",
            side="卖出",
            price=0.33,
            size=120.0,
            reason="买单成交，按成交价挂等价止盈卖单",
            price_basis="卖价=买入成交价 0.3300；来源：CLOB get_trades",
        )
        rows = db.get_actions()
        assert len(rows) == 1
        r = rows[0]
        assert r["wallet"] == "0xABC"
        assert r["action_type"] == "take_profit_sell"
        assert r["side"] == "卖出"
        assert r["price"] == 0.33
        assert r["size"] == 120.0
        assert "止盈" in r["reason"]
        assert "成交价" in r["price_basis"]
        assert r["created_at"] > 0

    def test_get_actions_filters_by_wallet(self, db):
        db.record_action("0xA", "m", "cancel_remainder", "-", -1, 0, "r", "b")
        db.record_action("0xB", "m", "cancel_remainder", "-", -1, 0, "r", "b")
        assert len(db.get_actions(wallet="0xA")) == 1
        assert db.get_actions(wallet="0xA")[0]["wallet"] == "0xA"

    def test_get_actions_filters_by_action_types(self, db):
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.3, 1, "r", "b")
        db.record_action("0xA", "m", "step3_replace_new", "买入", 0.4, 1, "r", "b")
        db.record_action("0xA", "m", "stoploss_market_sell", "卖出", 0.2, 1, "r", "b")
        rows = db.get_actions(action_types=["take_profit_sell", "stoploss_market_sell"])
        assert len(rows) == 2
        assert {r["action_type"] for r in rows} == {
            "take_profit_sell",
            "stoploss_market_sell",
        }

    def test_get_actions_filters_by_time_range(self, db):
        import time

        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.3, 1, "r", "b")
        now = time.time()
        assert len(db.get_actions(start=now - 3600, end=now + 3600)) == 1
        assert len(db.get_actions(start=now + 3600)) == 0

    def test_get_actions_orders_desc_by_created_at(self, db):
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.1, 1, "first", "b")
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.2, 1, "second", "b")
        rows = db.get_actions()
        assert rows[0]["reason"] == "second"
        assert rows[1]["reason"] == "first"

    def test_get_actions_limit_returns_most_recent(self, db):
        for i in range(5):
            db.record_action(
                "0xA", "m", "take_profit_sell", "卖出", 0.1, 1, f"a{i}", "b"
            )
        rows = db.get_actions(limit=2)
        assert [r["reason"] for r in rows] == ["a4", "a3"]

    def test_get_actions_limit_offset_slices(self, db):
        for i in range(5):
            db.record_action(
                "0xA", "m", "take_profit_sell", "卖出", 0.1, 1, f"a{i}", "b"
            )
        rows = db.get_actions(limit=2, offset=2)
        assert [r["reason"] for r in rows] == ["a2", "a1"]

    def test_get_actions_no_limit_returns_all(self, db):
        for i in range(5):
            db.record_action(
                "0xA", "m", "take_profit_sell", "卖出", 0.1, 1, f"a{i}", "b"
            )
        assert len(db.get_actions()) == 5

    def test_count_actions_total_and_filters(self, db):
        db.record_action("0xA", "m", "take_profit_sell", "卖出", 0.3, 1, "r", "b")
        db.record_action("0xA", "m", "cancel_remainder", "-", -1, 0, "r", "b")
        db.record_action("0xB", "m", "cancel_remainder", "-", -1, 0, "r", "b")
        assert db.count_actions() == 3
        assert db.count_actions(wallet="0xA") == 2
        assert db.count_actions(action_types=["cancel_remainder"]) == 2
        assert db.count_actions(wallet="0xA", action_types=["cancel_remainder"]) == 1


class TestEligibleMarkets:
    def test_min_cost_round_trips(self, db):
        db.save_eligible_markets(
            [
                {
                    "market_id": "0xabc",
                    "token_id": "tok1",
                    "market_name": "M",
                    "outcome": "Yes",
                    "daily_reward": 150.0,
                    "order_price": 0.30,
                    "order_size": 100,
                    "min_cost": 29.0,
                }
            ]
        )
        rows = db.get_eligible_markets()
        assert len(rows) == 1
        assert rows[0]["min_cost"] == 29.0

    def test_save_and_get_tags(self, db):
        db.save_eligible_markets(
            [
                {
                    "market_id": "A",
                    "token_id": "t1",
                    "market_name": "M",
                    "outcome": "Yes",
                    "daily_reward": 50,
                    "order_price": 0,
                    "order_size": 0,
                    "tags": ["sports"],
                }
            ]
        )
        assert db.get_eligible_markets()[0]["tags"] == ["sports"]

    def test_tags_default_empty_list(self, db):
        db.save_eligible_markets(
            [
                {
                    "market_id": "B",
                    "token_id": "t2",
                    "market_name": "M2",
                    "outcome": "No",
                    "daily_reward": 50,
                    "order_price": 0,
                    "order_size": 0,
                }
            ]
        )
        assert db.get_eligible_markets()[0]["tags"] == []

    def test_update_eligible_reward_overwrites_all_tokens_of_market(self, db):
        db.save_eligible_markets(
            [
                {
                    "market_id": "0xabc",
                    "token_id": "yes",
                    "market_name": "M",
                    "outcome": "Yes",
                    "daily_reward": 300.0,
                    "order_price": 0.30,
                    "order_size": 100,
                },
                {
                    "market_id": "0xabc",
                    "token_id": "no",
                    "market_name": "M",
                    "outcome": "No",
                    "daily_reward": 300.0,
                    "order_price": 0.70,
                    "order_size": 100,
                },
                {
                    "market_id": "0xother",
                    "token_id": "yes",
                    "market_name": "N",
                    "outcome": "Yes",
                    "daily_reward": 300.0,
                    "order_price": 0.30,
                    "order_size": 100,
                },
            ]
        )
        db.update_eligible_reward("0xabc", 5.0)
        rows = {
            (r["market_id"], r["token_id"]): r["daily_reward"]
            for r in db.get_eligible_markets()
        }
        assert rows[("0xabc", "yes")] == 5.0
        assert rows[("0xabc", "no")] == 5.0
        assert rows[("0xother", "yes")] == 300.0  # 别的市场不受影响

    def test_update_eligible_reward_empty_id_is_noop(self, db):
        db.save_eligible_markets(
            [
                {
                    "market_id": "0xabc",
                    "token_id": "yes",
                    "market_name": "M",
                    "outcome": "Yes",
                    "daily_reward": 300.0,
                    "order_price": 0.30,
                    "order_size": 100,
                }
            ]
        )
        db.update_eligible_reward("", 5.0)
        assert db.get_eligible_markets()[0]["daily_reward"] == 300.0

    def test_update_eligible_reward_unknown_market_is_noop(self, db):
        db.update_eligible_reward("0xnothere", 5.0)  # 不抛异常即可
        assert db.get_eligible_markets() == []


class TestMarketMeta:
    def test_upsert_and_get(self, db):
        db.upsert_market_meta("0xc1", "市场A", "slug-a", "evt-a")
        meta = db.get_market_meta()
        assert meta["0xc1"] == {
            "name": "市场A",
            "market_slug": "slug-a",
            "event_slug": "evt-a",
        }

    def test_upsert_updates_same_condition_id(self, db):
        db.upsert_market_meta("0xc1", "旧名", "old", "")
        db.upsert_market_meta("0xc1", "新名", "new", "evt")
        meta = db.get_market_meta()
        assert len(meta) == 1
        assert meta["0xc1"]["name"] == "新名"
        assert meta["0xc1"]["market_slug"] == "new"
        assert meta["0xc1"]["event_slug"] == "evt"

    def test_empty_condition_id_skipped(self, db):
        db.upsert_market_meta("", "x", "y", "z")
        assert db.get_market_meta() == {}

    def test_accumulates_across_calls(self, db):
        db.upsert_market_meta("0xc1", "A", "", "")
        db.upsert_market_meta("0xc2", "B", "", "")
        assert set(db.get_market_meta().keys()) == {"0xc1", "0xc2"}


class TestBlacklist:
    def test_add_and_get_ids(self, db):
        db.add_to_blacklist("0xc1", "spam market")
        assert db.get_blacklist_ids() == {"0xc1"}

    def test_get_blacklist_returns_note_and_time(self, db):
        db.add_to_blacklist("0xc1", "spam")
        bl = db.get_blacklist()
        assert len(bl) == 1
        assert bl[0]["condition_id"] == "0xc1"
        assert bl[0]["note"] == "spam"
        assert "added_at" in bl[0]

    def test_remove(self, db):
        db.add_to_blacklist("0xc1", "")
        db.remove_from_blacklist("0xc1")
        assert db.get_blacklist_ids() == set()

    def test_add_dedups_and_updates_note(self, db):
        db.add_to_blacklist("0xc1", "first")
        db.add_to_blacklist("0xc1", "second")
        bl = db.get_blacklist()
        assert len(bl) == 1
        assert bl[0]["note"] == "second"

    def test_empty_condition_id_skipped(self, db):
        db.add_to_blacklist("", "x")
        assert db.get_blacklist_ids() == set()

    def test_empty_when_none(self, db):
        assert db.get_blacklist_ids() == set()
        assert db.get_blacklist() == []


def test_template_defaults_has_exit_keys():
    from config import TEMPLATE_DEFAULTS

    assert TEMPLATE_DEFAULTS["theta_loss_cents"] == 2
    assert TEMPLATE_DEFAULTS["theta_stop_cents"] == 5
    assert TEMPLATE_DEFAULTS["case_a_mode"] == "ask"
    assert "stop_loss_pct" not in TEMPLATE_DEFAULTS


class TestTemplateCRUD:
    def test_create_and_list_templates(self, db):
        tid = db.create_template("保守")
        assert isinstance(tid, int)
        assert "保守" in [t["name"] for t in db.list_templates()]

    def test_create_duplicate_name_raises(self, db):
        db.create_template("保守")
        with pytest.raises(Exception):
            db.create_template("保守")

    def test_save_and_get_template_merges_defaults(self, db):
        from config import TEMPLATE_DEFAULTS

        tid = db.create_template("激进")
        db.save_template(tid, {"max_spread_cents": 6.0})
        t = db.get_template(tid)
        assert t["max_spread_cents"] == 6.0
        assert t["min_reward_usd"] == 100.0
        assert t["included_categories"] == TEMPLATE_DEFAULTS["included_categories"]
        assert "scan_interval_sec" not in t

    def test_default_template_exists_after_init(self, db):
        assert isinstance(db.get_default_template_id(), int)
        assert "默认" in [t["name"] for t in db.list_templates()]

    def test_set_wallet_template_and_get_template_for(self, db):
        db.add_wallet("0xAAA", "k")
        tid = db.create_template("激进")
        db.save_template(tid, {"max_spread_cents": 6.0})
        db.set_wallet_template("0xAAA", tid)
        assert db.get_template_for("0xAAA")["max_spread_cents"] == 6.0

    def test_get_template_for_null_falls_back_to_default(self, db):
        db.add_wallet("0xBBB", "k")
        t = db.get_template_for("0xBBB")
        assert t["min_reward_usd"] == 100.0
        assert t["max_spread_cents"] == 3.0

    def test_get_template_for_unknown_wallet_falls_back_to_default(self, db):
        assert db.get_template_for("0xNOPE")["max_spread_cents"] == 3.0

    def test_delete_default_template_rejected(self, db):
        with pytest.raises(Exception):
            db.delete_template(db.get_default_template_id())

    def test_delete_template_rebinds_wallets_to_default(self, db):
        db.add_wallet("0xCCC", "k")
        tid = db.create_template("临时")
        db.set_wallet_template("0xCCC", tid)
        db.delete_template(tid)
        w = next(w for w in db.list_wallets() if w["address"] == "0xCCC")
        assert w["template_id"] is None
        assert db.get_template_for("0xCCC")["max_spread_cents"] == 3.0


class TestTemplateSchema:
    def test_templates_table_exists(self, db):
        c = db.conn.cursor()
        c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='templates'"
        )
        assert c.fetchone() is not None

    def test_template_settings_table_exists(self, db):
        c = db.conn.cursor()
        c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='template_settings'"
        )
        assert c.fetchone() is not None

    def test_wallets_has_template_id_column(self, db):
        c = db.conn.cursor()
        c.execute("PRAGMA table_info(wallets)")
        cols = {row[1] for row in c.fetchall()}
        assert "template_id" in cols


def test_rename_template(db):
    tid = db.create_template("旧名")
    db.rename_template(tid, "新名")
    names = {t["name"] for t in db.list_templates()}
    assert "新名" in names and "旧名" not in names


def test_rename_template_duplicate_raises(db):
    import sqlite3

    db.create_template("A")
    tid = db.create_template("B")
    with pytest.raises(sqlite3.IntegrityError):
        db.rename_template(tid, "A")


def test_eligible_markets_roundtrips_spread_cents(tmp_path):
    from models.database import Database

    db = Database(str(tmp_path / "t.db"))
    db.init()
    try:
        db.save_eligible_markets(
            [
                {
                    "market_id": "c1",
                    "token_id": "tY",
                    "market_name": "Q",
                    "outcome": "YES",
                    "market_competitiveness": 0.1,
                    "daily_reward": 40.0,
                    "rewards_max_spread": 4,
                    "rewards_min_size": 100,
                    "reward_range_min": 0.51,
                    "reward_range_max": 0.59,
                    "spread_cents": 2.0,
                    "tags": [],
                }
            ]
        )
        rows = db.get_eligible_markets()
        assert len(rows) == 1
        assert round(rows[0]["spread_cents"], 4) == 2.0
    finally:
        db.close()


def test_template_defaults_size_tiers_replace_global_keys():
    from config import TEMPLATE_DEFAULTS

    assert TEMPLATE_DEFAULTS["size_tiers"] == []
    for dead in (
        "rule1_min_coeff",
        "rule2_min_coeff",
        "rule3_min_coeff",
        "gap_high_coeff_sum_min",
        "amount_value_table",
        "rewards_min_size_min",
        "rewards_min_size_max",
    ):
        assert dead not in TEMPLATE_DEFAULTS, f"被取代键 {dead} 应已删除"


def test_migration_deletes_superseded_template_keys(tmp_path):
    db = Database(str(tmp_path / "mig.db"))
    db.init()
    tid = db.get_default_template_id()
    db.conn.execute(
        "INSERT OR REPLACE INTO template_settings (template_id, key, value)"
        " VALUES (?, ?, ?)",
        (tid, "rule1_min_coeff", "3"),
    )
    db.conn.commit()
    db.close()
    db2 = Database(str(tmp_path / "mig.db"))
    db2.init()  # 再次 init 触发迁移
    try:
        c = db2.conn.cursor()
        c.execute(
            "SELECT COUNT(*) AS n FROM template_settings WHERE key = 'rule1_min_coeff'"
        )
        assert c.fetchone()["n"] == 0
    finally:
        db2.close()


def test_category_catalog_roundtrip(tmp_path):
    """品类计数快照存/取往返;从没存过返回 None;且不污染引擎级 settings。"""
    db = Database(str(tmp_path / "cat.db"))
    db.init()
    try:
        assert db.get_category_catalog() is None
        snap = {
            "ready": True,
            "categories": [{"slug": "politics", "label": "政治", "count": 3}],
            "other_count": 2,
            "updated_at": 123.0,
        }
        db.save_category_catalog(snap)
        assert db.get_category_catalog() == snap
        # 保留键不会混进引擎参数(get_settings 只认 ENGINE_DEFAULTS 键)
        assert "category_catalog" not in db.get_settings()
    finally:
        db.close()


class TestDailyPnl:
    def test_upsert_and_get(self, db):
        db.upsert_daily_pnl(
            "0xA", "2026-06-01", reward=7, rebate=0.5, sell_profit=2, loss=1, fee=0.1
        )
        row = db.get_daily_pnl("0xA", "2026-06-01", "2026-06-01")[0]
        assert row["reward"] == 7 and row["rebate"] == 0.5
        assert abs(row["net"] - (7 + 0.5 + 2 - 1 - 0.1)) < 1e-9

    def test_upsert_overwrites_same_day(self, db):
        db.upsert_daily_pnl("0xA", "2026-06-01", 1, 0, 0, 0, 0)
        db.upsert_daily_pnl("0xA", "2026-06-01", 9, 0, 0, 0, 0)  # 幂等覆盖
        rows = db.get_daily_pnl("0xA", "2026-06-01", "2026-06-01")
        assert len(rows) == 1 and rows[0]["reward"] == 9

    def test_get_filters_by_range_and_orders(self, db):
        db.upsert_daily_pnl("0xA", "2026-06-03", 3, 0, 0, 0, 0)
        db.upsert_daily_pnl("0xA", "2026-06-01", 1, 0, 0, 0, 0)
        db.upsert_daily_pnl("0xA", "2026-05-30", 9, 0, 0, 0, 0)  # 区间外
        rows = db.get_daily_pnl("0xA", "2026-06-01", "2026-06-03")
        assert [r["date"] for r in rows] == ["2026-06-01", "2026-06-03"]

    def test_get_all_aggregates_across_wallets(self, db):
        db.upsert_daily_pnl(
            "0xA", "2026-06-01", reward=7, rebate=0, sell_profit=0, loss=0, fee=0
        )
        db.upsert_daily_pnl(
            "0xB", "2026-06-01", reward=3, rebate=0, sell_profit=0, loss=0, fee=0
        )
        agg = db.get_daily_pnl_all("2026-06-01", "2026-06-01")[0]
        assert agg["date"] == "2026-06-01" and agg["reward"] == 10


class TestLiquidationQueries:
    def _seed_eligible(self, db, market_id, daily_reward, min_cost):
        c = db.conn.cursor()
        c.execute(
            "INSERT INTO eligible_markets (market_id, token_id, market_name, outcome,"
            " daily_reward, order_price, order_size, min_cost) VALUES (?,?,?,?,?,?,?,?)",
            (market_id, "tok", "n", "Yes", daily_reward, 0.1, 20, min_cost),
        )
        db.conn.commit()

    def test_get_market_daily_reward(self, db):
        self._seed_eligible(db, "0xC1", 25.0, 2.0)
        assert db.get_market_daily_reward("0xC1") == 25.0
        assert db.get_market_daily_reward("0xNONE") is None

    def test_get_min_order_cost(self, db):
        assert db.get_min_order_cost() is None  # 空表
        self._seed_eligible(db, "0xC1", 25.0, 5.0)
        self._seed_eligible(db, "0xC2", 40.0, 2.0)
        assert db.get_min_order_cost() == 2.0


def test_skip_new_markets_defaults():
    """跳过新建市场：默认关闭（升级零行为变化），保护期默认 24 小时。"""
    from config import TEMPLATE_DEFAULTS

    assert TEMPLATE_DEFAULTS["skip_new_markets"] is False
    assert TEMPLATE_DEFAULTS["new_market_hours"] == 24.0


def test_skip_new_categories_defaults():
    """新市场保护默认覆盖全部 curated 品类 + 「其他」——升级后行为与一刀切时期一致。"""
    from config import TEMPLATE_DEFAULTS, CATALOG_SLUGS

    assert set(TEMPLATE_DEFAULTS["skip_new_categories"]) == set(CATALOG_SLUGS)
    assert TEMPLATE_DEFAULTS["skip_new_other"] is True


def test_skip_new_categories_default_is_ordered_list():
    """默认值必须是有确定顺序的 list(不是 frozenset):要存进 DB、要进去重键。"""
    from config import TEMPLATE_DEFAULTS, CATEGORY_CATALOG

    assert TEMPLATE_DEFAULTS["skip_new_categories"] == [
        c["slug"] for c in CATEGORY_CATALOG
    ]
