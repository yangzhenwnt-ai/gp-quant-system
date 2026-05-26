"""
股票选择器：多因子打分 + ML 增强
在每个调仓日生成目标持仓列表
"""
import logging
import pandas as pd
import numpy as np
from factors.technical import compute_technical_factors
from factors.fundamental import compute_factor_scores, compute_total_score, winsorize

logger = logging.getLogger(__name__)


class StockSelector:
    """
    多因子选股器

    流程：
    1. 对每只股票计算截面技术因子快照（当日最新值）
    2. 拼接基本面数据快照
    3. 因子标准化 -> 加权评分
    4. 叠加 ML 预测信号
    5. 按总分排序，取前 N 只
    """

    def __init__(self, config: dict, factor_weights: dict, ml_model=None):
        self.hold_num = config.get("hold_num", 20)
        self.factor_weights = factor_weights
        self.ml_model = ml_model
        self.ml_weight = 0.2 if ml_model and ml_model.is_trained else 0.0

    def _build_snapshot(
        self,
        stock_data: dict[str, pd.DataFrame],
        rebalance_date: str,
        fundamental_data: dict = None,
    ) -> pd.DataFrame:
        """
        构建截面快照：每行一只股票，列为各因子值
        """
        rows = []
        for sym, df in stock_data.items():
            if df.empty:
                continue
            # 取截止调仓日的数据
            sub = df[df["date"] <= rebalance_date].copy()
            if len(sub) < 60:
                continue

            sub = compute_technical_factors(sub)
            latest = sub.iloc[-1]

            row = {"symbol": sym}
            tech_cols = [
                "rsi14", "rsi6", "macd", "macd_hist", "boll_pct_b",
                "mom5", "mom10", "mom20", "mom60",
                "volatility20", "price_ma20_ratio", "price_ma60_ratio",
                "vol_chg5", "close", "volume",
            ]
            for col in tech_cols:
                row[col] = latest.get(col, np.nan)

            # 附加基本面数据
            if fundamental_data and sym in fundamental_data:
                fd = fundamental_data[sym]
                row.update(fd)

            # ML 信号
            if self.ml_model and self.ml_model.is_trained:
                row["ml_score"] = self.ml_model.get_latest_signal(sub)

            rows.append(row)

        return pd.DataFrame(rows)

    def _filter_stocks(self, snapshot: pd.DataFrame) -> pd.DataFrame:
        """初步过滤：去除极端波动 / 缺失数据过多的股票"""
        # 去除波动率异常高的（可能停牌复牌）
        if "volatility20" in snapshot.columns:
            snapshot = snapshot[snapshot["volatility20"] < 1.5]
        # 去除近期动量极端异常（可能连板）
        if "mom20" in snapshot.columns:
            snapshot = snapshot[snapshot["mom20"].between(-0.5, 1.0)]
        return snapshot

    def select(
        self,
        stock_data: dict[str, pd.DataFrame],
        rebalance_date: str,
        fundamental_data: dict = None,
    ) -> list[str]:
        """
        执行选股，返回目标持仓股票代码列表
        """
        snapshot = self._build_snapshot(stock_data, rebalance_date, fundamental_data)
        if snapshot.empty:
            logger.warning(f"{rebalance_date}: 截面快照为空，无法选股")
            return []

        snapshot = self._filter_stocks(snapshot)

        # 对连续因子做缩尾处理
        for col in ["mom20", "mom60", "volatility20"]:
            if col in snapshot.columns:
                snapshot[col] = winsorize(snapshot[col])

        snapshot = compute_factor_scores(snapshot)

        ml_col = "ml_score" if self.ml_weight > 0 and "ml_score" in snapshot.columns else None
        snapshot = compute_total_score(
            snapshot,
            weights=self.factor_weights,
            ml_score_col=ml_col,
            ml_weight=self.ml_weight,
        )

        snapshot = snapshot.sort_values("total_score", ascending=False)
        selected = snapshot.head(self.hold_num)["symbol"].tolist()

        logger.info(
            f"{rebalance_date}: 选出 {len(selected)} 只股票，"
            f"平均得分={snapshot.head(self.hold_num)['total_score'].mean():.3f}"
        )
        return selected

    def get_score_table(
        self,
        stock_data: dict[str, pd.DataFrame],
        rebalance_date: str,
        fundamental_data: dict = None,
        top_n: int = 50,
    ) -> pd.DataFrame:
        """返回因子评分明细表（供分析用）"""
        snapshot = self._build_snapshot(stock_data, rebalance_date, fundamental_data)
        if snapshot.empty:
            return pd.DataFrame()
        snapshot = self._filter_stocks(snapshot)
        snapshot = compute_factor_scores(snapshot)
        ml_col = "ml_score" if self.ml_weight > 0 and "ml_score" in snapshot.columns else None
        snapshot = compute_total_score(snapshot, self.factor_weights, ml_col, self.ml_weight)
        return snapshot.sort_values("total_score", ascending=False).head(top_n)
