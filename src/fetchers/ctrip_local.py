"""Ctrip (携程) local-crawler adapter — placeholder skeleton only.

This is the plug-in slot for the domestic fallback crawler described in the
report (sections 3.3 and 4.2). It only becomes real if the M3 PoC is FALSIFIED
(fast-flights domestic coverage <80% or price deviation >15%). In that case a
local Selenium/IPv6 crawler (blueprint: Suysker/Ctrip-Crawler) runs on Leon's
own Mac via launchd, emits the SAME-schema JSONL, and git-pushes into the same
repo — so the dashboard and alerting are unaware of the source difference.

Because centralized cloud crawling of Ctrip gets IP-banned, this fetcher:
  * is disabled unless CTRIP_LOCAL_ENABLED=1 is set, AND
  * refuses to run under CI (GitHub Actions sets CI=true) — it is local-only.

``fetch`` intentionally raises NotImplementedError until A2 is falsified.


### 1. 假设驱动与证伪设计 (Falsification-driven)
* **核心策略**：系统默认**优先使用完全免费、云端可运行的 `fast_flights`**。
* **占位目的**：只有当 `fast_flights` 抓取国内航线的覆盖率 $<80\%$ 或价格偏差 $>15\%$ 时（即假设被“证伪”），开发人员才会真正去编写内部的 Selenium 爬虫。
* **好处**：**不预先过度设计（No Over-engineering）**，但提前在架构里留好卡槽，一旦需要扩展，随时填入代码即可。
"""

from __future__ import annotations

import os

from .base import FetcherAdapter, register_fetcher


@register_fetcher("ctrip_local")
class CtripLocalFetcher(FetcherAdapter):
    name = "ctrip_local"

    # 2. 严格的运行环境隔离（Cloud vs Local Guard）
    def available(self) -> bool:
        # Local-only: must be explicitly enabled and NOT in a CI environment.
        # 1. 必须手动显式开启开关
        if os.environ.get("CTRIP_LOCAL_ENABLED") != "1":
            return False
        # 2. 绝对禁止在 CI（如 GitHub Actions）环境运行！
        if os.environ.get("CI"):  # GitHub Actions / most CI set CI=true
            return False
        return True
    # 数据 Schema 归一化 (Same-schema JSONL)
    def fetch(self, route, depart_date: str) -> list:
        raise NotImplementedError(
            "ctrip_local is a placeholder. Implement only if the M3 domestic PoC "
            "is falsified; runs locally (launchd) and emits same-schema JSONL."
        )
    """
    上层无感（Source-agnostic）：前端 Dashboard、分析引擎和报警系统完全不知道也不关心数据到底是由云端 GitHub Actions 抓回来的，
    还是由 Leon 本地的 Mac 爬出来 git push 上来的。
    """

