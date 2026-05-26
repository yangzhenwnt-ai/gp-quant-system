"""
选股结果追踪 + 胜率统计模块

每次扫描选出股票后自动记录，收盘后计算实际涨跌，
长期积累数据来验证和改进选股逻辑。

数据结构（JSON）：
{
  "records": [
    {
      "date":       "2026-04-29",       # 选出日期
      "source":     "momentum",         # momentum/value/pick
      "code":       "002748",
      "name":       "世龙实业",
      "price_in":   13.01,              # 选出时价格
      "score":      97,
      "buy_zone":   [12.40, 12.60],
      "stop_loss":  11.80,
      "target":     15.34,
      "risk_reward": 1.8,
      "result": {                        # 收盘后填入
        "price_1d":  13.50,             # 次日收盘
        "price_3d":  14.20,             # 3日后收盘
        "price_5d":  13.80,             # 5日后收盘
        "pnl_1d":    3.77,              # 1日涨跌幅%
        "pnl_3d":    9.15,
        "pnl_5d":    6.07,
        "hit_target": true,             # 是否达到目标价
        "hit_stop":   false,            # 是否触发止损
        "updated_at": "2026-05-05"
      }
    }
  ]
}
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

TRACKER_FILE = Path(__file__).parent / "pick_tracker.json"


# ─────────────────────────────────────────────────────────────
# 读写
# ─────────────────────────────────────────────────────────────

def _load() -> dict:
    if not TRACKER_FILE.exists():
        return {"records": []}
    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"records": []}


def _save(data: dict):
    try:
        with open(TRACKER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"tracker 写入失败: {e}")


# ─────────────────────────────────────────────────────────────
# 记录选股结果
# ─────────────────────────────────────────────────────────────

def record_picks(picks: list[dict], source: str = "momentum"):
    """
    记录一批选股结果。picks 每项至少含：
      code, name, price, score, stop_loss, target, risk_reward
    source: "momentum" / "value" / "pick"（E区块热门）
    重复记录（同日同代码）会跳过。
    """
    data    = _load()
    today   = datetime.today().strftime("%Y-%m-%d")
    existing = {(r["date"], r["code"]) for r in data["records"]}
    added   = 0

    for p in picks:
        code = str(p.get("code", ""))
        if not code:
            continue
        if (today, code) in existing:
            continue

        price  = float(p.get("price", 0))
        score  = float(p.get("score", p.get("total_score", 0)))
        stop   = float(p.get("stop_loss", round(price * 0.92, 2)))
        target = float(p.get("target",   round(price * 1.18, 2)))
        rr     = float(p.get("risk_reward", 0))
        if rr == 0 and price > stop:
            rr = round((target - price) / (price - stop), 2)

        record = {
            "date":        today,
            "source":      source,
            "code":        code,
            "name":        str(p.get("name", "")),
            "price_in":    price,
            "score":       score,
            "stop_loss":   stop,
            "target":      target,
            "risk_reward": rr,
            "result":      None,
        }
        data["records"].append(record)
        existing.add((today, code))
        added += 1

    if added:
        _save(data)
        logger.info(f"tracker: 新增 {added} 条记录（{source}）")
    return added


# ─────────────────────────────────────────────────────────────
# 自动补充历史收盘价（每天收盘后调用）
# ─────────────────────────────────────────────────────────────

def update_results(max_records: int = 100):
    """
    对最近 max_records 条未填结果的记录，拉取历史收盘价计算盈亏。
    建议每天15:30后调用一次。
    """
    from data.reliable_api import API

    data    = _load()
    today   = datetime.today().strftime("%Y-%m-%d")
    updated = 0

    pending = [r for r in data["records"]
               if r.get("result") is None and r["date"] < today][-max_records:]

    for rec in pending:
        code     = rec["code"]
        date_in  = rec["date"]
        price_in = rec["price_in"]
        stop     = rec["stop_loss"]
        target   = rec["target"]

        try:
            start = date_in
            end   = today
            hist  = API.history(code, start, end)
            if hist is None or len(hist) < 2:
                continue

            hist = hist.sort_values("date").reset_index(drop=True)
            # 找到选出日之后的交易日序列
            after = hist[hist["date"].astype(str) > date_in].reset_index(drop=True)
            if after.empty:
                continue

            def _px(n):
                return float(after["close"].iloc[n - 1]) if len(after) >= n else None

            p1 = _px(1)
            p3 = _px(3)
            p5 = _px(5)

            def _pnl(p):
                return round((p - price_in) / price_in * 100, 2) if p else None

            # 检查是否触发止损或目标价（用最高最低价）
            window = after.head(5)
            hit_stop   = bool((window["low"]  <= stop).any())   if not window.empty else False
            hit_target = bool((window["high"] >= target).any()) if not window.empty else False

            rec["result"] = {
                "price_1d":   p1,
                "price_3d":   p3,
                "price_5d":   p5,
                "pnl_1d":    _pnl(p1),
                "pnl_3d":    _pnl(p3),
                "pnl_5d":    _pnl(p5),
                "hit_target": hit_target,
                "hit_stop":   hit_stop,
                "updated_at": today,
            }
            updated += 1
            time.sleep(0.1)

        except Exception as e:
            logger.warning(f"tracker update {code}: {e}")

    if updated:
        _save(data)
        logger.info(f"tracker: 更新了 {updated} 条历史结果")
    return updated


# ─────────────────────────────────────────────────────────────
# 胜率统计
# ─────────────────────────────────────────────────────────────

def calc_winrate(source: str = None, days: int = 30) -> dict:
    """
    计算胜率统计。
    source: None=全部, "momentum"/"value"/"pick"
    days:   统计最近N天的记录

    返回：
    {
      "total":      已有结果的总记录数,
      "win_1d":     次日盈利比例,
      "win_3d":     3日盈利比例,
      "win_5d":     5日盈利比例,
      "avg_pnl_5d": 5日平均盈亏%,
      "hit_target": 达到目标价比例,
      "hit_stop":   触发止损比例,
      "best":       最佳一笔（code, name, pnl_5d）,
      "worst":      最差一笔,
      "by_source":  各来源分别统计,
    }
    """
    data    = _load()
    cutoff  = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    recs = [r for r in data["records"]
            if r.get("result") is not None and r["date"] >= cutoff]
    if source:
        recs = [r for r in recs if r.get("source") == source]

    if not recs:
        return {"total": 0, "message": f"最近{days}天内暂无有效记录"}

    def _winrate(key):
        vals = [r["result"][key] for r in recs if r["result"].get(key) is not None]
        if not vals:
            return None
        return round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)

    def _avg(key):
        vals = [r["result"][key] for r in recs if r["result"].get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    pnl5 = [(r, r["result"]["pnl_5d"]) for r in recs if r["result"].get("pnl_5d") is not None]
    best  = max(pnl5, key=lambda x: x[1])[0] if pnl5 else None
    worst = min(pnl5, key=lambda x: x[1])[0] if pnl5 else None

    hit_target = [r for r in recs if r["result"].get("hit_target")]
    hit_stop   = [r for r in recs if r["result"].get("hit_stop")]

    # 按来源分类统计
    sources = set(r.get("source", "unknown") for r in recs)
    by_source = {}
    for src in sources:
        src_recs = [r for r in recs if r.get("source") == src]
        p5 = [r["result"]["pnl_5d"] for r in src_recs if r["result"].get("pnl_5d") is not None]
        by_source[src] = {
            "count":      len(src_recs),
            "win_5d":     round(sum(1 for v in p5 if v > 0) / len(p5) * 100, 1) if p5 else None,
            "avg_pnl_5d": round(sum(p5) / len(p5), 2) if p5 else None,
        }

    return {
        "total":       len(recs),
        "days":        days,
        "win_1d":      _winrate("pnl_1d"),
        "win_3d":      _winrate("pnl_3d"),
        "win_5d":      _winrate("pnl_5d"),
        "avg_pnl_1d":  _avg("pnl_1d"),
        "avg_pnl_3d":  _avg("pnl_3d"),
        "avg_pnl_5d":  _avg("pnl_5d"),
        "hit_target":  round(len(hit_target) / len(recs) * 100, 1),
        "hit_stop":    round(len(hit_stop)   / len(recs) * 100, 1),
        "best":  {"code": best["code"],  "name": best["name"],  "pnl_5d": best["result"]["pnl_5d"]}  if best  else None,
        "worst": {"code": worst["code"], "name": worst["name"], "pnl_5d": worst["result"]["pnl_5d"]} if worst else None,
        "by_source":   by_source,
    }


# ─────────────────────────────────────────────────────────────
# 连续上榜检测
# ─────────────────────────────────────────────────────────────

def get_consecutive_picks(days: int = 5) -> dict[str, int]:
    """
    返回最近 days 天内连续出现在选股结果中的股票。
    {code: 连续出现天数}，只返回连续>=2天的。
    """
    data   = _load()
    today  = datetime.today().date()
    cutoff = (today - timedelta(days=days)).strftime("%Y-%m-%d")

    # 按日期分组
    by_date: dict[str, set] = {}
    for r in data["records"]:
        if r["date"] >= cutoff:
            by_date.setdefault(r["date"], set()).add(r["code"])

    # 找出在最近连续出现的股票
    sorted_dates = sorted(by_date.keys())
    consecutive  = {}
    for code in set(c for codes in by_date.values() for c in codes):
        streak = 0
        for d in reversed(sorted_dates):
            if code in by_date.get(d, set()):
                streak += 1
            else:
                break
        if streak >= 2:
            consecutive[code] = streak

    return consecutive


# ─────────────────────────────────────────────────────────────
# 获取最近N条记录（供dashboard显示）
# ─────────────────────────────────────────────────────────────

def get_recent_records(days: int = 7) -> list[dict]:
    data   = _load()
    cutoff = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return sorted(
        [r for r in data["records"] if r["date"] >= cutoff],
        key=lambda x: x["date"],
        reverse=True,
    )
