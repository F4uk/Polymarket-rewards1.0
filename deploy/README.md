# VPS 部署说明

把这个程序部署到一台 Linux VPS，通过域名 + HTTPS 远程使用。
部署与后续维护均由管理员显式执行；运行中应用不会自行更新。
历史设计背景见 [`docs/superpowers/specs/2026-07-27-vps-deployment-design.md`](../docs/superpowers/specs/2026-07-27-vps-deployment-design.md)（其中网页自更新章节已停用）。

## 前提

- 一台 Debian 12 或 Ubuntu 22.04+ 的 VPS，有 root
- 一个域名，A 记录已经指向这台 VPS 的 IPv4 地址（必须先解析生效，
  Caddy 申请证书时要用它验证域名归属）

## 部署

> **⚠️ 注意：`install.sh` 会用 `ufw --force enable` 重置防火墙规则，只放行
> 22/80/443 端口。这个脚本假设你用的是一台全新的、只跑这一个程序的 VPS——
> 如果这台机器上还跑着别的服务，先打开 `install.sh` 看一眼 ufw 那几行，
> 不然那些服务会被这次 enable 挡在外面。
> 如果你把 SSH 端口改成了非 22，先把脚本里的 `ufw allow 22/tcp` 改成你的实际端口，
> 否则这一步会把你自己锁在外面。
> 另外脚本每次运行都会覆盖 `/etc/caddy/Caddyfile`，手动改过的内容会被抹掉。**

```bash
ssh root@<你的VPS地址>
rm -rf /tmp/pmm-src && git clone https://github.com/VMInsideVM/poly-marketmaker.git /tmp/pmm-src
bash /tmp/pmm-src/deploy/install.sh your-domain.com
```

脚本会：设时区为北京时间、建 `pmm` 服务用户、克隆代码到 `/opt/pmm/poly-marketmaker`、
建虚拟环境装依赖、安装并配置 Caddy、注册 systemd 服务、开启 ufw（只放行 22/80/443）。

完成后打开 `https://your-domain.com`，首次访问会进入设置页要求设定密码。

> **⚠️ 脚本跑完请立刻打开网址设置密码。** Caddy 申请证书时会把域名写进公开的
> 证书透明度(CT)日志，扫描机器人几乎是秒级发现新上线的域名；`/setup` 页面对谁
> 先访问没有任何限制，谁先设密码谁就占住了这个实例。如果第一次打开看到的是
> **登录页**而不是设置页，说明已经被人抢先设过密码了——删掉
> `/opt/pmm/poly-marketmaker/market_maker.db` 后 `systemctl restart pmm` 重来。

## 必做的 SSH 加固

私钥托管在这台机器上，SSH 弱口令等于把私钥送人。部署完立刻改
`/etc/ssh/sshd_config`：

```
PasswordAuthentication no
PermitRootLogin prohibit-password
```

然后 `systemctl restart sshd`。改之前先确认自己的公钥已经在
`~/.ssh/authorized_keys` 里，否则会把自己锁在外面。

## 日常运维

```bash
systemctl status pmm          # 看服务状态
journalctl -u pmm -f          # 看实时日志
systemctl restart pmm         # 重启(重启后需要重新登录网页)
```

> **⚠️ `systemctl restart pmm` 前请先在网页上停止引擎。** 进程退出时不会自动撤单、
> 不会停引擎——直接重启会让挂单留在交易所，且没有任何止损监控保护持仓。

**重启后必须有人登录网页。** 钱包私钥是用登录密码派生的密钥加密的，密钥只存在内存里，
进程一重启就没了。所以引擎不会自动恢复，得有人打开网页输密码、再手动启动引擎。
这是有意的设计——把密码存在服务器上就等于取消了加密。

## 更新

应用不提供网页自更新，也不会自行拉取、重置代码或重装依赖。维护者应在停止引擎后，按自己的变更管理流程手动部署并验证版本。

## 备份

`/opt/pmm/poly-marketmaker/market_maker.db` 存着加密后的私钥和全部历史数据。
它不在 git 里，更新不会动它。要备份就备份这个文件，但注意它只是密文——
没有登录密码解不开，所以密码得另外记牢。
