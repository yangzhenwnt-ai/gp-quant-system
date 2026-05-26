"""
数据获取模块 - 基于 AKShare
负责 A 股行情、指数成分股、基本面数据的获取与本地缓存
"""
import os
import json
import time
import logging
import pickle
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class DataLoader:
    """A 股数据加载器，带本地磁盘缓存"""

    def __init__(self, cache_dir: str):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._ak = None

    def _get_ak(self):
        if self._ak is None:
            import akshare as ak
            self._ak = ak
        return self._ak

    # ──────────────────────────────────────────────
    # 缓存工具
    # ──────────────────────────────────────────────
    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.pkl"

    def _load_cache(self, key: str, max_age_hours: int = 24):
        path = self._cache_path(key)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > max_age_hours * 3600:
            return None
        with open(path, "rb") as f:
            return pickle.load(f)

    def _save_cache(self, key: str, data):
        with open(self._cache_path(key), "wb") as f:
            pickle.dump(data, f)

    # ──────────────────────────────────────────────
    # 全 A 股股票池（过滤不合规标的）
    # ──────────────────────────────────────────────
    def get_all_a_stocks(self) -> list[str]:
        """
        获取全 A 股可交易标的，过滤规则：
          - 排除科创板（688 开头）
          - 排除北交所（8 开头，如 830/835/838/839/83 等）
          - 排除 ST / *ST / 退市整理（名称含 ST 或 退）
          - 排除上市不足 6 个月（新股流动性差、数据不足）
        缓存 24 小时（成分每天变化不大）
        """
        cache_key = "all_a_stocks_filtered"
        cached = self._load_cache(cache_key, max_age_hours=24)
        if cached is not None:
            logger.info(f"从缓存加载全A股股票池：{len(cached)} 只")
            return cached

        ak = self._get_ak()
        try:
            df = ak.stock_info_a_code_name()          # 返回 code / name 两列
        except Exception as e:
            logger.error(f"获取全A股列表失败: {e}")
            return []

        df.columns = [c.strip() for c in df.columns]
        # 统一列名
        if "code" not in df.columns:
            df = df.rename(columns={df.columns[0]: "code", df.columns[1]: "name"})

        total_raw = len(df)

        # 1. 排除科创板（688xxx）
        mask_kcb = df["code"].str.startswith("688")
        # 2. 排除北交所（82xxxx / 83xxxx / 87xxxx / 43xxxx / 40xxxx）
        mask_bse = df["code"].str.match(r"^(82|83|87|43|40)")
        # 3. 排除 ST / *ST / 退市整理
        mask_st  = df["name"].str.contains(r"ST|退", na=False, regex=True)
        # 4. 合并过滤
        mask_exclude = mask_kcb | mask_bse | mask_st
        df_clean = df[~mask_exclude].copy()

        # 5. 排除上市不足 6 个月（通过尝试获取上市日期；若接口失败则跳过此步）
        try:
            info = ak.stock_info_sh_name_code(symbol="主板A股")   # 上交所主板
            info2 = ak.stock_info_sh_name_code(symbol="科创板")
            ipo_map = {}
            for _df in [info, info2]:
                if "LISTING_DATE" in _df.columns:
                    _df["code"] = _df["SECURITY_CODE_A"].astype(str).str.zfill(6)
                    for _, row in _df.iterrows():
                        ipo_map[row["code"]] = str(row["LISTING_DATE"])
            if ipo_map:
                six_months_ago = (
                    pd.Timestamp.today() - pd.DateOffset(months=6)
                ).strftime("%Y-%m-%d")
                too_new = {
                    c for c, d in ipo_map.items()
                    if str(d) >= six_months_ago.replace("-", "")
                }
                df_clean = df_clean[~df_clean["code"].isin(too_new)]
        except Exception:
            pass  # 上市日期过滤失败不影响主流程

        symbols = df_clean["code"].tolist()
        self._save_cache(cache_key, symbols)

        filtered = total_raw - len(symbols)
        logger.info(
            f"全A股股票池: 原始 {total_raw} 只 → "
            f"过滤 {filtered} 只（科创板/北交所/ST/退市）→ "
            f"剩余 {len(symbols)} 只"
        )
        return symbols

    # ──────────────────────────────────────────────
    # 指数成分股（保留，用于基准对比等场景）
    # ──────────────────────────────────────────────
    def get_index_stocks(self, index: str = "000300") -> list[str]:
        """
        获取指数成分股代码列表
        index: '000300'=沪深300, '000905'=中证500, '000852'=中证1000
        """
        cache_key = f"index_stocks_{index}"
        cached = self._load_cache(cache_key, max_age_hours=24)
        if cached is not None:
            return cached

        ak = self._get_ak()
        index_map = {
            "000300": "沪深300",
            "000905": "中证500",
            "000852": "中证1000",
        }
        try:
            df = ak.index_stock_cons_weight_csindex(symbol=index)
            stocks = df["成分券代码"].tolist()
        except Exception:
            df = ak.index_stock_cons(symbol=index)
            stocks = df["品种代码"].tolist() if "品种代码" in df.columns else df.iloc[:, 0].tolist()

        self._save_cache(cache_key, stocks)
        logger.info(f"获取 {index_map.get(index, index)} 成分股 {len(stocks)} 只")
        return stocks

    # ──────────────────────────────────────────────
    # 个股日线行情
    # ──────────────────────────────────────────────
    def get_stock_daily(
        self, symbol: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> pd.DataFrame:
        """
        获取个股前复权日线数据
        返回列：date, open, high, low, close, volume, amount
        """
        cache_key = f"daily_{symbol}_{start_date}_{end_date}_{adjust}"
        cached = self._load_cache(cache_key, max_age_hours=12)
        if cached is not None:
            return cached

        # 委托给 reliable_api（带自动 failover + 缓存兜底）
        from data.reliable_api import API
        return API.history(symbol, start_date, end_date, adjust)

    # ──────────────────────────────────────────────
    # 批量获取行情（带进度和容错）
    # ──────────────────────────────────────────────
    def get_batch_daily(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
        delay: float = 0.2,
    ) -> dict[str, pd.DataFrame]:
        """批量获取多只股票行情，返回 {symbol: df}"""
        result = {}
        total = len(symbols)
        for i, sym in enumerate(symbols):
            df = self.get_stock_daily(sym, start_date, end_date)
            if not df.empty:
                result[sym] = df
            if (i + 1) % 20 == 0:
                logger.info(f"  已获取 {i+1}/{total} 只股票行情")
            time.sleep(delay)
        logger.info(f"批量行情获取完成，成功 {len(result)}/{total} 只")
        return result

    # ──────────────────────────────────────────────
    # 个股基本面（估值 + 盈利）
    # ──────────────────────────────────────────────
    def get_stock_valuation(self, symbol: str) -> pd.DataFrame:
        """获取个股 PE/PB 等估值指标（日频）"""
        cache_key = f"valuation_{symbol}"
        cached = self._load_cache(cache_key, max_age_hours=24)
        if cached is not None:
            return cached

        ak = self._get_ak()
        try:
            df = ak.stock_a_indicator_lg(symbol=symbol)
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            self._save_cache(cache_key, df)
            return df
        except Exception as e:
            logger.warning(f"获取 {symbol} 估值数据失败: {e}")
            return pd.DataFrame()

    # ──────────────────────────────────────────────
    # 财务指标（ROE / 净利润增速等）
    # ──────────────────────────────────────────────
    def get_financial_indicator(self, symbol: str) -> pd.DataFrame:
        """获取关键财务指标（季频）"""
        cache_key = f"financial_{symbol}"
        cached = self._load_cache(cache_key, max_age_hours=48)
        if cached is not None:
            return cached

        ak = self._get_ak()
        try:
            df = ak.stock_financial_analysis_indicator(symbol=symbol, start_year="2020")
            self._save_cache(cache_key, df)
            return df
        except Exception as e:
            logger.warning(f"获取 {symbol} 财务数据失败: {e}")
            return pd.DataFrame()

    # ──────────────────────────────────────────────
    # 指数行情（用于基准对比）
    # ──────────────────────────────────────────────
    def get_index_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取指数日线数据（沪深300等）"""
        cache_key = f"index_daily_{symbol}_{start_date}_{end_date}"
        cached = self._load_cache(cache_key, max_age_hours=12)
        if cached is not None:
            return cached

        ak = self._get_ak()
        try:
            df = ak.stock_zh_index_daily(symbol=f"sh{symbol}" if symbol.startswith("0") else f"sz{symbol}")
            df.columns = [c.strip() for c in df.columns]
            df["date"] = pd.to_datetime(df["date"])
            df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
            df = df.sort_values("date").reset_index(drop=True)
            self._save_cache(cache_key, df)
            return df
        except Exception as e:
            logger.warning(f"获取指数 {symbol} 行情失败: {e}")
            return pd.DataFrame()

    # ──────────────────────────────────────────────
    # 交易日历
    # ──────────────────────────────────────────────
    def get_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        """获取 A 股交易日历"""
        cache_key = f"trade_dates_{start_date}_{end_date}"
        cached = self._load_cache(cache_key, max_age_hours=72)
        if cached is not None:
            return cached

        ak = self._get_ak()
        try:
            df = ak.tool_trade_date_hist_sina()
            dates = pd.to_datetime(df["trade_date"])
            dates = dates[(dates >= start_date) & (dates <= end_date)]
            result = sorted(dates.dt.strftime("%Y-%m-%d").tolist())
            self._save_cache(cache_key, result)
            return result
        except Exception as e:
            logger.warning(f"获取交易日历失败: {e}")
            # 降级：生成工作日序列
            idx = pd.bdate_range(start_date, end_date)
            return idx.strftime("%Y-%m-%d").tolist()
