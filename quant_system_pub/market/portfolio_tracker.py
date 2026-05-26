"""
持仓跟踪 + 卖出信号模块
记录你买了哪些股、成本多少，每天告诉你：
  - 每只持仓的盈亏状态
  - 是否触发止损/止盈
  - 是否出现卖出信号（技术面恶化、消息反转）
  - 综合建议：继续持有 / 减仓 / 清仓

持仓数据存在本地 JSON 文件，程序关闭后不丢失
"""
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

PORTFOLIO_FILE = Path(__file__).parent.parent / "data" / "my_portfolio.json"
PORTFOLIO_FILE.parent.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────
# 持仓文件读写
# ─────────────────────────────────────────────────────────
def load_portfolio() -> dict:
    """加载持仓数据，格式: {code: {name, cost, shares, buy_date, stop_loss, target}}"""
    if not PORTFOLIO_FILE.exists():
        return {}
    with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_portfolio(portfolio: dict):
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)


def add_position(code: str, name: str, cost: float, shares: int,
                 stop_loss: float = None, target: float = None):
    """添加一笔持仓"""
    p = load_portfolio()
    p[code] = {
        "name":      name,
        "cost":      cost,
        "shares":    shares,
        "buy_date":  datetime.today().strftime("%Y-%m-%d"),
        "stop_loss": stop_loss or round(cost * 0.92, 2),   # 默认止损-8%
        "target":    target    or round(cost * 1.20, 2),   # 默认目标+20%
    }
    save_portfolio(p)
    print(f"  已添加持仓: {code} {name}  成本={cost}  {shares}股")


def remove_position(code: str):
    """清除一笔持仓"""
    p = load_portfolio()
    if code in p:
        del p[code]
        save_portfolio(p)
        print(f"  已移除持仓: {code}")


# ─────────────────────────────────────────────────────────
# 实时行情获取
# ─────────────────────────────────────────────────────────
def get_realtime_price(codes: list[str]) -> dict[str, dict]:
    """批量获取个股实时价格（通过 reliable_api 自动 failover）"""
    from data.reliable_api import API
    result = {}
    try:
        df = API.spot()
        if df.empty:
            return result
        # reliable_api 已标准化列名：code/name/price/chg
        sub = df[df["code"].isin(codes)]
        for _, row in sub.iterrows():
            result[str(row["code"])] = {
                "price":      float(row.get("price", 0) or 0),
                "change_pct": float(row.get("chg",   0) or 0),
                "name":       str(row.get("name", "")),
            }
    except Exception as e:
        logger.warning(f"实时行情获取失败: {e}")
    return result


# ─────────────────────────────────────────────────────────
# 卖出信号判断
# ─────────────────────────────────────────────────────────
def check_sell_signals(code: str, pos: dict, price: float, df_hist: pd.DataFrame) -> list[tuple]:
    """
    判断是否应该卖出，返回信号列表 [(urgency, reason)]
    urgency: 'SELL_NOW'(立刻清仓) / 'REDUCE'(减仓) / 'WATCH'(注意观察)
    """
    signals = []
    cost   = pos["cost"]
    stop   = pos["stop_loss"]
    target = pos["target"]
    pnl    = (price - cost) / cost * 100

    # ── 硬性止损（立刻清仓）──────────────────────────────
    if price <= stop:
        signals.append(("SELL_NOW", f"触发止损价 {stop}，当前亏损 {pnl:.1f}%，立刻清仓控制损失"))
        return signals   # 止损直接返回，不需要看后面

    # ── 目标价止盈（可减仓）──────────────────────────────
    if price >= target:
        signals.append(("REDUCE", f"已达目标价 {target}，盈利 {pnl:.1f}%，建议减仓50%锁定利润"))

    if df_hist is None or len(df_hist) < 20:
        return signals

    close  = df_hist["close"]
    volume = df_hist["volume"]
    ma5    = close.rolling(5).mean().iloc[-1]
    ma10   = close.rolling(10).mean().iloc[-1]
    ma20   = close.rolling(20).mean().iloc[-1]

    # ── 均线死叉（减仓信号）──────────────────────────────
    ma5_s  = close.rolling(5).mean()
    ma10_s = close.rolling(10).mean()
    if len(ma5_s) >= 2:
        if ma5_s.iloc[-1] < ma10_s.iloc[-1] and ma5_s.iloc[-2] >= ma10_s.iloc[-2]:
            signals.append(("REDUCE", "5日均线下穿10日均线（死叉），短期动能减弱，建议减仓30%"))

    # ── 跌破MA20（警告）─────────────────────────────────
    if price < ma20 and pnl > 0:
        signals.append(("WATCH", f"价格跌破20日均线({ma20:.2f})，若次日未收复则减仓"))
    elif price < ma20 and pnl <= 0:
        signals.append(("REDUCE", f"价格跌破20日均线({ma20:.2f})且持仓亏损，建议减仓"))

    # ── 放量大跌（立刻关注）──────────────────────────────
    vol_ma20 = volume.rolling(20).mean().iloc[-1]
    latest_vol = volume.iloc[-1]
    latest_ret = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]
    if latest_ret < -0.05 and latest_vol > vol_ma20 * 2:
        signals.append(("SELL_NOW", f"放量暴跌（量比{latest_vol/vol_ma20:.1f}），主力在出货，立刻清仓"))

    # ── 高位滞涨（盈利超20%但连续5天不创新高）───────────
    if pnl >= 20:
        recent_high = df_hist["high"].iloc[-5:].max()
        prev_high   = df_hist["high"].iloc[-10:-5].max()
        if recent_high <= prev_high * 1.01:
            signals.append(("REDUCE", f"盈利{pnl:.1f}%但5日内未创新高，高位滞涨，建议减仓锁利"))

    # ── 次日高开后回落（散户最常踩的坑）───────────────────
    # 判断条件：买入次日最高价比收盘涨幅 > 3%，但当天最终收盘涨幅 < 最高点一半
    hold_days = pos.get("_hold_days", 0)   # 由 analyze_portfolio 注入
    if hold_days == 1 and len(df_hist) >= 2:
        last = df_hist.iloc[-1]
        gap_from_high = (last["high"] - last["close"]) / last["close"] * 100
        day_chg = (last["close"] - df_hist.iloc[-2]["close"]) / df_hist.iloc[-2]["close"] * 100
        if gap_from_high >= 3 and day_chg < gap_from_high * 0.4:
            signals.append(("SELL_NOW",
                f"次日冲高{last['high']:.2f}后大幅回落（回落{gap_from_high:.1f}%），"
                "主力高开减仓，明日若继续弱势立刻清仓"))

    # ── 短线持有超3天未盈利（动量股不能拿）──────────────────
    if hold_days >= 3 and pnl < 2:
        signals.append(("REDUCE",
            f"持仓{hold_days}天仍未盈利（当前{pnl:+.1f}%），"
            "短线动量已消退，建议止损或清仓换股"))

    # ── 买入次日浮盈超5%但未锁利，之后缩水回成本附近 ────────
    if hold_days >= 2 and pnl < 1:
        max_price = df_hist["high"].iloc[-hold_days:].max() if hold_days <= len(df_hist) else price
        max_gain  = (max_price - cost) / cost * 100
        if max_gain >= 5:
            signals.append(("SELL_NOW",
                f"曾浮盈{max_gain:.1f}%未止盈，现已回吐至{pnl:+.1f}%，"
                "坚决止损，避免继续扩大亏损"))

    return signals


# ─────────────────────────────────────────────────────────
# 主函数：分析全部持仓
# ─────────────────────────────────────────────────────────
def analyze_portfolio(loader=None) -> list[dict]:
    """
    分析所有持仓，返回每只股的状态报告

    loader: DataLoader 实例（用于获取历史数据），可为None（仅看实时价格）
    """
    portfolio = load_portfolio()
    if not portfolio:
        return []

    codes = list(portfolio.keys())
    price_map = get_realtime_price(codes)

    reports = []
    for code, pos in portfolio.items():
        price_info = price_map.get(code, {})
        price = price_info.get("price", pos["cost"])
        chg   = price_info.get("change_pct", 0)

        pnl_pct  = (price - pos["cost"]) / pos["cost"] * 100
        pnl_amt  = (price - pos["cost"]) * pos["shares"]
        mkt_val  = price * pos["shares"]
        hold_days = (datetime.today() - datetime.strptime(pos["buy_date"], "%Y-%m-%d")).days

        # 获取历史数据做技术分析
        df_hist = None
        end   = datetime.today().strftime("%Y-%m-%d")
        start = (datetime.today() - pd.DateOffset(days=90)).strftime("%Y-%m-%d")
        if loader:
            try:
                df_hist = loader.get_stock_daily(code, start, end)
            except Exception as e:
                logger.warning(f"获取 {code} 历史数据失败，跳过技术分析: {e}")
        if df_hist is None:
            try:
                from data.reliable_api import API
                df_hist = API.history(code, start, end)
            except Exception:
                pass

        pos["_hold_days"] = hold_days   # 注入持仓天数供 check_sell_signals 使用
        sell_signals = check_sell_signals(code, pos, price, df_hist)

        # 综合建议
        if any(s[0] == "SELL_NOW" for s in sell_signals):
            advice = "立刻清仓"
            advice_color = "red"
        elif any(s[0] == "REDUCE" for s in sell_signals):
            advice = "建议减仓"
            advice_color = "yellow"
        elif any(s[0] == "WATCH" for s in sell_signals):
            advice = "注意观察"
            advice_color = "yellow"
        else:
            advice = "继续持有"
            advice_color = "green"

        reports.append({
            "code":       code,
            "name":       pos["name"],
            "cost":       pos["cost"],
            "price":      price,
            "change_pct": chg,
            "pnl_pct":    round(pnl_pct, 2),
            "pnl_amt":    round(pnl_amt, 0),
            "mkt_val":    round(mkt_val, 0),
            "shares":     pos["shares"],
            "hold_days":  hold_days,
            "stop_loss":  pos["stop_loss"],
            "target":     pos["target"],
            "sell_signals": sell_signals,
            "advice":     advice,
            "advice_color": advice_color,
        })

    return sorted(reports, key=lambda x: x["pnl_pct"])
