# VPS 部署方案设计（Linux + 域名 + HTTPS）

> **历史记录：** 本文中的网页自更新设计已经退役；当前部署只允许管理员显式更新。保留本文仅用于追溯，不得据此恢复运行时自更新。

日期：2026-07-27

## 背景与目标

本程序至今只在本机跑（`HOST = "127.0.0.1"`，双击 exe/dmg，浏览器自动打开）。现在要把它部署到一台
Linux VPS 上，让作者的一位朋友通过浏览器远程使用，不用自己电脑一直开着。

VPS 由作者开设和运维，朋友的钱包私钥托管在这台机器上。作者与朋友之间的信任问题不在本设计范围内
（作者会向朋友说明清楚）。本设计要解决的是**技术上的私钥泄露风险**。

### 已确认的需求边界

- VPS 上只跑**一个实例**，只给朋友用。作者自己继续用本地 Windows/mac 版。
- 朋友只用电脑浏览器访问，不愿安装任何客户端（Tailscale 等），因此必须公网暴露。
- 不需要无人值守 24/7。引擎的启动/暂停、更新后重启，都由朋友在网页上手动操作，
  和本地版的使用方式一致。
- 版本更新做成网页按钮，朋友自己点，不依赖作者 SSH。
- 登录防护：应用层限速 + 强制强密码。不加 Basic Auth，不加 TOTP。

### 不做的事

- 多用户 / 多实例 / 权限隔离。程序是单用户单进程设计，不改这个前提。
- 把登录密码持久化到 VPS 以实现开机自动解锁。那会让磁盘上同时存在密文和密码，
  加密形同虚设。
- Docker 化。
- 改动做市策略、引擎、订单逻辑的任何行为。

## 威胁模型

裸 IPv4 + HTTP 明文访问下，私钥在提交时以明文穿越公网，任何链路中间节点都能抓到；登录密码和
session cookie 同样明文。这是本方案要消除的首要风险。

消除之后的残留风险，作者已知悉并接受：

- VPS 被入侵、或供应商在宿主机层面读取内存/快照，可以拿到解密后的私钥。程序运行期间
  加密密钥必须常驻内存，这一点无法通过加密消除。
- 作者本人有 root，技术上可以拿到朋友的私钥。属于信任问题，不在技术范围内。

## 架构

```
朋友的浏览器
   │  HTTPS (443)
   ▼
Caddy ── 自动申请/续期 Let's Encrypt 证书，HSTS
   │  HTTP，127.0.0.1:8765（回环，不出网卡）
   ▼
waitress ── 单进程多线程 WSGI
   │
   ▼
Flask app（现有代码）
   └── EngineManager：扫描线程 + 各钱包 worker 线程
```

Flask 依然只绑 `127.0.0.1`，`config.py` 的 `HOST` 不变。公网上唯一开放的服务是 Caddy 的 443。
防火墙（ufw）只放行 22 / 80 / 443，其中 80 仅用于 ACME 验证和跳转 HTTPS。

### 为什么必须单进程

`web/routes.py` 的 `db` / `manager` / `encryption_key` 是模块级全局，引擎是进程内线程。gunicorn
默认起多个 worker，每个 worker 会持有一套独立的 `EngineManager`，同一批钱包被两套引擎重复下单——
真金白银的事故。waitress 单进程多线程，与现有模型一致。

### 服务器模式开关

服务器上的差异行为由 systemd 注入的环境变量 `PMM_SERVER=1` 显式打开，不靠 `sys.platform` 推断。
作者在 mac 上跑 `python app.py` 的行为完全不变。该开关控制四件事：

1. 不调用 `webbrowser.open`
2. 端口固定为 `PORT`，不走 `utils/net.py` 的 `pick_port`
3. 用 waitress 起服务，不用 Flask 开发服务器
4. 更新走 git 路径，不去 GitHub 下载 `.exe`

第 2 条的理由：`pick_port` 在首选端口绑不上时会回退到系统分配的随机端口，这是为 Windows 的
Hyper-V 保留端口区间写的。服务器上换了端口，Caddy 就反代不到，服务表现为"启动成功但打不开"。
服务器模式下绑不上直接报错退出，让 systemd 的重启循环和日志把问题暴露出来。

### 时区

部署时把 VPS 设为 `Asia/Shanghai`。每日盈亏台账、周报、监控 watermark（`init_watermark` 用 DB
`created_at`，本地时间）都按本地时间计算，留在 UTC 会导致日期错位。

## 代码改动

### 1. 补上缺失的鉴权（与部署无关的既有漏洞）

`web/routes.py` 中三个路由没有 `login_required`：

- `/api/update/check`（GET）
- `/api/update/apply`（POST）
- `/api/update/status`（GET）

本地只监听回环时无害，公网暴露后任何人 POST 一次 `/api/update/apply` 就能让进程退出重启；改为
git 更新后更严重，等于未鉴权触发拉取代码并重启。

`apply` 与 `status` 加 `login_required`。**`check` 保持免登录**：`login.html` 和 `setup.html`
底部都有「检查更新」链接，未登录时能查版本是有意功能；而 `check` 只是向固定的 GitHub URL 读一个
版本号、带 30 分钟 TTL 缓存，既不改变状态也不泄露钱包信息，未鉴权的代价至多是消耗一点 GitHub
匿名限额。（初版设计要求三个都加，实现时发现会让登录页的「检查更新」显示误导性的"无法连接
GitHub"，2026-07-27 改为此方案。）

### 2. `config.py`

新增 `SERVER_MODE = os.environ.get("PMM_SERVER") == "1"`。

### 3. `app.py` `main()`

服务器模式下：

- 跳过 `open_browser` 线程
- 直接使用 `PORT`，不调 `pick_port`；端口不可用时记录错误并退出
- `waitress.serve(app, host=HOST, port=PORT, threads=8)` 代替 `app.run(...)`
  （单人使用，8 个请求线程足够；引擎的扫描/worker 线程与之无关，各自独立）

非服务器模式的行为逐字不变。

### 4. `web/routes.py` 安全加固

- **Cookie**：`SESSION_COOKIE_HTTPONLY=True`、`SESSION_COOKIE_SAMESITE="Lax"` 两种模式都开；
  `SESSION_COOKIE_SECURE` **仅在 SERVER_MODE 下**开启——本地是 http，开了浏览器不会回传
  cookie，会直接登不进去。
- **真实客户端 IP**：Caddy 反代后 `request.remote_addr` 恒为 `127.0.0.1`，按 IP 限速会退化为
  全局限速，攻击者可借此把朋友锁在门外。用 `werkzeug.middleware.proxy_fix.ProxyFix(app.wsgi_app,
  x_for=1)` 取 `X-Forwarded-For` 的最后一跳。因为 Flask 只绑回环、只可能被本机 Caddy 访问，
  该头不可能由外部伪造，信任它是安全的。
- **登录限速**：模块级内存字典，按真实 IP 记录失败次数与锁定截止时间。连续失败 5 次锁定
  15 分钟，登录成功清零。单进程，无需外部存储。
- **密码强度**：`setup()` 的最小长度由 6 提升到 12。项目没有改密码功能，所以此改动只影响全新
  安装；朋友那台正是全新 setup，作者本地已设置的密码不受影响。
- **会话有效期**：`PERMANENT_SESSION_LIFETIME` 设为 7 天绝对过期。

**明确保持不变**：`app.secret_key = os.urandom(32)` 每次启动随机。进程重启后所有 session 失效、
必须重新登录——这与"重启后内存中的加密密钥丢失、本来就必须重新输密码解密私钥"是一致的，
不是缺陷。

### 5. `requirements.txt`

新增 `waitress`。

## 网页更新按钮（Linux 分支）

复用 `web/update.py` 的现有骨架：`engine_active(mgr)` 安全闸（引擎运行中拒绝更新，否则持仓失去
止损保护）和 `STATE` 进度状态机都不改，只替换"下载安装包"那一段。

流程：

1. 记录当前 commit（`git rev-parse HEAD`），用于回滚
2. `git fetch --tags origin`
3. `git reset --hard <release tag>`
4. `pip install -r requirements.txt`
5. 成功 → `os._exit(0)`，systemd 用新代码拉起进程
6. 任一步失败 → `git reset --hard <原 commit>`，`STATE` 置 error，**不退出进程**，
   旧版本继续运行

设计要点：

- **对齐 tag 而非 `origin/main`**：避免拉到作者尚未发布的中间提交。tag 取自 GitHub Release，
  与 `check_update` 比较的版本号同源。
- **不需要 sha256 校验**：原流程校验 sha256 是因为要下载并执行一个二进制安装包。`git fetch`
  走 HTTPS 并校验 GitHub 证书，完整性已经具备。
- **数据安全**：`git reset --hard` 不影响 untracked 文件，`market_maker.db` 及其 wal/shm 都在
  `.gitignore` 中。
- **`check_update` 需同步调整**：`parse_release` 按平台挑安装包时，Linux 会落入 `.exe` 分支
  （`pkg_suffix = ".dmg" if darwin else ".exe"`）。服务器模式下不应看 asset，只比较 tag 版本号。

### 残留风险

若某个新版本存在启动期 bug，更新后进程起不来，systemd 会持续重启失败，网页无法访问，只能由作者
SSH 上去 `git reset` 回退。没有纯自动的解法。缓解方式：作者发布新版后先在自己的环境验证，再让
朋友点更新。

## 部署产物

放入已有的 `deploy/` 目录：

- `pmm.service` — systemd unit：非 root 用户运行、venv 内的 python、`Environment=PMM_SERVER=1`、
  `Restart=always`
- `Caddyfile` — 域名占位符 + 反代 `127.0.0.1:8765` + HSTS 响应头
- `install.sh` — 一次性部署脚本：建服务用户、克隆仓库、建 venv 装依赖、设时区、配置 ufw、
  安装 Caddy、启动服务
- `README.md` — 中文部署步骤，含域名解析说明与 SSH 加固（禁用密码登录、只留公钥）

## 测试

`web/update.py` 本就是依赖注入式设计，Linux 分支同样把命令执行注入进去，可完全离线单测：

- 成功路径：fetch → reset → pip → 退出，各步按序调用
- pip 失败：回滚到原 commit，状态为 error，不退出进程
- fetch 失败：同上
- 引擎运行中：拒绝更新，不执行任何 git 命令
- `check_update` 在服务器模式下不依赖 asset，只按 tag 判断是否有新版本

其余单测：

- 登录限速：5 次失败后锁定、锁定期内拒绝、成功后清零、锁定到期后恢复
- `X-Forwarded-For` 存在时按该 IP 计数，而非 `127.0.0.1`
- SERVER_MODE 开关对应的配置值（cookie Secure、端口策略）
- `/api/update/*` 三个路由未登录时不执行操作

`Caddyfile`、`pmm.service`、`install.sh` 不写自动化测试，靠部署时实际运行验证。

## 验收标准

1. 浏览器访问 `https://<域名>` 能打开登录页，证书有效，http 自动跳转 https
2. 直接访问 `http://<VPS IP>:8765` 不通（Flask 只绑回环 + ufw 拦截）
3. 全新 setup 时密码少于 12 位被拒绝
4. 连续 5 次错误密码后被锁定，日志中记录的是真实客户端 IP 而非 `127.0.0.1`
5. 未登录状态下 `/api/update/apply` 不触发更新
6. 停止引擎后点网页更新按钮，进程自动重启到新版本，数据库内容保留
7. 引擎运行中点更新按钮，被拒绝并提示先停止引擎
8. `systemctl restart` 后服务自动起来，停在登录页，登录后引擎可正常启动
9. VPS 上 `date` 显示北京时间
