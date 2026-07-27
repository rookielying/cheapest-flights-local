"""Notifier abstraction + self-registration registry (milestone M2).

Adding a channel = new module implementing :class:`Notifier` decorated with
``@register_notifier("name")`` + a ``notifiers.<name>`` block in config. The
name must match the config key so ``dispatch`` can pair enabled config with the
right class.


# 工厂与策略模式（Factory & Strategy Pattern）结合 装饰器自注册机制
它的核心使命是：统一不同推送渠道（飞书、钉钉、邮件、Telegram 等）的调用接口，并实现核心管线与具体推送渠道的解耦。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

# -------------- 抽象基类 Notifier 接口设计 --------------
class Notifier(ABC):
    #: unique channel name, must match the config ``notifiers`` key
    #: 唯一渠道标识名称，必须与 config 中 notifiers.<name> 的 key 保持一致
    name: str = "base"

    def __init__(self, cfg: Optional[dict] = None):
        #: the ``notifiers.<name>`` config block
        #: 保存 config 中属于当前渠道的配置节点（如 webhook url、secret 等）
        self.cfg = cfg or {}

    @abstractmethod
    def send_digest(self, alerts: list, stats: dict,
                    summary: Optional[dict] = None,
                    routes: Optional[list] = None) -> bool:
        """Send the daily digest (heartbeat). Sent even when ``alerts`` empty.

        ``summary`` (enhanced dashboard summary) and ``routes`` (config Route
        list) let rich channels render per-route price摘要; plain channels may
        ignore them. Returns True on (attempted) success. Must never raise.
        """
        """
        发送每日价格/状态汇总（Daily Digest）。即使 alerts 为空也会发送（作为系统心跳）。
        返回 True 表示推送成功。接口实现内部必须捕获所有异常，绝对不能向上抛出
        """
        raise NotImplementedError

    @abstractmethod
    def send_urgent(self, alert, summary: Optional[dict] = None) -> bool:
        """Send a single urgent alert immediately. Must never raise.

        ``summary`` (optional) lets channels attach flight details.
        """
        """
        发送单条紧急告警（Urgent Spike）。即时推送，绝不延迟。
        接口实现内部必须捕获所有异常，绝对不能向上抛出。
        """
        raise NotImplementedError
"""
# 双模推送规范（Digest vs Urgent）：
- send_digest（每日晚报/心跳）：汇总整天的数据抓取状态、各航线的最高/最低价以及所有常规告警。
即使没有告警，也会作为“系统心跳”定时发送，让用户确认监控系统在正常运行。
- send_urgent（紧急单推）：当遇到价格暴跌、低于目标价或数据源连续失败时，触发单条卡片的即时推送。

# 极佳的接口兼顾性：
- 参数传入了 summary（经过 enrich.py 增强的富文本数据）和 routes 配置。
- 富文本渠道（如飞书、钉钉）：可利用 summary 和 routes 渲染出极其精美的图表、航司 Logo 和中转行程明细。
- 纯文本渠道（如 Bark、短信、Telegram 机器人）：如果不需要复杂样式，可以直接忽略这些扩展参数，只提取 alerts 里的文本进行发送。
"""

# -------------- 装饰器自注册机制 REGISTRY --------------
# ----------------------------------------------------------------- registry
# 全局推送器注册表：键为渠道名称 (str)，值为对应的类对象 (type)
REGISTRY: dict[str, type] = {}


def register_notifier(name: str) -> Callable[[type], type]:
    def deco(cls: type) -> type:
        cls.name = name
        REGISTRY[name] = cls    # 自动插入全局注册表
        return cls
    return deco

"""
# 优雅的解耦与扩展：
当需要增加一个新的推送渠道（例如 Telegram）时，只需要创建一个新文件实现 send_digest 和 send_urgent，并在类上挂载装饰器：

@register_notifier("telegram")
class TelegramNotifier(Notifier):
    # ... 实现代码 ...

主引擎 dispatch 会通过配置字典中的 notifiers.telegram.enabled: true 自动匹配实例化，完全不需要在主逻辑中硬编码 if channel == "telegram"！
"""