"""
基本面因子计算模块
计算价值、成长、质量因子，并对截面数据做百分位标准化
"""
import pandas as pd
import numpy as np


def percentile_rank(series: pd.Series) -> pd.Series:
    """将序列转换为 [0, 1] 的百分位排名（越高越好）"""
    return series.rank(pct=True, na_option="keep")


def inverse_rank(series: pd.Series) -> pd.Series:
    """反向百分位排名（数值越低越好，如PE越低越好）"""
    return 1 - series.rank(pct=True, na_option="keep")


def compute_factor_scores(snapshot: pd.DataFrame) -> pd.DataFrame:
    """
    在截面数据上计算多因子综合评分

    参数 snapshot: 每行一只股票，需含以下列（可选）：
        pe_ttm, pb, roe, net_profit_growth,
        mom20, mom60, volatility20,
        debt_ratio, gross_margin

    返回: 原 DataFrame 追加 *_score 和 total_score 列
    """
    df = snapshot.copy()

    # ─── 价值因子（低 PE / 低 PB 更好）───────────────────
    if "pe_ttm" in df.columns:
        df["value_pe_score"] = inverse_rank(df["pe_ttm"])
    else:
        df["value_pe_score"] = 0.5

    if "pb" in df.columns:
        df["value_pb_score"] = inverse_rank(df["pb"])
    else:
        df["value_pb_score"] = 0.5

    df["value_score"] = (df["value_pe_score"] + df["value_pb_score"]) / 2

    # ─── 成长因子（高 ROE / 高利润增速更好）─────────────
    if "roe" in df.columns:
        df["growth_roe_score"] = percentile_rank(df["roe"])
    else:
        df["growth_roe_score"] = 0.5

    if "net_profit_growth" in df.columns:
        df["growth_np_score"] = percentile_rank(df["net_profit_growth"])
    else:
        df["growth_np_score"] = 0.5

    df["growth_score"] = (df["growth_roe_score"] + df["growth_np_score"]) / 2

    # ─── 动量因子（高动量更好，但避免超买）─────────────
    if "mom20" in df.columns:
        df["momentum_20_score"] = percentile_rank(df["mom20"])
    else:
        df["momentum_20_score"] = 0.5

    if "mom60" in df.columns:
        df["momentum_60_score"] = percentile_rank(df["mom60"])
    else:
        df["momentum_60_score"] = 0.5

    df["momentum_score"] = (df["momentum_20_score"] + df["momentum_60_score"]) / 2

    # ─── 质量因子（低负债、高毛利率更好）────────────────
    if "debt_ratio" in df.columns:
        df["quality_debt_score"] = inverse_rank(df["debt_ratio"])
    else:
        df["quality_debt_score"] = 0.5

    if "gross_margin" in df.columns:
        df["quality_gm_score"] = percentile_rank(df["gross_margin"])
    else:
        df["quality_gm_score"] = 0.5

    df["quality_score"] = (df["quality_debt_score"] + df["quality_gm_score"]) / 2

    return df


def compute_total_score(
    df: pd.DataFrame,
    weights: dict,
    ml_score_col: str = None,
    ml_weight: float = 0.0,
) -> pd.DataFrame:
    """
    汇总各因子得分，计算综合评分 total_score

    weights: {'value': 0.3, 'growth': 0.3, 'momentum': 0.2, 'quality': 0.2}
    ml_score_col: 机器学习预测概率列名（可选）
    ml_weight: 机器学习分数权重（0~1），其余权重等比缩放
    """
    df = df.copy()

    base_score = (
        df.get("value_score", 0.5) * weights.get("value", 0)
        + df.get("growth_score", 0.5) * weights.get("growth", 0)
        + df.get("momentum_score", 0.5) * weights.get("momentum", 0)
        + df.get("quality_score", 0.5) * weights.get("quality", 0)
    )

    if ml_score_col and ml_score_col in df.columns and ml_weight > 0:
        scale = 1 - ml_weight
        df["total_score"] = base_score * scale + df[ml_score_col] * ml_weight
    else:
        df["total_score"] = base_score

    return df


def winsorize(series: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    """缩尾处理：将极端值截断到分位数边界"""
    lo = series.quantile(lower)
    hi = series.quantile(upper)
    return series.clip(lo, hi)
