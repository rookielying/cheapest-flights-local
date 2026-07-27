"""Pipeline orchestrator for flight-watch (report milestone M1).

Flow:
  load config -> for each enabled route, for each resolved depart_date,
  try sources in order (retry 2x with exponential backoff 30s/90s, then
  degrade to the next source) -> mark is_lowest_of_day -> dedup + append JSONL
  -> build docs/data/summary.json -> call alert engine + notifier hooks.

The alert engine (src.alerts.engine.run_alerts) and notifiers
(src.notifiers.dispatch) are wired via try/except ImportError so this runs
before those modules (M2) exist.

CLI:
  python -m src.main                 # normal run
  python -m src.main --dry-run       # MockFetcher fake data, backoff/sleep=0
  python -m src.main --routes a,b    # only these route ids
  python -m src.main --config path   # custom config file
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Callable, Optional

from .config import load_config, resolve_dates, Route
from .models import FlightQuote, iso_now, today_shanghai
from .storage import Storage
from .fetchers.base import FetcherAdapter, FetchError, get_fetcher
from . import fetchers  # noqa: F401  (triggers fetcher registration)

log = logging.getLogger("flight_watch")

# Project layout (repo root = parent of this src/ package).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# config.json is the single source of truth (the web panel writes it reliably;
# config.yaml is only a human-readable mirror and may carry panel formatting
# bugs). Prefer config.json when present; config.py also enforces this at load
# time, so a stale/broken config.yaml can never drive a scheduled run.
_CONFIG_JSON = os.path.join(ROOT, "config.json")
_CONFIG_YAML = os.path.join(ROOT, "config.yaml")
DEFAULT_CONFIG = _CONFIG_JSON if os.path.exists(_CONFIG_JSON) else _CONFIG_YAML
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")
STATE_DIR = os.path.join(ROOT, "state")

# -------------- 为 --dry-run 与单元测试而生的模拟源 --------------
# ---------------------------------------------------------------- dry-run source
class MockFetcher(FetcherAdapter):
    """Deterministic fake source for --dry-run / tests (no network, no deps)."""

    name = "mock"

    def available(self) -> bool:
        return True

    def fetch(self, route, depart_date: str) -> list:
        base = 800 + (abs(hash((route.id, depart_date))) % 1200)
        # 伪造国航、东航、吉祥航空的机票报价...
        """单测与本地调试利器：在不连接真实网络、不消耗 SerpAPI 配额、不触发真实请求的前提下，通过简单的哈希算法生成确定性的伪造报价数据。
        随机生成 800-2000 元之间的价格，并返回国航、东航、吉祥航空的机票报价。

        Args:   
            route: 航线对象
            depart_date: 出发日期

        Returns:
            list: 机票报价列表
        """

        fetched_at = iso_now()
        out = []
        for i, (airline, fno, dtime, atime) in enumerate([
            ("Air China", "CA880", "20:55", "23:40"),
            ("China Eastern", "MU523", "09:10", "12:15"),
            ("Juneyao Air", "HO1339", "14:30", "17:35"),
        ]):
            out.append(FlightQuote(
                fetched_at=fetched_at,
                route_id=route.id,
                origin=route.origin,
                dest=route.dest,
                depart_date=depart_date,
                airline=airline,
                flight_no=fno,
                depart_time=dtime,
                arr_time=atime,           # 👈 同步补充伪造的落地时间
                has_baggage=True,          # 👈 同步补充行李标志
                stops=i % 2,
                price=base + i * 60,
                currency="CNY",
                raw_price=float(base + i * 60),
                raw_currency="CNY",
                price_type="total_with_tax",
                source="mock",
            ))
        return out
    
    """命令行参数 --dry-run 支持：
    命令行加上 --dry-run 后，系统全流程走完，但将请求延迟和重试等待强制设为 0，用于快速验证流水线本身是否正常运行。
    不会触发真实网络请求，也不会消耗 SerpAPI 配额。
    """

# -------------- 指数退避重试助手 --------------
# ---------------------------------------------------------------- retry helper
def fetch_with_retry(
    fetcher: FetcherAdapter,
    route: Route,
    depart_date: str,
    backoffs: list,
    sleep_fn: Callable[[float], None],
) -> list:
    """Try fetch, retrying retryable FetchErrors with the given backoffs."""
    attempts = len(backoffs) + 1
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fetcher.fetch(route, depart_date)
        except FetchError as e:
            last_exc = e
            if not e.retryable or i >= len(backoffs):
                raise    # 如果是不允许重试的错误，或者已达到最大重试次数，直接抛给上层做源级别降级
            wait = backoffs[i]    # 读取退避间隔（如 30s, 90s）
            log.warning("  %s retryable error (%s), backoff %ss then retry %d/%d",
                        fetcher.name, e, wait, i + 1, len(backoffs))
            sleep_fn(wait)
    if last_exc:
        raise last_exc
    return []

"""精细化重试控制：
结合第二阶段定义的 FetchError(retryable)，只对网络抖动等可重试错误进行间隔等待重试；
如果是配额满等不可重试错误，则跳过重试，直接抛出异常让上层迅速降级（Degrade）到下一个数据源。
"""

# -------------- 连续失败追踪与状态持久化 --------------
# ---------------------------------------------------------------- failures state
def _load_failures() -> dict:
    path = os.path.join(STATE_DIR, "failures.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_failures(data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "failures.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _record_route_result(failures: dict, route_id: str, success: bool, day: str) -> None:
    entry = failures.get(route_id, {"consecutive_failures": 0, "last_success": None, "last_failure": None})
    if success:
        entry["consecutive_failures"] = 0
        entry["last_success"] = day
    else:
        entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
        entry["last_failure"] = day
    failures[route_id] = entry

"""状态感知的可观测性：
将每条航线的运行结果写入 state/failures.json。
通过记录 consecutive_failures（连续失败次数），主控系统能够清楚知道哪条航线已经“失联”了几天，
为后续的系统心跳健康度统计（Heartbeat Stats）提供数据来源。
"""

# ---------------------------------------------------------------- pipeline
def _detail_score(q) -> int:
    """How much flight detail a quote carries (airline/flight_no/depart_time).

    Used to break price ties: when several quotes share the day's lowest price,
    keep the one that actually has airline/航班号/起飞时间 filled in. Otherwise a
    record with an empty flight_no could win the tie and the Feishu digest /
    dashboard would show a price with no flight info (the YUL→PEK 缺时刻 bug).
    """
    return sum(1 for v in (getattr(q, "airline", ""), getattr(q, "flight_no", ""),
                           getattr(q, "depart_time", "")) if str(v or "").strip())

# -------------- 每日平价优选标记 --------------
def mark_lowest_of_day(quotes: list) -> None:
    """Set is_lowest_of_day on the cheapest quote per (route_id, depart_date).

    Ties on price are broken toward the record with the most flight detail.
    """
    groups: dict = {}
    for q in quotes:
        groups.setdefault((q.route_id, q.depart_date), []).append(q)
    for items in groups.values():
        for q in items:
            q.is_lowest_of_day = False
        # 价格低的优先；价格相同时，带有航班号/时间的“富数据”优先！
        cheapest = min(items, key=lambda q: (q.price, -_detail_score(q)))
        cheapest.is_lowest_of_day = True
"""解耦展示层：
在写入磁盘前，对本次 Run 抓取到的所有航班，
按 (route_id, depart_date) 分组，打上 is_lowest_of_day = True 的标志位，大幅降低了下游生成每日飞书卡片时的计算复杂度。
"""

# -------------- 流水线主控制器 run() --------------

def run(
    config_path: str = DEFAULT_CONFIG,
    dry_run: bool = False,
    routes_filter: Optional[list] = None,
    backoffs: Optional[list] = None,
    request_interval: Optional[float] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
# =========================================================================
    # 【新增健壮性防护 1】自动创建基础数据与状态目录（防止全新环境抛出 FileNotFoundError）
    # =========================================================================
    quotes_dir = os.path.join(DATA_DIR, "quotes")
    os.makedirs(quotes_dir, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(os.path.join(DOCS_DIR, "data"), exist_ok=True)


    # 1. 初始化 & 加载配置 (Config & Storage)
    cfg = load_config(config_path)
    today = today_shanghai()
    storage = Storage(DATA_DIR, DOCS_DIR)
    failures = _load_failures()

    # Backoff / inter-request sleep: injectable (0 for dry-run/tests).
    if backoffs is None:
        backoffs = list(cfg.defaults.get("retry_backoffs_seconds", [30, 90]))
    if request_interval is None:
        request_interval = float(cfg.defaults.get("request_interval_seconds", 10))
    if dry_run:
        backoffs = [0 for _ in backoffs]
        request_interval = 0


    # 2. 遍历航线与日期，逐源尝试 (Fetch with Retries & Fallback)
    #    [Mock / fast_flights / serpapi / ctrip_local]
    all_quotes: list = []
    touched_routes: list = []

    for route in cfg.routes:
        if routes_filter and route.id not in routes_filter:
            continue
        if not route.enabled and not dry_run:
            log.info("route %s disabled, skipping", route.id)
            continue

        touched_routes.append(route.id)
        dates = resolve_dates(route, today)
        log.info("route %s -> %d dates", route.id, len(dates))
        route_success = False

        for depart_date in dates:
            sources = ["mock"] if dry_run else route.sources
            got = False
            for source in sources:
                fetcher = MockFetcher() if source == "mock" else get_fetcher(source)
                if fetcher is None:
                    log.warning("  source %s not registered, skipping", source)
                    continue
                if not fetcher.available():
                    log.info("  source %s unavailable, degrade to next", source)
                    continue
                try:
                    quotes = fetch_with_retry(fetcher, route, depart_date, backoffs, sleep_fn)
                    all_quotes.extend(quotes)
                    got = True
                    route_success = True
                    log.info("  %s %s via %s: %d quotes", route.id, depart_date, source, len(quotes))
                    break  # success on this source, stop trying others
                except FetchError as e:
                    log.warning("  %s %s via %s failed: %s -> degrade", route.id, depart_date, source, e)
                    continue
            if not got:
                log.warning("  %s %s: all sources failed", route.id, depart_date)
            # Anti-throttle pause between requests (0 in dry-run/tests).
            if request_interval:
                sleep_fn(request_interval)

        _record_route_result(failures, route.id, route_success, today.isoformat())

    # Mark lowest-of-day, dedup + persist.
    mark_lowest_of_day(all_quotes)
    written = storage.append_quotes(all_quotes)
    log.info("wrote %d new quotes (of %d fetched)", written, len(all_quotes))

    _save_failures(failures)

    # SerpAPI remaining quota for the dashboard, if that fetcher is around.
    meta = {}
    serp = get_fetcher("serpapi")
    if serp is not None and hasattr(serp, "remaining_quota"):
        try:
            meta["serpapi_remaining_quota"] = serp.remaining_quota()  # type: ignore[attr-defined]
        except Exception:
            pass


    # 3. 数据处理与落盘 (storage.append_quotes -> storage.build_summary)
    # Run statistics for the digest heartbeat (consumed by src.notifiers).
    routes_ok = sum(
        1 for rid in touched_routes
        if int(failures.get(rid, {}).get("consecutive_failures", 0)) == 0
    )
    meta["run_stats"] = {
        "routes_total": len(touched_routes),
        "routes_ok": routes_ok,
        "routes_failed": len(touched_routes) - routes_ok,
        "fetched_count": len(all_quotes),
        "run_date": today.isoformat(),
    }

    # Only surface routes that still exist in the config: deleted routes keep
    # their historical data/ folder (never pruned) but must NOT reappear in the
    # dashboard summary or Feishu cards.
    summary = storage.build_summary(route_ids=[r.id for r in cfg.routes], extra=meta)
    log.info("summary.json built: %d routes", len(summary.get("routes", {})))
 

    # 4. 日报摘要信息增强 (src.enrich)
    #    └─ 补全 headline（精确时刻/中转/行李额）
    # --- SerpAPI 日报航班详情增强：为每条 enabled 航线最便宜的 route×date 回填
    #     真航班号/精确时刻/机型/中转机场/行李标记（summary[...]["headline"]）。
    #     受 config.enrich + 月额度守卫双限制；无 key / dry-run 时整段跳过（回退纯
    #     fast-flights 展示，绝不报错）。 ---
    enrich_cfg = getattr(cfg, "enrich", {}) or {}
    if enrich_cfg.get("enabled") and not dry_run:
        try:
            from src.enrich import enrich_summary  # type: ignore
            est = enrich_summary(
                cfg, summary, serp_fetcher=get_fetcher("serpapi"),
                max_per_run=int(enrich_cfg.get("max_per_run", 3)),
                which=str(enrich_cfg.get("which", "cheapest_per_route")),
                sleep_fn=sleep_fn, request_interval=request_interval,
            )
            log.info("digest enrichment: %s", est)
            if est.get("enriched"):
                storage.persist_summary(summary)  # re-write so dashboard sees headlines
        except Exception as e:  # never let enrichment crash the pipeline
            log.error("digest enrichment raised: %s", e)


    # 5. 触发告警引擎与通知 hook (src.alerts & src.notifiers)
    #    └─ 使用 try/except ImportError 动态解耦 hook
    # --- Hooks for later agents (M2). Modules may not exist yet. ---
    alerts: list = []
    try:
        from src.alerts.engine import run_alerts  # type: ignore
        alerts = run_alerts(cfg, storage, summary) or []
        log.info("alert engine ran: %d alerts", len(alerts))
    except ImportError:
        log.info("alert engine (src.alerts.engine) not present yet, skipping hook")
    except Exception as e:  # never let alerting crash the data pipeline  💥 核心护栏：绝不允许非核心 Hook 崩溃打断主流水线！
        log.error("alert engine raised: %s", e)

    try:
        from src.notifiers import dispatch  # type: ignore
        dispatch(cfg, summary, alerts=alerts)
        log.info("notifier dispatch ran")
    except ImportError:
        log.info("notifiers (src.notifiers.dispatch) not present yet, skipping hook")
    except Exception as e:
        log.error("notifier dispatch raised: %s", e)


    # 6. 运行隐藏城市/甩尾机票监控 (src.hidden_city)
    # --- Hidden-city (中转中国) monitor: runs AFTER the regular routes. ---
    # Never allowed to crash the data pipeline; strictly budget-guarded.
    try:
        from src.hidden_city import run_hidden_city  # type: ignore
        from src.notifiers import dispatch_hidden_city  # type: ignore
        hc_fast = MockFetcher() if dry_run else None
        hc_result = run_hidden_city(
            cfg, DATA_DIR, DOCS_DIR, today=today,
            fast_fetcher=hc_fast,
            sleep_fn=sleep_fn, request_interval=request_interval, dry_run=dry_run,
        )
        hc_hits = hc_result.get("hits", [])
        log.info("hidden_city ran: %d hits", len(hc_hits))
        dispatch_hidden_city(cfg, hc_hits)
    except ImportError:
        log.info("hidden_city module not present, skipping hook")
    except Exception as e:  # never let hidden-city crash the pipeline
        log.error("hidden_city raised: %s", e)

    return {
        "routes": touched_routes,
        "fetched": len(all_quotes),
        "written": written,
        "summary_routes": list(summary.get("routes", {}).keys()),
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="flight-watch daily pipeline")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true",
                        help="use MockFetcher fake data, backoff/sleep=0")
    parser.add_argument("--routes", default=None,
                        help="comma-separated route ids to run")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

    routes_filter = [r.strip() for r in args.routes.split(",")] if args.routes else None
    result = run(config_path=args.config, dry_run=args.dry_run, routes_filter=routes_filter)
    log.info("DONE: %s", result)
    if args.dry_run:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
