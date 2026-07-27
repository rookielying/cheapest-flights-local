"""JSONL storage layer — "Git as database" (report section 4.4).

Quotes are appended to ``data/{route_id}/{YYYY-MM}.jsonl`` where the month is
the fetch month in Asia/Shanghai. Writes are de-duplicated on the primary key
``(route_id, depart_date, flight_no, fetch_date, source)``.


数据存储与汇总状态层
“以 Git 作为数据库（Git as Database）”
将抓取到的数据以标准的 JSONL 格式 增量追加落盘，并通过 GitHub Actions 自动 Commit & Push 保存历史
同时，它还会编译生成可直接由 GitHub Pages 托管展示的 docs/data/summary.json，供前端 Dashboard 直接读取。

核心功能：
1. 数据增量追加（Append-only）：
    * 只做新增，不做修改或删除，确保历史数据完整性。
    * 通过 (route_id, depart_date, flight_no, fetch_date, source) 唯一键去重。
2. 数据版本控制（Version Control）：
    * 每次抓取都记录 fetch_date，形成时间序列。
    * 通过 fetch_date 可以追溯历史价格变动。
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Iterable, Optional

from .models import FlightQuote, iso_now, month_of, fetch_date_of, today_shanghai

# -------------- 目录架构与 JSONL 路径映射 --------------
class Storage:
    def __init__(self, data_dir: str, docs_dir: Optional[str] = None):
        self.data_dir = data_dir
        # summary.json lives under docs/data/ so GitHub Pages can serve it.
        # summary.json 存放在 docs/data/ 下，方便 GitHub Pages 直接将其作为静态 API 托管
        self.docs_dir = docs_dir or os.path.join(os.path.dirname(data_dir.rstrip("/")), "docs")
        
        # 🛡️ 防护 1：初始化时强制创建底层目录，避免 FileNotFoundError
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(os.path.join(self.docs_dir, "data"), exist_ok=True)

    # ------------------------------------------------------------------ paths
    def _file_for(self, route_id: str, month: str) -> str:
        d = os.path.join(self.data_dir, route_id)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{month}.jsonl")

    def _route_dir(self, route_id: str) -> str:
        return os.path.join(self.data_dir, route_id)
    # 数据落盘时按照 航线 ID + 抓取月份 自动按层级划分
    # 分片（Sharding）优势：
    # 把不同月份的数据隔离存储在独立的 .jsonl 文件中，可以大幅减少单个文件的大小，提高后续 Python 按需读取和 append 追加时的 I/O 效率。

    # ------------------------------------------------------------------ read
    def read_route(self, route_id: str) -> list[dict]:
        """安全读取某个航线下的所有 JSONL 文件"""
        d = self._route_dir(route_id)
        rows: list[dict] = []
       
       # 🛡️ 防护 2：如果航线目录根本不存在，直接安全返回空列表
        if not os.path.isdir(d):
            return rows

        for name in sorted(os.listdir(d)):
            if not name.endswith(".jsonl"):
                continue
            file_path = os.path.join(d, name)

            # 🛡️ 防护 3：判断文件是否存在，捕获 JSON 坏行
            if not os.path.exists(file_path):
                continue

            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # fetch_date is derived (not stored, to match report schema).
                    if "fetch_date" not in r and "fetched_at" in r:
                        r["fetch_date"] = fetch_date_of(r["fetched_at"])
                    rows.append(r)
        return rows
    
    # -------------- 幂等去重与增量追加写入 --------------
    def _existing_keys(self, path: str) -> set:
        # 读取指定 JSONL 文件，提炼出所有已存在记录的“唯一复合主键”集合
        keys: set = set()
        if not os.path.exists(path):
            return keys
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    keys.add(_key_of(r))
                except json.JSONDecodeError:
                    continue
        return keys

    # ------------------------------------------------------------------ write
    def append_quotes(self, quotes: Iterable[FlightQuote]) -> int:
        """Append quotes, skipping duplicates. Returns number actually written."""
        by_file: dict[str, list[FlightQuote]] = defaultdict(list)
        for q in quotes:
            by_file[self._file_for(q.route_id, q.month)].append(q)

        written = 0
        for path, items in by_file.items():
            seen = self._existing_keys(path)    # 先读取已有的主键 Set
            with open(path, "a", encoding="utf-8") as f:
                for q in items:
                    k = q.dedup_key
                    if k in seen:    # 🛡️ 幂等去重：如果在当前文件里已经有了，直接跳过！
                        continue
                    seen.add(k)
                    f.write(q.to_json() + "\n")
                    written += 1
        return written
    """
    # 复合主键公式 (_key_of)：
    包含 “航线 + 出发日期 + 航班号 + 抓取日期 + 数据源”。
    # 写幂等性（Idempotency）：
    即使同一个 GitHub Actions 任务因为网络波动被重试执行了 3 次，append_quotes 在写入前都会去读文件中的 seen 集合。
    确保同一天、同一数据源抓到的同一航班绝不会出现多条重复记录。
    """

    # -------------- 查询接口：按航线+日期 过滤出价格最低的记录 --------------
    # -------------------------------------------------------------- queries
    def _rows_for(self, route_id: str, depart_date: str) -> list[dict]:
        return [r for r in self.read_route(route_id) if r.get("depart_date") == depart_date]

    def latest_low(self, route_id: str, depart_date: str) -> Optional[dict]:
        """Lowest price on the most recent fetch_date for a route+depart_date.
        # 提取最新一次抓取中的最低价记录（价格相同时优先选信息丰富的记录)
        Ties on price prefer the row carrying flight detail (航司/航班号/起飞时间)
        so the digest/dashboard never show a bare price when a richer row exists.
        """
        rows = self._rows_for(route_id, depart_date)
        if not rows:
            return None
        latest_fd = max(r["fetch_date"] for r in rows)
        same = [r for r in rows if r["fetch_date"] == latest_fd]
        return min(same, key=_low_sort_key)

    def historical_low(self, route_id: str, depart_date: str) -> Optional[dict]:
        """All-time lowest price row for a route+depart_date (detail-preferred on tie)."""
        """提取历史全时段最低价记录"""
        rows = self._rows_for(route_id, depart_date)
        if not rows:
            return None
        return min(rows, key=_low_sort_key)

    def series(self, route_id: str, depart_date: str) -> list[dict]:
        """Per fetch_date lowest price, ascending by fetch_date."""
        """按 fetch_date 提取每日最低价，生成价格走势数组"""
        rows = self._rows_for(route_id, depart_date)
        by_day: dict[str, dict] = {}
        for r in rows:
            fd = r.get("fetch_date")
            if not fd:
                continue
            if fd not in by_day or r["price"] < by_day[fd]["price"]:
                by_day[fd] = r
        out = []
        for fd in sorted(by_day):
            r = by_day[fd]
            out.append({"fetch_date": fd, "price": r["price"], "currency": r.get("currency", "CNY")})
        return out

    def depart_dates(self, route_id: str) -> list[str]:
        return sorted({r["depart_date"] for r in self.read_route(route_id)})

    def route_ids(self) -> list[str]:
        if not os.path.isdir(self.data_dir):
            return []
        return sorted(
            name for name in os.listdir(self.data_dir)
            if os.path.isdir(os.path.join(self.data_dir, name))
        )

    # -------------------------------------------------------------- summary
    # -------------- 实时汇总表编译build_summary --------------
    def build_summary(self, route_ids: Optional[list[str]] = None, extra: Optional[dict] = None) -> dict:
        """Build and persist docs/data/summary.json for the dashboard.

        Structure (documented so the dashboard/agent M4 can rely on it):

            {
              "generated_at": "<ISO8601 Shanghai>",
              "routes": {
                "<route_id>": {
                  "depart_dates": {
                    "<YYYY-MM-DD>": {
                      "latest":         {"fetch_date","price","currency",
                                         "airline","flight_no","depart_time"} | null,
                      "historical_low": {"fetch_date","price","currency",
                                         "airline","flight_no","depart_time"} | null,
                      "series": [ {"fetch_date","price","currency"}, ... ]
                    }, ...
                  },
                  "headline": {           # OPTIONAL, added by src.enrich (SerpAPI
                                          # digest enrichment); absent when no key
                                          # / no budget. Describes the route's
                                          # cheapest depart_date in full detail.
                     "depart_date","price","airline","flight_no","airplane",
                     "depart_time","arrive_time","stops","layover_airports":[...],
                     "segments":[{leg,airline,flight_no,from,from_time,to,to_time,
                                  duration_min,airplane},...],   # 逐段行程
                     "layovers":[{"airport","wait_min"},...],    # 段间中转等待
                     "baggage_note","overnight","source"
                  }
                }, ...
              },
              "meta": { ...arbitrary extra (e.g. serpapi quota)... }
            }

        ``build_summary`` itself never fills ``headline`` (it only reads JSONL).
        The enrichment runs after this in the pipeline and re-persists via
        :meth:`persist_summary`.
        """
        """编译并生成 docs/data/summary.json"""

        ids = route_ids if route_ids is not None else self.route_ids()
        # Only surface departure dates that haven't already flown. Past dates
        # linger in the JSONL history (rolling window advances daily) and would
        # otherwise be picked as the "cheapest" — e.g. showing a 07-15 fare on
        # 07-18. History is preserved on disk; it's just excluded from the
        # live summary the cards/dashboard read.
       # 🧹 动态过期过滤：跳过上海时间今天之前的离港日期
        today = today_shanghai().isoformat()
        routes_out: dict = {}
        
        for rid in ids:
            dd_out: dict = {}
            for dd in self.depart_dates(rid):
                if dd < today:    # 已飞过的历史日期不放入 summary.json
                    continue
                latest = self.latest_low(rid, dd)
                hlow = self.historical_low(rid, dd)
                dd_out[dd] = {
                    "latest": _slim(latest),
                    "historical_low": _slim(hlow),
                    "series": self.series(rid, dd),  # 历史价格走势数组
                }
            routes_out[rid] = {"depart_dates": dd_out}
        
        summary = {
            "generated_at": iso_now(),
            "routes": routes_out,
            "meta": extra or {},
        }
        self.persist_summary(summary)
        return summary

        """滚动时间窗口过滤（Rolling Window Filter）：
        在生成 summary.json 时，代码会自动过滤掉 depart_date < today（即已经飞过的日期）。
        # 为什么不在落盘时删掉？
        历史数据在 .jsonl 文件里必须永久保留，以便做长期趋势分析；
        但在编译给前端和卡片展示的 summary.json 中，必须排除掉已过期航班，防止将昨天的低价误报为当前有效低价。
        """


    def persist_summary(self, summary: dict) -> str:
        """(Re)write docs/data/summary.json from an in-memory summary dict.

        Used both by build_summary and by src.enrich (which mutates the summary
        in place with per-route ``headline`` details and needs to re-persist so
        the static dashboard sees the enriched fields).
        """
        """将 summary 字典写入磁盘 docs/data/summary.json"""
        out_dir = os.path.join(self.docs_dir, "data")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "summary.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)
        return path

# -------------- 数据优选算法：_low_sort_key --------------
# “富数据”优先原则：
# 当价格相等时，排序算法会优先保留带有完整航班信息的记录，确保发给用户的飞书推送卡片和静态 Dashboard 上不会出现“空壳航班”的情况。
def _detail_score(r: dict) -> int:
    """Count of non-empty flight-detail fields (airline/flight_no/depart_time)."""
    """计算当前记录包含了多少个非空的航班细节字段 (航司/航班号/起飞时间)"""
    return sum(1 for k in ("airline", "flight_no", "depart_time", "arr_time")
               if str(r.get(k) or "").strip())


def _low_sort_key(r: dict) -> tuple:
    """Cheapest first; among equal prices, the row with the most detail first."""
    """排序规则：价格低的优先；价格一样时，字段更丰富（得分高）的优先！"""
    return (r["price"], -_detail_score(r))


def _key_of(r: dict) -> tuple:
    fd = r.get("fetch_date") or fetch_date_of(r.get("fetched_at", iso_now()))
    flight_no = r.get("flight_no") or ""
    return (
        r.get("route_id"),
        r.get("depart_date"),
        flight_no,
        fd,
        r.get("source"),
    )


def _slim(r: Optional[dict]) -> Optional[dict]:
    """精简摘要对象，透传新增的到达时间 arr_time 和托运行李标志 has_baggage"""
    
    if not r:
        return None
    # Carry the flight identity of the cheapest record (used by the Feishu
    # digest: 航司/航班号/起飞时间). Old JSONL rows may lack these -> "".
    return {
        "fetch_date": r["fetch_date"],
        "price": r["price"],
        "currency": r.get("currency", "CNY"),
        "airline": r.get("airline", "") or "",
        "flight_no": r.get("flight_no", "") or "",
        "depart_time": r.get("depart_time", "") or "",
        "arr_time": r.get("arr_time", "") or "",          # 👈 增强：支持到达时间
        "has_baggage": r.get("has_baggage", True),        # 👈 增强：支持行李标记
    
    }
