"""api/proxy.py — 每钱包 HTTP 代理:解析串 + contextvar 选路 + httpx/requests 注入。

py-clob-client-v2 的 HTTP 层是模块级全局 httpx.Client、不支持 per-instance 代理,
而同一 PolymarketAPI 实例又被采集线程(下单)与监控线程共用 -> 代理选择按「当前操作
的钱包」动态切换,用 contextvar。传输层(httpx 分发器 + requests 包装)读 contextvar,
操作边界(_tick/place_orders/采集器/构造)设 contextvar。
"""

import contextvars
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from contextlib import contextmanager
from urllib.parse import quote

import httpx
import requests

# 「当前操作的钱包」的代理 URL(或 None=直连)。传输层读它,操作边界设它。
current_proxy: contextvars.ContextVar = contextvars.ContextVar(
    "current_proxy", default=None
)


@contextmanager
def use_proxy(url):
    """在 with 块内把 current_proxy 设为 url(None=直连),退出复位。"""
    token = current_proxy.set(url)
    try:
        yield
    finally:
        current_proxy.reset(token)


def parallel_map(func, items, max_workers):
    """并发对 items 跑 func,结果按输入序返回。

    关键:ThreadPoolExecutor 的 worker 线程默认 current_proxy=None(会直连、泄露真实
    IP,违反「绝不直连」铁律)。这里捕获**调用线程**的 current_proxy,在每个 worker 里
    设回,保住代理 IP 隔离。func 应自带异常处理(否则异常在收结果时抛出)。

    max_workers 必传:各调用方的合适值不同(发现阶段 4、Step3 6),留默认值会误导。
    """
    items = list(items)
    if not items:
        return []
    proxy = current_proxy.get()

    def _task(item):
        token = current_proxy.set(proxy)
        try:
            return func(item)
        finally:
            current_proxy.reset(token)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as ex:
        return list(ex.map(_task, items))


# 仅这些「连接建立阶段」失败可安全重试:到代理的连接 / CONNECT 隧道从未建立,请求从未
# 送达 Polymarket,即使 POST 下单也不会重复提交。读超时 / 连接后错误不在此列(可能已送达)。
_CONNECT_ERRORS = (
    requests.exceptions.ConnectionError,  # 含 ProxyError / ConnectTimeout(requests)
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ProxyError,
)


def _retry_on_connect_error(fn, *, attempts=3, backoff=0.3):
    """调 fn();仅在代理连接建立阶段瞬时失败时退避重试,最多 attempts 次。

    烂 / 轮换代理常有约 30% 连接超时(实测),而一轮下单要连续多个代理调用、任一失败即
    整轮放弃 -> 几乎挂不出单。重试仍走同一代理(绝不直连,不泄露真实 IP)。只重试
    _CONNECT_ERRORS —— 请求从未送达,连 POST 下单也安全;读超时等「可能已送达」的错误
    立即上抛,避免重复下单。
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except _CONNECT_ERRORS as e:
            last = e
            if i < attempts - 1 and backoff:
                time.sleep(backoff)
    raise last


def http_get(url, **kw):
    """requests.get 包装:current_proxy 非空时注入 proxies=(http/https 同一代理)。

    代理连接瞬时失败先退避重试(仍走代理,见 _retry_on_connect_error);重试用尽仍失败
    才抛给调用方跳过,绝不直连(不泄露真实 IP)。
    """
    proxy = current_proxy.get()
    if proxy:
        kw.setdefault("proxies", {"http": proxy, "https": proxy})
    return _retry_on_connect_error(lambda: requests.get(url, **kw))


def http_post(url, **kw):
    """requests.post 包装:current_proxy 非空时注入 proxies=(http/https 同一代理)。

    与 http_get 同源,共用 _retry_on_connect_error。**重试不保证幂等**:requests 把
    ProtocolError(含 Connection aborted / RemoteDisconnected)也包进 ConnectionError,而那
    可能发生在请求体已经发出之后 —— 所以本函数只可用于「重复一次也无所谓」的外发通知(周报
    中继),**绝不可用于下单**。下单走 py-clob-client 的 httpx 分发器,那边命中的
    httpx.ConnectError/ConnectTimeout/ProxyError 确实只在连接阶段,与这里不是一回事。
    """
    proxy = current_proxy.get()
    if proxy:
        kw.setdefault("proxies", {"http": proxy, "https": proxy})
    return _retry_on_connect_error(lambda: requests.post(url, **kw))


class _ProxyDispatchingClient:
    """替换 py_clob_client_v2 的模块全局 ``_http_client``:按 ``current_proxy``
    选/缓存 per-proxy ``httpx.Client``。``helpers.request()`` 按模块全局名解析
    ``_http_client.request(**kw)``,故替换该名即对所有 CLOB 调用生效。

    None(直连)复用传入的原 client 以保持非代理钱包的现行为;每个代理 URL 懒建
    一个 http2 client 并缓存(多线程共用,建时加锁)。代理连接瞬时失败先退避重试
    (仍走代理);重试用尽才抛给调用方跳过,绝不直连。
    """

    def __init__(self, default_client):
        self._clients = {None: default_client}
        self._lock = threading.Lock()

    def _client_for(self, proxy):
        client = self._clients.get(proxy)
        if client is None:
            with self._lock:
                client = self._clients.get(proxy)
                if client is None:
                    client = httpx.Client(http2=True, proxy=proxy)
                    self._clients[proxy] = client
        return client

    def request(self, **kw):
        client = self._client_for(current_proxy.get())
        return _retry_on_connect_error(lambda: client.request(**kw))


class _ProxiedClient:
    """包装一个对象,使其每次**方法调用**都在 ``use_proxy(proxy)`` 内执行。

    用于 ClobClient:库的 HTTP 分发器读 ``current_proxy``,故只要调用期 contextvar
    被设为本钱包代理,该次 CLOB 网络调用即走对的代理——无论从采集线程、监控线程还是
    路由直接调用。非可调用属性(如 ``.builder``)原样透传,可正常读写其字段。
    """

    def __init__(self, target, proxy):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_proxy", proxy)

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def wrapped(*a, **k):
            with use_proxy(self._proxy):
                return attr(*a, **k)

        return wrapped


_installed = False
_install_lock = threading.Lock()


def install_clob_proxy():
    """幂等:把 py_clob_client_v2 的全局 httpx client 换成按代理选路的分发器。

    PolymarketAPI 构造时调用一次即可(多次安全)。
    """
    global _installed
    if _installed:
        return
    with _install_lock:
        if _installed:
            return
        from py_clob_client_v2.http_helpers import helpers

        helpers._http_client = _ProxyDispatchingClient(helpers._http_client)
        _installed = True


def parse_proxy(raw) -> str | None:
    """把用户填的代理串转成代理 URL;空/None -> None。

    - ``host:port:user:pass``        -> ``http://user:pass@host:port``(凭证 URL 编码)
    - ``host:port``                  -> ``http://host:port``
    - ``socks5:host:port[:user:pass]``-> ``socks5h://...``
    - 已是 ``http(s)://`` 原样返回;``socks5://`` 归一成 ``socks5h://``
    密码可能含 ``:`` -> ``split(":",3)`` 让前三段为 host/port/user、其余整体当密码。

    SOCKS5 一律用 ``socks5h``(域名交给代理端解析):``socks5``(本地解析)会从真实 IP
    发 DNS 查询、泄露访问目标,实测某些机房代理也直接拒连。
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith(("http://", "https://")):
        return s
    if low.startswith(("socks5://", "socks5h://")):
        return "socks5h://" + s.split("://", 1)[1]
    scheme = "http"
    for pfx in ("socks5h:", "socks5:"):
        if low.startswith(pfx):
            scheme, s = "socks5h", s[len(pfx) :]
            break
    parts = s.split(":", 3)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    host, port = parts[0], parts[1]
    if len(parts) == 2:
        return f"{scheme}://{host}:{port}"
    user = parts[2]
    pw = parts[3] if len(parts) >= 4 else ""
    auth = quote(user, safe="")
    if pw:
        auth += ":" + quote(pw, safe="")
    return f"{scheme}://{auth}@{host}:{port}"


class ProxyUnreachable(Exception):
    """两种协议都连不通(或代理到不了 Polymarket)。"""


# 探测用:必须能到交易所才算可用(有的代理能上网但被 Polymarket 拒);出口 IP 仅回显。
_PROBE_URL = "https://clob.polymarket.com/ok"
_EXIT_IP_URL = "https://api.ipify.org?format=json"


def _probe_once(url, timeout):
    requests.get(_PROBE_URL, proxies={"http": url, "https": url}, timeout=timeout)


def _exit_ip(url, timeout):
    try:
        r = requests.get(
            _EXIT_IP_URL, proxies={"http": url, "https": url}, timeout=timeout
        )
        return r.json().get("ip") or None
    except Exception:
        return None


def probe_proxy(raw, *, timeout=8) -> tuple[str, str | None]:
    """探测代理走 HTTP 还是 SOCKS5,返回(该存进库的串, 出口 IP 或 None)。

    用户只填 ``host:port:账户:密码``,并不知道供应商给的是哪种协议(如 iproyal 的
    机房 IP 只开 SOCKS5)。这里先按 HTTP 试、再按 SOCKS5 试,谁能连到 Polymarket 就
    用谁,并把探测结果以 ``socks5:`` 前缀写回串里,运行期不再猜。
    已显式写了协议的串只验连通、不改写。都不通 -> ProxyUnreachable(调用方拒绝保存:
    代理不通时本钱包每轮都会被跳过且绝不直连,存下来等于静默停摆)。
    """
    s = (str(raw) if raw else "").strip()
    if not s:
        return "", None
    low = s.lower()
    explicit = low.startswith(
        ("http://", "https://", "socks5://", "socks5h:", "socks5:")
    )
    candidates = [s] if explicit else [s, "socks5:" + s]
    for cand in candidates:
        url = parse_proxy(cand)
        if not url:
            raise ProxyUnreachable(
                "代理格式不对(应为 host:port 或 host:port:账户:密码)"
            )
        try:
            _probe_once(url, timeout)
        except Exception:
            continue
        return cand, _exit_ip(url, timeout)
    raise ProxyUnreachable("代理连不上(HTTP / SOCKS5 都试过)")

# --- Relayer 客户端代理注入(与 CLOB 同一套 current_proxy 选路) ---
# py-builder-relayer-client 的 HTTP 层是模块级 requests.request,不支持 per-instance
# 代理。与 install_clob_proxy 同理:替换其 helpers.request 为按 current_proxy 注入
# proxies 的版本。绝不静默直连:钱包配了代理时 Relayer 流量必须走该代理,否则泄露
# 真实 IP;代理不可用时按 _CONNECT_ERRORS 退避重试,仍失败则上抛(调用方降级)。

_RELAYER_PATCHED = False


def install_relayer_proxy():
    """幂等:让官方 relayer 客户端的全部请求走 current_proxy 选路。"""
    global _RELAYER_PATCHED
    if _RELAYER_PATCHED:
        return
    try:
        import py_builder_relayer_client.http_helpers.helpers as rh

        orig = rh.request

        def _relayer_request(endpoint, method="GET", headers=None, data=None):
            proxy = current_proxy.get()

            def _do():
                kw = {}
                if proxy:
                    kw["proxies"] = {"http": proxy, "https": proxy}
                return orig(endpoint, method=method, headers=headers, data=data, **kw)

            return _retry_on_connect_error(_do, attempts=2, backoff=0.3)

        rh.request = _relayer_request
        _RELAYER_PATCHED = True
    except Exception:
        # 包没装/装坏:让 Relayer 调用自然失败,由上层降级,绝不静默直连。
        _RELAYER_PATCHED = True
