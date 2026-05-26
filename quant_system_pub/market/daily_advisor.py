"""
今日操作建议汇总模块

整合：大盘环境 + 选股结果 + 风险收益比过滤
输出：今天该不该操作、建议仓位、可买清单（带优先级）

核心逻辑：
  1. 大盘情绪分 >= 55 才建议操作
  2. 个股必须通过风险收益比过滤（RR >= 1.5）
  3. 个股今日涨幅 > 4% 降级为"等回踩"
  4. 给出建议仓位（情绪越好仓位越重）
  5. 按综合优先级排序可买清单
"""

import logging
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

# 风险收益比最低门槛：目标收益 / 潜在亏损
MIN_RISK_REWARD = 1.5

# 大盘情绪分阈值
MARKET_THRESHOLDS = {
    "strong":  70,   # 强势市场：满仓可操作
    "normal":  55,   # 正常市场：轻仓操作
    "weak":    40,   # 弱势市场：仅观望
    "panic":    0,   # 恐慌市场：空仓等待
}


def _market_env(pulse_data: dict) -> tuple[str, int, str, str]:
    """
    解析大盘情绪数据，返回 (环境标签, 情绪分, 建议仓位, 颜色)
    """
    if not pulse_data or "error" in pulse_data:
        return "未知", 50, "30%", "yellow"

    score = 50
    try:
        sent = pulse_data.get("sentiment", {})
        score = int(sent.get("score", 50))
    except Exception:
        pass

    if score >= MARKET_THRESHOLDS["strong"]:
        return "强势上涨", score, "60~80%", "green"
    elif score >= MARKET_THRESHOLDS["normal"]:
        return "正常偏多", score, "30~50%", "cyan"
    elif score >= MARKET_THRESHOLDS["weak"]:
        return "弱势整理", score, "10~20%", "yellow"
    else:
        return "恐慌下跌", score, "空仓", "red"


def _rr_filter(price: float, ma5: float, ma20: float) -> tuple[float, float, float, float]:
    """
    计算建议买入区、止损、目标、风险收益比
    强势股策略：买入=MA5附近，止损=MA20下3%，目标=+18%
    """
    buy    = round(ma5 * 1.005, 2)          # MA5上方0.5%买入
    stop   = round(ma20 * 0.97, 2)          # MA20下方3%止损
    target = round(price * 1.18, 2)         # 目标+18%

    potential_gain = target - buy
    potential_loss = buy - stop
    rr = round(potential_gain / potential_loss, 2) if potential_loss > 0 else 0
    return buy, stop, target, rr


def _position_advice(price: float, ma5: float, chg: float) -> tuple[str, str]:
    """当前位置评价：(文字, 颜色)"""
    gap = (price - ma5) / ma5 * 100 if ma5 else 0
    if chg > 4:
        return "今日大涨等回踩", "yellow"
    if gap <= 1.5:
        return "紧贴MA5 可建仓", "green"
    if gap <= 3.5:
        return "略高MA5 等回踩", "cyan"
    return "远离MA5 暂观望", "red"


def build_daily_advice(
    pulse_data: dict,
    quality_data: dict,
    pick_data: list,
    consecutive_picks: dict,        # {code: 连续天数}，来自 tracker
) -> dict:
    """
    整合所有数据，生成今日操作建议。

    返回：
    {
      "market_env":   "强势上涨",
      "market_score": 72,
      "position_advice": "建议仓位 60~80%",
      "can_operate":  True,
      "summary":      "今日大盘强势，有3只票达到买入条件",
      "buylist": [
        {
          "code", "name", "price", "chg",
          "buy_price", "stop_loss", "target",
          "risk_reward", "position_label",
          "priority",       # 1=立即可买 2=等回踩 3=观望
          "consecutive",    # 连续上榜天数（0=首次）
          "source",         # momentum/value/pick
        }
      ],
      "watchlist":  [...],   # 今日大涨、等回踩的票
      "skip_reason": "",     # 大盘不好时的说明
    }
    """
    env_label, market_score, pos_advice, env_color = _market_env(pulse_data)

    result = {
        "market_env":      env_label,
        "market_score":    market_score,
        "env_color":       env_color,
        "position_advice": pos_advice,
        "can_operate":     market_score >= MARKET_THRESHOLDS["normal"],
        "summary":         "",
        "buylist":         [],
        "watchlist":       [],
        "skip_reason":     "",
        "generated_at":    datetime.now().strftime("%H:%M"),
    }

    if market_score < MARKET_THRESHOLDS["weak"]:
        result["skip_reason"] = (
            f"大盘情绪分 {market_score}（<{MARKET_THRESHOLDS['weak']}），"
            "市场弱势，建议空仓等待机会"
        )
        result["summary"] = "今日不建议操作"
        return result

    # ── 整合候选股 ────────────────────────────────────────────
    candidates = []

    # 来自 F 区块强势股
    if quality_data and isinstance(quality_data, dict) and "error" not in quality_data:
        momentum_df = quality_data.get("momentum", pd.DataFrame())
        value_df    = quality_data.get("value",    pd.DataFrame())
        for df, src in [(momentum_df, "momentum"), (value_df, "value")]:
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                candidates.append({
                    "code":   str(r.get("code", "")),
                    "name":   str(r.get("name", "")),
                    "price":  float(r.get("price", 0)),
                    "chg":    float(r.get("chg", 0)),
                    "ma5":    float(r.get("ma5", 0)),
                    "ma20":   float(r.get("ma20", 0)),
                    "score":  float(r.get("total_score", 0)),
                    "source": src,
                })

    # 来自 E 区块热门选股
    if pick_data:
        for sector in pick_data:
            for s in sector.get("stocks", []):
                candidates.append({
                    "code":   str(s.get("code", "")),
                    "name":   str(s.get("name", "")),
                    "price":  float(s.get("price", s.get("chg", 0))),
                    "chg":    float(s.get("chg", 0)),
                    "ma5":    0,
                    "ma20":   0,
                    "score":  float(s.get("score", 50)),
                    "source": "pick",
                })

    # ── 计算风险收益比，分类 ──────────────────────────────────
    buylist   = []
    watchlist = []
    seen      = set()

    for c in candidates:
        code  = c["code"]
        if not code or code in seen:
            continue
        seen.add(code)

        price = c["price"]
        ma5   = c["ma5"]  if c["ma5"]  > 0 else price
        ma20  = c["ma20"] if c["ma20"] > 0 else price * 0.95
        chg   = c["chg"]

        buy_px, stop_px, target_px, rr = _rr_filter(price, ma5, ma20)
        pos_label, pos_color = _position_advice(price, ma5, chg)
        streak = consecutive_picks.get(code, 0)

        item = {
            "code":        code,
            "name":        c["name"],
            "price":       price,
            "chg":         chg,
            "buy_price":   buy_px,
            "stop_loss":   stop_px,
            "target":      target_px,
            "risk_reward": rr,
            "position_label": pos_label,
            "pos_color":   pos_color,
            "score":       c["score"],
            "consecutive": streak,
            "source":      c["source"],
        }

        # 风险收益比过滤
        if rr < MIN_RISK_REWARD:
            continue

        # 分类：立即可买 or 等回踩
        if "可建仓" in pos_label:
            item["priority"] = 1
            buylist.append(item)
        elif "等回踩" in pos_label or "大涨" in pos_label:
            item["priority"] = 2
            watchlist.append(item)
        else:
            item["priority"] = 3

    # 排序：连续上榜 > 评分 > 风险收益比
    def _sort_key(x):
        return (-x["consecutive"], -x["score"], -x["risk_reward"])

    buylist.sort(key=_sort_key)
    watchlist.sort(key=_sort_key)

    result["buylist"]   = buylist[:10]
    result["watchlist"] = watchlist[:10]

    # 生成摘要
    n_buy   = len(result["buylist"])
    n_watch = len(result["watchlist"])
    if n_buy > 0:
        result["summary"] = (
            f"大盘{env_label}（{market_score}分），"
            f"有 {n_buy} 只票达到买入条件，{n_watch} 只等回踩"
        )
    else:
        result["summary"] = (
            f"大盘{env_label}（{market_score}分），"
            f"当前无票达到买入条件，{n_watch} 只等回踩后可关注"
        )

    return result
