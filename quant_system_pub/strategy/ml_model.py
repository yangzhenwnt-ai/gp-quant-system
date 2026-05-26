"""
机器学习增强模块
使用 RandomForest 预测未来 N 日涨跌，
输出预测概率作为辅助选股信号
"""
import logging
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score, roc_auc_score

logger = logging.getLogger(__name__)

# 用于训练的技术特征列
FEATURE_COLS = [
    "rsi14", "rsi6",
    "macd", "macd_hist",
    "boll_pct_b",
    "mom5", "mom10", "mom20", "mom60",
    "volatility20",
    "price_ma20_ratio", "price_ma60_ratio",
    "vol_chg5",
]


def build_label(close: pd.Series, n_days: int = 20) -> pd.Series:
    """
    构建未来 N 日收益率标签
    正收益 -> 1，负收益 -> 0
    """
    future_ret = close.shift(-n_days) / close - 1
    return (future_ret > 0).astype(int)


def prepare_ml_dataset(
    stock_data: dict[str, pd.DataFrame],
    predict_days: int = 20,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    合并多只股票的特征和标签，构建训练集

    返回: (X, y) 可直接传入 sklearn
    """
    frames = []
    for sym, df in stock_data.items():
        if df.empty or len(df) < 80:
            continue
        tmp = df[FEATURE_COLS].copy()
        tmp["label"] = build_label(df["close"], predict_days)
        tmp["symbol"] = sym
        frames.append(tmp)

    if not frames:
        return pd.DataFrame(), pd.Series(dtype=int)

    all_data = pd.concat(frames, ignore_index=True).dropna()
    X = all_data[FEATURE_COLS]
    y = all_data["label"]
    return X, y


class MLSignalModel:
    """随机森林选股信号模型"""

    def __init__(self, n_estimators: int = 100, predict_days: int = 20):
        self.predict_days = predict_days
        self.model = RandomForestClassifier(
            n_estimators=n_estimators,
            max_depth=6,
            min_samples_leaf=50,
            n_jobs=-1,
            random_state=42,
        )
        self.scaler = StandardScaler()
        self.is_trained = False
        self.feature_importances_ = None

    def train(self, X: pd.DataFrame, y: pd.Series) -> dict:
        """训练模型，返回验证集指标"""
        if X.empty or len(X) < 200:
            logger.warning("训练数据不足，跳过机器学习模型训练")
            return {}

        X_scaled = self.scaler.fit_transform(X)

        # 时间序列交叉验证
        tscv = TimeSeriesSplit(n_splits=3)
        acc_list, auc_list = [], []
        for train_idx, val_idx in tscv.split(X_scaled):
            self.model.fit(X_scaled[train_idx], y.iloc[train_idx])
            y_pred = self.model.predict(X_scaled[val_idx])
            y_prob = self.model.predict_proba(X_scaled[val_idx])[:, 1]
            acc_list.append(accuracy_score(y.iloc[val_idx], y_pred))
            auc_list.append(roc_auc_score(y.iloc[val_idx], y_prob))

        # 全量训练
        self.model.fit(X_scaled, y)
        self.is_trained = True
        self.feature_importances_ = pd.Series(
            self.model.feature_importances_, index=X.columns
        ).sort_values(ascending=False)

        metrics = {
            "cv_accuracy": np.mean(acc_list),
            "cv_auc": np.mean(auc_list),
            "train_samples": len(X),
        }
        logger.info(
            f"ML模型训练完成: CV准确率={metrics['cv_accuracy']:.3f}, "
            f"AUC={metrics['cv_auc']:.3f}, 样本={metrics['train_samples']}"
        )
        return metrics

    def predict_proba(self, df: pd.DataFrame) -> pd.Series:
        """
        对单只股票最新一行做预测，返回上涨概率
        df: 单只股票带技术因子的 DataFrame
        """
        if not self.is_trained:
            return pd.Series([0.5] * len(df), index=df.index)

        available = [c for c in FEATURE_COLS if c in df.columns]
        X = df[available].dropna()
        if X.empty:
            return pd.Series([0.5] * len(df), index=df.index)

        X_scaled = self.scaler.transform(X)
        proba = self.model.predict_proba(X_scaled)[:, 1]
        result = pd.Series(0.5, index=df.index)
        result.loc[X.index] = proba
        return result

    def get_latest_signal(self, df: pd.DataFrame) -> float:
        """获取最新一日的上涨概率预测"""
        proba = self.predict_proba(df)
        return float(proba.iloc[-1]) if not proba.empty else 0.5
