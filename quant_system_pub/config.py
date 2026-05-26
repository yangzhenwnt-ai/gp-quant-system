"""
全局配置文件 — 所有值从 config_loader 读取，支持 config.local.yaml 覆盖
"""
from datetime import datetime, timedelta
from core.config_loader import cfg, cache_dir, reports_dir, project_data_dir

# 路径
BASE_DIR   = str(project_data_dir().parent)
CACHE_DIR  = str(cache_dir())
REPORT_DIR = str(reports_dir())

# 自动计算日期范围
_today = datetime.today()
_end   = _today.strftime("%Y-%m-%d")
_start = (_today - timedelta(days=365)).strftime("%Y-%m-%d")

BACKTEST_CONFIG = {
    "start_date":      _start,
    "end_date":        _end,
    "initial_capital": cfg("backtest.initial_capital", 1_000_000),
    "commission_rate": cfg("backtest.commission_rate", 0.0003),
    "stamp_tax":       cfg("backtest.stamp_tax",       0.001),
    "slippage":        cfg("backtest.slippage",        0.002),
}

STRATEGY_CONFIG = {
    "stock_pool":         "all_a",
    "hold_num":           cfg("strategy.hold_num",           20),
    "rebalance_freq":     "monthly",
    "max_single_weight":  cfg("strategy.max_single_weight",  0.05),
    "stop_loss":          cfg("strategy.stop_loss",          -0.08),
    "max_drawdown_halt":  cfg("strategy.max_drawdown_halt",  -0.15),
}

FACTOR_WEIGHTS = {
    "value":    cfg("factor_weights.value",    0.30),
    "growth":   cfg("factor_weights.growth",   0.30),
    "momentum": cfg("factor_weights.momentum", 0.20),
    "quality":  cfg("factor_weights.quality",  0.20),
}

ML_CONFIG = {
    "predict_days":          cfg("ml.predict_days",          20),
    "train_years":           cfg("ml.train_years",           2),
    "n_estimators":          cfg("ml.n_estimators",          100),
    "feature_importance_top":cfg("ml.feature_importance_top",15),
}
