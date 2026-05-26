"""
个股买入时机评估模块
告诉你：这只股现在是高位还是低位，能不能买，目标价和止损价在哪

评估维度：
  1. 价格位置（相对52周区间的百分位）
  2. 均线系统（多头排列 / 空头排列）
  3. 成交量确认（量价配合）
  4. 距离关键支撑/压力位的距离
  5. 买入评级：强烈推荐 / 可以买 / 观望 / 不建议
"""
import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def evaluate_timing(symbol: str, df: pd.DataFrame) -> dict:
    """
    对一只股票的当前位置做买入时机评估

    参数:
        symbol: 股票代码
        df: 含 date/open/high/low/close/volume 的日线 DataFrame（至少120天）

    返回:
        包含评分、评级、目标价、止损价、详细分析的字典
    """
    if df is None or len(df) < 60:
        return {"error": "数据不足，至少需要60个交易日"}

    df = df.copy().sort_values("date").reset_index(drop=True)
    close  = df["close"]
    volume = df["volume"]
    high   = df["high"]
    low    = df["low"]
    latest = close.iloc[-1]
    latest_vol = volume.iloc[-1]

    result = {
        "symbol":  symbol,
        "price":   round(latest, 2),
        "signals": [],   # 信号列表
        "score":   50,   # 综合评分 0~100
        "rating":  "",
        "buy_zone":    None,
        "stop_loss":   None,
        "target1":     None,
        "target2":     None,
        "position_pct": None,
    }

    score = 50
    signals = []

    # ── 1. 价格位置（相对52周区间）────────────────────────
    window = min(252, len(df))
    high_52w = high.iloc[-window:].max()
    low_52w  = low.iloc[-window:].min()
    price_range = high_52w - low_52w

    if price_range > 0:
        position_pct = (latest - low_52w) / price_range * 100
    else:
        position_pct = 50.0
    result["position_pct"] = round(position_pct, 1)

    if position_pct <= 20:
        score += 15
        signals.append(("✅", f"价格在52周低位区间（位置{position_pct:.0f}%），属于低吸机会"))
    elif position_pct <= 40:
        score += 8
        signals.append(("✅", f"价格在52周偏低区间（位置{position_pct:.0f}%），有一定安全边际"))
    elif position_pct <= 60:
        score += 0
        signals.append(("➡️", f"价格在52周中间区间（位置{position_pct:.0f}%），位置适中"))
    elif position_pct <= 80:
        score -= 8
        signals.append(("⚠️", f"价格在52周偏高区间（位置{position_pct:.0f}%），追高风险上升"))
    else:
        score -= 15
        signals.append(("❌", f"价格在52周高位区间（位置{position_pct:.0f}%），历史高位，谨慎追高"))

    # ── 2. 均线系统 ───────────────────────────────────────
    ma5   = close.rolling(5).mean().iloc[-1]
    ma10  = close.rolling(10).mean().iloc[-1]
    ma20  = close.rolling(20).mean().iloc[-1]
    ma60  = close.rolling(60).mean().iloc[-1]
    ma120 = close.rolling(min(120, len(df))).mean().iloc[-1]

    above = sum([latest > ma5, latest > ma10, latest > ma20, latest > ma60, latest > ma120])

    if above == 5:
        score += 20
        signals.append(("✅", "多头排列完美，股价站上全部均线，趋势强烈"))
    elif above >= 4:
        score += 12
        signals.append(("✅", f"股价站上{above}条均线，趋势偏多"))
    elif above >= 3:
        score += 4
        signals.append(("➡️", f"股价站上{above}条均线，趋势中性偏多"))
    elif above >= 2:
        score -= 5
        signals.append(("⚠️", f"股价仅站上{above}条均线，趋势不明朗"))
    else:
        score -= 15
        signals.append(("❌", "空头排列，股价在多条均线下方，不建议买入"))

    # 均线金叉检测（5日上穿20日）
    ma5_series  = close.rolling(5).mean()
    ma20_series = close.rolling(20).mean()
    if len(ma5_series) >= 2:
        if ma5_series.iloc[-1] > ma20_series.iloc[-1] and ma5_series.iloc[-2] <= ma20_series.iloc[-2]:
            score += 8
            signals.append(("✅", "5日均线刚刚上穿20日均线（金叉），买入信号"))
        elif ma5_series.iloc[-1] < ma20_series.iloc[-1] and ma5_series.iloc[-2] >= ma20_series.iloc[-2]:
            score -= 8
            signals.append(("❌", "5日均线刚刚下穿20日均线（死叉），卖出信号"))

    # ── 3. 量价配合 ───────────────────────────────────────
    vol_ma5  = volume.rolling(5).mean().iloc[-1]
    vol_ma20 = volume.rolling(20).mean().iloc[-1]
    recent_ret = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]

    vol_ratio = latest_vol / vol_ma20 if vol_ma20 > 0 else 1.0

    if recent_ret > 0.02 and vol_ratio >= 1.5:
        score += 12
        signals.append(("✅", f"放量上涨（成交量是20日均量的{vol_ratio:.1f}倍），主力在推升"))
    elif recent_ret > 0.02 and vol_ratio < 0.8:
        score -= 5
        signals.append(("⚠️", "上涨缩量，涨势动能不足，注意假突破"))
    elif recent_ret < -0.02 and vol_ratio >= 1.5:
        score -= 10
        signals.append(("❌", f"放量下跌（量比{vol_ratio:.1f}），有资金在出逃"))
    elif recent_ret < -0.02 and vol_ratio < 0.8:
        score += 3
        signals.append(("➡️", "缩量回调，属于正常洗盘，不必恐慌"))
    else:
        signals.append(("➡️", f"量能正常（量比{vol_ratio:.1f}），无明显异动"))

    # ── 4. 近期回撤（从高点回落多少）────────────────────
    recent_high = high.iloc[-20:].max()
    drawdown_from_high = (latest - recent_high) / recent_high * 100

    if -5 <= drawdown_from_high <= 0:
        score -= 3
        signals.append(("➡️", f"距近20日高点仅回调{abs(drawdown_from_high):.1f}%，仍在高位"))
    elif -15 <= drawdown_from_high < -5:
        score += 8
        signals.append(("✅", f"从近期高点回调{abs(drawdown_from_high):.1f}%，有一定买入价值"))
    elif drawdown_from_high < -15:
        score += 5
        signals.append(("⚠️", f"从近期高点回调{abs(drawdown_from_high):.1f}%，回调较深，需判断是否已变趋势"))

    # ── 5. RSI超买超卖 ───────────────────────────────────
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rsi   = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).iloc[-1]

    if not np.isnan(rsi):
        if rsi <= 30:
            score += 12
            signals.append(("✅", f"RSI={rsi:.0f}，超卖区间，反弹概率大"))
        elif rsi <= 45:
            score += 5
            signals.append(("✅", f"RSI={rsi:.0f}，偏低，有上涨空间"))
        elif rsi <= 60:
            signals.append(("➡️", f"RSI={rsi:.0f}，中性区间"))
        elif rsi <= 75:
            score -= 5
            signals.append(("⚠️", f"RSI={rsi:.0f}，偏高，注意短期回调风险"))
        else:
            score -= 12
            signals.append(("❌", f"RSI={rsi:.0f}，严重超买，高风险区域"))

    # ── 综合评分限制 ──────────────────────────────────────
    score = max(0, min(100, score))
    result["score"] = round(score, 1)

    # ── 买入评级 ──────────────────────────────────────────
    if score >= 75:
        result["rating"] = "强烈推荐"
        result["rating_color"] = "green"
    elif score >= 60:
        result["rating"] = "可以买入"
        result["rating_color"] = "green"
    elif score >= 45:
        result["rating"] = "观望等待"
        result["rating_color"] = "yellow"
    elif score >= 30:
        result["rating"] = "不建议买入"
        result["rating_color"] = "red"
    else:
        result["rating"] = "强烈回避"
        result["rating_color"] = "red"

    # ── 目标价 & 止损价 ───────────────────────────────────
    # 止损：跌破20日均线或近期低点（取较近的一个）
    recent_low   = low.iloc[-20:].min()
    stop_by_ma20 = round(ma20 * 0.98, 2)   # 跌破MA20再给2%缓冲
    stop_by_low  = round(recent_low * 0.99, 2)
    result["stop_loss"] = max(stop_by_ma20, stop_by_low)

    # 目标1：前高（近60日最高点）
    target1 = round(high.iloc[-60:].max(), 2)
    if target1 <= latest:   # 已在前高之上，则给5%空间
        target1 = round(latest * 1.05, 2)
    result["target1"] = target1

    # 目标2：目标1基础上再涨15%（第二目标）
    result["target2"] = round(target1 * 1.15, 2)

    # 当前可买区间（在当前价 ±3%以内）
    result["buy_zone"] = (round(latest * 0.97, 2), round(latest * 1.02, 2))

    # 计算盈亏比
    potential_gain = result["target1"] - latest
    potential_loss = latest - result["stop_loss"]
    if potential_loss > 0:
        result["risk_reward"] = round(potential_gain / potential_loss, 2)
    else:
        result["risk_reward"] = 0

    result["signals"] = signals
    result["ma20"]    = round(ma20, 2)
    result["ma60"]    = round(ma60, 2)
    result["rsi"]     = round(rsi, 1) if not np.isnan(rsi) else None

    return result


def format_timing_report(ev: dict) -> str:
    """将评估结果格式化为可读字符串"""
    if "error" in ev:
        return f"  评估失败: {ev['error']}"

    lines = []
    score = ev["score"]
    rating = ev["rating"]

    # 评级颜色
    if ev.get("rating_color") == "green":
        rating_str = f"\033[92m{rating}\033[0m"
    elif ev.get("rating_color") == "red":
        rating_str = f"\033[91m{rating}\033[0m"
    else:
        rating_str = f"\033[93m{rating}\033[0m"

    lines.append(f"  当前价: {ev['price']}   评分: {score}/100   评级: {rating_str}")
    lines.append(f"  52周位置: {ev.get('position_pct', '?')}%   MA20: {ev.get('ma20', '?')}   RSI: {ev.get('rsi', '?')}")

    rr = ev.get("risk_reward", 0)
    rr_str = f"\033[92m{rr}\033[0m" if rr >= 2 else (f"\033[93m{rr}\033[0m" if rr >= 1 else f"\033[91m{rr}\033[0m")
    lines.append(
        f"  止损价: \033[91m{ev.get('stop_loss', '?')}\033[0m   "
        f"目标1: \033[92m{ev.get('target1', '?')}\033[0m   "
        f"目标2: \033[92m{ev.get('target2', '?')}\033[0m   "
        f"盈亏比: {rr_str}"
    )
    bz = ev.get("buy_zone")
    bz_str = f"({bz[0]:.2f} ~ {bz[1]:.2f})" if bz else "?"
    lines.append(f"  买入区间: {bz_str}")
    lines.append("")

    for icon, msg in ev.get("signals", []):
        lines.append(f"    {icon} {msg}")

    return "\n".join(lines)
