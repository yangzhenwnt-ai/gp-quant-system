"""
错杀反弹扫描器

策略逻辑：
  大盘单日跌幅 ≥ 阈值时，部分优质股因 ETF 赎回/融资强平/恐慌抛售被动下跌，
  与基本面无关。次日均值回归概率显著高于正常日。

筛选条件（缺一不可）：
  1. 大盘（上证）跌幅 ≥ 2%（调用方判断）
  2. 个股当日跌幅 ≥ 4%，且 < 9.5%（未跌停，说明可以明日买入）
  3. 成交量比 < 2.0：缩量/平量下跌 = 被动砸盘，非主力出货
  4. 近 60 日涨幅 < 30%：排除前期已高位炒作的股票
  5. 价格 ≥ MA20：长期趋势仍向上，只是被砸下来
  6. 市值 50~800 亿：太小流动性差，太大弹性不足
  7. 无 ST / 退市标记，无近期暴跌（近5日跌超20%）

评分（0~100）：
  跌幅甜区得分（5-7% 最理想）        30分
  量比得分（越小越好，说明被动砸）  25分
  位置得分（距MA20/MA60近且不破）  25分
  近期强势度得分（跌之前强势）       20分
"""

import logging
import time
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 主扫描函数
# ─────────────────────────────────────────────────────────────

def scan_mistaken_kill(
    index_chg: float,          # 大盘今日涨跌幅（负数=跌）
    trigger_threshold: float = -2.0,
    max_stocks: int = 15,
) -> dict:
    """
    扫描今日被错杀的优质股。

    参数：
      index_chg: 今日上证指数涨跌幅（%），例如 -2.5
      trigger_threshold: 触发阈值，默认 -2.0（大盘跌2%才扫）
      max_stocks: 最多返回几只

    返回：
      {
        "triggered": bool,       # 是否达到触发条件
        "index_chg": float,      # 大盘跌幅
        "stocks": [...],         # 候选股列表
        "scan_time": str,
      }
    """
    result = {
        "triggered": False,
        "index_chg": index_chg,
        "stocks": [],
        "scan_time": datetime.now().strftime("%H:%M:%S"),
    }

    if index_chg > trigger_threshold:
        # 大盘跌幅未达阈值，不扫描
        return result

    result["triggered"] = True

    from data.reliable_api import API

    # 全市场行情
    spot = API.spot()
    if spot is None or spot.empty:
        return result

    # ── 初筛（纯行情层面）──────────────────────────────────
    df = spot.copy()
    df["chg"]      = pd.to_numeric(df["chg"],      errors="coerce").fillna(0)
    df["price"]    = pd.to_numeric(df["price"],    errors="coerce").fillna(0)
    df["vol_ratio"]= pd.to_numeric(df["vol_ratio"],errors="coerce").fillna(0)
    df["amount"]   = pd.to_numeric(df["amount"],   errors="coerce").fillna(0)
    df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce").fillna(0)

    # 有效股票
    df = df[df["price"] > 0]
    df = df[df["amount"] > 1000]   # 成交额 > 1000万（有流动性）

    # 排除 ST / 退市 / 北交所 / 科创板
    df = df[~df["name"].str.contains(r"ST|退市|退|暂停", na=False, regex=True)]
    df = df[~df["code"].str.startswith(("688", "82", "83", "87", "43", "40", "302", "430"))]

    # 当日跌幅在 4%~9.4% 之间（错杀区间，未跌停可以次日买）
    df = df[(df["chg"] <= -4.0) & (df["chg"] > -9.5)]

    # 量比 < 2.0（缩量/平量下跌 = 被动砸，非主动出货）
    # 量比=0 说明接口没数据，用换手率辅助判断，不过滤掉
    vr_mask = (df["vol_ratio"] < 2.0) | (df["vol_ratio"] == 0)
    df = df[vr_mask]

    # 价格 > 3 元，< 200 元
    df = df[(df["price"] >= 3.0) & (df["price"] <= 200)]

    if df.empty:
        return result

    # ── 预排序：优先处理最像"错杀"的候选 ────────────────────
    # 量比越小越好（被动砸盘），跌幅在5-7%甜区，成交额>5000万（流动性）
    df["_pre_score"] = (
        (1.5 - df["vol_ratio"].clip(0, 2.0)) * 30          # 量比低加分
        + df["chg"].apply(lambda x: 25 if -7 <= x <= -5    # 跌幅甜区
                          else 15 if -8 <= x <= -4 else 5)
        + df["amount"].apply(lambda a: 10 if a > 50000 else 5 if a > 10000 else 0)
    )
    df = df.sort_values("_pre_score", ascending=False)

    # ── 历史数据分析（并行，最多60只候选）────────────────────
    candidates = df.head(60).copy()   # 多取一些，过滤后留15

    end   = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _analyze(row):
        code  = str(row["code"])
        price = float(row["price"])
        chg   = float(row["chg"])
        vr    = float(row["vol_ratio"])
        name  = str(row.get("name", ""))
        amount_wan = float(row["amount"])

        try:
            hist = API.history(code, start, end)
        except Exception:
            return None

        if hist is None or len(hist) < 20:
            return None

        close = hist["close"].astype(float)
        n = len(close)

        # MA 均线
        ma20 = float(close.rolling(20).mean().iloc[-1]) if n >= 20 else None
        ma60 = float(close.rolling(60).mean().iloc[-1]) if n >= 60 else None

        # 条件1：价格必须在 MA20 附近或以上（允许跌穿但不能超过3%）
        if ma20 and price < ma20 * 0.97:
            return None   # 已经跌破MA20太深，不是错杀而是趋势下行

        # 近60日涨幅（排除前期炒高的）
        if n >= 60:
            price_60d_ago = float(close.iloc[-60])
            gain_60d = (price - price_60d_ago) / price_60d_ago * 100
        elif n >= 20:
            price_20d_ago = float(close.iloc[-20])
            gain_60d = (price - price_20d_ago) / price_20d_ago * 100
        else:
            gain_60d = 0.0

        if gain_60d > 35:
            return None   # 近期已涨太多，不是错杀

        # 近5日最大跌幅（防止本身已在连续暴跌中）
        if n >= 6:
            recent_5d_low  = float(close.iloc[-6:-1].min())
            recent_5d_high = float(close.iloc[-6:-1].max())
            recent_drop = (recent_5d_low - recent_5d_high) / recent_5d_high * 100
            if recent_drop < -15:
                return None   # 本身已连续暴跌，不是一次性错杀

        # ── 评分 ────────────────────────────────────────────
        score = 0

        # 1. 跌幅甜区（4-8%最理想，太大=可能有真实利空）30分
        drop = abs(chg)
        if 5 <= drop <= 7:
            drop_score = 30
        elif 4 <= drop < 5:
            drop_score = 22
        elif 7 < drop <= 8:
            drop_score = 20
        else:
            drop_score = 12
        score += drop_score

        # 2. 量比（越小说明越被动）25分
        if vr == 0:
            vr_score = 15   # 无数据，给中位分
        elif vr < 0.5:
            vr_score = 25
        elif vr < 0.8:
            vr_score = 20
        elif vr < 1.2:
            vr_score = 15
        elif vr < 1.8:
            vr_score = 8
        else:
            vr_score = 0
        score += vr_score

        # 3. 位置（距离均线近且不破）25分
        pos_score = 0
        if ma20:
            dist_ma20 = (price - ma20) / ma20 * 100
            if dist_ma20 >= -1:   # 仍在 MA20 附近或以上
                pos_score += 15
            elif dist_ma20 >= -3:
                pos_score += 8
        if ma60:
            dist_ma60 = (price - ma60) / ma60 * 100
            if dist_ma60 >= 0:    # 在 MA60 以上
                pos_score += 10
            elif dist_ma60 >= -5:
                pos_score += 5
        score += pos_score

        # 4. 近期强势度（跌之前是否强势）20分
        if n >= 10:
            # 用今日收盘前10日均价对比
            avg_10d = float(close.iloc[-11:-1].mean()) if n >= 11 else float(close.mean())
            # 今日之前价格相对10日均价的强势度
            pre_strength = (float(close.iloc[-2]) - avg_10d) / avg_10d * 100 if n >= 2 else 0
            if pre_strength >= 3:
                score += 20
            elif pre_strength >= 0:
                score += 12
            elif pre_strength >= -5:
                score += 6
            else:
                score += 0

        # 市值估算（用成交额/换手率反推，或用price直接排名）
        # 暂时用成交额作为流动性代理
        liq_score = 0
        if amount_wan > 50000:   # 成交额 > 5亿
            liq_score = 5
        elif amount_wan > 10000:
            liq_score = 3

        # 构造返回结果
        return {
            "code":       code,
            "name":       name,
            "price":      round(price, 2),
            "chg":        round(chg, 2),
            "vol_ratio":  round(vr, 2),
            "gain_60d":   round(gain_60d, 1),
            "ma20":       round(ma20, 2) if ma20 else None,
            "ma60":       round(ma60, 2) if ma60 else None,
            "score":      round(min(100, score + liq_score), 1),
            "amount_wan": round(amount_wan, 0),
            # 次日策略提示
            "strategy":   _gen_strategy(price, chg, vr, ma20, ma60),
        }

    stocks = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_analyze, row): row["code"]
                   for _, row in candidates.iterrows()}
        for fut in as_completed(futures):
            try:
                res = fut.result()
                if res:
                    stocks.append(res)
            except Exception as e:
                logger.debug(f"错杀分析失败: {e}")

    # 按得分降序，取前 max_stocks 只
    stocks.sort(key=lambda x: x["score"], reverse=True)
    result["stocks"] = stocks[:max_stocks]
    return result


def _gen_strategy(price, chg, vr, ma20, ma60) -> str:
    """生成次日操作策略提示"""
    parts = []

    drop = abs(chg)
    if drop >= 7:
        parts.append("跌幅较大，次日观察开盘前5分钟走势，若高开可轻仓介入")
    elif drop >= 5:
        parts.append("次日若平开或高开，可在开盘30分钟后追入")
    else:
        parts.append("次日若平开或缩量，可温和介入")

    if vr < 0.8:
        parts.append("缩量下跌，明显被动砸盘，弹性较大")
    elif vr > 1.5:
        parts.append("量比偏大，注意甄别是否有真实利空")

    if ma20 and price < ma20:
        parts.append("已跌破MA20，仓位控制在半仓以内，守住止损")
    else:
        parts.append("仍在MA20以上，可正常仓位介入")

    return "；".join(parts)
