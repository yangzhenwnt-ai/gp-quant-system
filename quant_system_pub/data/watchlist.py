"""
观察池模块 —— 选股推荐后的持续价格追踪

功能：
  - 手动/自动将推荐股加入观察池
  - 每次启动时自动拍一张当日价格快照
  - 记录完整价格走势（每个交易日一条）
  - 计算相对买入点的盈亏、距止损/目标的距离
  - 到期（超过 track_days）自动归档

数据结构（watchlist.json）：
{
  "watching": [
    {
      "code":        "002748",
      "name":        "世龙实业",
      "add_date":    "2026-05-13",    # 加入观察池的日期
      "add_price":   13.01,           # 加入时价格（作为参考买入点）
      "source":      "momentum",      # 来源：momentum/value/pick/manual
      "stop_loss":   11.97,
      "target":      15.35,
      "track_days":  20,              # 跟踪天数，到期归档
      "note":        "",              # 用户备注
      "snapshots": [
        {
          "date":    "2026-05-13",
          "price":   13.01,
          "chg":     2.5,            # 当日涨跌幅%
          "pnl":     0.0,            # 相对 add_price 的盈亏%
          "volume":  1234567,
          "amount":  1600000         # 万元
        }
      ]
    }
  ],
  "archived": [...]   # 到期或手动移除的记录
}
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"


# ─────────────────────────────────────────────────────────────
# 读写
# ─────────────────────────────────────────────────────────────

def _load() -> dict:
    if not WATCHLIST_FILE.exists():
        return {"watching": [], "archived": []}
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("watching", [])
        data.setdefault("archived", [])
        return data
    except Exception:
        return {"watching": [], "archived": []}


def _save(data: dict):
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"watchlist 写入失败: {e}")


# ─────────────────────────────────────────────────────────────
# 增删
# ─────────────────────────────────────────────────────────────

def add_watch(code: str, name: str, price: float, source: str = "manual",
              stop_loss: float = None, target: float = None,
              track_days: int = 20, note: str = "") -> bool:
    """
    加入观察池。同代码已存在则跳过，返回 False。
    stop_loss/target 不传时按 -8% / +18% 自动计算。
    """
    data = _load()
    codes = {w["code"] for w in data["watching"]}
    if code in codes:
        return False

    today = datetime.today().strftime("%Y-%m-%d")
    entry = {
        "code":       code,
        "name":       name,
        "add_date":   today,
        "add_price":  round(float(price), 3),
        "source":     source,
        "stop_loss":  round(stop_loss or price * 0.92, 3),
        "target":     round(target    or price * 1.18, 3),
        "track_days": track_days,
        "note":       note,
        "snapshots":  [],
    }
    data["watching"].append(entry)
    _save(data)
    logger.info(f"watchlist: 加入 {code} {name} @ {price}")
    return True


def remove_watch(code: str, reason: str = "manual") -> bool:
    """从观察池移除，归档保留。"""
    data = _load()
    for i, w in enumerate(data["watching"]):
        if w["code"] == code:
            w["remove_date"]   = datetime.today().strftime("%Y-%m-%d")
            w["remove_reason"] = reason
            data["archived"].append(w)
            data["watching"].pop(i)
            _save(data)
            return True
    return False


def get_watching() -> list[dict]:
    """返回当前观察池列表（含计算字段）。"""
    return _load()["watching"]


# ─────────────────────────────────────────────────────────────
# 每日价格快照
# ─────────────────────────────────────────────────────────────

def take_snapshot(force: bool = False) -> int:
    """
    对观察池中所有股票拍今日价格快照。
    同一天已拍过则跳过（除非 force=True）。
    返回新增快照数。
    """
    data = _load()
    if not data["watching"]:
        return 0

    from data.reliable_api import API
    today = datetime.today().strftime("%Y-%m-%d")

    # 批量拉实时行情
    codes = [w["code"] for w in data["watching"]]
    try:
        spot = API.spot()
        spot_map = {}
        if not spot.empty:
            for _, row in spot[spot["code"].isin(codes)].iterrows():
                spot_map[str(row["code"])] = row
    except Exception as e:
        logger.warning(f"watchlist snapshot 行情失败: {e}")
        return 0

    added = 0
    to_archive = []

    for w in data["watching"]:
        code = w["code"]

        # 检查是否已过期
        add_dt   = datetime.strptime(w["add_date"], "%Y-%m-%d")
        elapsed  = (datetime.today() - add_dt).days
        if elapsed > w.get("track_days", 20):
            to_archive.append(code)
            continue

        # 今天已有快照则跳过
        snaps = w.setdefault("snapshots", [])
        if not force and snaps and snaps[-1]["date"] == today:
            continue

        row = spot_map.get(code)
        if row is None:
            continue

        price  = float(row.get("price",  0) or 0)
        chg    = float(row.get("chg",    0) or 0)
        vol    = float(row.get("volume", 0) or 0)
        amt    = float(row.get("amount", 0) or 0)
        pnl    = round((price - w["add_price"]) / w["add_price"] * 100, 2) if w["add_price"] else 0

        snaps.append({
            "date":   today,
            "price":  round(price, 3),
            "chg":    round(chg,   2),
            "pnl":    pnl,
            "volume": int(vol),
            "amount": round(amt, 1),
        })
        added += 1

    # 归档到期股票
    for code in to_archive:
        remove_watch(code, reason="expired")

    if added:
        _save(data)
        logger.info(f"watchlist: 拍摄 {added} 条快照")
    return added


# ─────────────────────────────────────────────────────────────
# 统计分析
# ─────────────────────────────────────────────────────────────

def get_stats(code: str) -> dict:
    """
    返回单只股票的观察统计：
    当前盈亏、最大浮盈、最大回撤、距止损/目标距离、趋势（连涨/连跌天数）
    """
    data = _load()
    entry = next((w for w in data["watching"] if w["code"] == code), None)
    if not entry:
        # 在归档里找
        entry = next((w for w in data["archived"] if w["code"] == code), None)
    if not entry or not entry.get("snapshots"):
        return {}

    snaps     = entry["snapshots"]
    prices    = [s["price"] for s in snaps if s["price"] > 0]
    pnls      = [s["pnl"]   for s in snaps]
    add_price = entry["add_price"]
    cur_price = prices[-1] if prices else add_price
    cur_pnl   = pnls[-1]   if pnls   else 0.0

    max_gain  = max(pnls) if pnls else 0
    max_draw  = min(pnls) if pnls else 0

    stop   = entry.get("stop_loss")
    target = entry.get("target")
    dist_stop   = round((cur_price - stop)   / stop   * 100, 2) if stop   else None
    dist_target = round((target - cur_price) / cur_price * 100, 2) if target else None

    # 当前连续涨/跌天数
    streak = 0
    if len(snaps) >= 2:
        direction = 1 if snaps[-1]["chg"] >= 0 else -1
        for s in reversed(snaps):
            if (s["chg"] >= 0) == (direction == 1):
                streak += 1
            else:
                break
        streak *= direction   # 正=连涨，负=连跌

    return {
        "code":        code,
        "name":        entry["name"],
        "add_price":   add_price,
        "cur_price":   cur_price,
        "cur_pnl":     cur_pnl,
        "max_gain":    round(max_gain, 2),
        "max_draw":    round(max_draw, 2),
        "dist_stop":   dist_stop,      # 距止损还有多少%（正=安全，负=已破）
        "dist_target": dist_target,    # 距目标还有多少%
        "streak":      streak,         # >0连涨天数，<0连跌天数
        "days":        len(snaps),
        "track_days":  entry.get("track_days", 20),
        "source":      entry.get("source", ""),
        "note":        entry.get("note", ""),
        "snapshots":   snaps,
    }


def get_all_stats() -> list[dict]:
    """返回所有观察股的统计，按当前盈亏降序。"""
    data = _load()
    stats = [get_stats(w["code"]) for w in data["watching"]]
    stats = [s for s in stats if s]
    return sorted(stats, key=lambda x: x.get("cur_pnl", 0), reverse=True)


def today_added_count() -> int:
    """返回今日已自动加入观察池的数量。"""
    today = datetime.today().strftime("%Y-%m-%d")
    data  = _load()
    return sum(1 for w in data["watching"]
               if w.get("add_date") == today and w.get("source") != "manual")


def can_auto_add(daily_limit: int = 5) -> bool:
    """今日自动加入是否还未达上限。"""
    return today_added_count() < daily_limit


def purge_watchlist(max_watch: int = 20, max_loss_pct: float = -10.0) -> int:
    """
    清理观察池，保证总数 ≤ max_watch。
    清理规则（优先级从高到低）：
      1. 亏损超过 max_loss_pct 的（止损已破）
      2. 持有天数超过 track_days 的
      3. 非手动添加且盈亏最差的，直到数量 ≤ max_watch
    返回清理数量。
    """
    data  = _load()
    today = datetime.today().strftime("%Y-%m-%d")
    removed = 0

    def _pnl(w):
        snaps = w.get("snapshots", [])
        if not snaps:
            return 0.0
        return snaps[-1].get("pnl", 0.0)

    def _archive(w, reason):
        nonlocal removed
        w["remove_date"]   = today
        w["remove_reason"] = reason
        data["archived"].append(w)
        removed += 1

    keep    = []
    to_drop = []

    for w in data["watching"]:
        add_dt  = datetime.strptime(w["add_date"], "%Y-%m-%d")
        elapsed = (datetime.today() - add_dt).days
        pnl     = _pnl(w)
        if pnl <= max_loss_pct:
            to_drop.append((w, "stop_loss_hit"))
        elif elapsed > w.get("track_days", 20):
            to_drop.append((w, "expired"))
        else:
            keep.append(w)

    for w, reason in to_drop:
        _archive(w, reason)

    # 若还超出上限，按盈亏从小到大移除非手动股
    if len(keep) > max_watch:
        auto = [w for w in keep if w.get("source") != "manual"]
        auto.sort(key=_pnl)
        while len(keep) > max_watch and auto:
            w = auto.pop(0)
            keep.remove(w)
            _archive(w, "over_limit")

    data["watching"] = keep
    if removed:
        _save(data)
        logger.info(f"watchlist purge: 清理 {removed} 只，剩余 {len(keep)} 只")
    return removed
