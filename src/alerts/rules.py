"""Alert rules + self-registration registry (milestone M2).

Adding a new alert = new ``AlertRule`` subclass decorated with
``@register_rule``. The engine evaluates every registered rule against each
``(route_id, depart_date)`` node of the dashboard summary and collects the
:class:`Alert` objects the rules emit.

Rules never touch the network and never crash the pipeline: an exception inside
``evaluate`` is swallowed by the engine (defence in depth), but rules are
written to simply return ``None`` when they do not fire.



采用了标准的 策略模式（Strategy Pattern） 与 自注册注册表（Self-Registration Registry），将“告警条件判断”与“引擎调度”高度解耦。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # avoid import cycle / heavy imports at module load
    from ..config import Config, Route
    from ..storage import Storage


# --------------------------------------------------------------------- Alert
@dataclass
class Alert:
    """A single alert emitted by a rule (or the failure watchdog).

    ``level`` is the *effective* disposition after the engine's merge step:
      ``"urgent"``  -> single push now (and also shown in the daily digest)
      ``"normal"``  -> folded into the daily digest only
    Rules emit their intended level; the engine may downgrade ``urgent`` ->
    ``normal`` on 24h dedup or the global daily cap.
    """

    rule_id: str
    level: str
    route_id: str
    depart_date: str
    price: Optional[float] = None
    prev_price: Optional[float] = None
    target_price: Optional[float] = None
    message: str = ""

    def key(self) -> str:
        """Dedup key for urgent single-push throttling."""
        return f"{self.route_id}|{self.depart_date}"

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "level": self.level,
            "route_id": self.route_id,
            "depart_date": self.depart_date,
            "price": self.price,
            "prev_price": self.prev_price,
            "target_price": self.target_price,
            "message": self.message,
        }


# ------------------------------------------------------------------ context
@dataclass
class RuleContext:
    """Everything a rule needs to decide whether to fire, for one node."""

    route: "Route"
    route_id: str
    depart_date: str
    node: dict          # summary node: {"latest", "historical_low", "series"}
    storage: "Storage"
    cfg: "Config"

    @property
    def latest(self) -> Optional[dict]:
        return self.node.get("latest")

    @property
    def series(self) -> list:
        return self.node.get("series") or []

# -------------- 装饰器自注册机制 ------------
# ------------------------------------------------------------------ registry
REGISTRY: dict[str, type] = {}


def register_rule(cls: type) -> type:
    """Class decorator: register an :class:`AlertRule` by its ``rule_id``."""
    """类装饰器：通过 rule_id 将 AlertRule 子类自动注册到全局 REGISTRY 字典中"""
    REGISTRY[cls.rule_id] = cls
    return cls

"""极佳的开闭原则（OCP）:
后续如果需要新增一条规则（例如“连续 3 天降价规则”），
只需继承 AlertRule 并加上 @register_rule 装饰器即可，完全不需要修改 engine.py 的任何一行代码！
"""

class AlertRule(ABC):
    #: unique identifier, also used as ``Alert.rule_id``
    rule_id: str = "base"

    @abstractmethod
    def evaluate(self, ctx: RuleContext) -> Optional[Alert]:
        """Return an :class:`Alert` if the rule fires for ``ctx`` else ``None``."""
        raise NotImplementedError


def _fmt(v) -> str:
    """Format a price-ish number without a trailing ``.0`` for whole values."""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:.2f}"

# -------------- 规则 1：目标价触发器 BelowTargetRule ------------
# -------------------------------------------------------------------- rules
@register_rule
class BelowTargetRule(AlertRule):
    """Fire (urgent) when today's lowest price is below the route target."""
    """当今日最低价低于设定的航线目标价 (target_price) 时触发"""

    rule_id = "below_target"

    def evaluate(self, ctx: RuleContext) -> Optional[Alert]:
        target = ctx.route.target_price
        latest = ctx.latest
        if target is None or not latest:
            return None
        price = latest.get("price")
        # 核心条件判断：价格低于目标价
        if price is None or price >= target:
            return None
        cur = latest.get("currency", "CNY")
        msg = (
            f"{ctx.route.origin}->{ctx.route.dest} {ctx.depart_date} "
            f"今日最低 {_fmt(price)} {cur}，已低于目标价 {_fmt(target)}"
        )
        return Alert(
            rule_id=self.rule_id,
            level="urgent",     # ⚡ 预设为紧急告警 (Urgent Single-push)
            route_id=ctx.route_id,
            depart_date=ctx.depart_date,
            price=price,
            prev_price=None,
            target_price=target,
            message=msg,
        )

# -------------- 规则 2：跌幅百分比触发器 DropPctRule ------------
@register_rule
class DropPctRule(AlertRule):
    """Fire when the latest fetch's low dropped >= ``drop_alert_pct`` vs the
    previous fetch's low for the same depart_date.

    A drop of >= 2x the threshold is ``urgent``; otherwise ``normal``.
    """
    """当最新价格较上一次抓取下降达到阈值 drop_alert_pct 时触发"""

    rule_id = "drop_pct"

    def evaluate(self, ctx: RuleContext) -> Optional[Alert]:
        threshold = ctx.route.drop_alert_pct
        series = ctx.series
        if threshold is None or len(series) < 2:
            return None
        prev = series[-2].get("price")   # 上一次抓取价格
        cur = series[-1].get("price")    # 当前抓取价格
        if not prev or cur is None or prev <= 0:
            return None
        drop_pct = (prev - cur) / prev * 100.0
        if drop_pct < threshold:
            return None
        # ⚡ 梯级判定：跌幅超标 2 倍以上为 urgent，否则为 normal (日报汇总)
        level = "urgent" if drop_pct >= 2 * threshold else "normal"
        msg = (
            f"{ctx.route.origin}->{ctx.route.dest} {ctx.depart_date} "
            f"较上次抓取下降 {drop_pct:.1f}%（{_fmt(prev)} -> {_fmt(cur)}），"
            f"阈值 {_fmt(threshold)}%"
        )
        return Alert(
            rule_id=self.rule_id,
            level=level,
            route_id=ctx.route_id,
            depart_date=ctx.depart_date,
            price=cur,
            prev_price=prev,
            target_price=ctx.route.target_price,
            message=msg,
        )

# -------------- 规则 3：历史新低触发器与冷启动保护 ------------
#: minimum number of recorded fetch-days before ``historical_low`` may fire.
HISTORICAL_MIN_DAYS = 7    # 保护门槛：至少积累 7 天历史数据


@register_rule
class HistoricalLowRule(AlertRule):
    """Fire (urgent) when the latest fetch sets a new all-time low for the
    route x depart_date, requiring >= 7 days of recorded history first
    (cold start never triggers).
    """
    """当最新价格刷新该 (route_id x depart_date) 的历史最低纪录时触发"""

    rule_id = "historical_low"

    def evaluate(self, ctx: RuleContext) -> Optional[Alert]:
        series = ctx.series
        # Need >= 7 recorded fetch-days of history (cold start: no fire).
        # 🛡️ 冷启动防骚扰护栏：数据未满 7 天绝对不触发历史新低告警
        if len(series) < HISTORICAL_MIN_DAYS:
            return None
        cur = series[-1].get("price")
        prior = [s.get("price") for s in series[:-1] if s.get("price") is not None]
        if cur is None or not prior:
            return None
        prior_min = min(prior)
        if cur >= prior_min:  # not a new low
            return None
        cur_ccy = series[-1].get("currency", "CNY")
        msg = (
            f"{ctx.route.origin}->{ctx.route.dest} {ctx.depart_date} "
            f"创历史新低 {_fmt(cur)} {cur_ccy}（前低 {_fmt(prior_min)}，"
            f"{len(series)} 天历史）"
        )
        return Alert(
            rule_id=self.rule_id,
            level="urgent",
            route_id=ctx.route_id,
            depart_date=ctx.depart_date,
            price=cur,
            prev_price=prior_min,
            target_price=ctx.route.target_price,
            message=msg,
        )
"""痛点解决（避免冷启动假报警）:
如果系统刚部署第 1 天，第一次抓到的价格“天然”就是历史最低，如果不加过滤就会误发“创历史新低！”的垃圾推送。
设定 HISTORICAL_MIN_DAYS = 7 确保只有积累了充分的样本空间后，创出的“新低”才具备真正的参考价值。
"""