"""Fetcher abstraction + self-registration registry (report section 4.4).

Adding a new data source = new file implementing ``FetcherAdapter`` decorated
with ``@register_fetcher("name")`` + one line in config's ``sources``. No core
changes required.

多数据源架构的核心灵魂：
1. 定义了统一的适配器基类（FetcherAdapter）、抓取定制异常（FetchError）以及自注册工厂模式（Registry Pattern）。
2. 正是有了它，系统才能实现开闭原则（OCP）：新增一个数据源，完全不需要修改任何主流水线代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

# -------------- 自定义异常 FetchError：带“重试状态”的结构化异常 --------------
class FetchError(Exception):
    """Raised when a fetch fails.

    ``retryable=True`` means the pipeline may retry (with backoff) before
    degrading to the next source (e.g. transient empty result / network blip).
    ``retryable=False`` means give up on this source immediately (e.g. quota
    exhausted, source disabled).
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable
    # 标记型异常（Typed Exception with Metadata）：
    # retryable: bool（是否可重试）：
    # retryable=True：代表临时性故障（如网络抖动、页面解析偶尔为空）。主流水线接收到后，会触发指数退避重试（Backoff Retry）。
    # retryable=False：代表永久性故障（如 API 额度耗尽 MONTHLY_CAP、环境变量未配置 SERPAPI_KEY）。
    # 主流水线会立刻放弃该数据源，直接切到下一个备用源，绝不做无意义的重复尝试。

# -------------- 抽象基类 FetcherAdapter：接口契约规范 --------------
class FetcherAdapter(ABC):
    #: unique source name, must match config ``sources`` entries
    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """Return True if this fetcher can run right now (deps/env present)."""
        raise NotImplementedError

    @abstractmethod
    def fetch(self, route, depart_date: str) -> list:
        """Return a list[FlightQuote] for one route on one departure date.

        Should raise :class:`FetchError` on failure. Must not crash the process
        when optional third-party libraries are missing (lazy import).
        """
        raise NotImplementedError
"""
# 面向接口编程（Interface Contract）：
定义了所有抓取适配器必须遵守的“铁律”。所有继承自 FetcherAdapter 的具体类
（如 FastFlightsFetcher、SerpApiFetcher）都必须实现 available() 和 fetch() 方法，否则 Python 在实例化时就会报错。
# 统一入参与出参：
无论底层的爬虫技术是 Selenium、逆向接口还是付费 API，它们的 fetch() 入参统一为 (route, depart_date)，
出参统一为 list[FlightQuote]，把底层的技术差异完全屏蔽在适配器内部。
"""

# ----------------------------------------------------------------- registry
# -------------- 自注册装饰器 @register_fetcher：解耦注册逻辑 --------------
REGISTRY: dict[str, type] = {}   # 类注册表：{"fast_flights": FastFlightsFetcher, ...}
_INSTANCES: dict[str, FetcherAdapter] = {} # 单例缓存池：{"fast_flights": <实例对象>, ...}


def register_fetcher(name: str) -> Callable[[type], type]:
    def deco(cls: type) -> type:
        cls.name = name
        REGISTRY[name] = cls   # 将类存入注册表
        return cls
    return deco

"""自注册设计（Self-Registration Pattern）：
在之前的适配器文件（如 fast_flights.py）头部，我们看到了 @register_fetcher("fast_flights")。
当 Python 解释器导入这些适配器模块时，装饰器自动触发，将该类注册进全局字典 REGISTRY 中。
主程序不需要写任何硬编码的 import 或 if-else 各种判断。
"""

# -------------- 动态工厂与延迟单例 get_fetcher --------------
def get_fetcher(name: str) -> Optional[FetcherAdapter]:
    """Return a cached fetcher instance by name, or None if unregistered."""
    if name not in REGISTRY:
        return None
    if name not in _INSTANCES:
        # 懒加载单例模式 (Lazy Singleton)
        _INSTANCES[name] = REGISTRY[name]()  # type: ignore[call-arg]
    return _INSTANCES[name]

"""单例缓存（Singleton Pattern）：
保证每个适配器在全局只会被实例化一次（懒加载），重复调用
get_fetcher("fast_flights") 会直接从 _INSTANCES 缓存池中获取同一个对象，
避免频繁创建对象开销，同时保持持久化状态（如配额计数器）在内存中共享。
"""

# -------------- 可用性探针 available_fetchers --------------

def available_fetchers() -> list[str]:
    out = []
    for name in REGISTRY:
        f = get_fetcher(name)
        if f is not None and f.available():
            out.append(name)
    return out

"""运行时健康感知（Health Check Probe）：
主流水线在准备开始大规模抓取任务前，先调用一次 available_fetchers()，
就能瞬间筛选出当前运行环境下真正可用、未超限额、依赖齐全的数据源列表
（例如返回 ["fast_flights", "serpapi"]），为后续的“主备源自动切换（Failover）”提供依据。
"""
"""
当团队未来需要增加一个新数据源（比如“携程”或“去哪儿”）时：

只需要在 src/fetchers/ 目录下新建 ctrip.py；

继承 FetcherAdapter 并加上 @register_fetcher("ctrip")；

在 config.json 的 sources 配置项里加上 "ctrip"。

一行核心代码都不用修改，整个系统就能自动识别并运行新数据源！
"""