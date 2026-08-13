# 前端 v4 重构(7屏 + 深度视觉升级 + 主题切换)Implementation Plan

> **历史记录：** 本文保留当时的前端设计；其中检查更新、`_update_modal.html`
> 与 `checkForUpdate` 内容已退役，不得据此恢复运行时自更新。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> ⚠️ **含中文的前端模板(`base.html` 及各 `web/templates/*.html`、`style.css`)必须由主 agent 直接 Write/Edit,不得派给 subagent**——历史上 subagent 反复把中文写成相似别字并给文件加 UTF-8 BOM(见记忆 [[v4-strategy-integration-roadmap]])。subagent 只用于纯后端任务(Task 1-4)与验证步骤。每个改过的模板:`node --check` 抽取的 `<script>` + grep 中文完整性 + 确认无 BOM。

**Goal:** 把前端从老单边单单结构重做为 7 屏、深/浅主题可切换的现代仪表盘,并在「市场发现」屏展示 v4 的 7 个做市名词(最低份数/单份奖励/订单厚度/累加厚度/奖励范围/有效价格/盘口价差)。

**Architecture:** 后端只补两类数据(scanner 给 eligible 行填 `reward_range`、加 `spread_cents` 列、`/api/eligible` 派生单份奖励;新增 `/api/markets/<id>/ladder` 按需预演接口,配纯函数 `preview_market_ladders`),其余接口契约不动、下单/离场逻辑不动。前端重写 `style.css`(CSS 变量主题令牌)+ `base.html`(左侧边栏 + 主题切换),7 屏套同一设计系统;不引前端框架/构建链(保 PyInstaller 单文件打包)。

**Tech Stack:** Python 3.12 / Flask / Jinja2 / 原生 JS / SQLite / pytest;前端无 JS 测试框架,验证靠 `node --check` + 人工走查。

**基线:** 当前 `git log` 顶端 `5cdc1b7`(spec 提交),`pytest` 应 408 passed。设计依据:`docs/superpowers/specs/2026-06-17-frontend-v4-redesign-design.md`。

---

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `engine/laddering.py` | 加 `preview_market_ladders`(verbose 预演,复用 `resolve_tier_share`) | 修改 |
| `tests/test_laddering_preview.py` | 预演纯函数单测 | 新建 |
| `engine/scanner.py` | `filter_for_template` 给 eligible 行填 `reward_range_min/max` + `spread_cents` | 修改 |
| `tests/test_eligible_fields.py` | eligible 行新字段单测 | 新建 |
| `models/database.py` | `eligible_markets` 加 `spread_cents` 列(迁移)+ save/get 带上 | 修改 |
| `tests/test_database.py` | spread_cents 落库往返 | 修改 |
| `web/routes.py` | 新增 `/api/markets/<id>/ladder`、`/api/eligible` 派生 per_share、`/markets` 页面路由 | 修改 |
| `tests/test_markets_route.py` | 预演路由 + eligible 派生契约 | 新建 |
| `web/static/style.css` | 重写为主题令牌 + 组件类 | 重写 |
| `web/templates/base.html` | 左侧边栏 + 主题切换 + 7 导航 | 重写 |
| `web/templates/markets.html` | ② 市场发现(新屏:列表 + 展开预演) | 新建 |
| `web/templates/dashboard.html` | ① 去掉 eligible 表、套新皮 | 修改 |
| `web/templates/orders.html` | ③ 挂单 + 持仓合并、套新皮 | 修改 |
| `web/templates/history.html` | ④ 去掉重复持仓表、只留动作记录、套新皮 | 修改 |
| `web/templates/logs.html` | ⑤ 只留实时状态、套新皮 | 修改 |
| `web/templates/config.html` | ⑥ 套新皮(SP6 功能不动) | 修改 |
| `web/templates/blacklist.html` | ⑦ 套新皮 | 修改 |

---

## Phase A — 后端补数(纯后端,TDD,可派 subagent)

### Task 1: `preview_market_ladders` 预演纯函数

**Files:**
- Modify: `engine/laddering.py`(尾部追加函数,复用现有 `resolve_tier_share`)
- Test: `tests/test_laddering_preview.py`

**设计:** 镜像 `compute_market_ladders` 的预算分配(档序升序、同档先 a 后 b、`resolve_tier_share` 同款规则与封顶),但**保留每个 bid 价位**并逐档标注。**不应用** §8 双边地板(那是整市场闸门;预演显示落地前的单侧梯队,另给一个 `double_sided_warn` 布尔)。`skip_reason ∈ {None, "超出奖励范围", "厚度<1", "超过最大档数", "规则判定不挂", "预算/敞口用尽"}`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_laddering_preview.py
"""tests/test_laddering_preview.py — preview_market_ladders verbose 预演。"""

from engine.laddering import preview_market_ladders


def _side(outcome="YES"):
    # min_size=100;中点 0.55、奖励范围 [0.51,0.59]
    return {
        "outcome": outcome,
        "token_id": "t_" + outcome,
        "min_size": 100,
        "reward_range_min": 0.51,
        "reward_range_max": 0.59,
        "best_bid": 0.54,
        "best_ask": 0.56,
        "spread_cents": 2.0,
        "bids": [
            {"price": 0.54, "size": 150},  # 厚 1.5 合格 -> 档0
            {"price": 0.53, "size": 80},   # 厚 0.8 <1 跳过
            {"price": 0.52, "size": 300},  # 厚 3.0 合格 -> 档1
            {"price": 0.51, "size": 120},  # 厚 1.2 合格 -> 档2
            {"price": 0.50, "size": 200},  # 价 < 0.51 超范围
        ],
    }


# 三档规则:每档都 min_size(=100 份)
TIER_RULES = [
    [{"upper": None, "action": {"type": "min_size"}}],
    [{"upper": None, "action": {"type": "min_size"}}],
    [{"upper": None, "action": {"type": "min_size"}}],
]


def test_levels_thickness_and_qualification():
    out = preview_market_ladders(_side(), None, TIER_RULES, 1000.0, 10000)
    levels = out["a"]["levels"]
    assert [round(l["thickness"], 2) for l in levels] == [1.5, 0.8, 3.0, 1.2, 2.0]
    assert [round(l["cumulative_thickness"], 1) for l in levels] == [1.5, 2.3, 5.3, 6.5, 8.5]
    # 合格档(in_range 且 厚度>=1):0.54/0.52/0.51
    assert [l["tier_index"] for l in levels] == [0, None, 1, 2, None]
    assert levels[1]["skip_reason"] == "厚度<1"
    assert levels[4]["skip_reason"] == "超出奖励范围"


def test_shares_allocated_to_qualifying_tiers():
    out = preview_market_ladders(_side(), None, TIER_RULES, 1000.0, 10000)
    placed = [l for l in out["a"]["levels"] if l["shares"] > 0]
    assert [l["price"] for l in placed] == [0.54, 0.52, 0.51]
    assert all(l["shares"] == 100 for l in placed)  # min_size 规则
    assert out["a"]["total_tiers"] == 3
    assert out["a"]["total_shares"] == 300


def test_budget_exhaustion_marks_skip():
    # 预算只够第 1 档(0.54*100=54);其余合格档应标"预算/敞口用尽"
    out = preview_market_ladders(_side(), None, TIER_RULES, 60.0, 10000)
    levels = out["a"]["levels"]
    assert levels[0]["shares"] == 100
    assert levels[2]["shares"] == 0 and levels[2]["skip_reason"] == "预算/敞口用尽"


def test_none_side_returns_none():
    out = preview_market_ladders(None, _side("NO"), TIER_RULES, 1000.0, 10000)
    assert out["a"] is None
    assert out["b"]["outcome"] == "NO"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_laddering_preview.py -q`
Expected: FAIL,`ImportError: cannot import name 'preview_market_ladders'`。

- [ ] **Step 3: 实现函数**

在 `engine/laddering.py` 末尾追加(`resolve_tier_share` 已在本模块):

```python
def _verbose_levels(side, tiers_k):
    """每个 bid 价位 -> 标注 dict;合格档(in_range 且厚度>=1)按序给 tier_index(<tiers_k)。"""
    min_size = side["min_size"]
    rmin, rmax = side["reward_range_min"], side["reward_range_max"]
    levels, running, tier_no = [], 0.0, 0
    for lvl in side["bids"]:
        price, size = float(lvl["price"]), float(lvl["size"])
        thickness = size / min_size if min_size > 0 else 0.0
        running += thickness
        in_range = rmin <= price <= rmax
        qualifies = in_range and thickness >= 1
        tier_index, skip_reason = None, None
        if not qualifies:
            skip_reason = "超出奖励范围" if not in_range else "厚度<1"
        elif tier_no < tiers_k:
            tier_index, tier_no = tier_no, tier_no + 1
        else:
            skip_reason = "超过最大档数"
        levels.append({
            "price": price, "size": size, "thickness": thickness,
            "cumulative_thickness": running, "in_range": in_range,
            "qualifies": qualifies, "tier_index": tier_index,
            "shares": 0, "amount": 0.0, "skip_reason": skip_reason,
        })
    return levels


def preview_market_ladders(side_a, side_b, tier_rules, budget_usd, max_shares):
    """两边共享敞口的逐档预演(只读、不下单)。

    分配口径与 compute_market_ladders 一致(档序升序、同档先 a 后 b、resolve_tier_share
    同款规则、按 USD/份额封顶);保留全部 bid 价位并标注 skip_reason。不应用 §8 双边地板。
    side_x:{"outcome","token_id","min_size","reward_range_min","reward_range_max",
            "best_bid","best_ask","spread_cents","bids":[{price,size}...]} 或 None。
    返回 {"a":<side|None>,"b":<side|None>};
    side = {"outcome","token_id","best_bid","best_ask","spread_cents","reward_range":[min,max],
            "levels":[...],"total_tiers","total_shares","total_amount","double_sided_warn"}。
    """
    tiers_k = len(tier_rules)
    out, lv = {}, {}
    for key, side in (("a", side_a), ("b", side_b)):
        if side is None:
            out[key], lv[key] = None, []
            continue
        lv[key] = _verbose_levels(side, tiers_k)
        out[key] = {
            "outcome": side.get("outcome", ""), "token_id": side.get("token_id", ""),
            "best_bid": side.get("best_bid"), "best_ask": side.get("best_ask"),
            "spread_cents": side.get("spread_cents"),
            "reward_range": [side["reward_range_min"], side["reward_range_max"]],
            "levels": lv[key], "total_tiers": 0, "total_shares": 0,
            "total_amount": 0.0, "double_sided_warn": False,
        }
    by_tier = {"a": {}, "b": {}}
    for key in ("a", "b"):
        for L in lv[key]:
            if L["tier_index"] is not None:
                by_tier[key][L["tier_index"]] = L
    spent_usd, spent_shares = 0.0, 0
    for j in range(tiers_k):
        for key, side in (("a", side_a), ("b", side_b)):
            if side is None:
                continue
            L = by_tier[key].get(j)
            if L is None:
                continue
            price, ct = L["price"], L["cumulative_thickness"]
            remaining_usd = budget_usd - spent_usd
            shares = resolve_tier_share(ct, tier_rules[j], price, side["min_size"], remaining_usd)
            if shares <= 0:
                L["skip_reason"] = "规则判定不挂"
                continue
            cap_usd = int(remaining_usd / price) if price > 0 else 0
            cap_shares = max_shares - spent_shares
            shares = min(shares, cap_usd, cap_shares)
            if shares <= 0:
                L["skip_reason"] = "预算/敞口用尽"
                continue
            L["shares"], L["amount"] = shares, price * shares
            spent_usd += price * shares
            spent_shares += shares
    for key in ("a", "b"):
        if out[key] is None:
            continue
        placed = [L for L in lv[key] if L["shares"] > 0]
        out[key]["total_tiers"] = len(placed)
        out[key]["total_shares"] = sum(L["shares"] for L in placed)
        out[key]["total_amount"] = sum(L["amount"] for L in placed)
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_laddering_preview.py -q`
Expected: PASS（4 passed）。

- [ ] **Step 5: 提交**

```bash
git add engine/laddering.py tests/test_laddering_preview.py
git commit -m "feat(laddering): preview_market_ladders 逐档预演纯函数"
```

---

### Task 2: `filter_for_template` 给 eligible 行填 reward_range + spread_cents

**Files:**
- Modify: `engine/scanner.py`（`filter_for_template` 内,产出 `eligible.append({...})` 处)
- Test: `tests/test_eligible_fields.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_eligible_fields.py
"""tests/test_eligible_fields.py — filter_for_template 产出行的市场级新字段。"""

from engine.scanner import MarketScanner


class _DB:
    def is_in_cooldown(self, wallet, cid):
        return False

    def get_blacklist_ids(self):
        return set()


def _candidate():
    # 中点=(0.54+0.56)/2=0.55;rewards_max_spread=4 -> 奖励范围 [0.51,0.59];spread=0.02
    return {
        "condition_id": "c1",
        "question": "Will XYZ resolve YES?",
        "market_reward": 40.0,
        "rewards_config": [{"rate_per_day": 40.0}],
        "rewards_max_spread": 4,
        "rewards_min_size": 100,
        "neg_risk": False,
        "tags": [],
        "end_date": "",
        "tokens": [{"token_id": "tY", "outcome": "YES", "price": 0.55}],
        "_orderbooks": {
            "tY": {
                "bids": [{"price": "0.54", "size": "150"}],
                "asks": [{"price": "0.56", "size": "150"}],
                "tick_size": "0.01",
                "spread": 0.02,
            }
        },
    }


def _template():
    return {
        "min_reward_usd": 1.0, "min_price_cents": 1, "max_price_cents": 99,
        "max_spread_cents": 3, "min_settlement_days": 0, "excluded_categories": [],
        "per_share_reward_thresholds": {"100": 0.30},
    }


def test_eligible_row_has_reward_range_and_spread():
    sc = MarketScanner(api=None, db=_DB(), wallet_address="0xabc")
    rows = sc.filter_for_template([_candidate()], _template(), "0xabc")
    assert len(rows) == 1
    r = rows[0]
    assert round(r["reward_range_min"], 4) == 0.51
    assert round(r["reward_range_max"], 4) == 0.59
    assert round(r["spread_cents"], 4) == 2.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_eligible_fields.py -q`
Expected: FAIL（`KeyError: 'reward_range_min'`）。

- [ ] **Step 3: 实现**

在 `engine/scanner.py` 顶部已 `from engine.strategy import ...`？没有则加导入:

```python
from engine.strategy import reward_price_range
```

在 `filter_for_template` 的 `for token in valid_tokens:` 循环里,已算出 `best_bid`/`best_ask`/`spread_val`,在 `eligible.append({` 字典里追加三个键(用该 token 的 `max_spread_reward`):

```python
                midpoint = (best_bid + best_ask) / 2
                rmin, rmax = reward_price_range(midpoint, max_spread_reward)
                eligible.append(
                    {
                        # ... 现有键不动 ...
                        "reward_range_min": rmin,
                        "reward_range_max": rmax,
                        "spread_cents": spread_val * 100,
                    }
                )
```

(`max_spread_reward`/`spread_val`/`best_bid`/`best_ask` 在该作用域已有。)

- [ ] **Step 4: 跑测试 + 全套**

Run: `python -m pytest tests/test_eligible_fields.py -q && python -m pytest -q`
Expected: 新测试 PASS;全套 = 基线 408 + Task1 的 4 + 本任务 1 = 413,无回归。

- [ ] **Step 5: 提交**

```bash
git add engine/scanner.py tests/test_eligible_fields.py
git commit -m "feat(scanner): eligible 行补 reward_range + spread_cents"
```

---

### Task 3: `eligible_markets` 加 `spread_cents` 列(迁移 + 落库)

**Files:**
- Modify: `models/database.py`（迁移块 ~§161-170 旁;`save_eligible_markets` INSERT;建表 DDL 可选加列)
- Test: `tests/test_database.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_database.py 追加
def test_eligible_markets_roundtrips_spread_cents(tmp_path):
    from models.database import Database
    db = Database(str(tmp_path / "t.db"))
    db.save_eligible_markets([{
        "market_id": "c1", "token_id": "tY", "market_name": "Q",
        "outcome": "YES", "market_competitiveness": 0.1, "daily_reward": 40.0,
        "rewards_max_spread": 4, "rewards_min_size": 100,
        "reward_range_min": 0.51, "reward_range_max": 0.59, "spread_cents": 2.0,
        "tags": [],
    }])
    rows = db.get_eligible_markets()
    assert len(rows) == 1
    assert round(rows[0]["spread_cents"], 4) == 2.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_database.py::test_eligible_markets_roundtrips_spread_cents -q`
Expected: FAIL（`sqlite3.OperationalError: table eligible_markets has no column named spread_cents` 或 KeyError）。

- [ ] **Step 3: 实现迁移 + 落库**

在 `models/database.py` 现有 `eligible_markets` 迁移块(已有 `min_cost`/`tags` 的 `PRAGMA table_info` + `ALTER`)后追加同款:

```python
        c.execute("PRAGMA table_info(eligible_markets)")
        em_cols3 = {row[1] for row in c.fetchall()}
        if em_cols3 and "spread_cents" not in em_cols3:
            c.execute("ALTER TABLE eligible_markets ADD COLUMN spread_cents REAL DEFAULT -1")
            self.conn.commit()
```

在 `save_eligible_markets` 的 INSERT 列清单加 `spread_cents`、VALUES 占位 `?` +1、参数加 `m.get("spread_cents", -1)`:

```python
                """INSERT INTO eligible_markets
                (market_id, token_id, market_name, outcome, market_competitiveness,
                 daily_reward, rewards_max_spread, rewards_min_size,
                 tick_size, tick_size_str, neg_risk,
                 reward_range_min, reward_range_max, spread_cents,
                 order_price, order_size, min_cost, end_date, tags, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
```
参数元组里 `m.get("reward_range_max", 1),` 之后插入 `m.get("spread_cents", -1),`。
(若建表 DDL 也想带上 `spread_cents REAL DEFAULT -1`,加在 `reward_range_max` 后;新库走 DDL、老库走 ALTER,两不误。)

- [ ] **Step 4: 跑测试**

Run: `python -m pytest tests/test_database.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add models/database.py tests/test_database.py
git commit -m "feat(db): eligible_markets 加 spread_cents 列(迁移+落库)"
```

---

### Task 4: `/api/markets/<id>/ladder` 预演路由 + `/api/eligible` 派生 per_share

**Files:**
- Modify: `web/routes.py`（新增预演路由;`api_eligible_markets` 加派生;新增 `/markets` 页面路由放 Task 6）
- Test: `tests/test_markets_route.py`

**预演路由逻辑:** 取 `?wallet=` 的 API(走 `_wallet_apis(wallet)`)+ 模板(`db.get_template_for(wallet)`);从内存/DB 的 eligible 找该 `market_id` 的 token 行,对每个 token `api.get_orderbook` 实时拉簿,组 side dict(含 `reward_range`/`spread`),按 `place_orders` 同款预算口径(`min(balance,max_exposure_usd)-已持仓市值`、`max_exposure_shares-已持仓份额`,用 `engine.positions.held_side_info`)调 `preview_market_ladders`。只读不下单。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_markets_route.py
"""tests/test_markets_route.py — 预演路由 + eligible per_share 派生契约。"""

import pytest
from web import routes


class _FakeAPI:
    def get_funder(self):
        return "0xfunder"

    def get_balance(self):
        return 1000.0

    def get_user_positions(self, funder):
        return []

    def get_orderbook(self, token_id):
        return {
            "bids": [{"price": "0.54", "size": "150"}, {"price": "0.52", "size": "300"}],
            "asks": [{"price": "0.56", "size": "150"}],
            "tick_size": "0.01",
        }


class _FakeDB:
    def get_template_for(self, addr):
        return {
            "tier_rules": [[{"upper": None, "action": {"type": "min_size"}}]],
            "max_exposure_usd": 250, "max_exposure_shares": 500,
            "per_share_reward_thresholds": {"100": 0.30},
        }

    def get_eligible_markets(self):
        return [{
            "market_id": "c1", "token_id": "tY", "outcome": "YES",
            "market_name": "Q", "daily_reward": 40.0, "rewards_min_size": 100,
            "rewards_max_spread": 4, "reward_range_min": 0.51,
            "reward_range_max": 0.59, "spread_cents": 2.0, "tags": [],
        }]


@pytest.fixture
def client(monkeypatch):
    routes.app.config["TESTING"] = True
    monkeypatch.setattr(routes, "db", _FakeDB())
    monkeypatch.setattr(routes, "manager", None)
    monkeypatch.setattr(routes, "_wallet_apis", lambda only=None: {"0xw": _FakeAPI()})
    monkeypatch.setattr(routes, "_enrich_rows", lambda rows, key: None)
    with routes.app.test_client() as c:
        with c.session_transaction() as s:
            s["logged_in"] = True
        yield c


def test_eligible_derives_per_share(client):
    r = client.get("/api/eligible")
    row = r.get_json()["markets"][0]
    assert round(row["per_share"], 4) == 0.40          # 40 / 100
    assert row["per_share_bracket"] == 100
    assert round(row["per_share_threshold"], 2) == 0.30


def test_ladder_preview_route(client):
    r = client.get("/api/markets/c1/ladder?wallet=0xw")
    assert r.status_code == 200
    data = r.get_json()
    assert data["market_id"] == "c1"
    side = data["sides"][0]
    assert side["outcome"] == "YES"
    assert "levels" in side and side["levels"][0]["thickness"] == 1.5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_markets_route.py -q`
Expected: FAIL（`/api/markets/...` 404；`per_share` KeyError）。

- [ ] **Step 3a: `api_eligible_markets` 加派生(两条返回路径都要)**

顶部导入处补:`from engine.scanner import reward_bracket`。
`api_eligible_markets` 有两处返回:`if not manager:` 早返回分支 + 主分支。加一个模块级 helper,在**两处** jsonify 前都调一次(测试里 `manager=None`,正好走早返回分支):

```python
def _derive_per_share(markets):
    """给 eligible 行派生单份奖励注解(不落库)。阈值取默认模板,取不到兜 0.30。"""
    try:
        thr = db.get_template(db.get_default_template_id()).get(
            "per_share_reward_thresholds", {}
        )
    except Exception:
        thr = {}
    for m in markets:
        ms = float(m.get("rewards_min_size", 0) or 0)
        m["per_share"] = (float(m.get("daily_reward", 0) or 0) / ms) if ms > 0 else None
        m["per_share_bracket"] = reward_bracket(int(ms)) if ms > 0 else None
        m["per_share_threshold"] = (
            float(thr.get(str(m["per_share_bracket"]), 0.30))
            if m.get("per_share_bracket") else None
        )
    return markets
```

早返回分支:`_enrich_rows(markets, "market_id")` 后加 `_derive_per_share(markets)` 再 jsonify。
主分支:`_enrich_rows(markets, "market_id")` 后同样加 `_derive_per_share(markets)`。

- [ ] **Step 3b: 新增预演路由**

在 `web/routes.py`（`# --- API: Eligible Markets ---` 段附近)加:

```python
@app.route("/api/markets/<market_id>/ladder", methods=["GET"])
@login_required
def api_market_ladder(market_id):
    from engine.laddering import preview_market_ladders
    from engine.strategy import reward_price_range
    from engine.positions import held_side_info

    wallet = request.args.get("wallet")
    apis = _wallet_apis(wallet)
    if not apis:
        return jsonify({"error": "钱包不可用"}), 404
    addr, api = next(iter(apis.items()))
    tmpl = db.get_template_for(addr)
    tier_rules = tmpl.get("tier_rules") or []
    max_exposure_usd = float(tmpl.get("max_exposure_usd", 250))
    max_exposure_shares = int(tmpl.get("max_exposure_shares", 500))

    # 从内存/DB eligible 找该市场的 token 行
    src = (manager.eligible_markets if (manager and manager.eligible_markets)
           else db.get_eligible_markets())
    rows = [dict(m) for m in src if m.get("market_id") == market_id]
    if not rows:
        return jsonify({"error": "市场不在 eligible 列表"}), 404

    try:
        positions = api.get_user_positions(api.get_funder())
    except Exception:
        positions = []
    _, held_value, held_shares = held_side_info(positions)
    balance = api.get_balance()
    budget = max(0.0, min(balance, max_exposure_usd) - held_value.get(market_id, 0.0))
    shares_budget = max(0, max_exposure_shares - int(held_shares.get(market_id, 0.0)))

    sides_in = []
    for r in rows[:2]:
        ob = api.get_orderbook(r["token_id"])
        bids = sorted(ob.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(ob.get("asks", []), key=lambda x: float(x["price"]))
        if not bids or not asks:
            continue
        bb, ba = float(bids[0]["price"]), float(asks[0]["price"])
        mid = (bb + ba) / 2
        rmin, rmax = reward_price_range(mid, float(r.get("rewards_max_spread", 2)))
        sides_in.append({
            "outcome": r.get("outcome", ""), "token_id": r["token_id"],
            "min_size": int(r.get("rewards_min_size", 0) or 0),
            "reward_range_min": rmin, "reward_range_max": rmax,
            "best_bid": bb, "best_ask": ba, "spread_cents": (ba - bb) * 100,
            "bids": bids,
        })
    a = sides_in[0] if sides_in else None
    b = sides_in[1] if len(sides_in) > 1 else None
    preview = preview_market_ladders(a, b, tier_rules, budget, shares_budget)
    sides = [preview[k] for k in ("a", "b") if preview.get(k)]
    return jsonify({
        "market_id": market_id,
        "market_name": rows[0].get("market_name", ""),
        "budget_usd": budget, "shares_budget": shares_budget,
        "sides": sides,
    })
```

- [ ] **Step 4: 跑测试 + 全套**

Run: `python -m pytest tests/test_markets_route.py -q && python -m pytest -q`
Expected: PASS;全套无回归。

- [ ] **Step 5: 提交**

```bash
git add web/routes.py tests/test_markets_route.py
git commit -m "feat(api): /api/markets/<id>/ladder 预演 + /api/eligible 派生单份奖励"
```

---

## Phase B — 全局设计系统(主 agent 直接写,勿派 subagent)

### Task 5: 重写 `style.css` 为主题令牌 + 组件类

**Files:**
- Rewrite: `web/static/style.css`

设计令牌与 brainstorm 原型一致。**保留**现有被 JS 引用的关键类名(`.btn`/`.btn-sm`/`.btn-success`/`.btn-danger`/`.btn-warning`/`.btn-primary`、`.data-table`、`.status.running`/`.status.stopped`、`.card`、`.flash`、`.config-section`、`profit`/`loss`),改为走变量;新增侧边栏与组件类。

- [ ] **Step 1: 写令牌 + 骨架样式**

`web/static/style.css` 顶部:

```css
:root[data-theme="light"]{--bg:#f4f5f7;--panel:#fff;--panel2:#f8fafc;--border:#e5e7eb;--rowline:#f1f3f5;--text:#1f2937;--muted:#8a93a0;--accent:#4f46e5;--accentsoft:#eef2ff;--good:#059669;--goodsoft:#ecfdf5;--bad:#dc2626;--badsoft:#fef2f2;--warn:#b45309;--warnsoft:#fff7ed;}
:root[data-theme="dark"]{--bg:#0b0f14;--panel:#101822;--panel2:#0f1620;--border:#1c2733;--rowline:#141d27;--text:#e6edf3;--muted:#8b98a5;--accent:#2dd4bf;--accentsoft:#13212b;--good:#34d399;--goodsoft:#0d2a22;--bad:#f87171;--badsoft:#2a1414;--warn:#fbbf24;--warnsoft:#2a2410;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif}
.layout{display:flex;min-height:100vh}
.sidebar{width:172px;background:var(--panel2);border-right:1px solid var(--border);padding:16px 0;flex-shrink:0}
.sidebar .brand{padding:0 18px 16px;font-weight:700;color:var(--accent)}
.sidebar a{display:block;padding:9px 18px;color:var(--muted);text-decoration:none}
.sidebar a.active{background:var(--accentsoft);color:var(--accent);font-weight:600;border-left:3px solid var(--accent)}
.sidebar .sb-foot{margin-top:18px;padding:0 18px;color:var(--muted);font-size:12px}
.content{flex:1;padding:18px 24px;min-width:0}
.theme-toggle{border:1px solid var(--border);background:var(--panel);color:var(--text);padding:6px 12px;border-radius:8px;cursor:pointer}
```

组件类(统计卡/徽章/表/按钮全部改用变量):

```css
.stat-cards{display:flex;gap:12px;margin:14px 0}
.stat-card{flex:1;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.stat-card h3{margin:0;color:var(--muted);font-size:13px;font-weight:500}
.stat-card .v{font-size:24px;font-weight:700;margin-top:4px}
.data-table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.data-table th{text-align:left;color:var(--muted);font-weight:500;padding:9px 10px;border-bottom:1px solid var(--border);font-size:13px}
.data-table td{padding:9px 10px;border-bottom:1px solid var(--rowline)}
.badge{padding:2px 8px;border-radius:10px;font-size:11px}
.badge.ok{background:var(--goodsoft);color:var(--good)}
.badge.no{background:var(--badsoft);color:var(--bad)}
.badge.warn{background:var(--warnsoft);color:var(--warn)}
.badge.acc{background:var(--accentsoft);color:var(--accent)}
.status.running{color:var(--good)} .status.stopped{color:var(--bad)}
.profit{color:var(--good)} .loss{color:var(--bad)}
.btn{border:1px solid var(--border);background:var(--panel);color:var(--text);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px}
.btn-sm{padding:4px 9px;font-size:12px}
.btn-primary,.btn-success{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn-danger{background:var(--bad);color:#fff;border-color:var(--bad)}
.btn-warning{background:var(--warn);color:#fff;border-color:var(--warn)}
.flash{background:var(--warnsoft);color:var(--warn);padding:8px 12px;border-radius:8px;margin-bottom:10px}
.config-section{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:14px}
```

(其余旧类按需移植为变量版;删除写死颜色。)

- [ ] **Step 2: 验证(人工,无单测)**

`python app.py` 登录后任意页应能渲染、不报 CSS 404;深浅切换在 Task 6 接好后再验色。
此步先确认无语法破坏:`node -e "process.exit(0)"` 跳过(CSS 无 lint);改为目视检查文件无截断。

- [ ] **Step 3: 提交**

```bash
git add web/static/style.css
git commit -m "feat(ui): style.css 重写为深浅主题令牌 + 组件类"
```

---

### Task 6: 重写 `base.html`(侧边栏 + 主题切换 + 7 导航)+ `/markets` 路由 + markets 占位

**Files:**
- Rewrite: `web/templates/base.html`
- Modify: `web/routes.py`（加 `/markets` 页面路由)
- Create: `web/templates/markets.html`（占位,Task 7 填满)

- [ ] **Step 1: 加 `/markets` 路由**

`web/routes.py` 在 `config_page` 等页面路由旁加:

```python
@app.route("/markets")
@login_required
def markets_page():
    return render_template("markets.html")
```

- [ ] **Step 2: markets.html 占位**

```html
{% extends "base.html" %}
{% block content %}<h1>市场发现</h1><p>（Task 7 实现）</p>{% endblock %}
```

- [ ] **Step 3: 重写 base.html**

```html
<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket 做市助手</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
    <script>
      // 主题在 <head> 早置,避免刷新闪烁
      (function(){var t=localStorage.getItem('mm-theme')||'light';document.documentElement.dataset.theme=t;})();
    </script>
</head>
<body>
<div class="layout">
    <nav class="sidebar">
        <div class="brand">◆ 做市助手</div>
        <a href="{{ url_for('dashboard') }}" class="{% if request.endpoint=='dashboard' %}active{% endif %}">仪表盘</a>
        <a href="{{ url_for('markets_page') }}" class="{% if request.endpoint=='markets_page' %}active{% endif %}">市场发现</a>
        <a href="{{ url_for('orders_page') }}" class="{% if request.endpoint=='orders_page' %}active{% endif %}">挂单与持仓</a>
        <a href="{{ url_for('history_page') }}" class="{% if request.endpoint=='history_page' %}active{% endif %}">历史</a>
        <a href="{{ url_for('logs_page') }}" class="{% if request.endpoint=='logs_page' %}active{% endif %}">监控</a>
        <a href="{{ url_for('config_page') }}" class="{% if request.endpoint=='config_page' %}active{% endif %}">配置</a>
        <a href="{{ url_for('blacklist_page') }}" class="{% if request.endpoint=='blacklist_page' %}active{% endif %}">黑名单</a>
        <div class="sb-foot">
            <button class="theme-toggle" onclick="toggleTheme(this)">切换主题</button>
            <div style="margin-top:10px"><a href="#" onclick="checkForUpdate(this);return false;">检查更新</a></div>
            <div style="margin-top:6px">v{{ app_version }}</div>
            <div style="margin-top:6px"><a href="{{ url_for('logout') }}">退出</a></div>
        </div>
    </nav>
    {% include "_update_modal.html" %}
    <main class="content">
        {% with messages = get_flashed_messages() %}
        {% if messages %}<div class="flash-messages">{% for msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}</div>{% endif %}
        {% endwith %}
        {% block content %}{% endblock %}
    </main>
</div>
<script src="{{ url_for('static', filename='app.js') }}"></script>
<script>
function toggleTheme(btn){var h=document.documentElement;var t=h.dataset.theme==='light'?'dark':'light';h.dataset.theme=t;localStorage.setItem('mm-theme',t);}
</script>
{% block scripts %}{% endblock %}
</body>
</html>
```

- [ ] **Step 4: 验证**

- `python app.py` 登录:侧边栏 7 项可点、各页不 500;点"切换主题"全站变色并刷新后保持。
- 抽 base.html 的两段内联 `<script>`(纯 JS、无 Jinja)到 `/tmp/b.js`,`node --check /tmp/b.js` 应通过。
- `head -c3 web/templates/base.html | od -An -tx1 | grep -q "ef bb bf" && echo "BOM!" || echo "no BOM"` 应输出 `no BOM`。
- `git grep -n "做市助手" web/templates/base.html` 确认中文未损。

- [ ] **Step 5: 提交**

```bash
git add web/templates/base.html web/templates/markets.html web/routes.py
git commit -m "feat(ui): base.html 侧边栏+主题切换+7导航；加 /markets 路由"
```

---

## Phase C — 逐屏(主 agent 直接写模板;每屏验证同款:渲染/node --check/grep 中文/BOM)

### Task 7: ② 市场发现(`markets.html` 列表 + 展开预演)

**Files:** Rewrite `web/templates/markets.html`

**内容(对应 spec §三②):**
- 顶部:扫描进度行 + "上次扫描…"(移植 dashboard 原有 `scan-progress`/`scan-time` 逻辑与轮询)。
- 表 `data-table`,列:`#` / 市场(`marketCell`) / 方向 `outcome` / 每日奖励 `daily_reward` / 最低份数 `rewards_min_size` / 单份奖励 `per_share`(后接 `档{per_share_bracket}·≥{per_share_threshold} ✓`) / 奖励范围 `[reward_range_min, reward_range_max]` / 盘口价差 `spread_cents`¢ / competitiveness / 展开钮。
- 展开:点钮 `fetch('/api/markets/'+market_id+'/ladder?wallet='+firstWallet)`,把返回 `sides[].levels` 渲染成子表(档/有效价格 `price`/盘口量 `size`/订单厚度 `thickness`/累加厚度 `cumulative_thickness`/命中→份额 `shares`/金额 `amount`);`skip_reason` 非空的行加 class `skip` 灰显并在"命中→份额"格显示原因;表尾 `total_tiers/total_shares/total_amount`。
- `wallet` 取自 `/api/wallets` 第一个 enabled(展开前先拉一次缓存)。

**数据源:** `/api/eligible`(列表)、`/api/markets/<id>/ladder?wallet=`(展开)、`/api/wallets`(取钱包)。

- [ ] **Step 1: 写 markets.html**（完整模板,含 `{% block scripts %}` 内联 JS:`renderMarkets()` 渲染常显行 + `toggleLadder(mid, btn)` fetch 并插入/收起子行 + 复用 dashboard 的扫描轮询函数。常显列用 `m.per_share!=null?('$'+m.per_share.toFixed(2)):'—'` 兜空;奖励范围 `[${m.reward_range_min?.toFixed(2)}, ${m.reward_range_max?.toFixed(2)}]`;盘口价差 `${m.spread_cents?.toFixed(0)}¢`。子表行:`skip_reason?'<tr class=\"skip\">…'+skip_reason:'<tr>…'+shares`。）参考 spec §三② 与 brainstorm 原型 `markets-full.html` 的列与子表结构。
- [ ] **Step 2: 验证** 渲染该页;有 eligible 数据时点"展开"应出梯队子表(引擎在跑且钱包可用);`node --check` 抽出的 `<script>`;grep 列名中文(最低份数/单份奖励/奖励范围/盘口价差/订单厚度/累加厚度/有效价格)齐全;无 BOM。
- [ ] **Step 3: 提交** `git add web/templates/markets.html && git commit -m "feat(ui): 市场发现页(v4 指标 + 按需梯队预演)"`

---

### Task 8: ① 仪表盘瘦身(去 eligible 表 + 套新皮)

**Files:** Modify `web/templates/dashboard.html`

- 删除 eligible 表块(`<h2>符合条件的市场…</h2>`、`#eligible-table`、`#scan-time`、`#scan-progress`)及其 JS(`sortEligible`/`renderEligibleTable`/`pollScanProgress`/`scanMarkets`/`refreshEligible`/相关 `setInterval`)——这些迁到 markets.html。
- 保留:控制按钮、手动调试按钮、4 张统计卡(改 `stat-cards`/`stat-card` 结构;新增"合格市场数"卡,值取 `/api/eligible` 的 `markets.length`)、钱包状态表。
- "扫描市场"按钮保留(POST `/api/engine/scan`)但跳转/提示去 markets 页看结果(或保留触发、结果在 markets 页)。
- [ ] **Step 1** 改 dashboard.html。 **Step 2** 验证(渲染/统计卡/钱包表/node --check/grep/BOM)。 **Step 3** `git commit -m "feat(ui): 仪表盘瘦身为控制台(eligible 迁出)"`

---

### Task 9: ③ 挂单与持仓合并

**Files:** Modify `web/templates/orders.html`

- 现 orders.html 已有"挂单表 + 持仓表",保留两表、套新皮(`data-table`/`badge`)。
- 挂单表"是否在赚奖励"列用 `scoring` 渲染徽章(✓ `badge ok` / ✗ `badge no` / ? 中性)。
- 持仓表盈亏用 `profit`/`loss`;成本价注明仅展示。
- 导航标签已是"挂单与持仓"(base 改过)。
- [ ] **Step 1** 改 orders.html。 **Step 2** 验证。 **Step 3** `git commit -m "feat(ui): 挂单与持仓合并页换皮"`

---

### Task 10: ④ 历史 / ⑤ 监控 去重 + 换皮

**Files:** Modify `web/templates/history.html`、`web/templates/logs.html`

- `history.html`:**删除**重复的持仓表(已在 orders 页);只留"动作/卖单记录表"(`/api/actions`),套新皮。
- `logs.html`:**删除**操作记录表(已归 history);只留"实时监控状态表"(`/api/monitor-status`,4s 轮询),套新皮。
- [ ] **Step 1** 改两文件。 **Step 2** 验证两页。 **Step 3** `git commit -m "feat(ui): 历史/监控去重换皮"`

---

### Task 11: ⑥ 配置 / ⑦ 黑名单 换皮

**Files:** Modify `web/templates/config.html`、`web/templates/blacklist.html`

- **只换视觉**:把容器/表/按钮类切到新组件类,**不动** SP6 的钱包导入、模板 CRUD、tier_rules 可视化编辑器、引擎参数表单的任何 JS 逻辑与 DOM id/class(避免破坏 `serializeTierRules` 等)。给 v4 名词加行内说明(如"单份奖励阈值"旁注其与最低份数取档关系)。
- blacklist.html:表/表单套新皮。
- [ ] **Step 1** 改两文件(谨慎:config.html 复杂,仅改外层呈现类,保留所有 `id`)。 **Step 2** 验证:配置页模板切换、tier 编辑器增删档、保存仍工作;`node --check` 抽出脚本;grep 中文;BOM。 **Step 3** `git commit -m "feat(ui): 配置/黑名单换皮(功能不动)"`

---

### Task 12: 收尾验证

**Files:** 无(纯验证)+ 按需补 `app.js` 共享函数

- [ ] **Step 1: 全套后端测试**
Run: `python -m pytest -q`
Expected: 绿(基线 408 + Task1 4 + Task2 1 + Task3 1 + Task4 2 = 416 左右,按实际核对、无回归)。
- [ ] **Step 2: 全模板静态检查**
对每个改过的模板抽 `<script>` 跑 `node --check`;`git grep -nP "[\x{4e00}-\x{9fff}]" web/templates | head`(目视中文无别字);批量 BOM 检查:
```bash
for f in web/templates/*.html web/static/style.css; do head -c3 "$f" | od -An -tx1 | grep -q "ef bb bf" && echo "BOM: $f"; done
```
Expected: 无 BOM 输出。
- [ ] **Step 3: 人工走查**(`python app.py` 登录)
逐项勾:① 主题切换深/浅并刷新保持;② 市场发现常显 7 列中的市场级 4 指标、展开出梯队子表(含灰色跳过行);③ 挂单与持仓两表 + 在赚奖励徽章;④ 历史只剩动作记录(无持仓表);⑤ 监控只剩实时状态(无操作记录);⑥ 配置 tier 编辑器仍可增删档+保存;⑦ 黑名单增删。
- [ ] **Step 4: 最终提交**(若 Step 1-3 有修补)
```bash
git add -A web/ tests/ engine/ models/ && git commit -m "chore(ui): 前端 v4 重构收尾验证修补"
```

---

## Self-Review(对照 spec)

- **spec §一 7 名词**:①最低份数②单份奖励⑤奖励范围⑦盘口价差 → Task 2/4 + Task 7 常显列;③订单厚度④累加厚度⑥有效价格 → Task 1 + Task 4 + Task 7 展开子表。✓ 全覆盖。
- **spec §二 设计系统**:令牌/侧边栏/主题切换/组件 → Task 5/6。✓
- **spec §三 7 屏**:① Task8 ② Task7 ③ Task9 ④⑤ Task10 ⑥⑦ Task11。✓
- **spec §四 后端**:4.1 → Task2/3 + Task4(派生);4.2 → Task1 + Task4;4.3 不改 → 仅 Task4 新增路由、未碰既有路由。✓
- **spec §六 YAGNI**:无图表任务、无 WebSocket、不碰鉴权/下单 → 计划中无对应任务,符合。✓
- **类型/命名一致**:`preview_market_ladders` 返回键(`levels`/`thickness`/`cumulative_thickness`/`shares`/`skip_reason`/`total_*`)在 Task1 定义、Task4 路由透传、Task7 前端按此渲染,一致。eligible 派生键(`per_share`/`per_share_bracket`/`per_share_threshold`)Task4 定义、Task7 用,一致。
- **占位符扫描**:后端任务均含完整测试+实现代码;前端任务给出确切文件/列/数据绑定/JS 函数名/验证命令,并指向 spec §三 与原型,无 "TODO/TBD"。
