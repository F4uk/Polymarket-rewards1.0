# 前端 v4 重构(7 屏 + 深度视觉升级)设计 / spec

> **历史记录：** 本文保留当时的前端设计；其中检查更新、`_update_modal.html`
> 与 `checkForUpdate` 内容已退役，不得据此恢复运行时自更新。

> 日期:2026-06-17
> 状态:待用户评审
> 背景:v4 做市策略接入(SP1-SP6)已全部完成、后端全链路上线。但前端仍是老单边单单时代的结构,v4 的核心名词(最低份数 / 单份奖励 / 订单厚度 / 累加厚度 / 奖励范围 / 有效价格 / 盘口价差)在界面上**无处展示**。本 spec 重做前端以适配 v4。父背景见记忆 [[v4-strategy-integration-roadmap]]、[[take-profit-position-driven]]。

## 零、目标与决策记录

**目标**:从第一性原理重排前端,做到全面、清晰、用户友好——既好配置,又不省关键信息;尤其要把 v4 的 7 个做市名词在界面上讲清楚。

**已与用户敲定的四个决策**(brainstorm 阶段,可视化伴侣):

1. **信息架构 = 7 屏精细拆分**:仪表盘 / 市场发现 / 挂单与持仓 / 历史 / 监控 / 配置 / 黑名单。每屏职责单一。
2. **梯队明细 = 按需预演**:市场发现页列表只常显市场级指标(扫描时已有,零额外开销);点某行"展开"才实时拉一次订单簿、跑一遍 v4 推演显示梯队。
3. **视觉 = 深度升级 + 深/浅主题可切换**:左侧边栏导航 + 一套 CSS 变量主题令牌 + 右上角主题切换(记 localStorage)。
4. **落地 = 一次性大改**:一个大 spec 覆盖后端补数 + 7 屏 + 新视觉。实现计划(writing-plans)再拆成多任务,但不分期上线。

## 一、7 个 v4 名词 → 数据来源映射

| # | 名词 | 含义 | 来源 | 现状 |
| --- | --- | --- | --- | --- |
| ① | 最低份数 | `rewards_min_size`(市场奖励合格的最小挂单份额) | 市场级,扫描时有 | eligible 行**已带** `rewards_min_size` |
| ② | 单份奖励 | `每日LP奖励 ÷ 最低份数`,与取档阈值比 | 市场级,扫描时可算 | filter 里算了 `per_share` 但**没存进 eligible 行** |
| ③ | 订单厚度 | 某盘口价位 `size ÷ min_size` | 梯队级,需实时订单簿 | 仅 `build_ladder` 内部计算,**即时丢弃** |
| ④ | 累加厚度 | 买一往下累计到该档的订单厚度之和 | 梯队级,需实时订单簿 | `build_ladder` 返回 `cumulative_thickness`,**未出接口** |
| ⑤ | 奖励范围 | `[中点 − d, 中点 + d]`,`d = rewards_max_spread × 0.01` | 市场级(需中点=最优买卖中值),扫描时可算 | `reward_price_range` 在下单时算,**仅留在 actions 文本里** |
| ⑥ | 有效价格 | 在奖励范围内且厚度≥1 的合格档价位 | 梯队级,需实时订单簿 | 下单后即 `place_limit_buy`,结构化数据**未出接口** |
| ⑦ | 盘口价差 | 实时 `best_ask − best_bid`(美分),对的是模板 `max_spread_cents` 上限 | 市场级,扫描时有 | scanner `_fetch_orderbooks` 存了 `spread` 但 filter **没存进 eligible 行** |

**两类落点**:①②⑤⑦ 是**市场级**——市场发现页列表常显;③④⑥ 是**梯队级**——展开行按需预演。注意 ⑤ 奖励范围与 ⑦ 盘口价差是两个不同的"spread":⑤ 用市场自带的 `rewards_max_spread` 算奖励合格带宽;⑦ 是真实盘口宽度,用于 eligibility 门槛。

## 二、全局设计系统

### 2.1 主题令牌(重写 `web/static/style.css`)

用 CSS 变量 + `:root[data-theme="light|dark"]`。两套调色板(已在 brainstorm 原型验证):

```
light: --bg #f4f5f7  --panel #fff  --panel2 #f8fafc  --border #e5e7eb  --rowline #f1f3f5
       --text #1f2937 --muted #8a93a0 --accent #4f46e5 --accentsoft #eef2ff
       --good #059669 --goodsoft #ecfdf5 --bad #dc2626 --badsoft #fef2f2 --warn #b45309 --warnsoft #fff7ed
dark:  --bg #0b0f14  --panel #101822 --panel2 #0f1620 --border #1c2733 --rowline #141d27
       --text #e6edf3 --muted #8b98a5 --accent #2dd4bf --accentsoft #13212b
       --good #34d399 --goodsoft #0d2a22 --bad #f87171 --badsoft #2a1414 --warn #fbbf24 --warnsoft #2a2410
```

所有页面颜色一律走变量,不写死。

### 2.2 骨架(重写 `web/templates/base.html`)

- **左侧边栏**:品牌 + 7 个导航项(图标 + 名),`active` 高亮主色;
- **顶栏**:引擎状态 pill + **主题切换钮**(`data-theme` 翻转 + `localStorage` 持久化,页面加载时先读取);版本号、检查更新、退出移到侧边栏底部或顶栏右侧;
- 保留 `_update_modal.html` include 与现有 `checkForUpdate` / 更新流程不变。

### 2.3 复用组件(CSS class,不引第三方库)

- **统计卡** `.stat-card`(标题 + 大数字 + 可选主色)
- **状态徽章** `.badge`:运行/停止、在赚奖励 ✓/✗、⚠️裸奔、阈值通过 ✓
- **数据表** `.data-table`:沿用现有可排序表头交互;新增**可展开行**(市场发现用)
- `app.js` 现有 `escapeHtml` / `showToast` / `marketCell` / `copyCid` 保留复用。

**不引入**任何前端框架 / 组件库 / 构建步骤(仍是 Jinja 模板 + 原生 JS + 单个 style.css,符合 PyInstaller 单文件打包约束)。

## 三、逐屏设计

每屏:目的 / 内容(表列) / 数据源 / 新或改。

### ① 仪表盘(控制台)
- **目的**:开关引擎 + 一眼健康度。**移走**现在挤在底部的 eligible 表(归② 市场发现)。
- **内容**:引擎状态条 + 全局按钮(全部启动 / 停止 / 重启)+ 手动步骤(扫描 / 分发挂单 / 启动监控 / 测试挂单 / 全部撤单);4 张统计卡(总挂单 / 总持仓 / 总盈亏 / 合格市场数);钱包状态表(地址 / 余额 / 挂单 / 持仓 / 状态 / 启停)。
- **数据**:`/api/dashboard`、`/api/wallets`、`/api/eligible`(取 `markets.length` 当"合格市场数")——均已有。
- **改**:纯前端;去掉 eligible 表块及其轮询脚本(迁至②)。

### ② 市场发现(新独立屏)
- **目的**:看筛出了啥、为什么合格、会怎么挂。
- **常显列**:# / 市场 / 方向 / 每日奖励 / **①最低份数** / **②单份奖励**(档 N · 阈值 ✓) / **⑤奖励范围 [min,max]** / **⑦盘口价差**(N¢ · 上限 ✓) / competitiveness / 展开钮。沿用扫描进度条 + "上次扫描"。
- **展开(按需预演)**:每侧(YES/NO)一张梯队表 = 档 / **⑥有效价格** / 盘口量 / **③订单厚度** / **④累加厚度** / 命中 tier_rules → 份额 / 金额;含灰色**跳过行**(厚度<1 仍累加 / 超出奖励范围),把"为什么这档不挂"显式画出。表尾:合计档数 / 份额 / 金额 + "与另一侧共享整市场敞口"说明。
- **数据**:列表 `/api/eligible`(**需后端补字段,见 §四.1**);展开 **新增 `/api/markets/<market_id>/ladder?wallet=<addr>`(见 §四.2)**。
- **新**:整屏新建;后端补字段 + 新预演接口。

### ③ 挂单与持仓(合并)
- **在挂买单表**:钱包 / 市场 / 方向 / Outcome / **⑥有效价格** / 数量 / 已成交 / **是否在赚奖励 ✓✗?** / 时间 / 操作(撤单 · 加黑名单)。
- **持仓表**:钱包 / 市场 / 方向 / **成本价**(Data API `avgPrice`,仅展示、非卖出依据) / 数量 / **现价** / **止损价** / **浮动盈亏**。
- 筛选(钱包 / 方向 / 在不在奖励区间)+ 批量(撤选中 / 一键撤买单)。
- **数据**:`/api/orders`、`/api/positions`——均已有,契约不动。
- **裸奔可见性**:成本能否从 `get_trades` 重建是 **monitor 才知道**的状态(⑤ 监控屏已有 `⚠️跳过·裸奔` / `⚠️跳过·无成本` 状态行),本屏**不臆造**该标志。若要在持仓表也标 ⚠️ 徽章,需给 `/api/positions` 增一个"成本可重建"标志位——列为**可选增强,不在核心范围**。
- **改**:合并 orders.html 现有两表;**删除 history.html 里重复的持仓表**。纯前端。

### ④ 历史(合并)
- **目的**:durable 记录"成交了什么、为什么卖"。
- **内容**:动作 / 卖单记录表 = 时间 / 钱包 / 市场 / 动作 / 方向 / 价格 / **原因** / **价格依据(逐笔成本构成)**;筛选(钱包 / 动作类型 / 日期)。动作类型标签沿用现有 `ACTION_LABELS`。
- **数据**:`/api/actions`——已有。
- **改**:以现 history.html 的"卖单理由"表为主体;**把现 logs.html 里重复的操作记录归到此屏**。纯前端。

### ⑤ 监控(诊断)
- **目的**:只留**实时每 tick 快照**(瞬时,非 durable)。
- **内容**:监控状态表 = 时间 / 钱包 / 市场 / 方向 / 价格 / 数量 / 已成交 / 阶段 / 判定 / 详情;每 4 秒刷新。
- **数据**:`/api/monitor-status`——已有。
- **改**:从现 logs.html 拆出"实时状态"部分,操作记录移走(归④)。纯前端套新皮。

### ⑥ 配置(保留 SP6 成果,换皮 + 标注)
- 钱包导入 + 模板绑定下拉 / 模板 CRUD / 策略参数表单 / **tier_rules 可视化嵌套编辑器** / 引擎参数表单——**功能全部保留不动**(SP6a/6b/6c 刚做完)。
- **数据**:`/api/templates`、`/api/settings`、`/api/wallets`——均已有,**不改契约**。
- **改**:只套新视觉骨架;给 v4 名词补行内说明(如"单份奖励阈值"旁注释其与最低份数取档的关系)。纯前端。

### ⑦ 黑名单(换皮)
- 加入表单(condition_id + 备注)+ 列表(市场 / 加入时间 / 备注 / 移除)+ 说明文本。
- **数据**:`/api/blacklist`——已有。纯前端套新皮。

## 四、后端改动(全部清单)

### 4.1 eligible 行补市场级字段

**现状**:`eligible_markets` 表**已带** `reward_range_min`/`reward_range_max` 列且 `save_eligible_markets` 已持久化,但 v4 的 `filter_for_template` 产出 per-token 行时**未填**这两个键,落库取默认 (0,1)。`spread_cents` 无列;`per_share` 可由已持久化的 `daily_reward` / `rewards_min_size` 现算,无需落库。

**改动**:

1. `engine/scanner.py::filter_for_template`:产出行处已有 `best_bid`/`best_ask`/`spread_val`/`max_spread_reward`,补算并写入行:
```python
midpoint = (best_bid + best_ask) / 2
rmin, rmax = reward_price_range(midpoint, max_spread_reward)   # 复用 engine/strategy.py
# eligible.append({...}) 里追加:
"reward_range_min": rmin, "reward_range_max": rmax, "spread_cents": spread_val * 100,
```
2. `models/database.py`:`eligible_markets` 加一列 `spread_cents REAL DEFAULT -1`(沿用 §161-170 现有 `PRAGMA table_info` + `ALTER TABLE` 迁移惯例);`save_eligible_markets` 的 INSERT 增列,`get_eligible_markets` 经 `SELECT *` 自动带出。reward_range 两列已存在,**无需迁移**。
3. `web/routes.py::api_eligible_markets`:对每行(memory 与 DB 两路都过)派生**单份奖励**注解,不落库——由已有的 `daily_reward` / `rewards_min_size` + 默认模板 `per_share_reward_thresholds` 算:
```python
per_share = daily_reward / min_size           # min_size>0 否则 None
bracket   = reward_bracket(min_size)          # 复用 engine/scanner.py
threshold = thresholds.get(str(bracket), 0.30)
# 附到响应行:per_share / per_share_bracket / per_share_threshold
```
eligible 列表是钱包无关的共享结果(一次扫描),阈值注解用默认模板口径即可;真正的 per-wallet 取档门槛在下单时把关。列表行只含通过筛选的市场,故 `per_share ≥ threshold` 恒真,前端展示 ✓。

### 4.2 新增预演接口

**路由**:`GET /api/markets/<market_id>/ladder?wallet=<address>`(`web/routes.py`,`@login_required`)。
**做什么**:取该钱包 API + 模板;对该 market 的每个 token 实时 `get_orderbook`,算 `best_bid/ask/midpoint/spread`、`reward_price_range`,跑预演,返回两侧逐档明细。预算口径与 `place_orders` 一致(`min(balance, max_exposure_usd) − 已持仓市值`、`max_exposure_shares − 已持仓份额`、§8 双边地板),**但只读不下单**。
**响应**(示意):
```json
{
  "market_id": "0x..", "market_name": "...",
  "sides": [
    {"outcome":"YES","token_id":"..","best_bid":0.54,"best_ask":0.56,"midpoint":0.55,
     "spread_cents":2.0,"reward_range":[0.51,0.59],
     "levels":[
       {"price":0.54,"size":150,"thickness":1.5,"cumulative_thickness":1.5,
        "in_range":true,"qualifies":true,"tier_index":0,"shares":100,"amount":54.0,"skip_reason":null},
       {"price":0.53,"size":80,"thickness":0.8,"cumulative_thickness":2.3,
        "in_range":true,"qualifies":false,"tier_index":null,"shares":0,"amount":0,"skip_reason":"厚度<1"},
       ...
     ],
     "total_tiers":3,"total_shares":450,"total_amount":234.0}
  ]
}
```

**纯函数支撑**(`engine/laddering.py`,新增、可单测):`preview_market_ladders(side_a, side_b, tier_rules, budget_usd, max_shares)` —— 镜像 `compute_market_ladders` 的预算分配(档序升序、同档先 a 后 b、§8 地板),但**保留每个 bid 价位**并标注 `thickness / cumulative_thickness / in_range / qualifies / tier_index / shares / skip_reason`。`skip_reason ∈ {null, "厚度<1", "超出奖励范围", "预算/敞口用尽"}`。现有 `build_ladder` / `resolve_tier_share` / `compute_market_ladders` **不改**(production 下单路径继续用它们),预演函数与之共用底层规则但独立返回 verbose 结构。

### 4.3 不改的后端
`/api/dashboard /wallets /orders /positions /actions /monitor-status /blacklist /templates /settings` 契约**全部不动**。`place_orders` 下单逻辑不动。

## 五、测试 / 验收

- **后端单测(pytest)**:
  - `preview_market_ladders`:档位/跳过原因/份额分配/§8 地板,与 `compute_market_ladders` 在"仅取合格档"投影下一致;
  - `filter_for_template`:断言 eligible 行新增 `reward_range_min`/`reward_range_max`/`spread_cents` 且数值正确(给定 bids/ask/min_size/reward);
  - `eligible_markets` 迁移:旧库无 `spread_cents` 列时启动自动补列、读写不崩;
  - `/api/eligible` 派生:断言响应行带 `per_share`/`per_share_bracket`/`per_share_threshold`,memory 与 DB 两路一致;
  - 新路由 `/api/markets/<id>/ladder` 契约:Flask test client + monkeypatch,验证 200 + 响应键齐全(沿用 `tests/test_settings_routes.py` / `tests/test_templates_routes.py` 模式)。
  - 基线 408 绿,新增测试后全绿。
- **前端**(无 JS 测试框架,沿用项目惯例):每个改动的模板抽出 `<script>` 跑 `node --check`;grep 中文完整性 + 确认无 BOM(`head -c 3` 检查);
- **人工走查**:`python app.py` 登录后逐屏点验:主题切换、市场发现展开预演、挂单/持仓合并、历史/监控去重、配置 tier 编辑器仍可用。

## 六、范围之外(YAGNI)

- **不做图表 / 时间序列**:历史奖励收益、PnL 曲线等需要后端长期留痕,而 `trades` 表现仅存 stop_loss、奖励收益未落库——**没有数据源**。"深度视觉升级"指视觉语言(侧边栏 / 令牌 / 卡片 / 徽章 / 主题),不含新增分析图表。**(请用户在复审时确认这条边界。)**
- 不做 WebSocket / SSE 实时推送,沿用现有轮询(dashboard 5s / monitor 4s / 扫描 2s)。
- 不改鉴权 / 加密 / funder 推导 / 引擎线程模型 / 下单与离场逻辑。
- 不引入前端框架与构建链(保打包约束)。
- 配置页功能不重做(SP6 刚完成),仅换皮 + 标注。

## 七、实现拆解建议(给 writing-plans)

虽是"一次性大改"单 spec,实现计划建议任务序:
1. 后端补数:`filter_for_template` 填 reward_range + spread_cents;`eligible_markets` 加 `spread_cents` 列(迁移);`/api/eligible` 派生 per_share 注解;`preview_market_ladders` 纯函数;新增 `/api/markets/<id>/ladder` 路由。各配单测 / 契约测试。
2. 全局骨架:重写 `style.css`(令牌)+ `base.html`(侧边栏 + 主题切换)。
3. 逐屏改/建:② 市场发现(新建,含展开预演)→ ① 仪表盘(去 eligible)→ ③ 挂单与持仓(合并)→ ④ 历史 + ⑤ 监控(拆分去重)→ ⑥ 配置 + ⑦ 黑名单(换皮)。
4. 收尾:全 `node --check` + 中文/BOM 核查 + `pytest` + 人工走查。

> ⚠️ 含中文的前端模板由主 agent 直接 Write/Edit(不经 subagent——历史上 subagent 反复把中文写成相似别字并加 BOM,见 [[v4-strategy-integration-roadmap]]),写后 `node --check` + grep 中文 + BOM 检查。
