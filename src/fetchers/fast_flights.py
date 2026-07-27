"""fast-flights adapter (report data-source #1, red-team fix #2).

Design constraints:
  * fast-flights is treated as a "may-die-any-day" dependency: pinned version
    (2.2 — 3.0.2 verified broken 2026-07-10: missing typing_extensions dep +
    parser IndexError), lazy import (missing lib -> available()=False, never a crash),
    and a >0-results assertion (empty -> retryable FetchError so the pipeline
    retries then degrades to the next source).
  * Prices are pinned to CNY where possible; currency is recorded explicitly.
    Non-CNY prices are converted via state/fx_rates.json (default table shipped;
    TODO: daily refresh — see refresh_fx_rates()).
  * Airline whitelist/blacklist filtering per route.
"""


# -------------- 标准库导入与全局汇率常量定义 --------------
from __future__ import annotations

import json
import os
import re
from typing import Optional

from ..models import FlightQuote, iso_now
from .base import FetcherAdapter, FetchError, register_fetcher

# Pinned params: 防范语言和币种随 IP 漂移
# Pinned params (red-team fix #3/#4): stop currency drifting with locale.
PINNED_CURRENCY = "CNY"
PINNED_HL = "zh-CN"

# 默认离线汇率表（外币/人民币比例）
# Default FX table (units of foreign currency per 1 unit -> multiply to get CNY).
# TODO: refresh daily from a free FX source and persist to state/fx_rates.json.
DEFAULT_FX_RATES = {
    "CNY": 1.0,
    "USD": 7.2,
    "EUR": 7.8,
    "GBP": 9.1,
    "JPY": 0.048,
    "KRW": 0.0053,
    "HKD": 0.92,
    "CAD": 5.2,
    "AUD": 4.7,
    "SGD": 5.3,
    "TWD": 0.22,
}

_SYMBOL_TO_CODE = {
    "CA$": "CAD",
    "AU$": "AUD",
    "A$": "AUD",
    "US$": "USD",
    "HK$": "HKD",
    "NT$": "TWD",
    "S$": "SGD",
    "$": "USD",
    "¥": "CNY",   # pinned locale means ¥ denotes CNY, not JPY
    "￥": "CNY",
    "€": "EUR",
    "£": "GBP",
    "₩": "KRW",
}


# -------------- 价格解析正则与函数 parse_price --------------
def parse_price(raw: str, default_currency: str = PINNED_CURRENCY) -> tuple[int, str]:
    """Parse a price string into (amount:int, currency_code:str).

    Handles currency symbols and thousands separators, e.g.::

        "¥1,234"  -> (1234, "CNY")
        "US$530"  -> (530,  "USD")
        "$1,299"  -> (1299, "USD")
        "CNY 1,234" -> (1234, "CNY")
        "1234"    -> (1234, default_currency)
    """
    if raw is None:
        raise ValueError("price is None")
    s = str(raw).strip()

    currency: Optional[str] = None
    # # 1) 优先提取显式的 3 字母 ISO 货币代码，如 "CNY 1,234" 或 "1234 USD"
    # 1) explicit 3-letter code prefix/suffix, e.g. "CNY 1,234" or "1234 USD"
    m = re.search(r"\b([A-Z]{3})\b", s)
    if m:
        currency = m.group(1)
    else:
        # 2) 匹配货币符号（按长度从长到短优先匹配）
        # 2) currency symbols (check multi-char symbols first)
        for sym in sorted(_SYMBOL_TO_CODE, key=len, reverse=True):
            if sym in s:
                currency = _SYMBOL_TO_CODE[sym]
                break

    # 剥离所有非数字字符（保留小数点，去除千分位逗号）
    # Extract the numeric portion, dropping thousands separators.
    num = re.sub(r"[^0-9.]", "", s.replace(",", ""))
    if not num or num == ".":
        raise ValueError(f"no numeric value in price {raw!r}")
    amount = int(round(float(num)))
    return amount, (currency or default_currency)

"""高鲁棒性文本解析：
能够安全处理像 "¥1,234"、"US$530"、"CNY 1,234" 甚至纯数字 "1234" 等各种脏数据文本，归一化输出为 (数值整数, 3字母币种代码)。
"""


# -------------- 出发时间标准化函数 --------------
# fast-flights 2.2 exposes departure as an English string like
# "8:30 AM on Thu, Aug 13" (or sometimes an empty string / a bare "20:55").
# Normalize to 24-hour "HH:MM"; unparsable -> "".
"""兼容 12/24 小时制：
将 fast-flights 2.2 剥离出来的复杂美式英语时间文本（如 "8:30 AM on Thu, Aug 13" 或 "12:00 AM"）
精准提炼归一化为标准的 24 小时制字符串 "08:30" / "00:00"。如果解析失败则返回空字符串 ""，保证系统不崩溃
"""
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?")


def parse_depart_time(raw) -> str:
    """Normalize a departure-time string to 24-hour ``"HH:MM"``.

    Handles the fast-flights English form and 12/24-hour edge cases::

        "8:30 AM on Thu, Aug 13" -> "08:30"
        "12:00 AM"               -> "00:00"   (midnight)
        "12:30 PM"               -> "12:30"   (noon)
        "1:05 PM"                -> "13:05"
        "20:55"                  -> "20:55"   (already 24-hour, no AM/PM)
        ""/None/"n/a"            -> ""        (unparsable)
    """
    if not raw:
        return ""
    m = _TIME_RE.search(str(raw))
    if not m:
        return ""
    hh, mm = int(m.group(1)), int(m.group(2))
    ap = (m.group(3) or "").upper()
    if ap == "PM" and hh != 12:
        hh += 12
    elif ap == "AM" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return ""
    return f"{hh:02d}:{mm:02d}"

# -------------- 到达时间解析函数 --------------
def parse_arrival_time(raw) -> str:
    """Normalize an arrival-time string to 24-hour ``"HH:MM"``.
    
    Handles format like "11:55 PM" or "23:55 (+1 day)" -> "23:55"
    """
    if not raw:
        return ""
    # 去除跨天标记如 (+1 day)
    raw_clean = str(raw).split("+")[0].strip()
    return parse_depart_time(raw_clean)
# 👈 核心在这里！直接调用了 parse_depart_time 函数
"""
为了让代码的可读性和架构更加清晰，更优雅的做法是将通用时间解析逻辑抽取为一个基础函数 _parse_time_str
内部通用函数：将任意包含 12/24 小时制的时间文本归一化为 "HH:MM"
"""


# -------------- 汇率表读取与换算计算 --------------
def load_fx_rates(state_dir: str) -> dict:
    """Load FX rates from state/fx_rates.json, creating defaults if absent."""
    path = os.path.join(state_dir, "fx_rates.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rates = dict(DEFAULT_FX_RATES)
            rates.update(data.get("rates", data))
            return rates
        except Exception:
            pass
    # 若文件不存在或读取损坏，写回默认汇率表
    os.makedirs(state_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"_comment": "TODO: refresh daily", "rates": DEFAULT_FX_RATES}, f,
                  ensure_ascii=False, indent=2)
    return dict(DEFAULT_FX_RATES)

"""本地状态缓存（Local State Persistence）：
优先读取本地 state/fx_rates.json 里的最新汇率；若文件缺失或损坏，自动降级至内置的 DEFAULT_FX_RATES 备用表并自动创盘修复。
"""


def convert_to_cny(amount: float, currency: str, rates: dict) -> int:
    rate = rates.get(currency.upper())
    if rate is None:
        # Unknown currency: keep the raw number rather than guessing.
        return int(round(amount))  # 未知币种：直接返回原始数值，不进行换算
    return int(round(amount * rate))


def refresh_fx_rates(state_dir: str) -> None:  # pragma: no cover - placeholder
    """TODO: fetch live FX rates and persist to state/fx_rates.json (daily job)."""
    raise NotImplementedError("Daily FX refresh not yet implemented")


# -------------- 航司黑白名单过滤逻辑 --------------
def _airline_allowed(airline: str, airlines_cfg: dict) -> bool:
    wl = airlines_cfg.get("whitelist") or []
    bl = airlines_cfg.get("blacklist") or []
    # fast-flights 2.2 sometimes fails to parse airline names (empty string).
    # An empty name must NOT be dropped by a whitelist, otherwise a configured
    # whitelist would silently discard every quote. Filtering only applies to
    # quotes whose airline was actually parsed.

    # 关键防崩设计：如果爬虫没有解析出航司名字（为空），绝对不能被白名单误杀剔除！
    if not airline:
        return True
    # fast-flights returns full carrier names ("China Southern"), not IATA
    # codes — configure whitelist/blacklist with names; match case-insensitively.
    """防空值误杀机制（Empty Name Tolerance）：
    fast-flights 在遇到未知的网页 DOM 时有时解析不出航司名字（返回空字符串 ""）。
    如果设置了白名单（例如指定要查“国航”），若直接过滤空值会导致整条航班数据被全盘丢弃。
    代码特意做了 if not airline: return True 的容错。
    """


    a = airline.strip().lower()
    wl_n = [w.strip().lower() for w in wl]
    bl_n = [b.strip().lower() for b in bl]
    if wl_n and a not in wl_n:
        return False
    if a in bl_n:
        return False
    return True

# -------------- 免费行李解析函数 --------------
def parse_has_baggage(fobj) -> bool:
    """Extract baggage allowance from fast-flights object or raw text.
    
    Google Flights usually provides flags like 'has_checked_bag', 'baggage', or 'bags'.
    """
    # 尝试提取对象属性
    baggage_info = _attr(fobj, "baggage") or _attr(fobj, "bags") or _attr(fobj, "has_checked_bag")
    if baggage_info is None:
        return True  # 如果数据源未返回行李信息，默认保留（防止误杀），由后续二次确认
    
    if isinstance(baggage_info, bool):
        return baggage_info
        
    s = str(baggage_info).lower()
    # 如果明确出现 "no checked bag" 或 "无托运行李"，判定为 False
    if "no checked" in s or "no bag" in s or "无托运" in s or "0件" in s:
        return False
    return True



# -------------- 核心类 FastFlightsFetcher 与动态延迟加载 --------------
@register_fetcher("fast_flights")
# 使用装饰器注册模式将当前适配器自动注册进适配器工厂。
class FastFlightsFetcher(FetcherAdapter):
    name = "fast_flights"

    def __init__(self, state_dir: str = "state"):
        self.state_dir = state_dir

    def _import(self):
        """Lazy import of the fast_flights library. Returns module or None."""
        try:
            import fast_flights  # type: ignore
            return fast_flights
        except Exception:
            return None

    def available(self) -> bool:
        return self._import() is not None
    
    # ------- 主采集与 API 版本双分支兼容 -------
    def fetch(self, route, depart_date: str) -> list:
        ff = self._import()
        if ff is None:
            raise FetchError("fast_flights library not installed", retryable=False)

        rates = load_fx_rates(self.state_dir)
        try:
            # 判断 fast-flights API 版本（2.x vs 3.x）
            if hasattr(ff, "FlightData"):
                # fast-flights 2.x API (pinned 2.2 — last version verified
                # working end-to-end on 2026-07-10; 3.0.2 has a broken parser).
                # 2.x has no hl/currency kwargs: the returned currency follows
                # Google's geo-detection, so we record raw_currency and convert
                # to CNY via the FX table instead of pinning.

                # 2.x API 锁定版本调用
                result = ff.get_flights(
                    flight_data=[
                        ff.FlightData(date=depart_date, from_airport=route.origin, to_airport=route.dest)
                    ],
                    trip="one-way",
                    seat="economy",
                    passengers=ff.Passengers(adults=1),
                    fetch_mode="fallback",
                )
                flights = getattr(result, "flights", result) or []
            
            else:
                # fast-flights 3.x API (create_query/FlightQuery). Parser was
                # broken in 3.0.2; this branch exists for a future fixed 3.x.

                # 3.x API 备用兼容分支
                query = ff.create_query(
                    flights=[
                        ff.FlightQuery(date=depart_date, from_airport=route.origin, to_airport=route.dest)
                    ],
                    trip="one-way",
                    seat="economy",
                    passengers=ff.Passengers(adults=1),
                    language=PINNED_HL,
                    currency=PINNED_CURRENCY,
                )
                
                result = ff.get_flights(query)
                flights = list(result) or []
        except Exception as e:  # network / parse failure -> retryable
            # 网络或页面解析报错，标记 retryable=True 抛出 FetchError
            raise FetchError(f"fast_flights query failed: {e}", retryable=True)

        # >0-results assertion (report: empty/骤降为0 must alert).
        # >0 结果断言（防空抓取）
        if not flights:
            raise FetchError(
                f"fast_flights returned 0 results for {route.id} {depart_date}", retryable=True
            )

        # ------- 数据清洗映射与标准 FlightQuote 实例化 -------
        fetched_at = iso_now()
        quotes: list[FlightQuote] = []
        for fobj in flights:
            raw_price = _attr(fobj, "price")    # _attr() 统一属性提取器：消除了字典与对象属性访问方式的不同
            if raw_price in (None, "", "Price unavailable"):
                continue
            try:
                amount, cur = parse_price(raw_price)
            except ValueError:
                continue

            price_cny = convert_to_cny(amount, cur, rates)
            airline = str(_attr(fobj, "name") or _attr(fobj, "airline") or "").strip()
            

            # 【优化】航司过滤交由 models.py 的 matches_filters 统一处理，或保留基础校验
            if not _airline_allowed(airline, getattr(route, "airlines", {})):
                continue
            #if not _airline_allowed(airline, route.airlines):
            #    continue

            flight_no = str(_attr(fobj, "flight_no") or _attr(fobj, "flight_number") or "").strip()
            
            
            """
            # fast-flights 2.2 exposes departure as ``f.departure`` (may be an
            # empty string / None when the parser can't read it — tolerate it).
            depart_time = parse_depart_time(
                _attr(fobj, "departure") or _attr(fobj, "depart_time") or "")
            """
            # 【修改点 1】解析起飞时间
            depart_time = parse_depart_time(
                _attr(fobj, "departure") or _attr(fobj, "depart_time") or ""
            )
            
            # 【修改点 2】解析落地/到达时间 (用于 23-24 点落地过滤)
            arr_time = parse_arrival_time(
                _attr(fobj, "arrival") or _attr(fobj, "arr_time") or _attr(fobj, "arrival_time") or ""
            )

            # 【修改点 3】解析免费托运行李额度
            has_baggage = parse_has_baggage(fobj)
            
            
            stops = _attr(fobj, "stops")

            # 实例化标准强类型 FlightQuote 对象
            quotes.append(FlightQuote(
                fetched_at=fetched_at,
                route_id=route.id,
                origin=route.origin,
                dest=route.dest,
                depart_date=depart_date,
                airline=airline,
                flight_no=flight_no,
                depart_time=depart_time,
                arr_time=arr_time,             # 传入落地时间
                has_baggage=has_baggage,       # 传入行李状态
                stops=int(stops) if isinstance(stops, (int, float)) else 0,
                price=price_cny,
                currency=PINNED_CURRENCY,
                raw_price=float(amount),
                raw_currency=cur,
                price_type="total_with_tax",
                source=self.name,
            ))

        if not quotes:
            raise FetchError(
                f"fast_flights: all {len(flights)} results filtered/unparsable "
                f"for {route.id} {depart_date}", retryable=True,
            )
        return quotes


def _attr(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
