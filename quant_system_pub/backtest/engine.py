"""
回测引擎（事件驱动）
模拟月度调仓 + 每日止损检查，计算组合净值曲线
"""
import logging
from collections import defaultdict

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class Portfolio:
    """持仓与资金管理"""

    def __init__(self, initial_capital: float, commission: float, stamp_tax: float, slippage: float):
        self.cash = initial_capital
        self.initial = initial_capital
        self.commission = commission
        self.stamp_tax = stamp_tax
        self.slippage = slippage

        # {symbol: {'shares': int, 'cost': float}}
        self.holdings: dict[str, dict] = {}
        self.nav_history: list[dict] = []   # 净值历史
        self.trade_log: list[dict] = []     # 交易记录

    # ─── 交易接口 ────────────────────────────────────────
    def buy(self, symbol: str, price: float, amount: float, date: str):
        """按金额买入"""
        exec_price = price * (1 + self.slippage)
        fee = amount * self.commission
        actual_amount = min(amount, self.cash - fee)
        if actual_amount <= 0:
            return
        shares = int(actual_amount / exec_price / 100) * 100  # 整手
        if shares <= 0:
            return
        actual_cost = shares * exec_price + shares * exec_price * self.commission
        if actual_cost > self.cash:
            return

        self.cash -= actual_cost
        if symbol in self.holdings:
            old = self.holdings[symbol]
            total_shares = old["shares"] + shares
            total_cost = old["cost"] * old["shares"] + exec_price * shares
            self.holdings[symbol] = {
                "shares": total_shares,
                "cost": total_cost / total_shares,
            }
        else:
            self.holdings[symbol] = {"shares": shares, "cost": exec_price}

        self.trade_log.append({
            "date": date, "symbol": symbol, "action": "BUY",
            "price": exec_price, "shares": shares,
            "amount": actual_cost,
        })

    def sell(self, symbol: str, price: float, date: str, reason: str = "rebalance"):
        """全仓卖出"""
        if symbol not in self.holdings:
            return
        shares = self.holdings[symbol]["shares"]
        exec_price = price * (1 - self.slippage)
        proceeds = shares * exec_price
        fee = proceeds * (self.commission + self.stamp_tax)
        self.cash += proceeds - fee
        del self.holdings[symbol]

        self.trade_log.append({
            "date": date, "symbol": symbol, "action": "SELL",
            "price": exec_price, "shares": shares,
            "amount": proceeds, "reason": reason,
        })

    # ─── 估值 ─────────────────────────────────────────────
    def get_value(self, price_map: dict[str, float]) -> float:
        stock_value = sum(
            info["shares"] * price_map.get(sym, info["cost"])
            for sym, info in self.holdings.items()
        )
        return self.cash + stock_value

    def record_nav(self, date: str, price_map: dict[str, float]):
        value = self.get_value(price_map)
        self.nav_history.append({
            "date": date,
            "value": value,
            "nav": value / self.initial,
            "cash": self.cash,
        })

    def get_price_map(self, stock_data: dict, date: str) -> dict[str, float]:
        pm = {}
        for sym, df in stock_data.items():
            sub = df[df["date"] <= date]
            if not sub.empty:
                pm[sym] = float(sub.iloc[-1]["close"])
        return pm


class BacktestEngine:
    """
    回测引擎

    用法：
        engine = BacktestEngine(config, selector, risk_manager, stock_data)
        result = engine.run()
    """

    def __init__(
        self,
        config: dict,
        selector,
        risk_manager,
        stock_data: dict[str, pd.DataFrame],
        benchmark_data: pd.DataFrame = None,
        fundamental_data: dict = None,
    ):
        self.config = config
        self.selector = selector
        self.risk_manager = risk_manager
        self.stock_data = stock_data
        self.benchmark = benchmark_data
        self.fundamental_data = fundamental_data or {}

        self.portfolio = Portfolio(
            initial_capital=config["initial_capital"],
            commission=config["commission_rate"],
            stamp_tax=config["stamp_tax"],
            slippage=config["slippage"],
        )

    def _get_rebalance_dates(self, trade_dates: list[str]) -> list[str]:
        """每月第一个交易日作为调仓日"""
        df = pd.DataFrame({"date": pd.to_datetime(trade_dates)})
        df["ym"] = df["date"].dt.to_period("M")
        first_days = df.groupby("ym")["date"].first()
        return first_days.dt.strftime("%Y-%m-%d").tolist()

    def run(self) -> dict:
        """执行回测，返回绩效结果"""
        start = self.config["start_date"]
        end = self.config["end_date"]

        # 汇总所有交易日
        all_dates = sorted(set(
            d.strftime("%Y-%m-%d")
            for sym, df in self.stock_data.items()
            for d in df["date"]
            if start <= d.strftime("%Y-%m-%d") <= end
        ))

        if not all_dates:
            logger.error("回测区间内无交易日数据")
            return {}

        rebalance_dates = set(self._get_rebalance_dates(all_dates))
        target_holdings: list[str] = []
        peak_value = self.portfolio.initial
        self.risk_manager.reset()

        logger.info(f"回测开始: {start} ~ {end}，共 {len(all_dates)} 个交易日")

        for date in all_dates:
            price_map = self.portfolio.get_price_map(self.stock_data, date)
            if not price_map:
                continue

            current_value = self.portfolio.get_value(price_map)
            peak_value = max(peak_value, current_value)

            # 每日止损检查
            stop_list = self.risk_manager.get_stop_loss_list(
                self.portfolio.holdings, price_map
            )
            for sym in stop_list:
                if sym in price_map:
                    self.portfolio.sell(sym, price_map[sym], date, reason="stop_loss")

            # 调仓日：重新选股并换仓
            halted = self.risk_manager.check_drawdown_halt(current_value, peak_value)
            if date in rebalance_dates and not halted:
                target_holdings = self.selector.select(
                    self.stock_data, date, self.fundamental_data
                )

                # 卖出不在目标列表的股票
                to_sell = [s for s in list(self.portfolio.holdings.keys()) if s not in target_holdings]
                for sym in to_sell:
                    if sym in price_map:
                        self.portfolio.sell(sym, price_map[sym], date)

                # 等权重买入目标股票
                if target_holdings:
                    per_stock = self.portfolio.cash / len(target_holdings)
                    target_positions = {sym: per_stock for sym in target_holdings}
                    target_positions = self.risk_manager.apply_position_limit(
                        target_positions, current_value
                    )
                    for sym in target_holdings:
                        if sym not in self.portfolio.holdings and sym in price_map:
                            self.portfolio.buy(sym, price_map[sym], target_positions[sym], date)

            self.portfolio.record_nav(date, price_map)

        logger.info("回测完成，开始计算绩效指标")
        return self._calc_performance()

    def _calc_performance(self) -> dict:
        """计算年化收益、最大回撤、夏普比率等"""
        if not self.portfolio.nav_history:
            return {}

        nav_df = pd.DataFrame(self.portfolio.nav_history)
        nav_df["date"] = pd.to_datetime(nav_df["date"])
        nav_df = nav_df.set_index("date").sort_index()

        nav = nav_df["nav"]
        daily_ret = nav.pct_change().dropna()

        total_ret = nav.iloc[-1] - 1
        years = (nav.index[-1] - nav.index[0]).days / 365
        annual_ret = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0

        rolling_max = nav.cummax()
        drawdown = (nav - rolling_max) / rolling_max
        max_dd = drawdown.min()

        sharpe = (daily_ret.mean() * 252) / (daily_ret.std() * np.sqrt(252)) if daily_ret.std() > 0 else 0

        # 基准对比
        benchmark_annual = None
        benchmark_total = None
        if self.benchmark is not None and not self.benchmark.empty:
            bm = self.benchmark.set_index("date")["close"] if "date" in self.benchmark.columns else self.benchmark["close"]
            bm_ret = bm.pct_change().dropna()
            bm_total = bm.iloc[-1] / bm.iloc[0] - 1
            bm_years = (bm.index[-1] - bm.index[0]).days / 365
            benchmark_total = bm_total
            benchmark_annual = (1 + bm_total) ** (1 / bm_years) - 1 if bm_years > 0 else 0

        result = {
            "nav_df": nav_df,
            "trade_log": pd.DataFrame(self.portfolio.trade_log),
            "total_return": total_ret,
            "annual_return": annual_ret,
            "max_drawdown": max_dd,
            "sharpe_ratio": sharpe,
            "benchmark_total": benchmark_total,
            "benchmark_annual": benchmark_annual,
            "years": years,
            "final_value": self.portfolio.initial * (1 + total_ret),
        }
        return result
