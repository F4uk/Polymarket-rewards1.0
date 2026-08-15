"""models/database.py — SQLite database layer."""

import sqlite3
import json
import threading
import time
from config import DEFAULTS, ENGINE_DEFAULTS, TEMPLATE_DEFAULTS


class ActiveMergeOperationExists(RuntimeError):
    """Raised when a wallet/condition already owns a funds-moving Merge."""

    def __init__(self, wallet: str, condition_id: str, operation_id: int | None = None):
        self.wallet = wallet
        self.condition_id = condition_id
        self.operation_id = operation_id
        suffix = f" (operation {operation_id})" if operation_id is not None else ""
        super().__init__(
            f"active Merge already exists for wallet={wallet} condition={condition_id}{suffix}"
        )


class ActiveExitCycleExists(RuntimeError):
    """Raised when a wallet/condition already owns an active (non-CLOSED) exit cycle."""

    def __init__(self, wallet: str, condition_id: str, cycle_id: int | None = None):
        self.wallet = wallet
        self.condition_id = condition_id
        self.cycle_id = cycle_id
        suffix = f" (cycle {cycle_id})" if cycle_id is not None else ""
        super().__init__(
            f"active exit cycle already exists for wallet={wallet} condition={condition_id}{suffix}"
        )


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # 每个线程一条独立连接。sqlite 连接不能安全地被多线程共享:曾用单个共享连接
        # (check_same_thread=False)且全程无锁,多 worker 并发时会把读结果冲坏——
        # get_template_for 偶发读到默认模板、离场按错阈值(20% 而非 80%)强平在手持仓
        # (2026-06-27 事故)。改为线程内惰性建连 + WAL,让读写互不阻塞、各自独立。
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL:读快照不阻塞写、写不阻塞读;busy_timeout:写-写争用时等待而非立即报错。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        with self._conn_lock:
            self._connections.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """本线程专属连接,首次访问时惰性创建;每个连接只被其创建线程使用。"""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._new_conn()
            self._local.conn = c
        return c

    def init(self):
        # 触发本线程建连 + 建表 + 迁移。WAL/schema 落到库文件,其余线程各自连上即可见。
        self._create_tables()

    def close(self):
        with self._conn_lock:
            for c in self._connections:
                try:
                    c.close()
                except Exception:
                    pass
            self._connections.clear()
        self._local = threading.local()

    def _create_tables(self):
        c = self.conn.cursor()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                password_hash TEXT NOT NULL,
                salt BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallets (
                address TEXT PRIMARY KEY,
                encrypted_key TEXT NOT NULL,
                funder TEXT NOT NULL DEFAULT '',
                signature_type INTEGER NOT NULL DEFAULT 2,
                proxy TEXT NOT NULL DEFAULT '',
                remark TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_active_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                market_name TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                market_name TEXT NOT NULL,
                buy_price REAL NOT NULL,
                size INTEGER NOT NULL,
                sell_order_id TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                market_name TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size INTEGER NOT NULL,
                pnl REAL NOT NULL DEFAULT 0.0,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL DEFAULT -1,
                size REAL NOT NULL DEFAULT 0,   -- REAL: fractional fill sizes (trades.size is INTEGER by legacy design)
                reason TEXT NOT NULL DEFAULT '',
                price_basis TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS cooldowns (
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (wallet, market_id)
            );
            CREATE TABLE IF NOT EXISTS side_pauses (
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (wallet, market_id, token_id)
            );
            CREATE TABLE IF NOT EXISTS eligible_markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                market_name TEXT NOT NULL,
                outcome TEXT NOT NULL,
                market_competitiveness REAL DEFAULT 0,
                daily_reward REAL NOT NULL,
                rewards_max_spread INTEGER DEFAULT 0,
                rewards_min_size INTEGER DEFAULT 0,
                tick_size REAL DEFAULT 0.01,
                tick_size_str TEXT DEFAULT '0.01',
                neg_risk INTEGER DEFAULT 0,
                reward_range_min REAL DEFAULT 0,
                reward_range_max REAL DEFAULT 1,
                spread_cents REAL DEFAULT -1,
                order_price REAL NOT NULL,
                order_size INTEGER NOT NULL,
                min_cost REAL DEFAULT 0,
                end_date TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                scanned_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS market_meta (
                condition_id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                market_slug TEXT NOT NULL DEFAULT '',
                event_slug TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS blacklist (
                condition_id TEXT PRIMARY KEY,
                note TEXT NOT NULL DEFAULT '',
                added_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS template_settings (
                template_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (template_id, key)
            );
            CREATE TABLE IF NOT EXISTS daily_pnl (
                wallet TEXT NOT NULL,
                date TEXT NOT NULL,
                reward REAL NOT NULL DEFAULT 0,
                rebate REAL NOT NULL DEFAULT 0,
                sell_profit REAL NOT NULL DEFAULT 0,
                loss REAL NOT NULL DEFAULT 0,
                fee REAL NOT NULL DEFAULT 0,
                net REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (wallet, date)
            );
            CREATE TABLE IF NOT EXISTS merge_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                funder TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                yes_asset_id TEXT NOT NULL,
                no_asset_id TEXT NOT NULL,
                requested_qty REAL NOT NULL,
                relayer_id TEXT NOT NULL DEFAULT '',
                tx_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                realized_pnl REAL,
                consumed_lots_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                confirmed_at REAL,
                inventory_reconciled_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_merge_operations_wallet_condition
                ON merge_operations(wallet, condition_id, status);
            CREATE TABLE IF NOT EXISTS scoring_observations (
                order_id TEXT PRIMARY KEY,
                wallet TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                local_eligible INTEGER NOT NULL,
                official_scoring TEXT NOT NULL,
                checked_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relayer_credentials (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                encrypted_api_key TEXT NOT NULL,
                encrypted_secret TEXT NOT NULL,
                encrypted_passphrase TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relayer_test_results (
                wallet TEXT PRIMARY KEY,
                last_test_ok INTEGER NOT NULL,
                last_test_at REAL NOT NULL,
                last_test_reason TEXT NOT NULL DEFAULT '',
                deposit_wallet_deployed INTEGER
            );
            CREATE TABLE IF NOT EXISTS inventory_exit_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                status TEXT NOT NULL,
                trigger TEXT NOT NULL DEFAULT 'reward_fill',
                held_side TEXT NOT NULL DEFAULT '',
                held_asset_id TEXT NOT NULL DEFAULT '',
                held_assets_json TEXT NOT NULL DEFAULT '[]',
                managed_qty REAL NOT NULL DEFAULT 0,
                initial_qty REAL NOT NULL DEFAULT 0,
                cost_basis REAL,
                paired_qty REAL NOT NULL DEFAULT 0,
                direct_recovery REAL,
                merge_recovery REAL,
                advantage REAL,
                selected_route TEXT NOT NULL DEFAULT '',
                maker_window_until REAL,
                realized_recovered_collateral REAL NOT NULL DEFAULT 0,
                inventory_pnl REAL NOT NULL DEFAULT 0,
                holding_duration_sec REAL NOT NULL DEFAULT 0,
                closed_reason TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                opened_at REAL NOT NULL,
                closed_at REAL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inventory_exit_legs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL,
                wallet TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                exit_method TEXT NOT NULL DEFAULT '',
                asset_id TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL DEFAULT '',
                qty REAL NOT NULL,
                price REAL NOT NULL DEFAULT 0,
                collateral REAL NOT NULL DEFAULT 0,
                pnl REAL,
                order_id TEXT NOT NULL DEFAULT '',
                relayer_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'done',
                note TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_exit_legs_cycle
                ON inventory_exit_legs(cycle_id);
            CREATE INDEX IF NOT EXISTS idx_exit_cycles_wallet_status
                ON inventory_exit_cycles(wallet, status);
        """
        )
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        """Apply schema migrations for existing databases."""
        c = self.conn.cursor()
        c.execute("PRAGMA table_info(wallets)")
        cols = {row[1] for row in c.fetchall()}
        if "funder" not in cols:
            c.execute("ALTER TABLE wallets ADD COLUMN funder TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
        if "signature_type" not in cols:
            c.execute(
                "ALTER TABLE wallets ADD COLUMN signature_type INTEGER NOT NULL DEFAULT 2"
            )
            self.conn.commit()
        if "proxy" not in cols:
            c.execute("ALTER TABLE wallets ADD COLUMN proxy TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
        if "remark" not in cols:
            c.execute("ALTER TABLE wallets ADD COLUMN remark TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
        if "last_active_at" not in cols:
            c.execute(
                "ALTER TABLE wallets ADD COLUMN last_active_at REAL NOT NULL DEFAULT 0"
            )
            self.conn.commit()
        c.execute("PRAGMA table_info(merge_operations)")
        merge_cols = {row[1] for row in c.fetchall()}
        if merge_cols and "consumed_lots_json" not in merge_cols:
            c.execute(
                "ALTER TABLE merge_operations ADD COLUMN consumed_lots_json TEXT NOT NULL DEFAULT '[]'"
            )
            self.conn.commit()
            merge_cols.add("consumed_lots_json")
        if merge_cols and "inventory_reconciled_at" not in merge_cols:
            c.execute(
                "ALTER TABLE merge_operations ADD COLUMN inventory_reconciled_at REAL"
            )
            # Rows confirmed before this migration are historical and must not
            # become permanent inventory barriers merely because the column
            # did not exist when they completed.
            c.execute(
                """UPDATE merge_operations
                SET inventory_reconciled_at=COALESCE(confirmed_at, created_at)
                WHERE status='confirmed'"""
            )
            self.conn.commit()
            merge_cols.add("inventory_reconciled_at")
        # A condition owns its own Merge lifecycle.  The partial unique index
        # prevents two planned/submitted operations for the same wallet and
        # condition without imposing a wallet-global serialization policy.
        #
        # Very old databases may already contain duplicate active rows.  Do
        # not rewrite or hide either potentially funds-moving operation during
        # migration; the BEGIN IMMEDIATE guard in create_merge_operation()
        # still blocks every new duplicate until an operator reconciles them.
        try:
            c.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_merge_active_wallet_condition
                ON merge_operations(wallet COLLATE NOCASE, condition_id COLLATE NOCASE)
                WHERE status IN ('planned', 'submitted')"""
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
        # V2: at most one active (non-CLOSED) Inventory Exit cycle per wallet and
        # condition.  Canonical comparison is NOCASE, matching condition_key().
        try:
            c.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_exit_cycle_active_wallet_condition
                ON inventory_exit_cycles(wallet COLLATE NOCASE, condition_id COLLATE NOCASE)
                WHERE status != 'CLOSED'"""
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
        c.execute("PRAGMA table_info(inventory_exit_cycles)")
        cycle_cols = {row[1] for row in c.fetchall()}
        if cycle_cols and "held_assets_json" not in cycle_cols:
            c.execute(
                "ALTER TABLE inventory_exit_cycles "
                "ADD COLUMN held_assets_json TEXT NOT NULL DEFAULT '[]'"
            )
            self.conn.commit()
        c.execute("PRAGMA table_info(eligible_markets)")
        em_cols = {row[1] for row in c.fetchall()}
        if em_cols and "min_cost" not in em_cols:
            c.execute("ALTER TABLE eligible_markets ADD COLUMN min_cost REAL DEFAULT 0")
            self.conn.commit()
        c.execute("PRAGMA table_info(eligible_markets)")
        em_cols2 = {row[1] for row in c.fetchall()}
        if em_cols2 and "tags" not in em_cols2:
            c.execute("ALTER TABLE eligible_markets ADD COLUMN tags TEXT DEFAULT '[]'")
            self.conn.commit()
        c.execute("PRAGMA table_info(eligible_markets)")
        em_cols3 = {row[1] for row in c.fetchall()}
        if em_cols3 and "spread_cents" not in em_cols3:
            c.execute(
                "ALTER TABLE eligible_markets ADD COLUMN spread_cents REAL DEFAULT -1"
            )
            self.conn.commit()
        c.execute("PRAGMA table_info(wallets)")
        wcols = {row[1] for row in c.fetchall()}
        if "template_id" not in wcols:
            c.execute("ALTER TABLE wallets ADD COLUMN template_id INTEGER")
            self.conn.commit()
        c.execute("SELECT COUNT(*) AS n FROM templates")
        if c.fetchone()["n"] == 0:
            c.execute(
                "INSERT INTO templates (name) VALUES (?)", (self.DEFAULT_TEMPLATE_NAME,)
            )
            default_id = c.lastrowid
            c.execute("SELECT key, value FROM settings")
            for row in list(c.fetchall()):
                if row["key"] in TEMPLATE_DEFAULTS:
                    c.execute(
                        "INSERT OR REPLACE INTO template_settings "
                        "(template_id, key, value) VALUES (?, ?, ?)",
                        (default_id, row["key"], row["value"]),
                    )
                    c.execute("DELETE FROM settings WHERE key = ?", (row["key"],))
            self.conn.commit()
        # 档位模块(size_tiers)取代 7 个模板级全局键:清掉存量死键行(幂等,SP6d 手法)。
        superseded = (
            "rule1_min_coeff",
            "rule2_min_coeff",
            "rule3_min_coeff",
            "gap_high_coeff_sum_min",
            "amount_value_table",
            "rewards_min_size_min",
            "rewards_min_size_max",
        )
        ph = ",".join("?" * len(superseded))
        c.execute(f"DELETE FROM template_settings WHERE key IN ({ph})", superseded)
        c.execute(f"DELETE FROM settings WHERE key IN ({ph})", superseded)
        self.conn.commit()

    # --- Settings ---

    def get_settings(self) -> dict:
        """引擎级全局参数(策略级参数见 get_template_for)。"""
        c = self.conn.cursor()
        c.execute("SELECT key, value FROM settings")
        stored = {row["key"]: json.loads(row["value"]) for row in c.fetchall()}
        result = dict(ENGINE_DEFAULTS)
        for k in ENGINE_DEFAULTS:
            if k in stored:
                result[k] = stored[k]
        return result

    def save_settings(self, settings: dict):
        c = self.conn.cursor()
        for key, value in settings.items():
            c.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
        self.conn.commit()

    def save_category_catalog(self, payload: dict):
        """持久化配置页品类计数快照(整份 catalog + 其他数 + 时间戳),供跨重启秒显。
        存 settings 表的保留键 category_catalog;get_settings 只认 ENGINE_DEFAULTS 键,
        故不会污染引擎参数。"""
        c = self.conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("category_catalog", json.dumps(payload)),
        )
        self.conn.commit()

    def get_category_catalog(self) -> dict | None:
        """读上次持久化的品类计数快照;从没存过返回 None。"""
        c = self.conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = ?", ("category_catalog",))
        row = c.fetchone()
        return json.loads(row["value"]) if row else None

    # --- Templates ---

    DEFAULT_TEMPLATE_NAME = "默认"

    def create_template(self, name: str) -> int:
        c = self.conn.cursor()
        c.execute("INSERT INTO templates (name) VALUES (?)", (name,))
        self.conn.commit()
        return c.lastrowid

    def list_templates(self) -> list[dict]:
        c = self.conn.cursor()
        c.execute("SELECT id, name, created_at FROM templates ORDER BY id")
        return [dict(row) for row in c.fetchall()]

    def get_default_template_id(self) -> int:
        c = self.conn.cursor()
        c.execute(
            "SELECT id FROM templates WHERE name = ?", (self.DEFAULT_TEMPLATE_NAME,)
        )
        row = c.fetchone()
        if row is None:
            return self.create_template(self.DEFAULT_TEMPLATE_NAME)
        return row["id"]

    def get_template(self, template_id: int) -> dict:
        """TEMPLATE_DEFAULTS 合并该模板的覆盖键(逐键 + JSON 值)。"""
        c = self.conn.cursor()
        c.execute(
            "SELECT key, value FROM template_settings WHERE template_id = ?",
            (template_id,),
        )
        stored = {row["key"]: json.loads(row["value"]) for row in c.fetchall()}
        result = dict(TEMPLATE_DEFAULTS)
        result.update(stored)
        return result

    def save_template(self, template_id: int, values: dict):
        c = self.conn.cursor()
        for key, value in values.items():
            c.execute(
                "INSERT OR REPLACE INTO template_settings (template_id, key, value) "
                "VALUES (?, ?, ?)",
                (template_id, key, json.dumps(value)),
            )
        self.conn.commit()

    def rename_template(self, template_id: int, name: str):
        c = self.conn.cursor()
        c.execute("UPDATE templates SET name = ? WHERE id = ?", (name, template_id))
        self.conn.commit()

    def set_wallet_template(self, address: str, template_id: int):
        c = self.conn.cursor()
        c.execute(
            "UPDATE wallets SET template_id = ? WHERE address = ?",
            (template_id, address),
        )
        self.conn.commit()

    def get_template_for(self, address: str) -> dict:
        """按钱包地址取其绑定模板;NULL/未知钱包回落默认模板。"""
        c = self.conn.cursor()
        c.execute("SELECT template_id FROM wallets WHERE address = ?", (address,))
        row = c.fetchone()
        tid = row["template_id"] if row and row["template_id"] is not None else None
        if tid is None:
            tid = self.get_default_template_id()
        return self.get_template(tid)

    def delete_template(self, template_id: int):
        if template_id == self.get_default_template_id():
            raise ValueError("默认模板不可删除")
        c = self.conn.cursor()
        c.execute(
            "UPDATE wallets SET template_id = NULL WHERE template_id = ?",
            (template_id,),
        )
        c.execute("DELETE FROM template_settings WHERE template_id = ?", (template_id,))
        c.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        self.conn.commit()

    # --- Auth ---

    def save_password(self, password_hash: str, salt: bytes):
        c = self.conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO auth (id, password_hash, salt) VALUES (1, ?, ?)",
            (password_hash, salt),
        )
        self.conn.commit()

    def get_password(self):
        c = self.conn.cursor()
        c.execute("SELECT password_hash, salt FROM auth WHERE id = 1")
        row = c.fetchone()
        if row is None:
            return None, None
        return row["password_hash"], row["salt"]

    # --- Relayer credentials (encrypted at rest, app-level) ---
    # Builder API credentials are application-level secrets, never part of a
    # wallet/template. The three ciphertext blobs are written as one atomic
    # unit so a partial update (new key + old secret) can never be persisted.

    def save_relayer_credentials(
        self,
        encrypted_api_key: str,
        encrypted_secret: str,
        encrypted_passphrase: str,
        now: float = None,
    ):
        """Atomically upsert the single credential row (id=1, CHECK-enforced)."""
        c = self.conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute(
                """INSERT OR REPLACE INTO relayer_credentials
                (id, encrypted_api_key, encrypted_secret, encrypted_passphrase, updated_at)
                VALUES (1, ?, ?, ?, ?)""",
                (
                    encrypted_api_key,
                    encrypted_secret,
                    encrypted_passphrase,
                    now if now is not None else time.time(),
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_relayer_credentials(self):
        c = self.conn.cursor()
        c.execute(
            """SELECT encrypted_api_key, encrypted_secret, encrypted_passphrase, updated_at
            FROM relayer_credentials WHERE id = 1"""
        )
        row = c.fetchone()
        return dict(row) if row else None

    def delete_relayer_credentials(self):
        c = self.conn.cursor()
        c.execute("DELETE FROM relayer_credentials WHERE id = 1")
        self.conn.commit()

    def save_relayer_test_result(
        self,
        wallet: str,
        ok: bool,
        reason: str,
        deposit_wallet_deployed=None,
        at: float = None,
    ):
        """Persist a read-only preflight result (display only, never authority)."""
        c = self.conn.cursor()
        c.execute(
            """INSERT OR REPLACE INTO relayer_test_results
            (wallet, last_test_ok, last_test_at, last_test_reason, deposit_wallet_deployed)
            VALUES (?, ?, ?, ?, ?)""",
            (
                wallet,
                1 if ok else 0,
                at if at is not None else time.time(),
                reason or "",
                None
                if deposit_wallet_deployed is None
                else (1 if deposit_wallet_deployed else 0),
            ),
        )
        self.conn.commit()

    def get_relayer_test_results(self) -> dict:
        """Return {wallet: {last_test_ok, last_test_at, last_test_reason, ...}}."""
        c = self.conn.cursor()
        c.execute(
            """SELECT wallet, last_test_ok, last_test_at, last_test_reason,
            deposit_wallet_deployed FROM relayer_test_results"""
        )
        out = {}
        for row in c.fetchall():
            d = dict(row)
            d["last_test_ok"] = bool(d["last_test_ok"])
            d["deposit_wallet_deployed"] = (
                None
                if d["deposit_wallet_deployed"] is None
                else bool(d["deposit_wallet_deployed"])
            )
            out[d.pop("wallet")] = d
        return out

    def delete_relayer_test_results(self):
        c = self.conn.cursor()
        c.execute("DELETE FROM relayer_test_results")
        self.conn.commit()

    def delete_relayer_test_result(self, wallet: str):
        c = self.conn.cursor()
        c.execute("DELETE FROM relayer_test_results WHERE wallet = ?", (wallet,))
        self.conn.commit()

    # --- Wallets ---

    def add_wallet(
        self,
        address: str,
        encrypted_key: str,
        funder: str = "",
        signature_type: int = 2,
        proxy: str = "",
        remark: str = "",
    ):
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO wallets (address, encrypted_key, funder, signature_type, proxy, remark) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (address, encrypted_key, funder, signature_type, proxy, remark),
        )
        self.conn.commit()

    def set_wallet_proxy(self, address: str, proxy: str):
        """更新某钱包的代理串(明文)。下次启动引擎/重建该 worker 时生效。"""
        c = self.conn.cursor()
        c.execute("UPDATE wallets SET proxy = ? WHERE address = ?", (proxy, address))
        self.conn.commit()

    def set_wallet_remark(self, address: str, remark: str):
        """更新某钱包的备注(纯展示,不影响任何交易/API 客户端)。"""
        c = self.conn.cursor()
        c.execute("UPDATE wallets SET remark = ? WHERE address = ?", (remark, address))
        self.conn.commit()

    def touch_wallet_active(self, address: str):
        """记下该钱包最近一次干活的时间(成功挂买单 / 抓到成交)。覆盖式,不留历史。

        纯展示字段。调用点在引擎线程里,地址不存在(钱包已删)时静默无事发生。
        """
        c = self.conn.cursor()
        c.execute(
            "UPDATE wallets SET last_active_at = ? WHERE address = ?",
            (time.time(), address),
        )
        self.conn.commit()

    def remove_wallet(self, address: str):
        c = self.conn.cursor()
        c.execute("DELETE FROM wallets WHERE address = ?", (address,))
        self.conn.commit()

    def toggle_wallet(self, address: str, enabled: bool):
        c = self.conn.cursor()
        c.execute(
            "UPDATE wallets SET enabled = ? WHERE address = ?",
            (1 if enabled else 0, address),
        )
        self.conn.commit()

    def list_wallets(self) -> list[dict]:
        c = self.conn.cursor()
        c.execute(
            "SELECT address, encrypted_key, funder, signature_type, proxy, remark, enabled, "
            "last_active_at, created_at, template_id FROM wallets"
        )
        return [dict(row) for row in c.fetchall()]

    # --- Orders ---

    def record_order(
        self,
        wallet: str,
        market_id: str,
        token_id: str,
        market_name: str,
        side: str,
        order_id: str,
        price: float,
        size: int,
        status: str = "open",
    ):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO orders
            (order_id, wallet, market_id, token_id, market_name, side, price, size, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id,
                wallet,
                market_id,
                token_id,
                market_name,
                side,
                price,
                size,
                status,
            ),
        )
        self.conn.commit()

    def update_order_status(self, order_id: str, status: str):
        c = self.conn.cursor()
        now = time.time()
        c.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
            (status, now, order_id),
        )
        self.conn.commit()

    def get_open_orders(self, wallet: str = None) -> list[dict]:
        c = self.conn.cursor()
        if wallet:
            c.execute(
                "SELECT * FROM orders WHERE status = 'open' AND wallet = ?", (wallet,)
            )
        else:
            c.execute("SELECT * FROM orders WHERE status = 'open'")
        return [dict(row) for row in c.fetchall()]

    def get_open_buy_orders(self, wallet: str = None) -> list[dict]:
        c = self.conn.cursor()
        if wallet:
            c.execute(
                "SELECT * FROM orders WHERE status = 'open' AND side = 'buy' AND wallet = ?",
                (wallet,),
            )
        else:
            c.execute("SELECT * FROM orders WHERE status = 'open' AND side = 'buy'")
        return [dict(row) for row in c.fetchall()]

    # --- Positions ---

    def record_position(
        self,
        wallet: str,
        market_id: str,
        token_id: str,
        market_name: str,
        buy_price: float,
        size: int,
        sell_order_id: str = None,
    ):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO positions
            (wallet, market_id, token_id, market_name, buy_price, size, sell_order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (wallet, market_id, token_id, market_name, buy_price, size, sell_order_id),
        )
        self.conn.commit()

    def get_positions(self, wallet: str = None) -> list[dict]:
        c = self.conn.cursor()
        if wallet:
            c.execute(
                "SELECT * FROM positions WHERE status = 'open' AND wallet = ?",
                (wallet,),
            )
        else:
            c.execute("SELECT * FROM positions WHERE status = 'open'")
        return [dict(row) for row in c.fetchall()]

    def close_position(self, position_id: int):
        c = self.conn.cursor()
        c.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (position_id,))
        self.conn.commit()

    # --- Trades ---

    def record_trade(
        self,
        wallet: str,
        market_id: str,
        market_name: str,
        side: str,
        price: float,
        size: int,
        pnl: float = 0.0,
    ):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO trades (wallet, market_id, market_name, side, price, size, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (wallet, market_id, market_name, side, price, size, pnl),
        )
        self.conn.commit()

    def get_trade_history(
        self, wallet: str = None, start: float = None, end: float = None
    ) -> list[dict]:
        c = self.conn.cursor()
        query = "SELECT * FROM trades WHERE 1=1"
        params = []
        if wallet:
            query += " AND wallet = ?"
            params.append(wallet)
        if start:
            query += " AND created_at >= ?"
            params.append(start)
        if end:
            query += " AND created_at <= ?"
            params.append(end)
        query += " ORDER BY created_at DESC"
        c.execute(query, params)
        return [dict(row) for row in c.fetchall()]

    # --- Actions (monitor order-mutating actions log) ---

    def record_action(
        self,
        wallet: str,
        market_id: str,
        action_type: str,
        side: str,
        price: float,
        size: float,
        reason: str,
        price_basis: str,
    ):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO actions
            (wallet, market_id, action_type, side, price, size,
             reason, price_basis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                wallet,
                market_id,
                action_type,
                side,
                price,
                size,
                reason,
                price_basis,
            ),
        )
        self.conn.commit()

    def _actions_filter(self, wallet, start, end, action_types):
        """构造 actions 查询的 WHERE 子句 + 参数(get_actions / count_actions 共用)。"""
        clause = "WHERE 1=1"
        params = []
        if wallet:
            clause += " AND wallet = ?"
            params.append(wallet)
        if start:
            clause += " AND created_at >= ?"
            params.append(start)
        if end:
            clause += " AND created_at <= ?"
            params.append(end)
        if action_types:
            placeholders = ",".join("?" * len(action_types))
            clause += f" AND action_type IN ({placeholders})"
            params.extend(action_types)
        return clause, params

    def get_actions(
        self,
        wallet: str = None,
        start: float = None,
        end: float = None,
        action_types: list[str] = None,
        limit: int = None,
        offset: int = 0,
    ) -> list[dict]:
        clause, params = self._actions_filter(wallet, start, end, action_types)
        query = f"SELECT * FROM actions {clause} ORDER BY created_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params = params + [int(limit), int(offset)]
        c = self.conn.cursor()
        c.execute(query, params)
        return [dict(row) for row in c.fetchall()]

    def count_actions(
        self,
        wallet: str = None,
        start: float = None,
        end: float = None,
        action_types: list[str] = None,
    ) -> int:
        clause, params = self._actions_filter(wallet, start, end, action_types)
        c = self.conn.cursor()
        c.execute(f"SELECT COUNT(*) FROM actions {clause}", params)
        return c.fetchone()[0]

    # --- Cooldowns ---

    def set_cooldown(self, wallet: str, market_id: str, minutes: int):
        expires_at = time.time() + int(minutes or 0) * 60
        c = self.conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO cooldowns (wallet, market_id, expires_at) VALUES (?, ?, ?)",
            (wallet, market_id, expires_at),
        )
        self.conn.commit()

    def is_in_cooldown(self, wallet: str, market_id: str) -> bool:
        c = self.conn.cursor()
        c.execute(
            "SELECT expires_at FROM cooldowns WHERE wallet = ? AND market_id = ?",
            (wallet, market_id),
        )
        row = c.fetchone()
        if row is None:
            return False
        return time.time() < row["expires_at"]

    # Token-side fill guards.  Existing condition cooldowns remain readable for
    # old databases and scanner compatibility, but new fill handling uses this
    # table so a YES fill never pauses its complement NO quote.
    def set_side_pause(self, wallet: str, market_id: str, token_id: str, minutes: int):
        expires_at = time.time() + int(minutes or 0) * 60
        c = self.conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO side_pauses (wallet, market_id, token_id, expires_at) VALUES (?, ?, ?, ?)",
            (wallet, market_id, token_id, expires_at),
        )
        self.conn.commit()

    def is_side_paused(self, wallet: str, market_id: str, token_id: str) -> bool:
        c = self.conn.cursor()
        c.execute(
            "SELECT expires_at FROM side_pauses WHERE wallet = ? AND market_id = ? AND token_id = ?",
            (wallet, market_id, token_id),
        )
        row = c.fetchone()
        return bool(row and time.time() < row["expires_at"])

    # --- Merge operations and scoring observations ---

    def create_merge_operation(self, wallet, funder, condition_id, yes_asset_id, no_asset_id, requested_qty):
        c = self.conn.cursor()
        try:
            # Serialize the check+insert across this process and any other
            # process sharing the SQLite file.  This remains the reliable
            # fallback for a legacy database whose pre-existing duplicates
            # prevented creation of the partial unique index above.
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                """SELECT id FROM merge_operations
                WHERE wallet = ? COLLATE NOCASE
                  AND condition_id = ? COLLATE NOCASE
                  AND (status IN ('planned', 'submitted')
                       OR (status='confirmed' AND inventory_reconciled_at IS NULL)
                       OR (status='confirmed' AND error LIKE 'FIFO_LEDGER_PENDING:%'))
                ORDER BY id LIMIT 1""",
                (wallet, condition_id),
            )
            existing = c.fetchone()
            if existing:
                raise ActiveMergeOperationExists(wallet, condition_id, int(existing["id"]))
            c.execute(
                """INSERT INTO merge_operations
                (wallet, funder, condition_id, yes_asset_id, no_asset_id, requested_qty, status)
                VALUES (?, ?, ?, ?, ?, ?, 'planned')""",
                (wallet, funder, condition_id, yes_asset_id, no_asset_id, float(requested_qty)),
            )
            operation_id = c.lastrowid
            self.conn.commit()
            return operation_id
        except ActiveMergeOperationExists:
            self.conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            # A concurrent creator may have won through the partial index
            # between statements (or the index may be the only guard in a
            # future implementation).  Present one stable domain error.
            c.execute(
                """SELECT id FROM merge_operations
                WHERE wallet = ? COLLATE NOCASE
                  AND condition_id = ? COLLATE NOCASE
                  AND (status IN ('planned', 'submitted')
                       OR (status='confirmed' AND inventory_reconciled_at IS NULL)
                       OR (status='confirmed' AND error LIKE 'FIFO_LEDGER_PENDING:%'))
                ORDER BY id LIMIT 1""",
                (wallet, condition_id),
            )
            existing = c.fetchone()
            if existing:
                raise ActiveMergeOperationExists(
                    wallet, condition_id, int(existing["id"])
                ) from exc
            raise
        except Exception:
            self.conn.rollback()
            raise

    def update_merge_operation(
        self,
        operation_id,
        status,
        relayer_id="",
        tx_hash="",
        error="",
        realized_pnl=None,
        consumed_lots=None,
    ):
        c = self.conn.cursor()
        confirmed_at = time.time() if status == "confirmed" else None
        c.execute(
            """UPDATE merge_operations SET status=?, relayer_id=COALESCE(NULLIF(?, ''), relayer_id),
            tx_hash=COALESCE(NULLIF(?, ''), tx_hash), error=?, realized_pnl=COALESCE(?, realized_pnl),
            consumed_lots_json=COALESCE(?, consumed_lots_json),
            confirmed_at=COALESCE(confirmed_at, ?) WHERE id=?""",
            (
                status,
                relayer_id,
                tx_hash,
                error or "",
                realized_pnl,
                json.dumps(consumed_lots) if consumed_lots is not None else None,
                confirmed_at,
                operation_id,
            ),
        )
        self.conn.commit()

    def get_unresolved_merges(self, wallet=None, condition_id=None):
        c = self.conn.cursor()
        query = "SELECT * FROM merge_operations WHERE status IN ('planned', 'submitted')"
        params = []
        if wallet:
            query += " AND wallet = ? COLLATE NOCASE"
            params.append(wallet)
        if condition_id:
            query += " AND condition_id = ? COLLATE NOCASE"
            params.append(condition_id)
        query += " ORDER BY id"
        c.execute(query, params)
        return [self._hydrate_merge_row(dict(row)) for row in c.fetchall()]

    @staticmethod
    def _hydrate_merge_row(value):
        try:
            value["consumed_lots"] = json.loads(value.pop("consumed_lots_json") or "[]")
        except (TypeError, ValueError):
            value["consumed_lots"] = []
        return value

    def get_confirmed_merges(self, wallet=None):
        c = self.conn.cursor()
        query = "SELECT * FROM merge_operations WHERE status = 'confirmed'"
        params = []
        if wallet:
            query += " AND wallet = ? COLLATE NOCASE"
            params.append(wallet)
        query += " ORDER BY confirmed_at, id"
        c.execute(query, params)
        rows = []
        for row in c.fetchall():
            rows.append(self._hydrate_merge_row(dict(row)))
        return rows

    def get_confirmed_pending_merges(self, wallet=None):
        """Confirmed on-chain operations whose optional FIFO ledger needs retry."""
        c = self.conn.cursor()
        query = (
            "SELECT * FROM merge_operations "
            "WHERE status='confirmed' AND error LIKE 'FIFO_LEDGER_PENDING:%'"
        )
        params = []
        if wallet:
            query += " AND wallet = ? COLLATE NOCASE"
            params.append(wallet)
        query += " ORDER BY confirmed_at, id"
        c.execute(query, params)
        return [self._hydrate_merge_row(dict(row)) for row in c.fetchall()]

    def get_merge_inventory_barriers(self, wallet=None):
        """Confirmed operations whose Data API consumption is not yet visible."""
        c = self.conn.cursor()
        query = (
            "SELECT * FROM merge_operations "
            "WHERE status='confirmed' AND inventory_reconciled_at IS NULL"
        )
        params = []
        if wallet:
            query += " AND wallet = ? COLLATE NOCASE"
            params.append(wallet)
        query += " ORDER BY confirmed_at, id"
        c.execute(query, params)
        return [self._hydrate_merge_row(dict(row)) for row in c.fetchall()]

    def mark_merge_inventory_reconciled(self, operation_id):
        c = self.conn.cursor()
        c.execute(
            """UPDATE merge_operations SET inventory_reconciled_at=?
            WHERE id=? AND status='confirmed'""",
            (time.time(), operation_id),
        )
        self.conn.commit()

    def record_scoring_observation(self, wallet, condition_id, token_id, order_id, local_eligible, official_scoring):
        c = self.conn.cursor()
        c.execute(
            """INSERT OR REPLACE INTO scoring_observations
            (order_id, wallet, condition_id, token_id, local_eligible, official_scoring, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (order_id, wallet, condition_id, token_id, int(bool(local_eligible)), official_scoring, time.time()),
        )
        self.conn.commit()

    # --- Inventory Exit cycles and legs (V2) ---

    CYCLE_STATUSES = ("ACTIVE", "CLOSING", "BLOCKED", "CLOSED")
    _EXIT_CYCLE_UPDATE_COLUMNS = {
        "status", "trigger", "held_side", "held_asset_id", "managed_qty",
        "initial_qty", "cost_basis", "paired_qty", "direct_recovery",
        "merge_recovery", "advantage", "selected_route", "maker_window_until",
        "realized_recovered_collateral", "inventory_pnl", "holding_duration_sec",
        "closed_reason", "error", "opened_at", "closed_at",
    }

    def create_exit_cycle(
        self,
        wallet,
        condition_id,
        trigger="reward_fill",
        held_side="",
        held_asset_id="",
        qty=0.0,
        opened_at=None,
        cost_basis=None,
        paired_qty=0.0,
        held_assets_json="[]",
    ):
        """Create one ACTIVE exit cycle, raising ActiveExitCycleExists on duplicates.

        Serializes check+insert with BEGIN IMMEDIATE; the partial unique index
        ``uq_exit_cycle_active_wallet_condition`` is the cross-process fallback.
        """
        c = self.conn.cursor()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                """SELECT id FROM inventory_exit_cycles
                WHERE wallet = ? COLLATE NOCASE
                  AND condition_id = ? COLLATE NOCASE
                  AND status != 'CLOSED'
                ORDER BY id LIMIT 1""",
                (wallet, condition_id),
            )
            existing = c.fetchone()
            if existing:
                raise ActiveExitCycleExists(wallet, condition_id, int(existing["id"]))
            now = opened_at if opened_at is not None else time.time()
            c.execute(
                """INSERT INTO inventory_exit_cycles
                (wallet, condition_id, status, trigger, held_side, held_asset_id,
                 held_assets_json,
                 managed_qty, initial_qty, cost_basis, paired_qty, opened_at, updated_at)
                VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    wallet,
                    condition_id,
                    trigger,
                    held_side,
                    held_asset_id,
                    held_assets_json or "[]",
                    float(qty),
                    float(qty),
                    cost_basis,
                    float(paired_qty),
                    now,
                    now,
                ),
            )
            self.conn.commit()
            return c.lastrowid
        except ActiveExitCycleExists:
            self.conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ActiveExitCycleExists(wallet, condition_id) from exc
        except Exception:
            self.conn.rollback()
            raise

    def get_exit_cycle(self, cycle_id) -> dict | None:
        c = self.conn.cursor()
        c.execute("SELECT * FROM inventory_exit_cycles WHERE id = ?", (cycle_id,))
        row = c.fetchone()
        return dict(row) if row else None

    def get_active_exit_cycle(self, wallet, condition_id) -> dict | None:
        """The wallet's non-CLOSED cycle for a condition, or None."""
        c = self.conn.cursor()
        c.execute(
            """SELECT * FROM inventory_exit_cycles
            WHERE wallet = ? COLLATE NOCASE
              AND condition_id = ? COLLATE NOCASE
              AND status != 'CLOSED'
            ORDER BY id LIMIT 1""",
            (wallet, condition_id),
        )
        row = c.fetchone()
        return dict(row) if row else None

    def get_non_closed_exit_cycles(self, wallet=None):
        """Active/CLOSING/BLOCKED cycles (restart recovery + monitor view)."""
        c = self.conn.cursor()
        query = "SELECT * FROM inventory_exit_cycles WHERE status != 'CLOSED'"
        params: list = []
        if wallet:
            query += " AND wallet = ? COLLATE NOCASE"
            params.append(wallet)
        query += " ORDER BY opened_at, id"
        c.execute(query, params)
        return [dict(row) for row in c.fetchall()]

    def get_exit_cycle_condition_keys(self, wallet) -> set:
        """Distinct condition ids that ever had a cycle (adoption suppression)."""
        c = self.conn.cursor()
        c.execute(
            "SELECT DISTINCT condition_id FROM inventory_exit_cycles "
            "WHERE wallet = ? COLLATE NOCASE",
            (wallet,),
        )
        return {str(row["condition_id"]) for row in c.fetchall()}

    def update_exit_cycle(self, cycle_id, **fields):
        """Whitelisted column update; unknown columns are ignored."""
        allowed = {k: v for k, v in fields.items() if k in self._EXIT_CYCLE_UPDATE_COLUMNS}
        if not allowed:
            return
        allowed["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in allowed)
        c = self.conn.cursor()
        c.execute(
            f"UPDATE inventory_exit_cycles SET {sets} WHERE id = ?",
            (*allowed.values(), cycle_id),
        )
        self.conn.commit()

    def close_exit_cycle(
        self,
        cycle_id,
        closed_reason,
        realized_recovered_collateral=None,
        inventory_pnl=None,
        holding_duration_sec=None,
        error="",
    ):
        """Terminate a cycle as CLOSED with final economic summary."""
        now = time.time()
        fields = {"status": "CLOSED", "closed_reason": closed_reason or "", "error": error or ""}
        if realized_recovered_collateral is not None:
            fields["realized_recovered_collateral"] = float(realized_recovered_collateral)
        if inventory_pnl is not None:
            fields["inventory_pnl"] = float(inventory_pnl)
        if holding_duration_sec is not None:
            fields["holding_duration_sec"] = float(holding_duration_sec)
        fields["closed_at"] = now
        self.update_exit_cycle(cycle_id, **fields)

    def add_exit_leg(
        self,
        cycle_id,
        wallet,
        condition_id,
        kind,
        asset_id="",
        side="",
        qty=0.0,
        price=0.0,
        collateral=0.0,
        pnl=None,
        order_id="",
        relayer_id="",
        status="done",
        note="",
        exit_method="",
        created_at=None,
    ):
        c = self.conn.cursor()
        c.execute(
            """INSERT INTO inventory_exit_legs
            (cycle_id, wallet, condition_id, kind, exit_method, asset_id, side,
             qty, price, collateral, pnl, order_id, relayer_id, status, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cycle_id,
                wallet,
                condition_id,
                kind,
                exit_method,
                asset_id,
                side,
                float(qty),
                float(price),
                float(collateral),
                pnl,
                order_id,
                relayer_id,
                status,
                note,
                created_at if created_at is not None else time.time(),
            ),
        )
        self.conn.commit()
        return c.lastrowid

    def get_exit_legs(self, cycle_id) -> list[dict]:
        c = self.conn.cursor()
        c.execute(
            "SELECT * FROM inventory_exit_legs WHERE cycle_id = ? ORDER BY created_at, id",
            (cycle_id,),
        )
        return [dict(row) for row in c.fetchall()]

    _EXIT_LEG_UPDATE_COLUMNS = {
        "kind", "exit_method", "asset_id", "side", "qty", "price",
        "collateral", "pnl", "order_id", "relayer_id", "status", "note",
    }

    def update_exit_leg(self, leg_id, **fields):
        """Whitelisted leg column update (realized fill reconciliation)."""
        allowed = {
            k: v for k, v in fields.items() if k in self._EXIT_LEG_UPDATE_COLUMNS
        }
        if not allowed:
            return
        sets = ", ".join(f"{k} = ?" for k in allowed)
        c = self.conn.cursor()
        c.execute(
            f"UPDATE inventory_exit_legs SET {sets} WHERE id = ?",
            (*allowed.values(), leg_id),
        )
        self.conn.commit()

    def get_ledger_pending_exit_cycles(self, wallet=None) -> list[dict]:
        """CLOSED cycles whose exit legs still await fill reconciliation."""
        c = self.conn.cursor()
        query = (
            "SELECT * FROM inventory_exit_cycles "
            "WHERE status = 'CLOSED' AND error LIKE 'LEDGER_PENDING:%'"
        )
        params: list = []
        if wallet:
            query += " AND wallet = ? COLLATE NOCASE"
            params.append(wallet)
        query += " ORDER BY closed_at, id"
        c.execute(query, params)
        return [dict(row) for row in c.fetchall()]

    def get_exit_cycles(
        self,
        wallet=None,
        statuses=None,
        start=None,
        end=None,
        closed_after=None,
        limit=None,
        offset=0,
    ) -> list[dict]:
        """Cycle list for history/UI. Optional wallet / statuses / time filters."""
        clause = "WHERE 1=1"
        params: list = []
        if wallet:
            clause += " AND wallet = ? COLLATE NOCASE"
            params.append(wallet)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            clause += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if start:
            clause += " AND opened_at >= ?"
            params.append(start)
        if end:
            clause += " AND opened_at <= ?"
            params.append(end)
        if closed_after:
            clause += " AND closed_at >= ?"
            params.append(closed_after)
        query = f"SELECT * FROM inventory_exit_cycles {clause} ORDER BY opened_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params += [int(limit), int(offset)]
        c = self.conn.cursor()
        c.execute(query, params)
        return [dict(row) for row in c.fetchall()]

    def get_exit_legs_since(self, since: float, wallet=None) -> list[dict]:
        """Legs created at/after ``since`` across the ledger (dashboard)."""
        c = self.conn.cursor()
        if wallet:
            c.execute(
                """SELECT * FROM inventory_exit_legs
                WHERE created_at >= ? AND wallet = ? COLLATE NOCASE
                ORDER BY created_at, id""",
                (since, wallet),
            )
        else:
            c.execute(
                """SELECT * FROM inventory_exit_legs
                WHERE created_at >= ? ORDER BY created_at, id""",
                (since,),
            )
        return [dict(row) for row in c.fetchall()]

    def count_exit_cycles(self, wallet=None, statuses=None, start=None, end=None) -> int:
        clause = "WHERE 1=1"
        params: list = []
        if wallet:
            clause += " AND wallet = ? COLLATE NOCASE"
            params.append(wallet)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            clause += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if start:
            clause += " AND opened_at >= ?"
            params.append(start)
        if end:
            clause += " AND opened_at <= ?"
            params.append(end)
        c = self.conn.cursor()
        c.execute(f"SELECT COUNT(*) FROM inventory_exit_cycles {clause}", params)
        return int(c.fetchone()[0])

    # --- Eligible Markets ---

    def save_eligible_markets(self, markets: list[dict]):
        """Replace all eligible markets with new scan results."""
        c = self.conn.cursor()
        c.execute("DELETE FROM eligible_markets")
        now = time.time()
        for m in markets:
            c.execute(
                """INSERT INTO eligible_markets
                (market_id, token_id, market_name, outcome, market_competitiveness,
                 daily_reward, rewards_max_spread, rewards_min_size,
                 tick_size, tick_size_str, neg_risk,
                 reward_range_min, reward_range_max, spread_cents,
                 order_price, order_size, min_cost, end_date, tags, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    m.get("market_id", ""),
                    m.get("token_id", ""),
                    m.get("market_name", ""),
                    m.get("outcome", ""),
                    m.get("market_competitiveness", 0),
                    m.get("daily_reward", 0),
                    m.get("rewards_max_spread", 0),
                    m.get("rewards_min_size", 0),
                    m.get("tick_size", 0.01),
                    m.get("tick_size_str", "0.01"),
                    1 if m.get("neg_risk", False) else 0,
                    m.get("reward_range_min", 0),
                    m.get("reward_range_max", 1),
                    m.get("spread_cents", -1),
                    m.get("order_price", 0),
                    m.get("order_size", 0),
                    m.get("min_cost", 0),
                    m.get("end_date", ""),
                    json.dumps(m.get("tags", []) or []),
                    now,
                ),
            )
        self.conn.commit()

    def get_eligible_markets(self) -> list[dict]:
        """Get all eligible markets from last scan."""
        c = self.conn.cursor()
        c.execute("SELECT * FROM eligible_markets ORDER BY market_competitiveness DESC")
        out = []
        for row in c.fetchall():
            d = dict(row)
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except (ValueError, TypeError):
                d["tags"] = []
            out.append(d)
        return out

    def update_eligible_reward(self, condition_id: str, reward: float):
        """把实时复查到的每日奖励写回该市场在 eligible_markets 的所有 token 行。

        监控 Step3 发现在挂单市场的奖励跌破门槛时调用,让 /api/eligible 的展示值
        与低余额清仓的 get_market_daily_reward 都跟着变准。市场不在表里是 no-op。
        """
        if not condition_id:
            return
        c = self.conn.cursor()
        c.execute(
            "UPDATE eligible_markets SET daily_reward = ? WHERE market_id = ?",
            (float(reward), condition_id),
        )
        self.conn.commit()

    # --- Market Meta (condition_id -> name + slugs, persistent across scans) ---

    def upsert_market_meta(
        self, condition_id: str, name: str, market_slug: str = "", event_slug: str = ""
    ):
        """Insert or update market metadata. No-op for empty condition_id."""
        if not condition_id:
            return
        c = self.conn.cursor()
        c.execute(
            """INSERT OR REPLACE INTO market_meta
            (condition_id, name, market_slug, event_slug, updated_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                condition_id,
                name or "",
                market_slug or "",
                event_slug or "",
                time.time(),
            ),
        )
        self.conn.commit()

    def get_market_meta(self) -> dict:
        """Return {condition_id: {name, market_slug, event_slug}}."""
        c = self.conn.cursor()
        c.execute("SELECT condition_id, name, market_slug, event_slug FROM market_meta")
        return {
            row["condition_id"]: {
                "name": row["name"],
                "market_slug": row["market_slug"],
                "event_slug": row["event_slug"],
            }
            for row in c.fetchall()
        }

    # --- Blacklist (global, by condition_id) ---

    def add_to_blacklist(self, condition_id: str, note: str = ""):
        """加入(或更新)一个 condition_id 到全局黑名单。空 id 跳过。"""
        if not condition_id:
            return
        c = self.conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO blacklist (condition_id, note, added_at) "
            "VALUES (?, ?, ?)",
            (condition_id, note or "", time.time()),
        )
        self.conn.commit()

    def remove_from_blacklist(self, condition_id: str):
        c = self.conn.cursor()
        c.execute("DELETE FROM blacklist WHERE condition_id = ?", (condition_id,))
        self.conn.commit()

    def get_blacklist(self) -> list[dict]:
        """全部黑名单条目(最新在前),供管理界面用。"""
        c = self.conn.cursor()
        c.execute(
            "SELECT condition_id, note, added_at FROM blacklist ORDER BY added_at DESC"
        )
        return [dict(row) for row in c.fetchall()]

    def get_blacklist_ids(self) -> set:
        """黑名单 condition_id 集合,供拦截热路径快速 membership 判断。"""
        c = self.conn.cursor()
        c.execute("SELECT condition_id FROM blacklist")
        return {row["condition_id"] for row in c.fetchall()}

    # --- Net worth history (每钱包净值快照:启动 + 每日) ---

    def get_market_daily_reward(self, condition_id):
        """某市场在 eligible_markets 里的 daily_reward(市场奖励);不在表 -> None。"""
        c = self.conn.cursor()
        c.execute(
            "SELECT daily_reward FROM eligible_markets WHERE market_id = ? LIMIT 1",
            (condition_id,),
        )
        row = c.fetchone()
        return float(row["daily_reward"]) if row else None

    def get_min_order_cost(self):
        """当前 eligible_markets 里最便宜一单的 min_cost(能挂得起的最小本金);空表 -> None。"""
        c = self.conn.cursor()
        c.execute("SELECT MIN(min_cost) AS m FROM eligible_markets")
        row = c.fetchone()
        return float(row["m"]) if row and row["m"] is not None else None

    def upsert_daily_pnl(self, wallet, date, reward, rebate, sell_profit, loss, fee):
        """幂等写入某钱包某日盈亏行(主键 wallet+date,补漏/重算安全覆盖)。net 内部算。"""
        net = reward + rebate + sell_profit - loss - fee
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO daily_pnl (wallet, date, reward, rebate, sell_profit, loss, fee, net)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(wallet, date) DO UPDATE SET"
            " reward=excluded.reward, rebate=excluded.rebate, sell_profit=excluded.sell_profit,"
            " loss=excluded.loss, fee=excluded.fee, net=excluded.net,"
            " updated_at=strftime('%s','now')",
            (wallet, date, reward, rebate, sell_profit, loss, fee, net),
        )
        self.conn.commit()

    def get_daily_pnl(self, wallet, from_date, to_date) -> list[dict]:
        """某钱包 [from_date, to_date] 的每日盈亏行,日期升序。"""
        c = self.conn.cursor()
        c.execute(
            "SELECT date, reward, rebate, sell_profit, loss, fee, net FROM daily_pnl"
            " WHERE wallet = ? AND date >= ? AND date <= ? ORDER BY date",
            (wallet, from_date, to_date),
        )
        return [dict(r) for r in c.fetchall()]

    def get_daily_pnl_all(self, from_date, to_date) -> list[dict]:
        """全钱包按日期求和的每日盈亏,日期升序。"""
        c = self.conn.cursor()
        c.execute(
            "SELECT date, SUM(reward) reward, SUM(rebate) rebate, SUM(sell_profit) sell_profit,"
            " SUM(loss) loss, SUM(fee) fee, SUM(net) net FROM daily_pnl"
            " WHERE date >= ? AND date <= ? GROUP BY date ORDER BY date",
            (from_date, to_date),
        )
        return [dict(r) for r in c.fetchall()]
