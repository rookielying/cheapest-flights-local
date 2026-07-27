"""Core data model for flight-watch.

All timestamps and date sharding use the Asia/Shanghai timezone (red-team
mandated fix #4). ``fetched_at`` is stored as a timezone-aware ISO-8601 string
in Shanghai local time (``+08:00``); ``fetch_date`` (the calendar day a quote
was collected) and the monthly JSONL shard are both derived in this timezone.
"""
# 整个系统的数据强一致性与时区准确性。
# 它承载了系统最重要的数据载体——FlightQuote（机票报价观察值模型），以及全局统一的 Asia/Shanghai 时区计算辅助工具。
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, date
from zoneinfo import ZoneInfo

# Single source of truth for the project timezone.
SHANGHAI = ZoneInfo("Asia/Shanghai")
"""
红队安全修复（Red-team mandated fix #4）：由于 GitHub Actions 运行在分布于全球的虚拟机上，系统默认的 UTC 时间可能会导致在跨天
（如北京时间早上 8 点前）抓取的数据时区错乱，从而引发数据库分片落错月份或去重逻辑错乱。

SHANGHAI = ZoneInfo("Asia/Shanghai")：使用 Python 3.9+ 内置的 zoneinfo 模块，
明确定义统一的项目时区标准为 Asia/Shanghai (UTC+8)，作为全系统的“唯一时间基准”。

"""
# --------------- 时区转换与时间计算工具函数 ---------------
def now_shanghai() -> datetime:
    """Current time as a tz-aware datetime in Asia/Shanghai."""
    return datetime.now(SHANGHAI)


def today_shanghai() -> date:
    """Today's calendar date in Asia/Shanghai."""
    return now_shanghai().date()


def iso_now() -> str:
    """Current Shanghai time as an ISO-8601 string with offset (seconds precision)."""
    return now_shanghai().replace(microsecond=0).isoformat()
# iso_now()：生成带时区偏移量的 ISO-8601 时间戳字符串

def fetch_date_of(fetched_at: str) -> str:
    """Return the calendar day (YYYY-MM-DD, Shanghai tz) for an ISO timestamp.

    Naive timestamps are assumed to already be Shanghai local time.
    """
    dt = datetime.fromisoformat(fetched_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(SHANGHAI).date().isoformat()


def month_of(fetched_at: str) -> str:
    """Return the monthly shard key (YYYY-MM, Shanghai tz) for an ISO timestamp."""
    return fetch_date_of(fetched_at)[:7]


# --------------- 机票报价数据模型：FlightQuote 类 ---------------
@dataclass
class FlightQuote:
    """A single flight price observation.

    Fields (schema per report section 4.4, with red-team fixes #3):
      fetched_at      ISO-8601 timestamp in Asia/Shanghai (with +08:00 offset)
      route_id        config route id, e.g. "sha-nrt"
      origin          IATA origin, e.g. "SHA"
      dest            IATA destination, e.g. "NRT"
      depart_date     departure calendar date "YYYY-MM-DD"
      airline         airline IATA code, e.g. "MU"
      flight_no       flight number, e.g. "MU523"
      depart_time     local departure time "HH:MM" (optional; "" when unknown —
                      fast-flights 2.2 may not parse it)
      stops           number of stops (0 = direct)
      price           normalized price as int in ``currency`` (default CNY)
      currency        normalized currency code, e.g. "CNY"
      raw_price       original numeric price as returned by the source
      raw_currency    original currency code as returned by the source
      price_type      price口径, e.g. "total_with_tax" | "base" | "unknown"
      source          fetcher name, e.g. "fast_flights" | "serpapi"
      is_lowest_of_day  True if this is the lowest price seen for the
                        (route_id, depart_date) on its fetch_date
    """
    # 1. 抓取元数据 
    fetched_at: str  # ISO-8601 格式抓取时间戳 (带 +08:00 偏移)
    route_id: str  # 航线唯一标识，如 "yul-pek"
    origin: str  # 出发地 airport code，如 "YUL"
    dest: str  # 目的地 airport code，如 "PEK"
    depart_date: str  # 航班出发日期 "YYYY-MM-DD"
    
    # 2. 航班元数据
    airline: str  # 执飞航司/代码，如 "AC" 或 "Air Canada"
    flight_no: str  # 航班号，如 "AC011"
    stops: int  # 中转次数 (0 代表直飞)
   
    # 3. 价格数据
    price: int  # 标准化换算后的价格 (整数，单位为 currency)
    
    # 4. 补充及过滤字段 [重点配套修改位置]
    depart_time: str = ""  # 出发时间 "HH:MM" (设为默认值 ""，向下兼容旧数据)
    arr_time: str = ""       # 【新增】落地时间 "HH:MM" (用于过滤 23-24 点落地航班)
    has_baggage: bool = True # 【新增】是否有免费托运行李 (默认 True)
    currency: str = "CNY"  # 换算后的目标币种
    raw_price: float = 0.0  # 爬虫源返回的原始未换算数值
    raw_currency: str = "CNY"  # 爬虫源返回的原始币种
    price_type: str = "total_with_tax"  # 价格口径 (含税全价 / 纯票价)
    source: str = "fast_flights"  # 数据抓取源名称
    is_lowest_of_day: bool = False  # 是否为该航线该出发日当天的最低价标记

    """
    强类型设计：
    包含抓取元数据（时间、数据源）、航班元数据（航司、航班号、转机次数、出发时间）与价格数据（换算前后价格、币种、含税标记）。

    向下兼容性 # （depart_time: str = ""）
    考虑到第三方爬虫适配器升级或读取早期落盘的旧 JSONL 行数据时可能缺失该字段，给可选字段设置了默认值，保证系统反序列化旧数据时不会崩溃。
    """


# --------------- FlightQuote 动态属性与序列化方法 ---------------
    @property
    def fetch_date(self) -> str:
        """The calendar day this quote was collected (Shanghai tz)."""
        return fetch_date_of(self.fetched_at)

    @property
    def month(self) -> str:
        """Monthly shard key (YYYY-MM, Shanghai tz) for JSONL storage."""
        return month_of(self.fetched_at)

    @property
    def dedup_key(self) -> tuple:
        """Deduplication primary key (report section 4.4)."""
        return (
            self.route_id, 
            self.depart_date, 
            self.flight_no or "", 
            self.fetch_date, 
            self.source)
# 联合主键去重:返回五元组：(航线ID, 出发日期, 航班号, 抓取日期, 数据源)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
    # 序列化:to_json()：开启 sort_keys=True 和 ensure_ascii=False，保证生成干净的标准 JSON 文本。

    @classmethod
    def from_dict(cls, d: dict) -> "FlightQuote":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in allowed})
    # 容错反序列化:allowed = {f for f in cls.__dataclass_fields__} 过滤防御：
    # 在 from_dict 中，只提取字典里属于 FlightQuote 属性的键值对。
    # 如果未来第三方接口返回了多余的冗余字段，这个过滤器会自动过滤掉杂质，防止触发 TypeError: unexpected keyword argument

    # --------------- 【新增】业务规则过滤方法 ---------------
    def matches_filters(self, filters: dict, airlines_config: dict = None) -> bool:
        """
        判断当前报价对象是否符合配置的要求：
        1. 直飞、免费行李额
        2. 航司黑/白名单
        3. 起飞/落地时段限制
        """
        # 1. 检验直飞 (stops == 0)
        if filters.get("direct_only", False) and self.stops != 0:
            return False

        # 2. 检验免费行李额
        if filters.get("require_baggage", False) and not self.has_baggage:
            return False

        # 3. 检验航司黑名单 / 白名单
        if airlines_config:
            airline_name = (self.airline or "").upper()
            flight_no = (self.flight_no or "").upper()
            
            # 黑名单
            blacklist = airlines_config.get("blacklist", [])
            for item in blacklist:
                target = item.strip().upper()
                if target and (target in airline_name or flight_no.startswith(target)):
                    return False

            # 白名单
            whitelist = airlines_config.get("whitelist", [])
            if whitelist:
                matched = any(
                    item.strip().upper() in airline_name or flight_no.startswith(item.strip().upper())
                    for item in whitelist if item.strip()
                )
                if not matched:
                    return False

        # 4. 检验起飞时间段限制 (排除 0-9 点起飞)
        exclude_dep = filters.get("exclude_departure_hours", [])
        if exclude_dep and self.depart_time:
            try:
                dep_hour = int(self.depart_time.split(":")[0])
                if dep_hour in exclude_dep:
                    return False
            except (ValueError, IndexError):
                pass

        # 5. 检验落地时间段限制 (排除 23-24 点落地)
        exclude_arr = filters.get("exclude_arrival_hours", [])
        if exclude_arr and self.arr_time:
            try:
                arr_hour = int(self.arr_time.split(":")[0])
                if arr_hour in exclude_arr:
                    return False
            except (ValueError, IndexError):
                pass

        return True