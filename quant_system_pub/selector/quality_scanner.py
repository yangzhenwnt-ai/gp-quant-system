"""
全市场优质股扫描器
从5000只股票中筛选真正值得关注的潜力股

两个维度并行扫描：
  【强势股】技术面强势 + 主力资金持续流入（适合追涨，3-10天）
  【价值股】估值合理 + 基本面稳健 + 价格在低位（适合左侧布局，1-4周）

评分逻辑：
  技术面（60%）：均线多头排列、股价相对强度、量价配合
  资金面（30%）：成交额放大、换手率适中、量比健康
  风险控制（扣分）：ST/暴雷特征、极端换手、价格异常
"""

import logging
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 第一步：全市场初筛（从行情数据快速过滤）
# ─────────────────────────────────────────────────────────────

def _initial_filter(spot_df: pd.DataFrame) -> pd.DataFrame:
    """
    用当日行情快速过滤，去掉明显不符合的股票
    保留约 500-800 只进入精选池
    """
    df = spot_df.copy()

    # 基础过滤：价格、成交额有效
    df = df[df["price"] > 0]
    df = df[df["amount"] > 0]

    # 排除 ST / *ST / 退市风险 / 科创板(688) / 北交所(8x/4x)
    # 用正则一次匹配：名称含 ST（含*ST）、退、暂停
    df = df[~df["name"].str.contains(r"(^|\s|\*)?ST|退市|退|暂停", na=False, regex=True)]
    df = df[~df["code"].str.startswith("688")]
    df = df[~df["code"].str.match(r"^(82|83|87|43|40)")]

    # 排除停牌（当天无成交量）
    df = df[df["volume"] > 0]

    # 价格过滤：3元以上（排除仙股和低价垃圾股）、300元以下
    df = df[(df["price"] >= 3.0) & (df["price"] <= 300)]

    # 涨跌幅：排除已经涨停 / 跌停（追不进去 / 有风险）
    if "chg" in df.columns:
        df = df[(df["chg"] > -9.5) & (df["chg"] < 9.5)]

    # 换手率：0.3% ~ 15% 之间（太低=僵尸股，太高=被爆炒）
    if "turnover" in df.columns:
        df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce").fillna(0)
        df = df[df["turnover"].between(0.3, 15) | (df["turnover"] == 0)]

    # 成交额 > 5000万（有基本流动性，腾讯接口单位：万元）
    df = df[df["amount"] >= 5000]

    # 市值过滤（通过总市值列，若有）：20亿~500亿
    if "market_cap" in df.columns:
        cap = pd.to_numeric(df["market_cap"], errors="coerce")
        # 腾讯接口市值单位为元
        df = df[cap.between(20e8, 500e8) | cap.isna()]

    return df.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────
# 第二步：技术面评分（从历史日线计算）
# ─────────────────────────────────────────────────────────────

def _technical_score(hist: pd.DataFrame) -> dict:
    """
    基于近60日日线数据计算技术面评分 (0-100)
    返回 {score, ma_align, rs, details}
    """
    if hist is None or len(hist) < 20:
        return {"score": 0, "ma_align": False, "rs": 0, "details": "数据不足"}

    close = hist["close"]
    vol   = hist["volume"]
    n = len(close)

    score = 50  # 基础分
    details = []

    # ── 1. 均线多头排列（MA5 > MA10 > MA20 > MA60）──────────
    ma5  = close.rolling(5).mean().iloc[-1]
    ma10 = close.rolling(10).mean().iloc[-1] if n >= 10 else ma5
    ma20 = close.rolling(20).mean().iloc[-1] if n >= 20 else ma5
    ma60 = close.rolling(60).mean().iloc[-1] if n >= 60 else ma20
    price = close.iloc[-1]

    ma_align = (price > ma5 > ma10 > ma20)
    if price > ma5 > ma10 > ma20 > ma60:
        score += 20
        details.append("均线完美多头")
    elif price > ma5 > ma10 > ma20:
        score += 12
        details.append("短中期多头")
    elif price > ma20:
        score += 5
        details.append("站上MA20")
    else:
        score -= 15
        details.append("跌破MA20")

    # ── 2. 近期相对强度（vs MA20，避免跌太深）───────────────
    rs = (price - ma20) / ma20 * 100
    if 0 < rs <= 8:
        score += 10  # 刚突破，最佳买点区间
        details.append(f"价格高于MA20 {rs:.1f}%（理想区间）")
    elif 8 < rs <= 20:
        score += 5
        details.append(f"高于MA20 {rs:.1f}%")
    elif rs > 20:
        score -= 8  # 涨幅过大，追高风险
        details.append(f"高于MA20 {rs:.1f}%（追高风险）")

    # ── 3. 近期趋势：最近5日均线斜率向上 ─────────────────────
    if n >= 7:
        ma5_series = close.rolling(5).mean()
        slope = (ma5_series.iloc[-1] - ma5_series.iloc[-5]) / ma5_series.iloc[-5] * 100
        if slope > 1.0:
            score += 8
            details.append("MA5上行")
        elif slope > 0:
            score += 3
        elif slope < -1.0:
            score -= 8
            details.append("MA5下行")

    # ── 4. 量价配合：上涨日成交量 > 下跌日成交量 ─────────────
    if n >= 10 and "volume" in hist.columns:
        recent = hist.tail(10)
        up_days   = recent[recent["close"] >= recent["close"].shift(1)]
        down_days = recent[recent["close"] <  recent["close"].shift(1)]
        up_vol    = up_days["volume"].mean() if not up_days.empty else 0
        down_vol  = down_days["volume"].mean() if not down_days.empty else 0
        if up_vol > down_vol * 1.3:
            score += 8
            details.append("量价配合良好")
        elif down_vol > up_vol * 1.5:
            score -= 10
            details.append("跌多涨少（出货形态）")

    # ── 5. 近5日有放量（不是死水）─────────────────────────────
    if n >= 25 and "volume" in hist.columns:
        vol_ma20   = vol.rolling(20).mean().iloc[-1]
        recent_vol = vol.iloc[-5:].mean()
        vol_ratio  = recent_vol / vol_ma20 if vol_ma20 > 0 else 1
        if 1.2 <= vol_ratio <= 3.0:
            score += 5
            details.append(f"近期放量{vol_ratio:.1f}x")
        elif vol_ratio > 3.0:
            score -= 3  # 过度放量，可能是顶部

    # ── 6. 不在近期高点附近（防止买到顶部）─────────────────────
    if n >= 20:
        high_20 = hist["high"].iloc[-20:].max()
        dist_from_high = (high_20 - price) / high_20 * 100
        if dist_from_high > 15:
            score += 5  # 远离高点，安全边际好
        elif dist_from_high < 3:
            score -= 5  # 接近历史高点

    score = max(0, min(100, score))
    return {
        "score":    round(score, 1),
        "ma_align": ma_align,
        "rs":       round(rs, 2),
        "details":  "  ".join(details),
        "ma5": round(ma5, 2), "ma20": round(ma20, 2),
    }


# ─────────────────────────────────────────────────────────────
# 第三步：资金面评分（从当日行情评估）
# ─────────────────────────────────────────────────────────────

def _money_score(row: pd.Series) -> float:
    """基于当日行情估算资金面评分 (0-100)"""
    score = 50.0

    chg      = float(row.get("chg", 0) or 0)
    amount   = float(row.get("amount", 0) or 0)
    turnover = float(row.get("turnover", 0) or 0)
    vol_ratio= float(row.get("vol_ratio", 0) or 0)

    # 今日涨幅（温和上涨最好）
    if 1 < chg <= 5:
        score += 12
    elif 5 < chg <= 8:
        score += 6
    elif chg > 8:
        score += 2   # 接近涨停，买不了
    elif -1 <= chg <= 1:
        score += 5   # 横盘整理也可以
    elif chg < -3:
        score -= 15

    # 成交额（腾讯接口单位：万元）
    # 合理区间：5000万~5亿 → 5000~50000万
    if 5000 <= amount <= 50000:
        score += 10
    elif amount > 100000:
        score -= 5  # 超大成交（>10亿），可能是出货

    # 量比（1.5~3倍最佳：有放量但不过激）
    if 1.5 <= vol_ratio <= 3.0:
        score += 8
    elif vol_ratio > 5:
        score -= 8  # 爆量，可能见顶
    elif vol_ratio < 0.5:
        score -= 5  # 缩量，无人关注

    # 换手率（1%~5% 黄金区间：有换手但不被爆炒）
    if 1 <= turnover <= 5:
        score += 10
    elif turnover > 10:
        score -= 10

    return max(0, min(100, score))


# ─────────────────────────────────────────────────────────────
# 核心函数：全市场扫描
# ─────────────────────────────────────────────────────────────

def scan_quality_stocks(
    mode: str = "both",      # "momentum"=强势股 | "value"=价值股 | "both"=两者
    top_n: int = 20,         # 最终输出前N名
    hist_days: int = 60,     # 历史日线天数
) -> dict:
    """
    扫描全市场优质股
    返回 {"momentum": DataFrame, "value": DataFrame, "scan_time": str}
    """
    from data.reliable_api import API

    scan_time = datetime.now().strftime("%H:%M")
    result = {"momentum": pd.DataFrame(), "value": pd.DataFrame(), "scan_time": scan_time}

    # ── Step 1: 获取全市场行情 ──────────────────────────────
    print("  获取全市场实时行情...")
    spot = API.spot()
    if spot.empty:
        logger.warning("全市场行情为空，无法扫描")
        return result

    candidates = _initial_filter(spot)
    logger.info(f"初筛后候选: {len(candidates)} 只")

    # ── Step 2: 快速资金面初评，只对资金面 top150 做历史分析 ──
    candidates["money_score"] = candidates.apply(_money_score, axis=1)

    # 按资金面排序，取 top 80 做技术分析（降低并发压力，避免崩溃）
    pool = candidates.nlargest(80, "money_score").reset_index(drop=True)

    # ── Step 3: 并行获取历史日线，计算技术面评分 ──────────────
    print(f"  对 {len(pool)} 只候选股做技术分析（并行）...")
    end_date   = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - timedelta(days=hist_days + 10)).strftime("%Y-%m-%d")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    rows_by_code = {str(row["code"]): row for _, row in pool.iterrows()}

    def _fetch_tech(code):
        try:
            hist = API.history(code, start_date, end_date)
            tech = _technical_score(hist)
        except Exception:
            tech = {"score": 0, "ma_align": False, "rs": 0, "details": "获取失败",
                    "ma5": 0, "ma20": 0}
        time.sleep(0.05)   # 小间隔，避免同时大量请求打崩接口
        return code, tech

    tech_map = {}
    with ThreadPoolExecutor(max_workers=4) as pool_ex:
        futures = {pool_ex.submit(_fetch_tech, code): code for code in rows_by_code}
        done = 0
        for fut in as_completed(futures):
            try:
                code, tech = fut.result()
                tech_map[code] = tech
            except Exception:
                pass
            done += 1
            if done % 30 == 0:
                logger.info(f"  技术分析进度: {done}/{len(rows_by_code)}")

    tech_results = []
    for code, row in rows_by_code.items():
        tech = tech_map.get(code, {"score": 0, "ma_align": False, "rs": 0,
                                   "details": "获取失败", "ma5": 0, "ma20": 0})
        tech_results.append({
            "code":        code,
            "name":        str(row.get("name", "")),
            "price":       float(row.get("price", 0)),
            "chg":         float(row.get("chg", 0)),
            "amount":      float(row.get("amount", 0)),
            "turnover":    float(row.get("turnover", 0)),
            "vol_ratio":   float(row.get("vol_ratio", 0)),
            "money_score": float(row.get("money_score", 0)),
            "tech_score":  tech["score"],
            "ma_align":    tech["ma_align"],
            "rs_pct":      tech["rs"],
            "tech_detail": tech["details"],
            "ma5":         tech.get("ma5", 0),
            "ma20":        tech.get("ma20", 0),
        })

    # ── Step 3.5: 基本面评分（仅在有缓存时使用，不主动拉取避免拖慢）───
    # 主扫描流程不等待财务数据；F详情页打开时再异步补充
    fund_score_map: dict[str, float] = {}
    fund_flag_map:  dict[str, str]   = {}
    try:
        from data.fundamental import get_fundamental_score
        import pickle, pathlib
        _fund_cache_dir = pathlib.Path(__file__).parent.parent / "cache" / "fundamental"
        for r in tech_results:
            code = r["code"]
            cp = _fund_cache_dir / f"{code}.pkl"
            if cp.exists():
                try:
                    info = pickle.loads(cp.read_bytes())
                    if not info.get("error"):
                        fscore, _ = get_fundamental_score(info)
                        flag = "good" if fscore >= 65 else ("warn" if fscore < 40 else "")
                        fund_score_map[code] = fscore
                        fund_flag_map[code]  = flag
                except Exception:
                    pass
    except Exception:
        pass

    for r in tech_results:
        r["fund_score"] = fund_score_map.get(r["code"], 50.0)
        r["fund_flag"]  = fund_flag_map.get(r["code"], "")

    df = pd.DataFrame(tech_results)
    if df.empty:
        return result

    # ── Step 4: 综合评分 ──────────────────────────────────────
    # 技术面60% + 资金面40%（基本面仅在缓存存在时微调）
    df["total_score"] = (
        df["tech_score"]  * 0.60 +
        df["money_score"] * 0.40
    )

    # 加分：均线多头完美排列
    df.loc[df["ma_align"], "total_score"] += 5

    # 基本面缓存命中时：优秀加分 / 差劲扣分（命中才生效）
    df.loc[df["fund_flag"] == "good", "total_score"] += 4
    df.loc[df["fund_flag"] == "warn", "total_score"] -= 8

    # 扣分：今日大跌
    df.loc[df["chg"] < -2, "total_score"] -= 10
    df.loc[df["chg"] < -4, "total_score"] -= 10

    # 扣分：低价股（3~5元区间，垃圾股聚集区）
    df.loc[df["price"] < 5,  "total_score"] -= 8
    df.loc[df["price"] < 4,  "total_score"] -= 8   # 双重扣（<4元扣16）

    # 扣分：成交额过小（5000~8000万，流动性偏弱）
    df.loc[df["amount"] < 8000, "total_score"] -= 5

    df["total_score"] = df["total_score"].clip(0, 100)

    # ── Step 5: 分类输出 ──────────────────────────────────────
    # 强势股：技术面 > 60 且 当日涨幅 > 0（今天在涨）
    momentum = (
        df[(df["tech_score"] >= 60) & (df["chg"] > 0) & (df["ma_align"])]
        .sort_values("total_score", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )
    momentum.index += 1

    # 价值股（相对低位）：技术面中等 + rs_pct 在低位 + 均线走平/向上
    value = (
        df[(df["tech_score"] >= 50) & (df["rs_pct"].between(-5, 8)) & (df["chg"] > -1)]
        .sort_values("total_score", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )
    value.index += 1

    result["momentum"] = momentum
    result["value"]    = value
    return result
