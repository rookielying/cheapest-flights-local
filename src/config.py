"""Configuration loading and date resolution for flight-watch.

Source-of-truth policy (2026-07 fix): ``config.json`` is the single canonical
config. The web settings panel writes ``config.json`` reliably (structured JSON)
and only *mirrors* a human-readable ``config.yaml`` for eyeballing. Because the
panel's YAML mirror generation had indentation/leftover-line bugs that could
break ``yaml.safe_load`` and thus the scheduled run, loading now **prefers
``config.json`` whenever it exists** (next to, or as the sibling of, the given
path) and treats ``config.yaml`` as a fallback only. This makes the pipeline
immune to any YAML-mirror formatting glitch.
"""

from __future__ import annotations

import json     # 于解析配置
import logging   # 用于输出安全注入与配置加载日志
import os
from dataclasses import dataclass, field  # 用于定义强类型数据模型
from datetime import date, timedelta
from typing import Any, Optional

log = logging.getLogger("flight_watch.config")

# ------------------ 全局成本与采样控制常量 ------------------
#: hard upper bound on the number of concrete depart_dates generated per route 硬性成本护栏
#: (protects the daily抓取 runtime — each date ≈ 17s of fetching).
MAX_DATES_PER_ROUTE = 60
#: rolling scalar窗口：前 DAILY_WINDOW 天逐日采样，之后每 SPARSE_STEP 天采样一次。
ROLLING_DAILY_WINDOW = 30
ROLLING_SPARSE_STEP = 3

# ------------------ 可选依赖退避降级机制 ------------------
""" 双镜像回退（Yaml ↔ Json）：GitHub Actions 或精简版 Docker 容器偶尔会因网络或底包原因导致 pip install PyYAML 失败。
代码中检测 HAS_YAML，失效时优雅读取预先同步好的 config.json，确保流水线不会在初始化阶段雪崩。
"""
try:  # PyYAML is preferred but optional (sandbox / offline degradation).
    import yaml  # type: ignore
    _HAS_YAML = True
except Exception:  # pragma: no cover - only hit when PyYAML missing
    yaml = None  # type: ignore
    _HAS_YAML = False

# ------------------ 数据模型类：Route 航线配置 ------------------
@dataclass
class Route:
    id: str
    origin: str
    dest: str
    dates: dict
    airlines: dict = field(default_factory=lambda: {"whitelist": [], "blacklist": []})
    target_price: Optional[float] = None
    drop_alert_pct: Optional[float] = None
    sources: list = field(default_factory=lambda: ["fast_flights"])
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Route":
        airlines = d.get("airlines") or {}
        return cls(
            id=d["id"],
            origin=d.get("from") or d.get("origin"),
            dest=d.get("to") or d.get("dest"),
            dates=d.get("dates") or {},
            airlines={
                "whitelist": list(airlines.get("whitelist", []) or []),
                "blacklist": list(airlines.get("blacklist", []) or []),
            },
            target_price=d.get("target_price"),
            drop_alert_pct=d.get("drop_alert_pct"),
            sources=list(d.get("sources", ["fast_flights"]) or ["fast_flights"]),
            enabled=bool(d.get("enabled", True)),
        )

# ------------------ 隐藏城市 (Hidden City) 成本护栏与配置 ------------------
#: default per-onward sampling cap for隐藏城市抓取 (protects daily runtime —
#: onward_routes × dates can explode; each fast-flights query ≈ 17s).
HIDDEN_CITY_MAX_DATES = 15

#: 默认「中国承运人」名单（航司全名/常见写法；子串匹配、大小写不敏感）。用于把有限的
#: SerpAPI 确认额度优先花在「疑似经中国大陆中转」的候选上。注意：国泰(Cathay)经香港
#: HKG 中转，而 HKG 不在 chinese_hubs 内，纳入只会浪费额度确认永不命中的候选，故默认
#: 不含 Cathay——与 chinese_hubs 口径保持一致。用户可在 config 里覆盖 cn_carriers。
DEFAULT_CN_CARRIERS = [
    "Air China",
    "China Eastern",
    "China Southern",
    "Xiamen Air",
    "XiamenAir",
    "Hainan Airlines",
    "Shenzhen Airlines",
    "Sichuan Airlines",
    "Shanghai Airlines",
    "Juneyao",
    "Spring Airlines",
    "Beijing Capital",
]


@dataclass
class HiddenCityConfig:
    """隐藏城市（中转中国）特价票监控配置（config.json 顶层 ``hidden_city`` 段）。

    监控「从 ``origin`` 出发、飞往 ``onward_routes`` 里某个延伸目的地、但中途在
    ``chinese_hubs`` 里某个中国城市中转」的跳程/隐藏城市票——真实目的地其实是那个
    中转的中国城市，用户只飞第一段。
    """

    enabled: bool = False
    origin: str = ""
    onward_routes: list = field(default_factory=list)
    chinese_hubs: list = field(default_factory=list)
    dates: dict = field(default_factory=lambda: {"mode": "rolling", "depart_in_days": 45})
    max_serpapi_per_run: int = 10
    min_saving_pct: float = 0.0
    #: 每条 onward_route 最多采样多少个日期（成本护栏）。
    max_dates_per_onward: int = HIDDEN_CITY_MAX_DATES
    #: fast-flights 直飞基线最多查几次（成本护栏；0 = 不查直飞基线）。
    max_direct_lookups: int = 6
    #: True=只把 SerpAPI 确认额度花在「疑似中国承运人」候选上（没有则本次不花额度，
    #: 把额度留给日报增强/其他运行）；False=疑似优先、有余额再确认最便宜的其它候选。
    confirm_only_suspected: bool = True
    #: 中国承运人名单（子串匹配、大小写不敏感），用于候选优先级排序。
    cn_carriers: list = field(default_factory=lambda: list(DEFAULT_CN_CARRIERS))

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "HiddenCityConfig":
        d = d or {}
        cn = d.get("cn_carriers")
        return cls(
            enabled=bool(d.get("enabled", False)),
            origin=str(d.get("origin", "") or "").upper(),
            onward_routes=[str(x).upper() for x in (d.get("onward_routes") or [])],
            chinese_hubs=[str(x).upper() for x in (d.get("chinese_hubs") or [])],
            dates=d.get("dates") or {"mode": "rolling", "depart_in_days": 45},
            max_serpapi_per_run=int(d.get("max_serpapi_per_run", 10) or 0),
            min_saving_pct=float(d.get("min_saving_pct", 0) or 0),
            max_dates_per_onward=int(d.get("max_dates_per_onward", HIDDEN_CITY_MAX_DATES)
                                     or HIDDEN_CITY_MAX_DATES),
            max_direct_lookups=int(d.get("max_direct_lookups", 6) or 0),
            confirm_only_suspected=bool(d.get("confirm_only_suspected", True)),
            cn_carriers=[str(x) for x in cn] if cn else list(DEFAULT_CN_CARRIERS),
        )

"""
额度保护机制：隐藏城市抓取的组合数量极易爆炸（$Onward\_Routes \times Dates$）。代码通过 HIDDEN_CITY_MAX_DATES (15)、
max_direct_lookups (6) 和 confirm_only_suspected (True) 层层设置成本拦截网，保证只对疑似中国国籍航司花付费额度。
DEFAULT_CN_CARRIERS 明确剔除国泰 (Cathay)：注释详细说明了原因 —— 国泰经香港 (HKG) 中转，不在大陆 chinese_hubs 列表内，
将 Cathay 纳入只会白白浪费 SerpAPI 额度。
"""

# ------------------ 主配置容器 Config 类  ------------------
@dataclass
class Config:
    timezone: str
    defaults: dict
    routes: list
    cross_check: dict
    alerts: dict
    notifiers: dict
    dashboard: dict
    raw: dict
    hidden_city: Optional[HiddenCityConfig] = None
    #: 日报航班详情增强段（config.json 顶层 ``enrich``）：
    #: {"enabled": bool, "max_per_run": int, "which": str}
    enrich: dict = field(default_factory=lambda: {
        "enabled": False, "max_per_run": 3, "which": "cheapest_per_route"})

    def route_by_id(self, route_id: str) -> Optional[Route]:
        for r in self.routes:
            if r.id == route_id:
                return r
        return None
# route_by_id() 辅助方法：提供通过 route_id 快速检索 Route 对象的接口，便于后续告警引擎和主流程调用


# JSON 优先的配置加载函数 _load_raw
def _load_raw(path: str) -> dict:
    """Load the raw config dict.

    ``config.json`` is the single source of truth (see module docstring): it is
    preferred whenever present, regardless of whether ``path`` points at the
    JSON or the YAML mirror. YAML is only parsed when no JSON is available.
    """
    # 1) Explicit .json path -> load it directly.
    if path.endswith(".json") and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    # 2) Any path whose .json sibling exists -> JSON wins (authoritative mirror,
    #    immune to YAML-panel formatting bugs). For a .json path this is itself;
    #    for config.yaml this is config.json next to it.
    sibling = os.path.splitext(path)[0] + ".json"
    if os.path.exists(sibling):
        with open(sibling, "r", encoding="utf-8") as f:
            return json.load(f)
    # 3) Fallback: parse the YAML mirror only when no JSON exists and PyYAML is
    #    installed.
    if path.endswith((".yaml", ".yml")) and _HAS_YAML and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    raise FileNotFoundError(
        f"No loadable config found for {path!r} (need a .json sibling or PyYAML for .yaml)"
    )

"""
核心算法：JSON 胜出（JSON-Wins Policy）：
1. 如果传入路径是 .json 且存在，直接读取 JSON；
2. 如果传入路径是 config.yaml，优先查找同级目录下的 config.json 并读取它；
3. 只有当同级目录下完全不存在 .json 文件时，才降级解析 YAML。
目的：彻底免疫前端 Web 面板生成 YAML 时的缩进错乱Bug
"""


"""
def load_config(path: str) -> Config:
    raw = _load_raw(path)
    routes = [Route.from_dict(r) for r in raw.get("routes", [])]
    return Config(
        timezone=raw.get("timezone", "Asia/Shanghai"),
        defaults=raw.get("defaults", {}) or {},
        routes=routes,
        cross_check=raw.get("cross_check", {}) or {},
        alerts=raw.get("alerts", {}) or {},
        notifiers=raw.get("notifiers", {}) or {},
        dashboard=raw.get("dashboard", {}) or {},
        raw=raw,
        hidden_city=HiddenCityConfig.from_dict(raw.get("hidden_city")),
        enrich={
            "enabled": bool((raw.get("enrich") or {}).get("enabled", False)),
            "max_per_run": int((raw.get("enrich") or {}).get("max_per_run", 3) or 0),
            "which": str((raw.get("enrich") or {}).get("which", "cheapest_per_route")),
        },
    )

"""
# ------------------ 环境变量级联覆盖与 load_config  ------------------

def load_config(path: str) -> Config:
    # 1. 依然使用项目最权威的 JSON/YAML 镜像回退逻辑加载原始字典
    raw = _load_raw(path)

    # ========================== 🔒 核心安全性级联升级开始 ==========================
    # 安全加固策略 (2026-07 修复)：如果项目开源，明文密钥禁止 Push 进 Git。
    # 逻辑：优先从云端环境 (如 GitHub Actions Secrets) 抓取真实凭证，如果没有才降级看配置文件。

    # 1) 飞书机器人 Webhook 动态覆盖
    env_feishu_webhook = os.environ.get("FEISHU_WEBHOOK")
    if env_feishu_webhook:
        if "notifiers" not in raw or raw["notifiers"] is None:
            raw["notifiers"] = {}
        if "feishu" not in raw["notifiers"] or raw["notifiers"]["feishu"] is None:
            raw["notifiers"]["feishu"] = {}
        # 强制强力覆盖字典内的明文 url
        raw["notifiers"]["feishu"]["webhook_url"] = env_feishu_webhook
        log.info("🔒 成功从系统环境变量中安全注入：FEISHU_WEBHOOK")

    # 2) SerpAPI 密钥动态覆盖
    env_serpapi_key = os.environ.get("SERPAPI_KEY")
    if env_serpapi_key:
        if "cross_check" not in raw or raw["cross_check"] is None:
            raw["cross_check"] = {}
        if "serpapi" not in raw["cross_check"] or raw["cross_check"]["serpapi"] is None:
            raw["cross_check"]["serpapi"] = {}
        # 强制强力覆盖字典内的明文 key
        raw["cross_check"]["serpapi"]["api_key"] = env_serpapi_key
        log.info("🔒 成功从系统环境变量中安全注入：SERPAPI_KEY")
    # ========================== 🔒 核心安全性级联升级结束 ==========================

    # 2. 将脱敏/解密后的字典正常转化为强类型 DataClass 对象返回
    routes = [Route.from_dict(r) for r in raw.get("routes", [])]
    return Config(
        timezone=raw.get("timezone", "Asia/Shanghai"),
        defaults=raw.get("defaults", {}) or {},
        routes=routes,
        cross_check=raw.get("cross_check", {}) or {},
        alerts=raw.get("alerts", {}) or {},
        notifiers=raw.get("notifiers", {}) or {},
        dashboard=raw.get("dashboard", {}) or {},
        raw=raw,
        hidden_city=HiddenCityConfig.from_dict(raw.get("hidden_city")),
        enrich={
            "enabled": bool((raw.get("enrich") or {}).get("enabled", False)),
            "max_per_run": int((raw.get("enrich") or {}).get("max_per_run", 3) or 0),
            "which": str((raw.get("enrich") or {}).get("which", "cheapest_per_route")),
        },
    )

"""动态安全注入：
在解析原始字典后、实例化 Config 对象前，读取系统环境变量（GitHub Secrets 注入的内存数据）。
如果存在环境变量，直接覆盖字典中的明文或占位符，实现代码库公开与凭证安全的解耦。
"""




# 稀疏采样算法
def _rolling_offsets(depart_in_days) -> list[int]:
    """Resolve the ``depart_in_days`` field into a list of day offsets (>=1).

    Two accepted forms (report section 6.3 + 2026-07 fix):

      * **list** ``[7, 14, 30]`` -> those exact future offsets (legacy form,
        kept working verbatim).
      * **scalar** ``N`` -> an auto-sampled future window: every day for the
        first ``ROLLING_DAILY_WINDOW`` days, then every ``ROLLING_SPARSE_STEP``
        days out to day ``N``. e.g. ``90`` -> 30 daily + 20 sparse = 50 offsets.
    """
    # List form: explicit offsets.
    if isinstance(depart_in_days, (list, tuple)):
        offsets = []
        for v in depart_in_days:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv >= 1:
                offsets.append(iv)
        return sorted(set(offsets))

    # Scalar form: sampled window.
    try:
        n = int(depart_in_days or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        return []
    offsets = list(range(1, min(n, ROLLING_DAILY_WINDOW) + 1))
    if n > ROLLING_DAILY_WINDOW:
        offsets.extend(range(ROLLING_DAILY_WINDOW + ROLLING_SPARSE_STEP, n + 1,
                             ROLLING_SPARSE_STEP))
    return offsets

# 日期解析函数
def resolve_dates(route: Route, today: date) -> list[str]:
    """Expand a route's ``dates`` block into concrete "YYYY-MM-DD" strings.

    Modes (report section 6.3):
      rolling  -> future offsets from ``depart_in_days`` (scalar sampled window
                  or explicit list; see :func:`_rolling_offsets`)
      fixed    -> the explicit fixed_dates list
      both     -> union of rolling and fixed, de-duplicated and sorted

    A hard cap of :data:`MAX_DATES_PER_ROUTE` concrete dates is enforced per
    route (excess truncated with a warning) to bound the daily抓取 runtime.
    """
    dates_cfg = route.dates or {}
    mode = (dates_cfg.get("mode") or "fixed").lower()
    result: set[str] = set()

    if mode in ("rolling", "both"):
        for i in _rolling_offsets(dates_cfg.get("depart_in_days")):
            result.add((today + timedelta(days=i)).isoformat())

    if mode in ("fixed", "both"):
        for d in dates_cfg.get("fixed_dates", []) or []:
            result.add(str(d))

    ordered = sorted(result)
    if len(ordered) > MAX_DATES_PER_ROUTE:
        log.warning(
            "route %s produced %d dates, truncating to cap %d",
            route.id, len(ordered), MAX_DATES_PER_ROUTE,
        )
        ordered = ordered[:MAX_DATES_PER_ROUTE]
    return ordered
