"""
技术因子计算模块
计算 MA、EMA、RSI、MACD、Bollinger、动量等技术指标因子
"""
import pandas as pd
import numpy as np


def calc_ma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window).mean()


def calc_ema(close: pd.Series, window: int) -> pd.Series:
    return close.ewm(span=window, adjust=False).mean()


def calc_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def calc_macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = calc_ema(close, fast)
    ema_slow = calc_ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calc_bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0):
    mid = close.rolling(window).mean()
    std = close.rolling(window).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    # 百分比位置 [0,1]
    width = (upper - lower).replace(0, np.nan)
    pct_b = (close - lower) / width
    return mid, upper, lower, pct_b


def calc_momentum(close: pd.Series, window: int = 20) -> pd.Series:
    """简单动量：N日收益率"""
    return close.pct_change(window)


def calc_volatility(close: pd.Series, window: int = 20) -> pd.Series:
    """历史波动率（年化）"""
    daily_ret = close.pct_change()
    return daily_ret.rolling(window).std() * np.sqrt(252)


def calc_turnover_rate(volume: pd.Series, window: int = 20) -> pd.Series:
    """成交量的 N 日均值（相对流动性）"""
    return volume.rolling(window).mean()


def compute_technical_factors(df: pd.DataFrame) -> pd.DataFrame:
    """
    输入：含 open/high/low/close/volume 的 DataFrame
    输出：追加技术因子列后的 DataFrame
    """
    close = df["close"]
    volume = df["volume"]

    df["ma5"] = calc_ma(close, 5)
    df["ma10"] = calc_ma(close, 10)
    df["ma20"] = calc_ma(close, 20)
    df["ma60"] = calc_ma(close, 60)

    df["rsi14"] = calc_rsi(close, 14)
    df["rsi6"] = calc_rsi(close, 6)

    macd, signal, hist = calc_macd(close)
    df["macd"] = macd
    df["macd_signal"] = signal
    df["macd_hist"] = hist

    _, _, _, df["boll_pct_b"] = calc_bollinger(close)

    df["mom5"] = calc_momentum(close, 5)
    df["mom10"] = calc_momentum(close, 10)
    df["mom20"] = calc_momentum(close, 20)
    df["mom60"] = calc_momentum(close, 60)

    df["volatility20"] = calc_volatility(close, 20)
    df["vol_ma20"] = calc_turnover_rate(volume, 20)

    # 价格相对均线偏离
    df["price_ma20_ratio"] = close / df["ma20"] - 1
    df["price_ma60_ratio"] = close / df["ma60"] - 1

    # 量价背离（成交量变化率）
    df["vol_chg5"] = volume.pct_change(5)

    return df
