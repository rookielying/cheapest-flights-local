"""Alert engine — evaluate rules, add the failure watchdog, apply the merge /
throttle strategy (milestone M2).

Called from the pipeline via ``run_alerts(cfg, storage, summary)``.

Merge strategy (report + M2 brief):
  * ``normal`` alerts   -> all folded into the daily digest.
  * ``urgent`` alerts   -> single push, but at most once per (route_id,
    depart_date) within ``alerts.urgent_dedup_hours`` (default 24h), recorded in
    ``state/alert_sent.json``; and a global daily cap of
    ``alerts.max_urgent_per_day`` (default 5). Anything deduped or over the cap
    is downgraded to ``normal`` (still shown in the digest).

Failure watchdog: reads ``state/failures.json``; any route with
``consecutive_failures >= 2`` yields an urgent system alert.


负责收集所有规则产生的原始告警，并施加防打扰去重、熔断限流与系统监控。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from ..models import now_shanghai, SHANGHAI
from .rules import REGISTRY, Alert, RuleContext

log = logging.getLogger("flight_watch.alerts")

# Repo root = parent of src/  -> default state dir.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_STATE_DIR = os.path.join(_ROOT, "state")




FAILURE_THRESHOLD = 2  # consecutive failures before a system alert fires   连续失败阈值


# ------------------------------------------------------------ state helpers
def _load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt

# -------------- 系统失败狗 _failure_alerts --------------
# ---------------------------------------------------------- failure watchdog
def _failure_alerts(state_dir: str) -> list[Alert]:
    """读取 state/failures.json，针对连续失败 >= 2 次的数据源生成系统级 Urgent 告警"""
    failures = _load_json(os.path.join(state_dir, "failures.json"))
    out: list[Alert] = []
    for route_id, entry in failures.items():
        if not isinstance(entry, dict):
            continue
        n = int(entry.get("consecutive_failures", 0) or 0)
        if n >= FAILURE_THRESHOLD:
            out.append(Alert(
                rule_id="source_failure",
                level="urgent",
                route_id=route_id,
                depart_date="",
                price=None,
                prev_price=None,
                target_price=None,
                message=f"数据源连续 {n} 天无数据（route={route_id}）",
            ))
    return out
"""自愈与维稳保障：
不仅监控机票价格，还监控监控系统本身！
当某个航线因为目标网站改版或 Anti-bot 升级导致连续 2 次抓不到数据时，触发 urgent 系统告警通知开发者修复。
"""

# -------------- 核心降级与限流算法 _apply_merge --------------
# ------------------------------------------------------------- merge / cap
def _apply_merge(raw: list[Alert], cfg, state_dir: str, now: datetime) -> list[Alert]:
    alerts_cfg = getattr(cfg, "alerts", {}) or {}
    max_urgent = int(alerts_cfg.get("max_urgent_per_day", 5))        # 单日最多单发 5 条
    dedup_hours = float(alerts_cfg.get("urgent_dedup_hours", 24))    # 同航线x日期 24h 只能单发 1 条

    sent_path = os.path.join(state_dir, "alert_sent.json")
    sent = _load_json(sent_path)
    today = now.date().isoformat()

    # How many urgent single-pushes already went out today.
    urgent_today = sum(1 for v in sent.values()
                       if isinstance(v, dict) and v.get("date") == today)

    result: list[Alert] = []
    for a in raw:
        if a.level != "urgent":
            result.append(a)
            continue

        key = a.key()
        last = sent.get(key)
        last_ts = _parse_ts(last["ts"]) if isinstance(last, dict) and last.get("ts") else None

        # 1. 24h 时间窗口去重检查
        # 24h dedup: same route x depart_date pushed recently -> digest only.
        if last_ts is not None and (now - last_ts) < timedelta(hours=dedup_hours):
            a.level = "normal"    # 🛡️ 降级为 normal (只发日报，不单推)
            result.append(a)
            continue

        # 2. 全局单日最大单推送配额 (熔断机制)
        # Global daily cap (circuit breaker) -> downgrade to digest.
        if urgent_today >= max_urgent:
            a.level = "normal"    # 🛡️ 超额降级为 normal
            result.append(a)
            log.info("urgent cap reached (%d), downgrading %s %s to digest",
                     max_urgent, a.route_id, a.depart_date)
            continue

        # 3. 获得单发资格，记录状态
        # Keep as urgent single-push; record for future dedup.
        sent[key] = {"ts": now.isoformat(), "date": today}
        urgent_today += 1
        result.append(a)

    _save_json(sent_path, sent)
    return result


# -------------- 主入口 run_alerts 调度管线 --------------
# ----------------------------------------------------------------- public
def run_alerts(cfg, storage, summary: dict,
               state_dir: str = DEFAULT_STATE_DIR,
               now: Optional[datetime] = None) -> list[Alert]:
    """Evaluate all rules over the summary, add failure alerts, apply the
    merge/throttle strategy, and return the resulting :class:`Alert` list.

    The returned list is what the notifier layer consumes: entries with
    ``level == "urgent"`` are single-pushed; every entry (urgent + normal) is
    available for the daily digest.
    """
    now = now or now_shanghai()
    raw: list[Alert] = []

    routes_summary = (summary or {}).get("routes", {})
    # 1. 遍历 summary 中的每一个 (route_id, depart_date) 节点
    for route_id, rdata in routes_summary.items():
        route = cfg.route_by_id(route_id)
        if route is None:
            continue
        for depart_date, node in (rdata.get("depart_dates", {}) or {}).items():
            ctx = RuleContext(
                route=route, route_id=route_id, depart_date=depart_date,
                node=node, storage=storage, cfg=cfg,
            )

            # 2. 依次评估全局注册表中的每一条规则
            for rule_cls in REGISTRY.values():
                try:
                    alert = rule_cls().evaluate(ctx)
                except Exception as e:  # a broken rule must not sink the run
                # 🛡️ 防御性隔离：单条规则崩溃绝不拖垮整个 Alert 管线
                    log.warning("rule %s crashed on %s %s: %s",
                                getattr(rule_cls, "rule_id", "?"),
                                route_id, depart_date, e)
                    continue
                if alert is not None:
                    raw.append(alert)

    # 3. 追加系统失败监控告警
    # Failure watchdog (system alerts) participate in dedup / cap too.
    raw.extend(_failure_alerts(state_dir))


    # 4. 执行限流去重与降级合并
    merged = _apply_merge(raw, cfg, state_dir, now)
    n_urgent = sum(1 for a in merged if a.level == "urgent")
    log.info("alerts: %d raw -> %d total (%d urgent single-push)",
             len(raw), len(merged), n_urgent)
    return merged
