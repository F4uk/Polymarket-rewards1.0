"""engine/scanner.py — Market scanner: shared candidate collection + per-wallet filtering.

Two-phase design:
- fetch_candidates(templates): wallet-independent network work. Fetches all reward
  markets, tags each with the curated categories it matches (queried per tag_slug over
  the full CATALOG_SLUGS), pre-filters to markets wanted by at least one template
  (whitelist union of included_categories, plus the "other" bucket when some template
  sets include_other) and by the loosest reward floor, fetches precise per-market
  reward, and caches each token's orderbook. Does NOT compute order prices.
- filter_for_template(pool, template, wallet): per-wallet CPU work. Applies the
  template's thresholds (reward floor, settlement, price band, spread), keeps only
  markets matching the template's included_categories (or untagged when include_other),
  checks cooldown. Does NOT compute prices or costs —
  pricing and sizing happen at placement time (place_orders).

Optional on_progress(checked, total, msg) / on_found(market) callbacks fire per
candidate during fetch_candidates.
"""

import re
import time
import logging
from datetime import datetime, date, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from engine.categories import (
    included_union,
    any_include_other,
    tag_pool,
    market_in_categories,
    market_wanted,
    count_by_category,
)
from config import CATALOG_SLUGS, CATEGORY_CATALOG, ENGINE_DEFAULTS
from engine.strategy import reward_price_range
from engine.tiers import enabled_sizes
from api.proxy import use_proxy, current_proxy, parallel_map

logger = logging.getLogger(__name__)

_DISCOVERY_MAX_WORKERS = 4  # 发现阶段奖励端点并发上限(端点/代理娇气,降到 4 减轻挤压)


class ScanSuperseded(Exception):
    """本轮扫描被更新一轮接管,应立即让位(合作式取消)。"""


def book_spread(bids, asks) -> float:
    """买卖价差 = 卖一 − 买一,从已抓回的订单簿本地算;缺任一边或价格解析不出返回 -1
    (价差不可知);交叉盘(买一>卖一,负价差)clamp 到 0。

    以前每个 token 额外发一次 GET /spread 拿这个数(实测 1.27s/次,比抓订单簿本身还慢
    3 倍),整池刷簿时它占了一半的网络调用——而值就是订单簿一减。监控 Step 3 早就是
    本地算的(monitor.py),这里与之统一。-1 沿用旧 /spread 的「无簿」语义,filter 据此
    跳过该 token;价格字段缺失/为 null 也归入 -1,绝不抛(否则一个畸形 level 会掀翻整轮
    刷簿——refresh_orderbooks 的 try 只包住 get_orderbook)。交叉盘价差为负,若也当 -1
    会被 filter 误当无簿跳过,而旧 /spread 让这类(常是最紧、最高奖励的瞬时交叉)市场可
    下单——故 clamp 到 0(最窄价差,过 max_spread 门槛)。round 到 4 位小数:裸减的浮点尘
    (0.53-0.51=0.020000000000000018)会让美分门槛的比较翻面。"""
    if not bids or not asks:
        return -1
    try:
        best_bid = max(float(b["price"]) for b in bids)
        best_ask = min(float(a["price"]) for a in asks)
    except (KeyError, TypeError, ValueError):
        return -1
    return max(0.0, round(best_ask - best_bid, 4))


def _parse_end_date(end_date_str: str) -> float:
    """Parse end_date string to Unix timestamp."""
    if not end_date_str:
        return 0
    s = end_date_str.strip()
    if s.endswith("Z"):
        s = s[:-1]
    else:
        m = re.search(r"(\d{2}:\d{2}:\d{2})[+-]\d{2}:?\d{0,2}$", s)
        if m:
            s = s[: m.end(1)]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return 0


def _settlement_days_until(end_ts: float) -> int:
    """结算日距今整天数(本地日历日):0=今天,1=明天,2=后天…;已过为负。

    end_ts 来自 _parse_end_date(去 Z 按 naive 解析、.timestamp() 按本地还原,一去一
    回时区抵消,date.fromtimestamp(end_ts) 即 end_date 字符串里写的那个日期)。按整天
    日历日比较,避免小数天数在临近午夜分不清「今天」和「明天」。"""
    return (date.fromtimestamp(end_ts) - date.fromtimestamp(time.time())).days


def _batch_rate(market: dict) -> float:
    """批量奖励端点给的每日奖励(rewards_config 各档 rate_per_day 之和)。精确奖励拉取
    失败/跳过时的兜底值。"""
    return sum(rc.get("rate_per_day", 0) for rc in market.get("rewards_config", []))


def _token_price_cents(token: dict) -> float:
    """token 现价(美分)。price 缺失/JSON null/空串一律按 0(必落价带外),绝不抛——
    奖励端点偶有 price 为 null 的 token,裸 float(None) 会 TypeError 掀翻整轮筛选。"""
    return float(token.get("price") or 0) * 100


def _tokens_in_price_band(market: dict, min_price_cents, max_price_cents) -> bool:
    """该市场是否至少有一个 token 的现价落在单价区间内(端点含)。全都不在 -> 整市场
    没有可下单的一侧,连订单簿都不必抓。"""
    return any(
        min_price_cents <= _token_price_cents(t) <= max_price_cents
        for t in market.get("tokens", [])
    )


def _in_settlement_window(end_ts: float, min_days, max_days) -> bool:
    """结算日是否落在窗口 [min_days, max_days] 内(整天)。max_days=None 表示不限上限。"""
    d = _settlement_days_until(end_ts)
    if min_days is not None and d < min_days:
        return False
    if max_days is not None and d > max_days:
        return False
    return True


_CREATED_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


def _new_market_hours(template) -> float:
    """模板的新市场保护期(小时)。非数字/负数/缺失一律归 0(= 不筛)——DB 里的值可能被手改,
    绝不让一个畸形配置抛进整轮扫描。"""
    try:
        return max(0.0, float(template.get("new_market_hours") or 0))
    except (TypeError, ValueError):
        return 0.0


def market_age_hours(created_at: str, now: float):
    """市场创建至今的小时数；created_at 缺失/解析不出返回 None（调用方 fail-open 保留）。

    created_at 是真正的 UTC 时刻（奖励端点给的形如 '2026-07-22T23:10:03.086269Z'），
    **不能**套 _parse_end_date —— 那个是刻意按 naive 本地时区还原的（end_date 的语义是
    「日历日」，一去一回时区抵消），拿来解析这里会平白差一个时区，把「25 小时前创建」
    算成「17 小时前」。小数秒直接丢掉（最多差 1 秒，无意义），顺带绕开
    datetime.fromisoformat 只认 3 位或 6 位微秒的限制（实测存在 2 位的样本）。
    非 Z 结尾的时区偏移不处理、一律按 UTC：实测样本 100% 带 Z，真出现别的格式时正则
    仍匹配得上，误差最大一个时区。
    形状合法但取值越界的串（月 13、日 45、"0000-00-00" 这类零值哨兵）一律按解析不出处理，
    返回 None —— 绝不让一条畸形 created_at 掀翻整轮扫描。
    """
    m = _CREATED_RE.match(str(created_at or "").strip())
    if not m:
        return None
    try:
        ts = datetime(*map(int, m.groups()), tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        return None  # 形状合法但取值越界(月 13、日 45、零值哨兵…):同样按不可知处理
    return (now - ts) / 3600.0


def loosest_new_market_hours(templates, tags) -> float:
    """发现阶段可安全排除该市场的门槛(小时);tags 是该市场命中的 curated 品类。

    发现阶段是钱包无关的共享阶段,只有**每个**模板都会因「太新」排除这个市场才能在这里
    排除(否则会把没排除它的模板要的市场也一起剔掉);此时取各模板 N 的最小值(最宽松)。
    任一模板没开开关、或没把该市场的品类列进自己的保护名单 -> 0(不排除)。空列表 -> 0。
    缺保护名单的键按「不保护」处理(fail-open,与 created_at 解析不出即保留同方向)。
    保护名单(skip_new_categories)与「其他」开关(skip_new_other)先分别与该模板自己的
    做市名单(included_categories/include_other)取交集/取与:发现阶段的 tag_pool 只按
    included_categories 打标签,不勾「其他」时 tags 可能残缺,不取交集的话保护判定会随
    include_other 翻面——同一份保护意图因为一个不相关的开关得出相反结果。
    """
    hours = []
    for t in templates:
        if not t.get("skip_new_markets"):
            return 0.0
        if not market_in_categories(
            tags,
            set(t.get("skip_new_categories") or [])
            & set(t.get("included_categories") or []),
            bool(t.get("skip_new_other")) and bool(t.get("include_other")),
        ):
            return 0.0
        hours.append(_new_market_hours(t))
    return min(hours) if hours else 0.0


class MarketScanner:
    def __init__(self, api, db, wallet_address: str):
        self.api = api
        self.db = db
        self.wallet_address = wallet_address

    def discover_candidates(
        self,
        templates,
        on_progress=None,
        on_found=None,
        cancel=None,
        max_pages=ENGINE_DEFAULTS["reward_scan_max_pages"],
    ) -> list[dict]:
        """共享发现:抓各需要品类的奖励市场并集(不勾「其他」不抓全品类),打 tags,
        补精确奖励。钱包无关、网络密集(rewards 端点)、不抓订单簿、不算价。"""
        union = included_union(templates)
        inc_other = any_include_other(templates)
        # 只查用得上的品类:收「其他」时判其他绕不开、必须查全 14;否则只需 included 并集。
        # 注意:只按 included_categories 打标签,故 tags 可能残缺;新市场保护判定因此与
        # 做市名单取交集(见 loosest_new_market_hours),改这行前先确认那边仍成立。
        slugs_needed = set(CATALOG_SLUGS) if inc_other else (union & set(CATALOG_SLUGS))
        floors = [t.get("min_reward_usd", 0) for t in templates]
        min_floor = min(floors) if floors else 0

        def _tag_slug(slug):  # 返回整条记录(用于并入候选池 + 建 cid 集打标签)
            try:
                rows = self.api.get_rewards_markets(tag_slug=slug, max_pages=max_pages)
                return slug, rows
            except Exception as e:
                # 单个品类查询失败不拖垮整轮发现(奖励端点偶发 500):该 slug 记空,
                # 其命中市场退化为「其他」(include_other 时仍会被 full 收)。
                logger.warning(
                    "Discovery tag_slug %s failed (treated as empty): %s", slug, e
                )
                return slug, []

        # 只查 slugs_needed 各一次奖励端点,并发拉;每 slug 保留整条记录。
        slug_rows = dict(parallel_map(_tag_slug, slugs_needed, _DISCOVERY_MAX_WORKERS))
        category_ids = {
            slug: {m.get("condition_id", "") for m in rows}
            for slug, rows in slug_rows.items()
        }

        # full(不带品类=全品类)仅在有号收「其他」时需要:用来捞无 curated 标签的市场。
        # 不勾「其他」的号(如天气号)彻底跳过这次全品类抓取——正是它当初把低奖励品类
        # 挤出前 500、造成候选偏少的根因。
        full = self.api.get_rewards_markets(max_pages=max_pages) if inc_other else []

        # 品类计数快照(配置页勾选用):仅当查了全 14(即 inc_other,full 有值)才算得准。
        # 光 slugs_needed 覆盖全 14 不够——勾满 14 个 curated 品类但不勾「其他」时同样会覆盖全 14,
        # 但 full 从未抓取,算出的计数会是假的全零(2026-07-09 review 发现的回归)。
        if inc_other and slugs_needed == set(CATALOG_SLUGS):
            self.last_catalog = self._catalog_payload(full, category_ids)

        # 候选池 = 各需要品类 tag 记录按 cid 去重的并集;inc_other 再并入 full(补「其他」)。
        by_cid = {}
        for slug in slugs_needed:
            for market in slug_rows.get(slug, []):
                cid = market.get("condition_id", "")
                if cid and cid not in by_cid:
                    by_cid[cid] = market
        if inc_other:
            for market in full:
                cid = market.get("condition_id", "")
                if cid and cid not in by_cid:
                    by_cid[cid] = market

        pool = tag_pool(list(by_cid.values()), category_ids, slugs_needed)
        blacklist = self.db.get_blacklist_ids()

        # 「新市场」门槛:发现阶段只能排除「每个模板都会因太新排除」的市场,门槛按市场的
        # 品类逐条算(见 loosest_new_market_hours);各模板自己的 N 由 prefilter_for_template
        # 精筛。created_at 由奖励端点白拿,判定不发网络请求。
        now = time.time()

        # 并集档位 sizes + 各模板结算窗口:精确奖励拉取(每市场一次网络、~0.78s)是发现
        # 阶段的大头。档位/窗口这两个门槛既不需要订单簿也不需要精确奖励就能判——用它们
        # 决定「要不要对该市场拉精确奖励」,而不是决定「进不进候选池」:品类匹配的市场都进
        # 池(市场发现页照常显示、非幸存者价差显—),门控外的只用批量 total_rate 兜底奖励、
        # 省下那次网络。union_sizes 空(无启用档位)时不按档位筛;窗口 fail-open(结算日
        # 解析不出则保留,与 filter 一致)。
        union_sizes = set()
        windows = []
        for t in templates:
            union_sizes |= enabled_sizes(t.get("size_tiers") or [])
            windows.append((t.get("min_settlement_days"), t.get("max_settlement_days")))

        def _should_price(market):
            """该市场是否值得拉精确奖励:档位被某模板要(或无档位门控)且落在某模板窗口内。"""
            if (
                union_sizes
                and int(market.get("rewards_min_size", 0) or 0) not in union_sizes
            ):
                return False
            end_ts = _parse_end_date(market.get("end_date", ""))
            if end_ts and not any(
                _in_settlement_window(end_ts, mn, mx) for mn, mx in windows
            ):
                return False
            return True

        # 品类匹配(过黑名单/奖励地板)的市场都进候选池;其中被档位+窗口门控选中的才拉精确
        # 奖励(priced,并发),其余用批量奖励兜底(extra,不发网络)。
        priced, extra = [], []
        for market in pool:
            if cancel and cancel():
                raise ScanSuperseded()
            cid = market.get("condition_id", "")
            if cid in blacklist:
                continue
            if not market_wanted(market.get("tags", []), union, inc_other):
                continue  # 没被任何模板 include(且非其他)
            if _batch_rate(market) < min_floor:
                continue  # 比最宽松模板还低,任何模板都不会要
            min_age_hours = loosest_new_market_hours(templates, market.get("tags", []))
            if min_age_hours:
                age = market_age_hours(market.get("created_at", ""), now)
                if age is not None and age < min_age_hours:
                    continue  # 太新;created_at 取不到 -> fail-open 保留
            (priced if _should_price(market) else extra).append(market)

        def _precise_reward(market):
            # 精确每市场奖励(与旧 scan 一致:/rewards/markets/{cid});失败退回批量 total_rate。
            cid = market.get("condition_id", "")
            try:
                raw = self.api.get_rewards_for_market(cid)
                if raw:
                    return sum(
                        rc.get("rate_per_day", 0)
                        for rd in raw
                        for rc in rd.get("rewards_config", [])
                    )
            except Exception as e:
                logger.warning("Precise reward fetch failed for %s: %s", cid, e)
            return _batch_rate(market)

        out = []

        def _finalize(market, reward):
            cid = market.get("condition_id", "")
            self.db.upsert_market_meta(
                cid,
                market.get("question", ""),
                market.get("market_slug", ""),
                market.get("event_slug", ""),
            )
            # 候选池展示就绪键(供 /api/eligible 与持久化显示市场名/奖励)。不含
            # order_price/outcome:候选池按市场,单价每钱包下单时实时算,前端缺失以「—」兜底。
            market["market_reward"] = reward
            market["market_id"] = cid
            market["market_name"] = market.get("question", "")
            market["daily_reward"] = reward
            out.append(market)
            if on_found:
                on_found(market)

        # 门控外市场:不发网络,批量奖励直接入池(市场发现页照常显示,下单时由 filter 再筛)。
        # extra 集可能几百个(每个 upsert + on_found),循环里查 cancel 让手动重扫能及时抢占。
        for market in extra:
            if cancel and cancel():
                raise ScanSuperseded()
            _finalize(market, _batch_rate(market))

        # 门控内市场:精确奖励并发拉,as_completed **增量**出结果——每完成一个就报进度 +
        # on_found,让进度条/候选列表随扫描逐渐增长,而不是跑完后一次性蹦出(2026-07-05
        # 用户反馈「一直卡在 0、然后突然 100 多个」)。返回序=完成序(池后续按
        # market_competitiveness 重排,不依赖此序);代理隔离同 parallel_map。
        total = len(priced)
        if on_progress:
            on_progress(0, total, "核对各市场精确奖励…")
        proxy = current_proxy.get()

        def _reward_task(market):
            token = current_proxy.set(proxy)
            try:
                return market, _precise_reward(market)
            finally:
                current_proxy.reset(token)

        done = 0
        with ThreadPoolExecutor(
            max_workers=min(_DISCOVERY_MAX_WORKERS, total or 1)
        ) as ex:
            futures = [ex.submit(_reward_task, m) for m in priced]
            for fut in as_completed(futures):
                if cancel and cancel():
                    raise ScanSuperseded()
                market, market_reward = fut.result()
                _finalize(market, market_reward)
                done += 1
                if on_progress:
                    on_progress(done, total, f"Checking: {market.get('question','')}")
        logger.info(
            "discover_candidates: %d candidates (included union %d categories)",
            len(out),
            len(union),
        )
        return out

    def refresh_orderbooks(self, pool, cancel=None):
        """给候选池每个市场刷新订单簿快照(覆盖写)。钱包无关、可重复调。
        某 token 抓不到则不入该市场的 _orderbooks(filter 现有逻辑会跳过该 token),
        覆盖写保证不留上一轮的陈旧簿。

        整池的 token **一次批量**拉(get_orderbooks,内部按 100 个一批):刷簿的请求数
        是 O(候选池 × 每市场 token 数),~180 市场就是 360 次单发往返,过代理时能吃掉
        几十分钟、下单轮基本跑不完,filter 拿不到簿 -> eligible 空 -> 不挂单(2026-07-04
        实盘)。批量整体失败时 books 为空,各市场照旧覆盖成空簿 —— 覆盖写是刻意的,留着
        上一轮的陈旧簿会让下单按过时盘口定价。"""
        if cancel and cancel():
            raise ScanSuperseded()
        token_ids = [
            t
            for t in {
                tk.get("token_id", "") for m in pool for tk in m.get("tokens", [])
            }
            if t
        ]
        try:
            books = self.api.get_orderbooks(token_ids)
        except Exception as e:
            logger.warning("批量刷簿失败,本轮各市场按空簿处理: %s", e)
            books = {}
        for market in pool:
            market["_orderbooks"] = self._build_orderbooks(market, books)
        if token_ids:
            got = sum(1 for t in token_ids if isinstance(books.get(t), dict))
            if got < len(token_ids):
                # 只在真缺的时候说话。缺的 token 会被 filter 跳过 = 那个市场这一轮
                # 不下单,系统性取不到就是「预演能出却不下单」,只有这条看得见。
                logger.warning(
                    "刷簿缺 %d/%d 个 token", len(token_ids) - got, len(token_ids)
                )

    def fetch_candidates(
        self,
        templates,
        on_progress=None,
        on_found=None,
        skip_orderbook=False,
        cancel=None,
        max_pages=ENGINE_DEFAULTS["reward_scan_max_pages"],
    ) -> list[dict]:
        """共享采集 = 发现 + (除非 skip_orderbook)刷新订单簿。手动扫描/单测用。
        cancel(): 返回 True 时在最近的检查点抛 ScanSuperseded 让位(手动重扫接管)。"""
        pool = self.discover_candidates(
            templates,
            on_progress=on_progress,
            on_found=on_found,
            cancel=cancel,
            max_pages=max_pages,
        )
        if not skip_orderbook:
            self.refresh_orderbooks(pool, cancel=cancel)
        return pool

    def _catalog_payload(self, full, category_ids) -> dict:
        """由「全量奖励市场 + 各 slug 命中集」组装配置页品类快照。计数在全量集上交集,
        与勾选无关;discover_candidates(扫描)与 category_counts(手动刷新)共用口径。"""
        full_ids = {m.get("condition_id", "") for m in full if m.get("condition_id")}
        narrowed = {s: category_ids.get(s, set()) & full_ids for s in CATALOG_SLUGS}
        counts, other = count_by_category(full_ids, narrowed, CATALOG_SLUGS)
        cats = [
            {"slug": c["slug"], "label": c["label"], "count": counts.get(c["slug"], 0)}
            for c in CATEGORY_CATALOG
        ]
        return {"categories": cats, "other_count": other, "ready": True}

    def category_counts(
        self, catalog, max_pages=ENGINE_DEFAULTS["reward_scan_max_pages"]
    ) -> dict:
        """catalog: [{'slug','label'}]. 返回各 curated 品类在当前奖励市场的市场数 +
        「其他」数。钱包无关;各 slug 并发查 CLOB 奖励端点,与全量取交集计数。"""
        full = self.api.get_rewards_markets(max_pages=max_pages)
        full_ids = {m.get("condition_id", "") for m in full if m.get("condition_id")}

        # get_rewards_markets 是静态方法,靠环境 current_proxy 走代理,而 contextvar
        # 不会自动继承到线程池 worker —— 每个 worker 必须自己重设一次代理。
        proxy = getattr(self.api, "proxy_url", None)

        def _slug_ids(slug):
            with use_proxy(proxy):
                rows = self.api.get_rewards_markets(tag_slug=slug, max_pages=max_pages)
            return {m.get("condition_id", "") for m in rows} & full_ids

        slugs = [c["slug"] for c in catalog]
        category_ids = {}
        with ThreadPoolExecutor(max_workers=min(8, len(slugs) or 1)) as pool:
            futures = {pool.submit(_slug_ids, s): s for s in slugs}
            for fut in as_completed(futures):
                slug = futures[fut]
                try:
                    category_ids[slug] = fut.result()
                except Exception as e:
                    logger.warning("category_counts slug %s failed: %s", slug, e)
                    category_ids[slug] = set()

        counts, other = count_by_category(full_ids, category_ids, slugs)
        cats = [
            {"slug": c["slug"], "label": c["label"], "count": counts.get(c["slug"], 0)}
            for c in catalog
        ]
        return {"categories": cats, "other_count": other}

    def _build_orderbooks(self, market: dict, books: dict) -> dict:
        """从本轮批量取回的簿里挑出该市场每 token 的快照(钱包无关)。取不到的略过。

        `books` 由 refresh_orderbooks 一次批量取好;里面缺的 token 或值为 None 的
        (批量对取不到的 token 静默丢弃)一律跳过 —— filter 对没有簿的 token 会跳过,
        而写进一个空壳簿会被当成真实盘口参与判定。

        价差由 book_spread 从这份簿本地算,不再多发 /spread。"""
        out = {}
        for token in market.get("tokens", []):
            token_id = token.get("token_id", "")
            if not token_id:
                continue
            ob = books.get(token_id)
            if not isinstance(ob, dict):
                continue  # 缺多少由 refresh_orderbooks 汇总打一条,不逐 token 刷屏
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            out[token_id] = {
                "bids": bids,
                "asks": asks,
                "tick_size": ob.get("tick_size", "0.01"),
                "spread": book_spread(bids, asks),
            }
        return out

    def prefilter_for_template(
        self, candidate_pool, template, wallet_address
    ) -> list[dict]:
        """只跑「不需要订单簿」的门槛(品类/奖励/结算窗口/冷却/档位/价带),返回幸存市场。

        下单轮据此决定给谁刷簿:整池 300+ 市场里真正能下单的只有几十个,以前却全刷了
        (每市场每 token 一次网络请求),是首单等待时间的大头。filter_for_template 复用
        本函数,两处门槛口径不会漂。"""
        min_reward = template["min_reward_usd"]
        min_price_cents = template["min_price_cents"]
        max_price_cents = template["max_price_cents"]
        min_days = template["min_settlement_days"]
        max_days = template.get("max_settlement_days")  # None=不限上限
        included = set(template.get("included_categories", []) or [])
        include_other = bool(template.get("include_other", False))
        tier_sizes = enabled_sizes(template.get("size_tiers") or [])
        skip_new = bool(template.get("skip_new_markets"))
        new_hours = _new_market_hours(template)
        # 保护名单与做市名单取交集，因为发现阶段的 tag_pool 只按 included_categories 打
        # 标签（不勾「其他」时 tags 是残缺的），不取交集的话保护判定会随 include_other 翻
        # 面；这也正是配置页「做市」未勾时把「跳过新市场」置灰的含义。
        skip_cats = set(template.get("skip_new_categories", []) or []) & included
        skip_other = bool(template.get("skip_new_other", False)) and include_other
        now = time.time()

        survivors = []
        for market in candidate_pool:
            tags = market.get("tags", [])
            if not market_wanted(tags, included, include_other):
                continue
            total_rate = _batch_rate(market)
            market_reward = market.get("market_reward", total_rate)
            if total_rate < min_reward or market_reward < min_reward:
                continue
            end_ts = _parse_end_date(market.get("end_date", ""))
            # 结算窗口 [min_days, max_days](整天)。无法解析结算日 -> 保留(fail-open)。
            if end_ts and not _in_settlement_window(end_ts, min_days, max_days):
                continue
            if (
                skip_new
                and new_hours
                and market_in_categories(tags, skip_cats, skip_other)
            ):
                # 该品类开了保护:创建不足 new_hours 小时的市场不做;
                # created_at 取不到 -> fail-open 保留。
                age = market_age_hours(market.get("created_at", ""), now)
                if age is not None and age < new_hours:
                    continue
            if self.db.is_in_cooldown(wallet_address, market.get("condition_id", "")):
                continue  # retains pre-existing/manual cooldown compatibility
            # 档位模块精确匹配:最低份额必须等于某个已启用模块的档位值,否则不做。
            if int(market.get("rewards_min_size", 0) or 0) not in tier_sizes:
                continue
            if not _tokens_in_price_band(market, min_price_cents, max_price_cents):
                continue
            survivors.append(market)
        return survivors

    def filter_for_template(
        self, candidate_pool, template, wallet_address
    ) -> list[dict]:
        """从候选池产出某模板的 eligible(无簿门槛 + 订单簿门槛/定价)。"""
        eligible = []
        for market in self.prefilter_for_template(
            candidate_pool, template, wallet_address
        ):
            eligible.extend(self.book_eligible(market, template))
        logger.info(
            "filter_for_template(%s): %d eligible from %d candidates",
            wallet_address,
            len(eligible),
            len(candidate_pool),
        )
        return eligible

    def book_eligible(self, market, template) -> list[dict]:
        """单个**已过无簿门槛**的市场,跑订单簿门槛(价带/价差)+ 定价,产出逐 token eligible。

        与无簿门槛(prefilter_for_template)分离:下单轮已用 prefilter 选出 subset 决定刷哪些
        簿,直接对 subset 逐个调本函数即可,不必再走一遍 prefilter 的无簿门槛(热路径上省下
        重复的品类/冷却 DB 查询)。filter_for_template = prefilter + 本函数,对外仍自包含。"""
        min_price_cents = template["min_price_cents"]
        max_price_cents = template["max_price_cents"]
        max_spread_cents = template["max_spread_cents"]

        condition_id = market.get("condition_id", "")
        end_date_str = market.get("end_date", "")
        market_reward = market.get("market_reward", _batch_rate(market))
        max_spread_reward = float(market.get("rewards_max_spread", 2))
        min_size = int(market.get("rewards_min_size", 0) or 0)
        neg_risk = market.get("neg_risk", False)
        books = market.get("_orderbooks", {})
        valid_tokens = [
            t
            for t in market.get("tokens", [])
            if min_price_cents <= _token_price_cents(t) <= max_price_cents
        ]
        out = []
        for token in valid_tokens:
            token_id = token.get("token_id", "")
            book = books.get(token_id)
            if not book:
                continue
            spread_val = book.get("spread", -1)
            if spread_val < 0 or spread_val * 100 >= max_spread_cents:
                continue
            bids = sorted(
                book.get("bids", []), key=lambda x: float(x["price"]), reverse=True
            )
            asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
            if not bids or not asks:
                continue
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            if best_bid * 100 < min_price_cents or best_bid * 100 > max_price_cents:
                continue
            tick_size_str = book.get("tick_size", "0.01")
            midpoint = (best_bid + best_ask) / 2
            rmin, rmax = reward_price_range(midpoint, max_spread_reward)
            out.append(
                {
                    "market_id": condition_id,
                    "token_id": token_id,
                    "market_name": market.get("question", ""),
                    "outcome": token.get("outcome", ""),
                    "market_competitiveness": market.get("market_competitiveness", 0),
                    "end_date": end_date_str,
                    "daily_reward": market_reward,
                    "rewards_max_spread": max_spread_reward,
                    "rewards_min_size": min_size,
                    "tick_size_str": tick_size_str,
                    "neg_risk": neg_risk,
                    "tags": market.get("tags", []),
                    "reward_range_min": rmin,
                    "reward_range_max": rmax,
                    "spread_cents": spread_val * 100,
                }
            )
        return out
