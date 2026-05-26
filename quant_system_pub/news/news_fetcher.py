"""
实时快讯拉取模块
数据源：财联社全球资讯 + 新浪财经快讯
"""
import logging
import time
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)


def fetch_cls_telegraph() -> pd.DataFrame:
    """财联社全球资讯（stock_info_global_cls）"""
    import akshare as ak
    try:
        df = ak.stock_info_global_cls(symbol="全部")
        df.columns = [c.strip() for c in df.columns]

        # 列名：标题 / 内容 / 发布日期 / 发布时间
        rename = {}
        for c in df.columns:
            if "标题" in c or "title" in c.lower():
                rename[c] = "title"
            elif "内容" in c or "content" in c.lower():
                rename[c] = "content"
            elif "时间" in c and "日期" not in c:
                rename[c] = "time_part"
            elif "日期" in c:
                rename[c] = "date_part"
        df = df.rename(columns=rename)

        # 合并日期+时间
        if "date_part" in df.columns and "time_part" in df.columns:
            df["time"] = pd.to_datetime(
                df["date_part"].astype(str) + " " + df["time_part"].astype(str),
                errors="coerce"
            )
        elif "time_part" in df.columns:
            df["time"] = pd.to_datetime(df["time_part"], errors="coerce")
        else:
            df["time"] = pd.Timestamp.now()

        if "content" not in df.columns:
            df["content"] = df.get("title", "")
        if "title" not in df.columns:
            df["title"] = df.get("content", "")

        df["source"] = "财联社"
        return df[["time", "title", "content", "source"]].head(50)
    except Exception as e:
        logger.warning(f"财联社快讯获取失败: {e}")
        return pd.DataFrame()


def fetch_sina_news() -> pd.DataFrame:
    """新浪财经快讯（stock_info_global_sina）"""
    import akshare as ak
    try:
        df = ak.stock_info_global_sina()
        df.columns = [c.strip() for c in df.columns]

        # 列名：时间 / 内容
        rename = {}
        for c in df.columns:
            if "时间" in c or "time" in c.lower():
                rename[c] = "time"
            elif "内容" in c or "content" in c.lower() or "标题" in c:
                rename[c] = "content"
        df = df.rename(columns=rename)

        df["time"] = pd.to_datetime(df.get("time", pd.Timestamp.now()), errors="coerce")
        df["title"] = df.get("content", "")
        df["source"] = "新浪财经"
        cols = [c for c in ["time", "title", "content", "source"] if c in df.columns]
        return df[cols].head(50)
    except Exception as e:
        logger.warning(f"新浪快讯获取失败: {e}")
        return pd.DataFrame()


def fetch_stock_news(symbol: str = "600745") -> pd.DataFrame:
    """获取个股专属新闻（stock_news_em），用于监控持仓股公告"""
    import akshare as ak
    try:
        df = ak.stock_news_em(symbol=symbol)
        df.columns = [c.strip() for c in df.columns]
        rename = {}
        for c in df.columns:
            if "时间" in c or "日期" in c:
                rename[c] = "time"
            elif "标题" in c or "新闻标题" in c:
                rename[c] = "title"
            elif "内容" in c or "新闻内容" in c:
                rename[c] = "content"
        df = df.rename(columns=rename)
        if "content" not in df.columns:
            df["content"] = df.get("title", "")
        df["time"] = pd.to_datetime(df.get("time", pd.Timestamp.now()), errors="coerce")
        df["source"] = f"个股公告({symbol})"
        cols = [c for c in ["time", "title", "content", "source"] if c in df.columns]
        return df[cols].head(20)
    except Exception as e:
        logger.debug(f"个股新闻 {symbol} 获取失败: {e}")
        return pd.DataFrame()


def fetch_latest_news(max_age_minutes: int = 60) -> pd.DataFrame:
    """
    合并所有快讯源，去重，按时间倒序排列
    max_age_minutes：只保留最近N分钟的新闻（0=不过滤）
    """
    frames = []
    for fetcher in [fetch_cls_telegraph, fetch_sina_news]:
        df = fetcher()
        if not df.empty:
            frames.append(df)
        time.sleep(0.3)

    if not frames:
        logger.error("所有快讯源均获取失败")
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)

    if "time" in combined.columns:
        combined["time"] = pd.to_datetime(combined["time"], errors="coerce")
        combined = combined.dropna(subset=["time"])
        combined = combined.sort_values("time", ascending=False)

        if max_age_minutes > 0:
            cutoff = pd.Timestamp.now() - pd.Timedelta(minutes=max_age_minutes)
            combined = combined[combined["time"] >= cutoff]

    if "title" in combined.columns:
        combined = combined.drop_duplicates(subset="title")

    combined = combined.reset_index(drop=True)
    logger.info(f"获取快讯 {len(combined)} 条")
    return combined
