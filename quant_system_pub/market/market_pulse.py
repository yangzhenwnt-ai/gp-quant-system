"""
大盘情绪仪表盘（通过 reliable_api 访问数据，自动 failover + 缓存兜底）
"""
import logging
import time
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


def get_market_overview() -> dict:
    from data.reliable_api import API
    result = {}
    indices = {
        "sh":  ("sh000001", "上证指数"),
        "sz":  ("sz399001", "深成指"),
        "cyb": ("sz399006", "创业板指"),
    }
    for code, (symbol, name) in indices.items():
        try:
            df = API.index_daily(symbol)
            if df.empty:
                continue
            df = df.sort_values("date").tail(65)
            for n in [5, 10, 20, 60]:
                df[f"ma{n}"] = df["close"].rolling(n).mean()
            latest = df.iloc[-1]
            prev   = df.iloc[-2]
            result[code] = {
                "name":       name,
                "close":      float(latest["close"]),
                "change_pct": float((latest["close"] - prev["close"]) / prev["close"] * 100),
                "above_ma5":  bool(latest["close"] > latest["ma5"]),
                "above_ma10": bool(latest["close"] > latest["ma10"]),
                "above_ma20": bool(latest["close"] > latest["ma20"]),
                "above_ma60": bool(latest["close"] > latest["ma60"]),
                "ma5":  float(latest["ma5"]),
                "ma20": float(latest["ma20"]),
            }
        except Exception as e:
            logger.warning(f"处理 {name} 数据失败: {e}")
    return result


def get_market_breadth() -> dict:
    from data.reliable_api import API
    result = {"up": 0, "down": 0, "zt": 0, "dt": 0, "zt_list": [], "dt_list": []}

    # 涨停池
    zt_df = API.zt_pool()
    result["zt"] = len(zt_df)
    if not zt_df.empty:
        code_col = next((c for c in zt_df.columns if "代码" in c), None)
        name_col = next((c for c in zt_df.columns if "名称" in c), None)
        if code_col and name_col:
            result["zt_list"] = list(zip(
                zt_df[code_col].tolist()[:10],
                zt_df[name_col].tolist()[:10],
            ))

    # 跌停池
    dt_df = API.dt_pool()
    result["dt"] = len(dt_df)
    if not dt_df.empty:
        code_col = next((c for c in dt_df.columns if "代码" in c), None)
        name_col = next((c for c in dt_df.columns if "名称" in c), None)
        if code_col and name_col:
            result["dt_list"] = list(zip(
                dt_df[code_col].tolist()[:5],
                dt_df[name_col].tolist()[:5],
            ))

    # 涨跌家数 + 补充涨跌停
    act = API.market_activity()
    result["up"]   = act["up"]
    result["down"] = act["down"]
    if result["zt"] == 0:
        result["zt"] = act["zt"]
    if result["dt"] == 0:
        result["dt"] = act["dt"]

    return result


def get_market_volume() -> dict:
    from data.reliable_api import API
    result = {"total_amount_yi": 0.0, "north_flow_yi": 0.0}

    # 成交额：从全市场 spot 数据汇总（腾讯接口单位为万元）
    try:
        spot = API.spot()
        if not spot.empty and "amount" in spot.columns:
            total_wan = pd.to_numeric(spot["amount"], errors="coerce").fillna(0).sum()
            result["total_amount_yi"] = round(total_wan / 10000, 0)  # 万元→亿元
    except Exception as e:
        logger.warning(f"成交额获取失败: {e}")

    # 北向资金
    result["north_flow_yi"] = API.north_flow()

    return result


def calc_sentiment_score(overview: dict, breadth: dict, volume: dict) -> dict:
    score = 50

    sh = overview.get("sh", {})
    ma_score = 0
    if sh.get("above_ma5"):  ma_score += 5
    if sh.get("above_ma10"): ma_score += 5
    if sh.get("above_ma20"): ma_score += 7
    if sh.get("above_ma60"): ma_score += 3
    score += ma_score - 10

    sh_chg = sh.get("change_pct", 0)
    score += max(-10, min(10, sh_chg * 3))

    zt = breadth.get("zt", 0)
    dt = breadth.get("dt", 0)
    zt_ratio = zt / (zt + dt) if (zt + dt) > 0 else 0.5
    score += (zt_ratio - 0.5) * 40

    if zt >= 100:  score += 10
    elif zt >= 60: score += 5
    elif zt < 10:  score -= 10

    amt = volume.get("total_amount_yi", 0)
    if amt >= 12000:   score += 10
    elif amt >= 9000:  score += 5
    elif amt >= 4000:  score -= 5
    elif amt > 0:      score -= 10

    north = volume.get("north_flow_yi", 0)
    if north >= 100:    score += 10
    elif north >= 20:   score += 5
    elif north >= -20:  score += 0
    elif north >= -100: score -= 5
    else:               score -= 10

    score = max(0, min(100, score))

    if score >= 75:
        level, emoji = "强势", "🔥"
        advice = "市场赚钱效应强，热点明确，可以积极参与热门板块"
    elif score >= 55:
        level, emoji = "偏强", "📈"
        advice = "市场偏暖，选择性操作，优先跟随强势板块龙头"
    elif score >= 45:
        level, emoji = "平衡", "➡️"
        advice = "市场分化，轻仓观察，等待方向明确后再入场"
    elif score >= 30:
        level, emoji = "偏弱", "📉"
        advice = "市场偏冷，建议观望，持仓注意控制回撤"
    else:
        level, emoji = "危险", "⚠️"
        advice = "市场恐慌，空仓为上，等待企稳信号"

    return {
        "score": round(score, 1),
        "level": level, "emoji": emoji, "advice": advice,
        "zt_count": zt, "dt_count": dt,
        "zt_dt_ratio": f"{zt}:{dt}",
        "amount_yi": amt,
        "north_flow_yi": north,
        "sh_above_ma20": sh.get("above_ma20", False),
    }


def run_market_pulse() -> dict:
    print("  正在获取大盘数据...")
    overview  = get_market_overview()
    print("  正在获取涨跌停数据...")
    breadth   = get_market_breadth()
    print("  正在获取量能/北向数据...")
    volume    = get_market_volume()
    sentiment = calc_sentiment_score(overview, breadth, volume)
    return {"overview": overview, "breadth": breadth,
            "volume": volume, "sentiment": sentiment}
