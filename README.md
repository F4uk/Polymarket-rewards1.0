# Polymarket 做市奖励工具 · Polymarket Market-Making Rewards Bot

> 本地单用户的 Polymarket 自动做市工具，通过挂单赚取流动性奖励（liquidity rewards）。
> A local, single-user app that automates reward-farming market making on Polymarket.

![version](https://img.shields.io/badge/version-8.2.0-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![flask](https://img.shields.io/badge/flask-3.1-black)
![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)

中文说明在前，English notes follow each section.

---

## ⚠️ 风险免责声明 / Disclaimer

**中文**

- 本工具会使用你的**真实钱包私钥**在 Polymarket 上**下真实订单、动用真实资金**。
- 做市与持仓本身存在亏损风险（行情、滑点、未成交、平台规则变化等）。本工具不保证盈利，也不构成任何投资建议。
- 你需自行承担使用本工具产生的一切后果，包括但不限于资金损失。请先用小额资金充分测试后再决定是否加大投入。
- 请遵守 Polymarket 的服务条款及你所在司法辖区的法律法规。

**English**

This software places **real orders with real funds** on Polymarket using **your real wallet private key**. Market making carries financial risk and is **not** investment advice. Use at your own risk, test with small amounts first, and comply with Polymarket's Terms of Service and your local laws.

---

## 这是什么 / What is this

面向**非技术用户**设计：下载安装包 → 双击运行 → 浏览器自动打开 → 设置密码并录入钱包私钥 → 一键启动，程序自动扫描符合奖励条件的市场、挂单、监控成交并执行止盈/止损。

Built for **non-technical users**: install, double-click, a browser opens, you set a password and enter your wallet private key, then the app scans reward-eligible markets, places orders, and monitors fills with automatic take-profit / stop-loss.

技术栈 / Stack：Python + Flask（本地 Web 应用，仅监听 `127.0.0.1`），通过 [`py-clob-client-v2`](https://pypi.org/project/py-clob-client-v2/) 访问 Polymarket CLOB；打包为单个 Windows 安装程序（PyInstaller + Inno Setup）。

---

## 功能特性 / Features

- **自动 / 手动两种模式**
  - *自动*：发现（慢节奏，默认 4 小时一轮）→ 下单（快节奏，刷新订单簿后挂单）→ 监控的全流程循环。
  - *手动*：分步执行「扫描 / 分发挂单 / 启动监控」，由你掌控每一步。
- **两种挂单模式，按模板切换**（配置 → 挂单参数 → 挂单模式）：
  - *断层单档*（默认）：按买单簿「相邻档最大价差」把市场归为宽断层 / 中断层 / 密盘三级之一，再从买一往下取第一个「风险系数 > 该级门槛」的价位，**只挂这一档一单**。三级各有一道闸门：断层上方到买一的风险系数之和不够就整个市场不挂。
  - *厚墙*（v1.0.15 老策略）：在买单簿里找到第一堵挂量够厚的墙，把买单挂在**它下面一档**，让那堵墙挡住砸盘。1 美分盘看单档挂量是否超「厚墙阈值」（默认 2000），0.1 美分盘从买一往下累加、累计超「累计阈值」（默认 6000）即停。找到第一堵墙就定死，它的下一档不合格就整个市场不挂。挂多少份由「挂单份数」决定（最低合格份额 / 按美元上限 / 全额余额），此模式下档位模块只用来圈定做哪些份额档的市场。
- **档位模块（按最低奖励份额精确匹配）**：挂单参数按「市场最低奖励份额」逐档配置，每个模块自带挂单份数（可大于该市场最低份数）、三级选档门槛、高位系数和门槛、金额数值表。市场的最低份额必须**恰好等于**某个已启用模块的档位值才会做，没有模块对得上就不挂。
- **奖励做市选品**：按最低奖励、结算天数、价格区间、买卖价差、品类白名单、冷却时间过滤；并按竞争度（competitiveness）从低到高优先下单（竞争越少奖励份额越大）。
- **每钱包独立策略模板**：多模板增删改，每个钱包绑定自己的参数模板（阈值 / 敞口 / 离场 / 档位模块各自可调）。
- **持仓驱动的两段式离场**：成本价由真实 CLOB 成交逐笔重建（FIFO 净额，绝不用 Data API avgPrice），每个持仓**始终只挂一张**卖单，且**永不低于成本卖出**。成本 ≤ 买一（浮盈）挂卖一做 maker 吃价差；成本 > 买一（保本 / 套牢）挂成本价等回本。唯一认亏出口是强平兜底：亏损达到止损线（按比例，默认成本的 20%；或按固定美分）时市价清仓。成本无法可靠重建时**跳过并显著告警**（⚠️裸奔，绝不按不确定成本卖出，自愈式重试）。
- **账户净值曲线**：引擎运行期间每个钱包每天记一次净值（现金 + 持仓市值），「资产曲线」页看历史走势，也可查任意某天的净值。
- **全局黑名单**：在下单 / 扫描 / 监控三处统一拦截不想参与的市场。
- **私钥本地加密**：钱包私钥用 AES-256-GCM 加密，密钥由你的密码经 PBKDF2（60 万次迭代）派生，仅存在于内存。

> Gap-tier single-rung placement with per-tier modules keyed by the market's minimum reward size (each module carries its own share count and gating thresholds — a market whose minimum size matches no enabled module is never placed), per-wallet strategy templates, position-driven exit that never sells below a cost reconstructed from real fills, a daily net-worth history per wallet, a global blacklist enforced at three choke points, and AES-256-GCM encrypted keys held only in memory.

---

## 界面 / UI

8 个页面、左侧边栏导航，支持**深 / 浅主题切换**（侧边栏底部按钮，选择记在浏览器本地，刷新保持）。

| 页面 | 内容 |
| --- | --- |
| 仪表盘 | 引擎开关 / 手动步骤 + 健康汇总（总挂单 / 总持仓 / 总盈亏 / 合格市场数）+ 各钱包状态；模板没配档位模块时在顶部醒目提示 |
| 市场发现 | 扫描出的合格市场；点「展开」实时拉订单簿做**断层预演**，显示市场归到哪一级、最大断层、逐档的**价格 / 盘口量 / 风险系数 / 是否高位**、选中哪一档、**挂几份**，以及该侧的**奖励范围 / 盘口价差**；不挂的市场直接给出原因 |
| 挂单与持仓 | 在挂买单（价 / 量 / 已成交 / 是否在赚奖励）+ 当前持仓（成本 / 现价 / 止损价 / 浮动盈亏） |
| 历史 | 引擎动作记录（挂买 / 止盈止损卖 / 撤改 / 复查撤单等），含每笔卖单的逐笔成本构成依据 |
| 资产曲线 | 每个钱包的净值日线（净值 = 现金 + 持仓市值，含现金辅线），可查任意某天的净值明细 |
| 监控 | 每 4 秒刷新的实时监控快照（瞬时状态） |
| 配置 | 钱包导入与模板绑定、多模板管理、策略参数、**档位模块卡片编辑器**、引擎参数 |
| 黑名单 | 加入 / 移除不参与的市场 |

> Eight sidebar screens with a light/dark theme toggle. Market Discovery expands into a live gap-tier preview (which rule the market fell into, per-rung price / book size / risk coefficient, the chosen rung and its share count, plus the reason when nothing is placed). The Config page edits size-tier modules as cards; the Net Worth page charts each wallet's daily balance history.

---

## 快速开始（普通用户）/ Quick Start (end users)

到本仓库的 [**Releases**](../../releases) 页面下载对应系统的安装包。

### Windows

1. 下载 `PolymarketMarketMaker_Setup.exe`，运行安装程序完成安装。
2. 从开始菜单/桌面双击启动。

> 运行时数据写入 `%LOCALAPPDATA%\PolymarketMarketMaker`，不会污染安装目录。

### Linux 服务器

想让程序 7×24 跑着、不用自己电脑一直开机，可以装到一台 Linux VPS 上，通过自己的域名用 https 访问。步骤见 [`deploy/README.md`](deploy/README.md)。

> ⚠️ **必须配域名并走 https。** 用裸 IP 加 http 访问的话，私钥在提交时是明文穿过公网的。
> ⚠️ **进程重启后要重新登录**：私钥用登录密码加密，密钥只存在内存里，引擎不会自动恢复。

> Run it 24/7 on a Linux VPS behind your own domain — see [`deploy/README.md`](deploy/README.md). HTTPS is mandatory; the private key crosses the network in plaintext over bare-IP HTTP.

### macOS

**不再提供 macOS 安装包**（2026-07-28 起）。此前发布的 `.dmg` 未做 Apple 签名、也从未在真机上验证过，现已停止构建。macOS 用户可以按 [`deploy/README.md`](deploy/README.md) 的服务器模式自行从源码运行。

### 启动后 / After launch

浏览器会自动打开 `http://127.0.0.1:8765`：

1. **首次运行**进入 `/setup`：设置一个登录密码（用于加密你的私钥，**忘记不可找回**）。
2. 录入钱包私钥（仅需私钥，程序会自动推导你的 Polymarket 资金存款地址 / Gnosis Safe）。
3. 确认参数后启动引擎。**建议先用小额资金试跑**，观察止盈/止损与挂单是否符合预期。

> Download the Windows installer (`.exe`) from **Releases**, or deploy to a Linux VPS per [`deploy/README.md`](deploy/README.md). Open `http://127.0.0.1:8765`, set a login password (used to encrypt your key, **not recoverable if lost**), enter your wallet private key, and start the engine. Test with small amounts first.

⚠️ **停止引擎会同时停止止损监控**：已停止时仍持有的仓位将不再被止损保护——界面对此有明确提示，请留意。

> Stopping the engine also stops stop-loss monitoring; open positions are then unprotected.

---

## 从源码运行（开发者）/ Run from Source

环境要求：Python 3.10+（Windows 为主要目标平台）。

```powershell
pip install -r requirements.txt   # 运行依赖
pip install pytest                # 跑测试用

python app.py                     # 启动；浏览器打开 http://127.0.0.1:8765（首次为 /setup）

pytest                            # 全部单元测试（纯逻辑，不联网）
pytest tests/test_strategy.py     # 单个文件
```

依赖 / Dependencies：`flask`、`py-clob-client-v2`、`py-builder-relayer-client`、`cryptography`（见 `requirements.txt`）。

> `tests/` 是纯逻辑、不联网的 pytest 单测。仓库根目录的 `test_live.py`、`test_simulate.py`、`test_real_order.py` **不是** pytest 测试，而是会命中真实 Polymarket API / 本地数据库的手动脚本（`test_real_order.py` 会下真实订单），**请勿**纳入测试套件运行。

> The files `test_live.py` / `test_simulate.py` / `test_real_order.py` at the repo root are manual scripts that hit the **live** API (the last one places **real** orders) — they are not part of the pytest suite.

---

## 工作原理 / Architecture

**鉴权门控一切。** 钱包私钥用从密码派生的密钥加密，密钥只在登录后存在于内存——因此引擎**无法开机自启**，必须先登录。`EngineManager` 在 `/setup` 与 `/login` 路由中、拿到加密密钥后才构造。整体为**单进程、单用户**设计。

**线程模型（`engine/manager.py`）。** 一个共享的 `MarketScanner` 线程产出「合格市场列表」（扫描无需特定钱包鉴权）；每个启用的钱包对应一个 `WalletWorker` 线程，负责（a）从共享列表下单、（b）监控成交/止损。SQLite 连接以 `check_same_thread=False` 打开并在线程间共享。

**流水线：扫描 → 策略 → 下单 → 监控 / scan → strategy → place → monitor。**

| 阶段 | 模块 | 职责 |
| --- | --- | --- |
| 扫描 Scan | `engine/scanner.py` · `engine/tiers.py` | 发现奖励市场、产出共享**候选池**（奖励阈值、结算天数、价格带、价差、品类白名单、冷却）；再按**档位模块精确匹配**（市场最低奖励份额须等于某个已启用档位）。各钱包下单时按自己的模板从候选池精筛。 |
| 策略 Strategy | `engine/laddering.py` · `engine/legacy_wall.py` · `engine/order_sizing.py` | 两套并列的选价策略（都是纯函数、完整单测、核心 IP），按模板的 `placement_mode` 二选一：`gap_single` 走 `laddering.py` 的断层分级 + 风险系数选档，挂档位模块配置的份数；`legacy_wall` 走 `legacy_wall.py` 的找厚墙挂墙下一档，份数由 `order_sizing.py` 按 min/custom/balance 算。 |
| 下单 Place | `engine/manager.py` | 下单前**每次重读余额**防并发超支；按竞争度从低到高；按市场最低份额取对应档位模块的参数与份数（无匹配档位直接跳过）；撤改收敛（reconcile），成交后单侧暂停。 |
| 监控 Monitor | `engine/monitor.py` · `engine/networth.py` | 检测成交、维护每仓**唯一**卖单（成本逐笔重建、永不低于成本）、强平止损、重核价差与价格区间；每天记一次账户净值。 |

> Auth gates everything (engines can't auto-start). One shared scanner thread feeds per-wallet workers; SQLite is shared across threads. The pipeline is scan → strategy → place → monitor. The gap-tier placement logic in `engine/laddering.py` (plus `engine/strategy.py` and the tier matching in `engine/tiers.py`) is pure, fully unit-tested, and the core IP.

更详细的设计文档（简体中文）见 `docs/superpowers/specs/2026-05-17-polymarket-market-maker-design.md`。
开发约定与关键不变量见 [`CLAUDE.md`](CLAUDE.md)。

---

## 切换挂单模式 / Switching placement modes

挂单定价有两套并列的策略，在「配置 → 挂单参数 → 挂单模式」切换，**模板级**生效（可以让一部分钱包跑一套、另一部分跑另一套做对比）。

| | 断层单档（默认，`gap_single`） | 厚墙（v1.0.15 老策略，`legacy_wall`） |
| --- | --- | --- |
| 怎么挑价 | 按买单簿相邻档最大价差分三级，用「风险系数」从买一往下取第一个过门槛的档 | 找到第一堵挂量够厚的墙，挂在**它下面一档** |
| 判据 | 相对值：挂量 ÷（最低份数 × 金额数值） | 绝对值：单档挂量是否超阈值（1 美分盘 2000 / 0.1 美分盘累计 6000） |
| 挂几份 | 档位模块的 `shares` | `order_size_mode`：`min` / `custom` / `balance` |
| 档位模块 | 全部字段生效 | **只用 `size` 和 `enabled`**（选品门控），其余忽略 |

**切到厚墙**：选好模板 → 挂单模式选「厚墙」→ **先去「市场发现」页展开几个市场看预演表里的盘口量级** → 按量级调「厚墙阈值 / 累计阈值」→ 选份数模式（选 `custom` 必须填金额）→ 保存 → **重启引擎生效**。

**切回断层单档**：挂单模式改回去、保存、重启。断层那套参数切走时只是隐藏，值没被清掉，切回来不用重填。

**切换时会发生什么**：换模式等于换了一套目标挂价，下一轮会把所有对不上新目标的在挂买单撤掉再重挂；新策略判不挂的市场则只撤不挂。已成交的持仓不受影响，照常由离场逻辑看守。

### 配置注意点

1. **厚墙的默认阈值可能对当前盘口偏高。** 2000 / 6000 是 v1.0.15 年代的值。实际观察到的一个市场最厚的一档只有 236 份，按 2000 的阈值会被判「无档达到阈值」而整个不挂。切之前务必先用预演页看盘口量级，否则结果就是一单都挂不出去。
2. **厚墙模式下档位模块只剩 `size` / `enabled` 有用**，仍需至少启用一个，否则选不出任何市场。
3. **`order_size_mode=custom` 忘填金额等于全部不挂**（份数永远不够最低奖励份额）。历史页会记原因。
4. **厚墙不是 100% 的 v1.0.15**：监控侧的悬崖复查、实时奖励复查、盘口价差复查仍生效。要纯原版行为把 `cliff_probe_cents` 配成 `0`。
5. **断层单档下实际起作用的主要是规则1 的参数**。分级看整个买单簿，而低价区通常稀疏、容易冒出超过 10¢ 的断层，所以大部分市场归规则1；规则2/3 的门槛虽可配但很少触发，调参先调规则1。
6. **策略表单的数字框留空表示「这一项不改」**，保存后仍是原值，不是恢复默认。唯一例外是「最长结算天数」，留空表示不限。
7. **两套模式共用**：选品筛选、品类白名单、预算与敞口封顶、撤改收敛、离场止损、低余额清仓、每钱包代理、盈亏台账。切换只换「挑哪个价、挂几份」。

**排查「切了厚墙一单不挂」**：看历史页跳过记录的「原因」列。写「无档达到阈值 2000（扫描范围内最厚 236）」是阈值问题；写「悬崖」是买单下方没支撑被否决；写「按份数模式 custom 算出的份数不足最低份数」是第 3 条那个坑。

## 核心参数 / Configuration

参数分两级：**引擎级**（全局单值，存 `settings` 表）与**策略级 / 模板**（每钱包绑定的模板各自取值，存 `template_settings` 表）；默认值见 `config.py`（`ENGINE_DEFAULTS` / `TEMPLATE_DEFAULTS`）。界面改动在下次启动引擎时生效。

**引擎级 / Engine（`settings`）**

| Key | 默认值 | 含义 |
| --- | --- | --- |
| `scan_interval_sec` | 30 | 自动模式下单轮间隔（秒） |
| `fill_check_interval_sec` | 5 | 成交 / 监控检查间隔（秒） |
| `cooldown_minutes` | 20 | 同市场成交后冷却（分钟） |
| `rewards_cache_ttl_sec` | 0 | 奖励参数复查缓存 TTL（秒），0=每次实时取 |
| `discovery_interval_sec` | 14400 | 市场发现间隔（秒，默认 4 小时） |
| `reward_scan_max_pages` | 20 | 每品类奖励市场抓取页数上限（每页 100） |

**策略级 / 每模板 Strategy（`template_settings`）**

| Key | 默认值 | 含义 |
| --- | --- | --- |
| `min_reward_usd` | 100.0 | 市场最低奖励门槛（美元） |
| `max_spread_cents` | 3.0 | 最大买卖价差（美分） |
| `min_price_cents` / `max_price_cents` | 10.0 / 50.0 | 单价区间（美分，含端点） |
| `min_settlement_days` / `max_settlement_days` | 4 / 不限 | 结算窗口（整天；上限留空 = 只卡下限） |
| `skip_new_markets` / `new_market_hours` | `false` / 24.0 | 跳过最近 N 小时内创建的新市场（默认关；0 小时 = 不筛）。只对下面勾中的品类生效，实际生效可能比 N 晚最多一个「市场发现间隔」 |
| `skip_new_categories` / `skip_new_other` | 全部品类 / `true` | 新市场保护对哪些做市品类生效（tag slug）；`skip_new_other` 管「其他/未分类」。默认全部，等同升级前的一刀切行为 |
| `included_categories` | 除 sports/esports/weather 外全部 | 做市品类白名单（勾中的才做，tag slug） |
| `include_other` | `true` | 是否做市「其他/未分类」（不属于任何 curated 品类的市场） |
| `size_tiers` | （空） | **档位模块**：按市场最低奖励份额**精确匹配**。每档配 启用 / 挂单份数（≥档位值）/ 规则 1-3 选档门槛 / 规则 1-3 高位系数和门槛 / 金额数值表。**没有任何已启用模块对得上的市场不挂单** |
| `gap_wide_cents` / `gap_mid_cents` | 10 / 5 | 断层分级阈值（美分）：> 宽 → 规则 1，≥ 中 → 规则 2，更小 → 规则 3 |
| `cliff_probe_cents` | 2 | 悬崖否决：奖励区间下沿往下这么多美分内没有买档，该侧不挂（0 = 关）。两种挂单模式都生效 |
| `gap_veto_cents` | 0（关） | 断层上限否决：全簿最大相邻价差 **>** 此值该侧不挂，不再归级选档。下单判一次，监控每 tick 复查在挂买单（超限即撤、不重挂）。口径是整个买单簿，低价区零散挂单会把数值撑得很大且价格越高撑得越狠，填之前先看预演页的「最大断层」实际量级 |
| `placement_mode` | `gap_single` | 挂单模式：`gap_single` 断层单档（现行）/ `legacy_wall` 厚墙（v1.0.15 老策略）。只认精确字符串，其余值一律回落 `gap_single` |
| `legacy_wall_threshold` | 2000 | 厚墙模式·1 美分盘：某档挂量 > 此值算一堵墙 |
| `legacy_cumulative_threshold` | 6000 | 厚墙模式·0.1 美分盘：从买一往下累加，累计 > 此值即停 |
| `order_size_mode` | `min` | 厚墙模式的挂单份数：`min` 最低合格份额 / `custom` 按美元上限 / `balance` 全额余额。断层单档模式不看这项 |
| `order_size_custom_usd` | 0 | `order_size_mode=custom` 时的美元上限。**留 0 等于一单不挂** |
| `stop_loss_mode` | `percent` | 强平止损方式：`percent`（占成本百分比）/ `fixed`（固定美分）/ `off`（关闭） |
| `stop_loss_percent` | 20 | 按比例止损时：亏到成本的百分之多少市价清仓 |
| `theta_stop_cents` | 5 | 按固定金额止损时：亏损达到这么多美分市价清仓 |
| `take_profit_mode` | `maker` | 浮盈卖法：`maker` 挂卖一吃价差 / `market` 成本 < 买一时立即市价清仓 |
| `max_exposure_usd` / `max_exposure_shares` | 250 / 500 | 单市场最大敞口（美元 / 份数） |
| `max_concurrent_markets` | 10 | 最大并发做市市场数 |

**几个概念怎么算 / Key metrics**

- **档位模块** = 一组「只对最低奖励份额等于某个值的市场生效」的挂单参数。例：启用 20 档（挂单份数填 40）和 50 档（份数填 50）——最低份额 20 的市场挂 40 份，最低份额 50 的市场挂 50 份，最低份额 30 的市场因为没有模块对得上而**完全不做**。匹配是精确相等，不做区间也不做就近取档。
- **风险系数** = 该档盘口挂单量 ÷（市场最低份数 × 金额数值）（逐档、非累计）。金额数值按价格档配（价越高数值越大），价超该档位模块的金额表 → 这一档不挂。例：最低份数 20，某档价 0.25、盘口 60 张、金额数值 1.5 → 风险系数 = 60 ÷ (20 × 1.5) = 2.0。价越高分母越大，同样盘口算出的系数越低。**注意**：系数分母用的是市场的最低份数，不是你配的挂单份数——份数只决定挂多少，不影响选档。
- **断层分级** = 整个买单簿上相邻买档之间的最大价差（含奖励区间外的档：区间下方的深坑正是要防的东西，只看区间内就看不见它）。> 宽断层阈值归规则 1，≥ 中断层阈值归规则 2，更小归规则 3。归到哪级就用哪级的选档门槛，从买一往下取第一个「风险系数 > 门槛」的档。能挂单的价位仍只限奖励区间内。
- **高位厚度闸门** = 选档之前先看「断层上方到买一」这一段的风险系数之和够不够，不够就整个市场不挂。三级各有自己的门槛（在档位模块里逐档配）：规则 1 默认 20，规则 2、规则 3 默认 0，0 表示不设这道闸。

运行时数据位置 / Runtime data：
- 打包运行：`%LOCALAPPDATA%\PolymarketMarketMaker\`（`market_maker.db`、`market_maker.log`）
- 源码运行：项目根目录
- 监听地址：`http://127.0.0.1:8765`（端口被占用时自动回退到空闲端口）

---

## 打包与发版 / Build & Release

```powershell
# 构建 Windows 安装包(PyInstaller 打 exe + Inno Setup 打安装程序)
powershell -ExecutionPolicy Bypass -File build_installer.ps1

# 一键发版:构建 -> 计算 SHA-256 -> 打 git tag -> gh release create
# 发版前先修改 version.py 的版本号
powershell -ExecutionPolicy Bypass -File release.ps1
```

- 版本号唯一来源：`version.py`（被 build / release 共同读取）。
- 发版需要已安装并登录的 [GitHub CLI](https://cli.github.com/)（`gh auth login`）。
- 根目录若存在 `RELEASE_NOTES.md` 则作为 Release 说明，否则自动生成。
- **发版只出 Windows 安装包**（2026-07-28 起）。Linux 服务器模式不需要安装包，上游更新按「git fetch -> audit -> merge -> tests -> deploy」手动进行。
- **macOS 已停止构建**：`.github/workflows/build-mac.yml` 的 `release published` 自动触发已移除，只留手动触发（Actions 页填 tag 可给某个 Release 补挂 `.dmg`）。

> Single source of version truth is `version.py`. `release.ps1` builds the Windows installer locally; macOS builds are no longer published (the workflow keeps a manual trigger only).

---

## 安全说明 / Security

- 私钥使用 **AES-256-GCM** 加密；加密密钥由你的登录密码经 **PBKDF2（600,000 次迭代）** 派生，**仅存在于内存**，不落盘明文。
- 应用仅监听本机 `127.0.0.1`，不对外开放。
- 单进程、单用户设计：`web/routes.py` 持有模块级的 `db` / `manager` / `encryption_key`。
- 你只需输入私钥；资金存款地址（Polymarket Gnosis Safe）由签名 EOA 自动推导（CREATE2）。

> Keys are AES-256-GCM encrypted with a PBKDF2 (600k iterations) key held only in memory; the app binds to `127.0.0.1` only.

---

## License

本仓库**当前未声明开源许可证**（All rights reserved）。如需复用/二次分发，请先与作者确认。

This repository currently has **no open-source license** (all rights reserved). Contact the author before reuse or redistribution.
