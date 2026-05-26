"""
选股历史胜率统计模块
回溯验证：系统过去选出的股票，在买入后N天内胜率如何

统计逻辑：
  读取历史选股记录（pick_YYYYMMDD.csv）
  对每只选出的股票，查询其选出后 3/5/10 个交易日的涨跌幅
  计算：胜率、平均收益、盈亏比、最大盈利、最大亏损
"""
import logging
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

REPORT_DIR = Path(__file__).parent.parent / "reports"


def load_pick_history() -> pd.DataFrame:
    """读取所有历史选股记录，合并为一个 DataFrame"""
    files = sorted(REPORT_DIR.glob("pick_*.csv"))
    if not files:
        return pd.DataFrame()

    frames = []
    for f in files:
        try:
            date_str = f.stem.replace("pick_", "")   # 20260401
            df = pd.read_csv(f, encoding="utf-8-sig")
            df["pick_date"] = pd.to_datetime(date_str, format="%Y%m%d")
            frames.append(df)
        except Exception as e:
            logger.warning(f"读取 {f.name} 失败: {e}")

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    # 统一列名（兼容中英文）
    rename = {}
    for c in combined.columns:
        if "代码" in c or c.lower() == "code":
            rename[c] = "code"
        elif "名称" in c or c.lower() == "name":
            rename[c] = "name"
        elif "板块" in c:
            rename[c] = "sector"
    combined = combined.rename(columns=rename)
    if "code" in combined.columns:
        combined["code"] = combined["code"].astype(str).str.zfill(6)
    return combined


def calc_forward_return(code: str, pick_date: pd.Timestamp,
                         days: int, loader) -> float | None:
    """
    计算个股在 pick_date 之后 days 个交易日的收益率
    返回百分比，None 表示数据不足
    """
    try:
        start = pick_date.strftime("%Y-%m-%d")
        end   = (pick_date + pd.DateOffset(days=days + 30)).strftime("%Y-%m-%d")
        df = loader.get_stock_daily(code, start, end)
        if df is None or len(df) < days + 1:
            return None
        df = df.sort_values("date").reset_index(drop=True)
        # 找到 pick_date 对应的收盘价
        sub = df[df["date"] >= pick_date]
        if len(sub) < days + 1:
            return None
        buy_price  = float(sub.iloc[0]["close"])
        sell_price = float(sub.iloc[days]["close"])
        return (sell_price - buy_price) / buy_price * 100
    except Exception:
        return None


def analyze_win_rate(loader, forward_days: list[int] = [3, 5, 10]) -> dict:
    """
    对所有历史选股记录计算胜率统计

    返回格式：
    {
      3:  {"win_rate": 0.65, "avg_return": 3.2, "records": [...]},
      5:  {...},
      10: {...},
    }
    """
    picks = load_pick_history()
    if picks.empty:
        return {}

    if "code" not in picks.columns or "pick_date" not in picks.columns:
        logger.warning("历史选股数据格式不正确")
        return {}

    # 只分析3个月前的记录（太新的数据没到期）
    cutoff = pd.Timestamp.today() - pd.DateOffset(days=max(forward_days) + 5)
    picks  = picks[picks["pick_date"] <= cutoff].copy()

    if picks.empty:
        return {"msg": "暂无足够的历史选股数据（需要选股后等N天才能统计胜率）"}

    results = {}
    total = len(picks)

    for days in forward_days:
        returns = []
        print(f"    计算 {days} 日胜率，共 {total} 条记录...")

        for _, row in picks.iterrows():
            ret = calc_forward_return(
                str(row["code"]).zfill(6),
                row["pick_date"],
                days,
                loader
            )
            if ret is not None:
                returns.append({
                    "code":       row.get("code", ""),
                    "name":       row.get("name", ""),
                    "sector":     row.get("sector", ""),
                    "pick_date":  row["pick_date"].strftime("%Y-%m-%d"),
                    "return_pct": round(ret, 2),
                    "win":        ret > 0,
                })

        if not returns:
            results[days] = {"win_rate": None, "msg": "数据不足"}
            continue

        ret_df = pd.DataFrame(returns)
        wins   = ret_df["win"].sum()
        total_valid = len(ret_df)

        pos_returns = ret_df[ret_df["return_pct"] > 0]["return_pct"]
        neg_returns = ret_df[ret_df["return_pct"] <= 0]["return_pct"]

        results[days] = {
            "win_rate":    round(wins / total_valid * 100, 1),
            "avg_return":  round(ret_df["return_pct"].mean(), 2),
            "avg_win":     round(pos_returns.mean(), 2) if not pos_returns.empty else 0,
            "avg_loss":    round(neg_returns.mean(), 2) if not neg_returns.empty else 0,
            "max_win":     round(ret_df["return_pct"].max(), 2),
            "max_loss":    round(ret_df["return_pct"].min(), 2),
            "total":       total_valid,
            "records":     ret_df.sort_values("return_pct", ascending=False).to_dict("records"),
        }

        # 盈亏比
        if neg_returns.empty or neg_returns.mean() == 0:
            results[days]["risk_reward"] = 999
        else:
            results[days]["risk_reward"] = round(
                abs(pos_returns.mean() / neg_returns.mean()), 2
            ) if not pos_returns.empty else 0

    return results


def print_win_rate_report(results: dict):
    """格式化输出胜率报告"""
    if not results:
        print("  暂无历史数据")
        return
    if "msg" in results:
        print(f"  {results['msg']}")
        return

    for days, stat in results.items():
        if stat.get("win_rate") is None:
            print(f"\n  {days}日胜率: 数据不足")
            continue

        wr = stat["win_rate"]
        wr_color = "\033[92m" if wr >= 60 else ("\033[93m" if wr >= 50 else "\033[91m")

        print(f"\n  \033[1m买入后 {days} 个交易日表现\033[0m  （样本={stat['total']}笔）")
        print(f"    胜率:     {wr_color}{wr}%\033[0m")
        print(f"    平均收益: {stat['avg_return']:+.2f}%"
              f"（盈利均值 \033[92m+{stat['avg_win']}%\033[0m / "
              f"亏损均值 \033[91m{stat['avg_loss']}%\033[0m）")
        print(f"    盈亏比:   {stat['risk_reward']}")
        print(f"    最大盈利: \033[92m+{stat['max_win']}%\033[0m   最大亏损: \033[91m{stat['max_loss']}%\033[0m")

        # 按板块统计胜率
        df = pd.DataFrame(stat["records"])
        if "sector" in df.columns and not df["sector"].isna().all():
            sector_stats = df.groupby("sector").agg(
                胜率=("win", lambda x: f"{x.mean()*100:.0f}%"),
                均收益=("return_pct", lambda x: f"{x.mean():+.1f}%"),
                次数=("win", "count")
            ).sort_values("次数", ascending=False).head(8)
            if not sector_stats.empty:
                print(f"\n    按板块胜率分布（Top8）:")
                print(sector_stats.to_string(index=True))
