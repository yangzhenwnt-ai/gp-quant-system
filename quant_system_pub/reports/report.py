"""
可视化报告模块
生成净值曲线、回撤分析、因子贡献等图表
"""
import os
import logging
from pathlib import Path

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")  # 无界面模式
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

logger = logging.getLogger(__name__)

# 中文字体设置（Windows）
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False


def plot_performance(result: dict, save_dir: str = "reports"):
    """
    绘制回测绩效全景图（4 子图）
    - 净值曲线 vs 基准
    - 回撤曲线
    - 月度收益热力图
    - 年度收益柱状图
    """
    Path(save_dir).mkdir(parents=True, exist_ok=True)

    nav_df = result.get("nav_df")
    if nav_df is None or nav_df.empty:
        logger.warning("净值数据为空，跳过绘图")
        return

    nav = nav_df["nav"]
    drawdown = (nav - nav.cummax()) / nav.cummax()

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle("A股多因子量化策略 - 回测绩效报告", fontsize=16, fontweight="bold")

    # ─── 子图1：净值曲线 ─────────────────────────────────
    ax1 = axes[0, 0]
    ax1.plot(nav.index, nav.values, label="策略净值", color="#E74C3C", linewidth=1.5)
    ax1.axhline(1.0, color="gray", linestyle="--", linewidth=0.8)

    bm_annual = result.get("benchmark_annual")
    if bm_annual is not None:
        bm_nav = (1 + bm_annual) ** (pd.Series(
            (nav.index - nav.index[0]).days / 365, index=nav.index
        ))
        ax1.plot(nav.index, bm_nav.values, label="沪深300基准", color="#3498DB",
                 linewidth=1.2, linestyle="--", alpha=0.8)

    ax1.set_title("净值曲线")
    ax1.set_ylabel("净值")
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.2f}"))

    # ─── 子图2：回撤曲线 ─────────────────────────────────
    ax2 = axes[0, 1]
    ax2.fill_between(drawdown.index, drawdown.values, 0,
                     color="#E74C3C", alpha=0.4, label="回撤")
    ax2.plot(drawdown.index, drawdown.values, color="#C0392B", linewidth=0.8)
    ax2.axhline(result.get("max_drawdown", 0), color="darkred",
                linestyle=":", linewidth=1, label=f"最大回撤 {result.get('max_drawdown', 0):.2%}")
    ax2.set_title("回撤曲线")
    ax2.set_ylabel("回撤幅度")
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.1%}"))

    # ─── 子图3：月度收益热力图 ─────────────────────────────
    ax3 = axes[1, 0]
    monthly_ret = nav.resample("ME").last().pct_change().dropna()
    monthly_df = pd.DataFrame({
        "year": monthly_ret.index.year,
        "month": monthly_ret.index.month,
        "ret": monthly_ret.values,
    })
    if not monthly_df.empty:
        pivot = monthly_df.pivot(index="year", columns="month", values="ret")
        vmax = max(abs(pivot.values.flatten()[~np.isnan(pivot.values.flatten())].max()), 0.05)
        im = ax3.imshow(pivot.values, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto")
        ax3.set_xticks(range(12))
        ax3.set_xticklabels(["1月", "2月", "3月", "4月", "5月", "6月",
                              "7月", "8月", "9月", "10月", "11月", "12月"])
        ax3.set_yticks(range(len(pivot.index)))
        ax3.set_yticklabels(pivot.index.tolist())
        fig.colorbar(im, ax=ax3, format=mticker.FuncFormatter(lambda x, _: f"{x:.1%}"))
        # 在格子内写数字
        for r in range(pivot.shape[0]):
            for c in range(pivot.shape[1]):
                val = pivot.values[r, c]
                if not np.isnan(val):
                    ax3.text(c, r, f"{val:.1%}", ha="center", va="center",
                             fontsize=6, color="black")
    ax3.set_title("月度收益热力图")

    # ─── 子图4：年度收益柱状图 ────────────────────────────
    ax4 = axes[1, 1]
    annual_ret = nav.resample("YE").last().pct_change().dropna()
    if not annual_ret.empty:
        colors = ["#27AE60" if r > 0 else "#E74C3C" for r in annual_ret.values]
        bars = ax4.bar(annual_ret.index.year, annual_ret.values, color=colors, width=0.6)
        ax4.axhline(0, color="black", linewidth=0.8)
        ax4.set_title("年度收益")
        ax4.set_ylabel("收益率")
        ax4.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.1%}"))
        for bar, val in zip(bars, annual_ret.values):
            ax4.text(bar.get_x() + bar.get_width() / 2,
                     bar.get_height() + 0.005 * (1 if val > 0 else -1),
                     f"{val:.1%}", ha="center", va="bottom" if val > 0 else "top",
                     fontsize=9)
    ax4.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    save_path = os.path.join(save_dir, "backtest_report.png")
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info(f"绩效图表已保存: {save_path}")
    return save_path


def print_summary(result: dict):
    """在终端打印绩效摘要"""
    print("\n" + "=" * 55)
    print("  A股多因子量化策略  回测绩效摘要")
    print("=" * 55)
    print(f"  回测年数        : {result.get('years', 0):.1f} 年")
    print(f"  初始资金        : ¥{result.get('final_value', 0) / (1 + result.get('total_return', 0)):,.0f}")
    print(f"  期末资产        : ¥{result.get('final_value', 0):,.0f}")
    print(f"  总收益率        : {result.get('total_return', 0):.2%}")
    print(f"  年化收益率      : {result.get('annual_return', 0):.2%}")
    print(f"  最大回撤        : {result.get('max_drawdown', 0):.2%}")
    print(f"  夏普比率        : {result.get('sharpe_ratio', 0):.3f}")
    if result.get("benchmark_annual") is not None:
        print(f"  基准年化收益    : {result.get('benchmark_annual', 0):.2%}")
        alpha = result.get("annual_return", 0) - result.get("benchmark_annual", 0)
        print(f"  超额收益(Alpha) : {alpha:.2%}")
    print("=" * 55)

    trade_log = result.get("trade_log")
    if trade_log is not None and not trade_log.empty:
        buys = trade_log[trade_log["action"] == "BUY"]
        sells = trade_log[trade_log["action"] == "SELL"]
        print(f"  总交易次数      : {len(trade_log)}")
        print(f"  买入次数        : {len(buys)}")
        print(f"  卖出次数        : {len(sells)}")
        stop_sells = sells[sells.get("reason", "") == "stop_loss"] if "reason" in sells.columns else pd.DataFrame()
        print(f"  止损次数        : {len(stop_sells)}")
    print("=" * 55 + "\n")
