"""
盘中异动扫描模块
扫描：
  1. 突然放量拉升（量比>3x 且涨幅>2%）
  2. 涨停封板（涨幅>=9.8%）
  3. 跌停（涨幅<=-9.8%）
  4. 超大单净流入（合成自行情数据）

API.spot() 返回标准化英文列：
  code / name / price / chg / high / low / volume / amount / turnover / vol_ratio
"""
import logging
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

# ST 过滤正则（同 quality_scanner 保持一致）
_ST_PATTERN = r"(^|\*)?ST|退市|退|暂停"
_EXCLUDE_PREFIX = ("688",)
_EXCLUDE_MATCH  = r"^(82|83|87|43|40)"


def _base_filter(df: pd.DataFrame) -> pd.DataFrame:
    """去 ST、科创板、北交所、停牌"""
    df = df[~df["name"].str.contains(_ST_PATTERN, na=False, regex=True)]
    df = df[~df["code"].astype(str).str.startswith(_EXCLUDE_PREFIX)]
    df = df[~df["code"].astype(str).str.match(_EXCLUDE_MATCH)]
    df = df[df["volume"] > 0]
    return df


def scan_volume_surge(spot_df: pd.DataFrame, threshold: float = 3.0) -> pd.DataFrame:
    """量比 > threshold 且涨幅 > 2% 的放量拉升股"""
    if spot_df.empty:
        return pd.DataFrame()

    df = _base_filter(spot_df.copy())
    df["vol_ratio"] = pd.to_numeric(df["vol_ratio"], errors="coerce").fillna(0)
    df["chg"]       = pd.to_numeric(df["chg"],       errors="coerce").fillna(0)

    mask = (df["vol_ratio"] >= threshold) & (df["chg"] > 2.0)
    result = df[mask].sort_values("vol_ratio", ascending=False).head(20)
    return result[["code", "name", "price", "chg", "vol_ratio"]].rename(
        columns={"chg": "change_pct"}
    ).reset_index(drop=True)


def scan_zt_status(spot_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """涨停股 和 跌停股"""
    if spot_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    df = spot_df.copy()
    df["chg"] = pd.to_numeric(df["chg"], errors="coerce").fillna(0)

    zt_df = _base_filter(df[df["chg"] >= 9.8].copy())
    dt_df = df[df["chg"] <= -9.8].copy()
    # 跌停不过滤ST（知道哪些暴雷股跌停也有参考价值）
    dt_df = dt_df[~dt_df["code"].astype(str).str.startswith(_EXCLUDE_PREFIX)]

    def _fmt(d):
        return d[["code", "name", "price", "chg"]].rename(
            columns={"chg": "change_pct"}
        ).sort_values("change_pct", ascending=False).reset_index(drop=True)

    return _fmt(zt_df), _fmt(dt_df)


def scan_fund_surge(spot_df: pd.DataFrame) -> pd.DataFrame:
    """
    用实时行情合成超大单净流入估算：
      成交额（万元）× 涨跌幅% × 0.3 作为主力净流入估算
    取正值前15名（涨幅 > 2% 且 成交额 > 5000万）
    """
    if spot_df.empty:
        return pd.DataFrame()

    df = _base_filter(spot_df.copy())
    df["chg"]    = pd.to_numeric(df["chg"],    errors="coerce").fillna(0)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)

    # 成交额单位：万元，转为元
    df["super_flow"] = df["amount"] * 1e4 * df["chg"].clip(lower=0) / 100 * 0.3

    mask = (df["chg"] > 2.0) & (df["amount"] >= 5000)
    top  = df[mask].sort_values("super_flow", ascending=False).head(15)
    return top[["code", "name", "chg", "amount", "super_flow"]].rename(
        columns={"chg": "change_pct"}
    ).reset_index(drop=True)


def run_intraday_scan() -> dict:
    """执行一次完整的盘中异动扫描"""
    from data.reliable_api import API

    result = {
        "scan_time":  datetime.now().strftime("%H:%M:%S"),
        "vol_surge":  pd.DataFrame(),
        "zt_stocks":  pd.DataFrame(),
        "dt_stocks":  pd.DataFrame(),
        "fund_surge": pd.DataFrame(),
    }

    print("    获取全市场实时行情...")
    spot_df = API.spot()
    if spot_df.empty:
        logger.warning("intraday_scan: spot 数据为空")
        return result

    result["vol_surge"] = scan_volume_surge(spot_df)
    zt, dt = scan_zt_status(spot_df)
    result["zt_stocks"]  = zt
    result["dt_stocks"]  = dt

    print("    获取资金流异动数据...")
    result["fund_surge"] = scan_fund_surge(spot_df)

    return result
