"""
基本面数据模块

数据来源：同花顺 stock_financial_abstract_new_ths
缓存策略：24h 磁盘缓存（财报季频，无需频繁刷新）

主要指标：
  - ROE（加权平均净资产收益率）
  - 净利润增速（YoY）
  - 营收增速（YoY）
  - 资产负债率
  - 毛利率 / 净利率
  - 每股净资产（用于估算 PB）
  - 每股收益 EPS

调用：
  from data.fundamental import get_fundamental, get_fundamental_score
  info = get_fundamental("000001")   # 返回 dict
  score, signals = get_fundamental_score(info)
"""
import logging
import pickle
import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent / "cache" / "fundamental"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 指标 metric_name → 内部字段名
_METRIC_MAP = {
    "index_weighted_avg_roe":                          "roe",
    "index_full_diluted_roe":                          "roe_diluted",
    "calculate_parent_holder_net_profit_yoy_growth_ratio": "profit_yoy",
    "deduct_net_profit_yoy_growth_ratio":              "profit_yoy_deduct",
    "calculate_operating_income_total_yoy_growth_ratio":   "revenue_yoy",
    "assets_debt_ratio":                               "debt_ratio",
    "sale_gross_margin":                               "gross_margin",
    "sale_net_interest_ratio":                         "net_margin",
    "basic_eps":                                       "eps",
    "calc_per_net_assets":                             "bvps",
    "equity_ratio":                                    "equity_multiplier",
    "current_ratio":                                   "current_ratio",
    "quick_ratio":                                     "quick_ratio",
    "operating_income_total":                          "revenue",
    "parent_holder_net_profit":                        "net_profit",
}


def _cache_path(code: str) -> Path:
    return _CACHE_DIR / f"{code}.pkl"


def _load_cache(code: str, max_age_h: int = 24):
    p = _cache_path(code)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > max_age_h * 3600:
        return None
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_cache(code: str, data: dict):
    try:
        with open(_cache_path(code), "wb") as f:
            pickle.dump(data, f)
    except Exception:
        pass


def _parse_value(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(str(v).replace("%", "").replace(",", "").strip())
    except Exception:
        return None


def get_fundamental(code: str, force: bool = False) -> dict:
    """
    获取个股基本面数据，返回 dict。
    所有数值字段为 float 或 None（数据缺失）。

    返回键：
      code, report_date,
      roe, profit_yoy, revenue_yoy,
      debt_ratio, gross_margin, net_margin,
      eps, bvps, revenue, net_profit,
      pe (需传入 price), pb (需传入 price),
      signals: list[str]   # 正面/警示信号
    """
    if not force:
        cached = _load_cache(code)
        if cached is not None:
            return cached

    result: dict = {"code": code, "report_date": None, "error": None}

    try:
        import akshare as ak
        df = ak.stock_financial_abstract_new_ths(symbol=code, indicator="按报告期")
        if df is None or df.empty:
            result["error"] = "无财务数据"
            return result

        # 取最新报告期
        df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
        df = df.dropna(subset=["report_date"])
        latest_date = df["report_date"].max()
        latest = df[df["report_date"] == latest_date]

        result["report_date"] = latest_date.strftime("%Y-%m-%d")

        # 提取各指标
        for metric, field in _METRIC_MAP.items():
            row = latest[latest["metric_name"] == metric]
            if not row.empty:
                result[field] = _parse_value(row.iloc[0]["value"])
            else:
                result[field] = None

        # 近3期 ROE 趋势（判断是否持续改善）
        roe_trend = []
        for _, r in df[df["metric_name"] == "index_weighted_avg_roe"].sort_values(
            "report_date", ascending=False
        ).head(4).iterrows():
            v = _parse_value(r["value"])
            if v is not None:
                roe_trend.append(v)
        result["roe_trend"] = roe_trend  # 最新→最旧

        # 近3期净利润增速趋势
        profit_trend = []
        for _, r in df[df["metric_name"] == "calculate_parent_holder_net_profit_yoy_growth_ratio"].sort_values(
            "report_date", ascending=False
        ).head(4).iterrows():
            v = _parse_value(r["value"])
            if v is not None:
                profit_trend.append(v)
        result["profit_yoy_trend"] = profit_trend

    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"获取 {code} 基本面数据失败: {e}")

    _save_cache(code, result)
    return result


def enrich_with_price(info: dict, price: float) -> dict:
    """
    根据实时价格计算 PE / PB（info 由 get_fundamental 返回）
    PE = 价格 / (EPS × 4)  （年化单季 EPS，仅用于估算）
    PB = 价格 / 每股净资产
    """
    info = dict(info)
    eps  = info.get("eps")
    bvps = info.get("bvps")

    # PE：用 EPS 年化估算（季报 EPS × 4）
    if eps and eps > 0 and price > 0:
        # 判断是否年报（12月）——年报直接用，季报×4
        rd = info.get("report_date", "")
        is_annual = rd.endswith("-12-31")
        annualized_eps = eps if is_annual else eps * 4
        info["pe"] = round(price / annualized_eps, 1)
    else:
        info["pe"] = None

    # PB：价格 / 每股净资产
    if bvps and bvps > 0 and price > 0:
        info["pb"] = round(price / bvps, 2)
    else:
        info["pb"] = None

    return info


def get_fundamental_score(info: dict) -> tuple[float, list[str]]:
    """
    根据基本面数据给出 0~100 的基本面评分，以及信号列表。

    评分维度：
      ROE（40分）    - 衡量股东回报率，核心指标
      成长性（30分）  - 净利润/营收增速
      财务安全（20分）- 资产负债率（非金融股）
      盈利质量（10分）- 净利率

    返回 (score, signals)
    signals: 正面信号前缀 ✓，警示信号前缀 ✗，观察信号前缀 ·
    """
    score = 0.0
    signals: list[str] = []

    roe          = info.get("roe")
    profit_yoy   = info.get("profit_yoy")
    revenue_yoy  = info.get("revenue_yoy")
    debt_ratio   = info.get("debt_ratio")
    net_margin   = info.get("net_margin")
    gross_margin = info.get("gross_margin")
    roe_trend    = info.get("roe_trend", [])
    profit_trend = info.get("profit_yoy_trend", [])

    # ── ROE（40分）─────────────────────────────────────────
    # 注意：周期行业（半导体/化工/钢铁/航运）景气低点ROE天然偏低，
    # 但这不代表公司质地差，需结合利润趋势判断
    cycle_low = (profit_yoy is not None and profit_yoy < -20)   # 利润大降=周期低点

    if roe is not None:
        if roe >= 20:
            score += 40
            signals.append(f"✓ ROE={roe:.1f}%，优秀（≥20%），股东回报极强")
        elif roe >= 15:
            score += 32
            signals.append(f"✓ ROE={roe:.1f}%，良好（≥15%）")
        elif roe >= 10:
            score += 22
            signals.append(f"· ROE={roe:.1f}%，合格（≥10%）")
        elif roe >= 5:
            score += 10
            if cycle_low:
                signals.append(f"· ROE={roe:.1f}%，偏低但处于行业周期低点，关注景气回升节奏")
            else:
                signals.append(f"· ROE={roe:.1f}%，偏低（5~10%），盈利能力一般")
        elif roe >= 0:
            score += (5 if cycle_low else 0)   # 周期底部给基础分
            if cycle_low:
                signals.append(f"· ROE={roe:.1f}%，行业周期底部，利润暂时受压，非公司质地恶化")
            else:
                signals.append(f"✗ ROE={roe:.1f}%，极低，盈利能力差")
        else:
            signals.append(f"✗ ROE={roe:.1f}%，净资产收益为负，关注是否持续亏损")

        # ROE 趋势：改善是最重要信号（尤其周期底部开始回升）
        if len(roe_trend) >= 3:
            if roe_trend[0] > roe_trend[1] > roe_trend[2]:
                score += 5
                signals.append("✓ ROE 持续改善（近3期上升趋势），景气回升信号")
            elif roe_trend[0] < roe_trend[1] < roe_trend[2]:
                score -= 3
                signals.append("✗ ROE 持续下滑（近3期下降趋势）")
        elif len(roe_trend) >= 2 and roe_trend[0] > roe_trend[1]:
            score += 2
            signals.append("· ROE 最新期已改善，关注是否持续")
    else:
        signals.append("· ROE 数据缺失")

    # ── 成长性（30分）─────────────────────────────────────
    growth_pts = 0
    if profit_yoy is not None:
        if profit_yoy >= 30:
            growth_pts += 18
            signals.append(f"✓ 净利润同比 +{profit_yoy:.1f}%，高速增长")
        elif profit_yoy >= 15:
            growth_pts += 13
            signals.append(f"✓ 净利润同比 +{profit_yoy:.1f}%，稳健增长")
        elif profit_yoy >= 0:
            growth_pts += 7
            signals.append(f"· 净利润同比 +{profit_yoy:.1f}%，小幅增长")
        elif profit_yoy >= -20:
            growth_pts += 0
            signals.append(f"✗ 净利润同比 {profit_yoy:.1f}%，业绩下滑")
        else:
            growth_pts -= 5
            signals.append(f"✗ 净利润同比 {profit_yoy:.1f}%，业绩大幅下滑，风险较高")

    if revenue_yoy is not None:
        if revenue_yoy >= 20:
            growth_pts += 12
            signals.append(f"✓ 营收同比 +{revenue_yoy:.1f}%，高速扩张")
        elif revenue_yoy >= 10:
            growth_pts += 8
            signals.append(f"✓ 营收同比 +{revenue_yoy:.1f}%，稳健增长")
        elif revenue_yoy >= 0:
            growth_pts += 4
            signals.append(f"· 营收同比 +{revenue_yoy:.1f}%，持平")
        else:
            growth_pts += 0
            signals.append(f"✗ 营收同比 {revenue_yoy:.1f}%，收入萎缩")

    score += min(30, max(0, growth_pts))

    # ── 财务安全（20分）──────────────────────────────────
    if debt_ratio is not None:
        # 注意：银行等金融股负债率天然高（>80%），需特殊处理
        # 此处按非金融股标准
        if debt_ratio <= 30:
            score += 20
            signals.append(f"✓ 资产负债率={debt_ratio:.1f}%，财务极稳健")
        elif debt_ratio <= 50:
            score += 15
            signals.append(f"✓ 资产负债率={debt_ratio:.1f}%，财务健康")
        elif debt_ratio <= 65:
            score += 8
            signals.append(f"· 资产负债率={debt_ratio:.1f}%，负债偏高，注意财务风险")
        elif debt_ratio <= 80:
            score += 3
            signals.append(f"✗ 资产负债率={debt_ratio:.1f}%，高负债，关注偿债能力")
        else:
            score += 0
            signals.append(f"✗ 资产负债率={debt_ratio:.1f}%，极高负债（>{80}%），暴雷风险")
    else:
        signals.append("· 负债率数据缺失")

    # ── 盈利质量（10分）─────────────────────────────────
    if net_margin is not None:
        if net_margin >= 20:
            score += 10
            signals.append(f"✓ 净利率={net_margin:.1f}%，盈利质量极高")
        elif net_margin >= 10:
            score += 7
            signals.append(f"✓ 净利率={net_margin:.1f}%，盈利质量良好")
        elif net_margin >= 5:
            score += 4
            signals.append(f"· 净利率={net_margin:.1f}%，盈利质量一般")
        else:
            score += 0
            signals.append(f"✗ 净利率={net_margin:.1f}%，利润空间极薄")
    elif gross_margin is not None:
        if gross_margin >= 40:
            score += 7
            signals.append(f"✓ 毛利率={gross_margin:.1f}%，产品溢价能力强")
        elif gross_margin >= 20:
            score += 4
            signals.append(f"· 毛利率={gross_margin:.1f}%，毛利率一般")
        else:
            score += 0
            signals.append(f"✗ 毛利率={gross_margin:.1f}%，竞争激烈，产品无定价权")

    score = round(min(100, max(0, score)), 1)
    return score, signals


def get_pe_pb_signal(pe: float | None, pb: float | None, sector: str = "",
                     profit_yoy: float | None = None) -> list[str]:
    """
    根据 PE/PB 给出估值判断信号。
    自动识别两类特殊情况，避免误判：
      1. 周期底部：PE虚高因利润基数极低，应参考PB/PS
      2. 科技/成长股：高PE是市场给的成长溢价，不等于高风险
    """
    signals = []

    # 判断是否周期底部（利润极低导致PE虚高）
    cycle_bottom = (profit_yoy is not None and profit_yoy < -30) or pe is None or pe > 200

    if pe is not None:
        if pe <= 0:
            signals.append("✗ PE 为负（亏损股），关注扭亏节奏")
        elif cycle_bottom and pe > 80:
            # 周期底部高PE不直接判负面
            signals.append(f"· PE={pe:.1f}x，利润处于周期低点导致PE虚高，"
                           f"建议参考PB和行业景气度判断，不宜单纯以PE估值")
        elif pe <= 20:
            signals.append(f"✓ PE={pe:.1f}x，估值偏低，安全边际充足")
        elif pe <= 35:
            signals.append(f"· PE={pe:.1f}x，估值合理")
        elif pe <= 60:
            signals.append(f"· PE={pe:.1f}x，估值偏高，需成长性支撑")
        else:
            signals.append(f"· PE={pe:.1f}x，高估值，若业绩高增长可接受，否则注意回调风险")

    if pb is not None:
        if pb <= 1.0:
            signals.append(f"✓ PB={pb:.2f}x，破净股，资产价值被低估")
        elif pb <= 3.0:
            signals.append(f"✓ PB={pb:.2f}x，估值合理")
        elif pb <= 6.0:
            signals.append(f"· PB={pb:.2f}x，溢价偏高，需强成长性支撑")
        else:
            signals.append(f"✗ PB={pb:.2f}x，高溢价，需极高成长性支撑")

    return signals
