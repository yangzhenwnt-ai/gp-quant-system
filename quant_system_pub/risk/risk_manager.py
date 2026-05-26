"""
风控模块
负责止损检查、最大回撤熔断、仓位上限控制
"""
import logging
import pandas as pd

logger = logging.getLogger(__name__)


class RiskManager:
    """
    风控管理器

    检查项：
    1. 单股止损：持仓亏损超过阈值时平仓
    2. 最大回撤熔断：组合整体回撤超过阈值时清仓
    3. 单股仓位上限
    """

    def __init__(self, config: dict):
        self.stop_loss = config.get("stop_loss", -0.08)
        self.max_drawdown_halt = config.get("max_drawdown_halt", -0.15)
        self.max_single_weight = config.get("max_single_weight", 0.05)
        self.halted = False  # 熔断标志

    def reset(self):
        self.halted = False

    def check_stop_loss(
        self,
        symbol: str,
        cost_price: float,
        current_price: float,
    ) -> bool:
        """返回 True 表示触发止损，应平仓"""
        if cost_price <= 0:
            return False
        ret = (current_price - cost_price) / cost_price
        if ret <= self.stop_loss:
            logger.warning(
                f"[止损] {symbol}: 成本={cost_price:.2f}, "
                f"现价={current_price:.2f}, 亏损={ret:.2%}"
            )
            return True
        return False

    def check_drawdown_halt(
        self,
        portfolio_value: float,
        peak_value: float,
    ) -> bool:
        """返回 True 表示触发最大回撤熔断"""
        if peak_value <= 0:
            return False
        drawdown = (portfolio_value - peak_value) / peak_value
        if drawdown <= self.max_drawdown_halt and not self.halted:
            logger.warning(
                f"[熔断] 组合回撤={drawdown:.2%}，超过阈值 {self.max_drawdown_halt:.2%}，停止开仓"
            )
            self.halted = True
        return self.halted

    def apply_position_limit(
        self,
        target_positions: dict[str, float],
        total_value: float,
    ) -> dict[str, float]:
        """
        将目标仓位按单股上限截断
        target_positions: {symbol: 目标金额}
        返回调整后的 {symbol: 调整后金额}
        """
        max_amount = total_value * self.max_single_weight
        adjusted = {}
        for sym, amount in target_positions.items():
            adjusted[sym] = min(amount, max_amount)
        return adjusted

    def get_stop_loss_list(
        self,
        holdings: dict[str, dict],
        price_map: dict[str, float],
    ) -> list[str]:
        """
        批量检查持仓止损
        holdings: {symbol: {'cost': 成本价, 'shares': 持股数}}
        price_map: {symbol: 当前价}
        返回需要止损的股票列表
        """
        to_stop = []
        for sym, info in holdings.items():
            cur = price_map.get(sym)
            if cur is None:
                continue
            if self.check_stop_loss(sym, info.get("cost", 0), cur):
                to_stop.append(sym)
        return to_stop
