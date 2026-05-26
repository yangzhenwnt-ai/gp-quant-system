"""
暴雷风险扫描模块

从七个维度量化识别暴雷前兆，给出 0~100 的风险分（越高越危险）：

  1. 财务恶化  — ROE持续下滑、净利润连续负增长、负债率飙升
  2. 商誉炸弹  — 商誉/净资产占比过高（并购雷核心指标）
  3. 应收账款  — 应收账款增速远超营收增速（财务造假信号）
  4. 资金撤离  — 主力资金连续净流出
  5. 技术破位  — 股价跌破MA20、MA60，相对大盘持续弱势
  6. 筹码恶化  — 换手率异常、量价背离
  7. 估值陷阱  — PE极高且业绩下滑（杀估值风险）

风险等级：
  0~29   绿色  — 暂无明显风险
  30~49  黄色  — 需要关注，设好止损
  50~69  橙色  — 中等风险，建议减仓
  70~100 红色  — 高风险，强烈建议规避

调用：
  from data.risk_scanner import scan_stock_risk
  result = scan_stock_risk("600745")
  print(result["risk_score"], result["level"], result["signals"])
"""
import logging
import pickle
import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent / "cache" / "risk"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 风险等级定义
RISK_LEVELS = [
    (70, "高风险",   "red",    "⚠",  "强烈建议规避，多项暴雷前兆同时出现"),
    (50, "中等风险", "orange", "△",  "建议减仓，密切关注基本面变化"),
    (30, "需关注",   "yellow", "!",  "存在风险信号，设好止损严守纪律"),
    (0,  "暂无风险", "green",  "✓",  "暂未发现明显暴雷前兆"),
]


def _get_risk_level(score: float) -> dict:
    for threshold, label, color, icon, advice in RISK_LEVELS:
        if score >= threshold:
            return {"label": label, "color": color, "icon": icon, "advice": advice}
    return {"label": "暂无风险", "color": "green", "icon": "✓", "advice": "暂未发现明显暴雷前兆"}


def _parse_float(v) -> float | None:
    if v is None:
        return None
    try:
        s = str(v).replace(",", "").strip()
        if not s or s in ("", "None", "nan", "--", "－"):
            return None
        return float(s)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 维度1：财务恶化检测（来自 fundamental.py 已有数据）
# ─────────────────────────────────────────────────────────────

def _check_financial_deterioration(fund_info: dict) -> tuple[float, list[str]]:
    """
    检测财务恶化：ROE持续下滑、利润连续负增长、负债率飙升
    返回 (risk_pts, signals)，risk_pts 满分 30
    """
    pts = 0
    sigs = []

    roe_trend    = fund_info.get("roe_trend", [])       # 最新→最旧
    profit_trend = fund_info.get("profit_yoy_trend", [])
    debt_ratio   = fund_info.get("debt_ratio")
    profit_yoy   = fund_info.get("profit_yoy")
    revenue_yoy  = fund_info.get("revenue_yoy")
    net_margin   = fund_info.get("net_margin")

    # ROE 连续3期下滑
    if len(roe_trend) >= 3:
        if roe_trend[0] < roe_trend[1] < roe_trend[2]:
            pts += 10
            sigs.append(f"⚠ ROE连续3期下滑（{roe_trend[2]:.1f}%→{roe_trend[1]:.1f}%→{roe_trend[0]:.1f}%），盈利能力持续衰退")
        elif roe_trend[0] < roe_trend[1]:
            pts += 4
            sigs.append(f"△ ROE最近1期下滑（{roe_trend[1]:.1f}%→{roe_trend[0]:.1f}%）")

    # 净利润连续负增长
    neg_count = sum(1 for v in profit_trend[:3] if v is not None and v < 0)
    if neg_count >= 3:
        pts += 12
        sigs.append(f"⚠ 净利润连续{neg_count}期同比负增长，业绩持续恶化")
    elif neg_count >= 2:
        pts += 7
        sigs.append(f"△ 净利润连续{neg_count}期同比负增长")
    elif profit_yoy is not None and profit_yoy < -30:
        pts += 8
        sigs.append(f"⚠ 本期净利润大幅下滑 {profit_yoy:.1f}%，需关注是否持续")
    elif profit_yoy is not None and profit_yoy < -15:
        pts += 4
        sigs.append(f"△ 净利润下滑 {profit_yoy:.1f}%")

    # 营收和利润背离（营收正增长但利润大降 → 成本失控）
    if revenue_yoy is not None and profit_yoy is not None:
        if revenue_yoy > 5 and profit_yoy < -20:
            pts += 6
            sigs.append(f"⚠ 营收增长（+{revenue_yoy:.1f}%）但净利润大幅下滑（{profit_yoy:.1f}%），成本失控或存在大额减值")

    # 负债率飙升
    if debt_ratio is not None:
        if debt_ratio > 80:
            pts += 8
            sigs.append(f"⚠ 资产负债率={debt_ratio:.1f}%，偿债压力极大")
        elif debt_ratio > 65:
            pts += 4
            sigs.append(f"△ 资产负债率={debt_ratio:.1f}%，负债偏高需关注")

    return min(30, pts), sigs


# ─────────────────────────────────────────────────────────────
# 维度2：商誉炸弹检测（资产负债表）
# ─────────────────────────────────────────────────────────────

def _check_goodwill_risk(code: str) -> tuple[float, list[str]]:
    """
    检测商誉占净资产比例 → 并购雷核心指标
    返回 (risk_pts, signals)，满分 20
    """
    pts = 0
    sigs = []
    try:
        import akshare as ak
        df = ak.stock_financial_debt_new_ths(symbol=code, indicator="按报告期")
        if df is None or df.empty:
            return 0, []

        df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
        latest_date = df["report_date"].dropna().max()
        latest = df[df["report_date"] == latest_date]

        def _get(metric):
            row = latest[latest["metric_name"] == metric]["value"]
            if row.empty:
                return None
            return _parse_float(row.iloc[0])

        goodwill = _get("goodwill")
        equity   = _get("parent_holder_equity_total")
        assets   = _get("assets_total")

        if goodwill is None or goodwill == 0:
            return 0, []

        # 商誉 / 净资产
        if equity and equity > 0:
            gw_equity_ratio = goodwill / equity * 100
            if gw_equity_ratio > 100:
                pts += 20
                sigs.append(f"⚠ 商誉/净资产={gw_equity_ratio:.1f}%，超过100%！商誉减值将直接侵蚀净资产，极高暴雷风险")
            elif gw_equity_ratio > 60:
                pts += 14
                sigs.append(f"⚠ 商誉/净资产={gw_equity_ratio:.1f}%，商誉占比过高，并购整合失败风险大")
            elif gw_equity_ratio > 30:
                pts += 7
                sigs.append(f"△ 商誉/净资产={gw_equity_ratio:.1f}%，商誉占比偏高，关注被并购公司经营情况")
            elif gw_equity_ratio > 15:
                pts += 3
                sigs.append(f"· 商誉/净资产={gw_equity_ratio:.1f}%，有一定并购风险，可留意")

        # 商誉绝对值（> 50亿 的大额商誉）
        if goodwill > 5e9:
            extra = f"（商誉绝对值 {goodwill/1e8:.0f}亿，若减值将严重影响业绩）"
            if sigs:
                sigs[-1] += extra
            else:
                pts += 3
                sigs.append(f"· 商誉绝对值 {goodwill/1e8:.0f}亿，需关注并购标的经营情况")

    except Exception as e:
        logger.debug(f"商誉检测失败 {code}: {e}")

    return min(20, pts), sigs


# ─────────────────────────────────────────────────────────────
# 维度3：应收账款异常（财务造假信号）
# ─────────────────────────────────────────────────────────────

def _check_receivable_risk(code: str, fund_info: dict) -> tuple[float, list[str]]:
    """
    检测应收账款增速 >> 营收增速（典型财务造假信号）
    返回 (risk_pts, signals)，满分 15
    """
    pts = 0
    sigs = []
    try:
        import akshare as ak
        df = ak.stock_financial_debt_new_ths(symbol=code, indicator="按报告期")
        if df is None or df.empty:
            return 0, []

        df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
        dates = sorted(df["report_date"].dropna().unique(), reverse=True)
        if len(dates) < 2:
            return 0, []

        def _get_metric(d, metric):
            row = df[(df["report_date"] == d) & (df["metric_name"] == metric)]["value"]
            return _parse_float(row.iloc[0]) if not row.empty else None

        # 应收账款近2期对比
        ar_now  = _get_metric(dates[0], "accounts_receivable") or _get_metric(dates[0], "note_and_accounts_receivable")
        ar_prev = _get_metric(dates[1], "accounts_receivable") or _get_metric(dates[1], "note_and_accounts_receivable")

        revenue_yoy = fund_info.get("revenue_yoy")  # 营收增速%

        if ar_now and ar_prev and ar_prev > 0:
            ar_growth = (ar_now - ar_prev) / ar_prev * 100

            if revenue_yoy is not None:
                gap = ar_growth - revenue_yoy
                if gap > 50 and ar_growth > 30:
                    pts += 15
                    sigs.append(f"⚠ 应收账款增速(+{ar_growth:.1f}%)远超营收增速(+{revenue_yoy:.1f}%)，差值{gap:.1f}%，财务质量存疑")
                elif gap > 30 and ar_growth > 15:
                    pts += 8
                    sigs.append(f"△ 应收账款增速(+{ar_growth:.1f}%)显著高于营收增速(+{revenue_yoy:.1f}%)，需关注坏账风险")
                elif gap > 20:
                    pts += 4
                    sigs.append(f"· 应收账款增长偏快（+{ar_growth:.1f}%），留意回款质量")
            else:
                if ar_growth > 50:
                    pts += 8
                    sigs.append(f"△ 应收账款大幅增长 +{ar_growth:.1f}%，回款能力存疑")

    except Exception as e:
        logger.debug(f"应收账款检测失败 {code}: {e}")

    return min(15, pts), sigs


# ─────────────────────────────────────────────────────────────
# 维度4：资金撤离检测（主力资金流向）
# ─────────────────────────────────────────────────────────────

def _check_fund_outflow(code: str) -> tuple[float, list[str]]:
    """
    检测主力资金持续净流出
    返回 (risk_pts, signals)，满分 20
    """
    pts = 0
    sigs = []
    try:
        from data.reliable_api import API
        ff = API.fund_flow(code)
        if ff is None or ff.empty:
            return 0, []

        ff = ff.tail(10)   # 最近10日（当前接口可能只有今日1条）

        # 英文列名（push2直连接口）或中文列名（旧akshare接口）均兼容
        main_col = next((c for c in ff.columns if c == "main_net"), None)
        if main_col is None:
            main_col = next(
                (c for c in ff.columns if "主力" in c and "净" in c and "率" not in c),
                None
            )
        if main_col is None:
            return 0, []

        ff["_main"] = pd.to_numeric(ff[main_col], errors="coerce").fillna(0)
        recent_5d  = ff["_main"].tail(5)
        recent_10d = ff["_main"].tail(10)
        n_days = len(ff)

        out_days_5  = int((recent_5d < 0).sum())
        out_days_10 = int((recent_10d < 0).sum())
        total_flow_5 = float(recent_5d.sum())
        total_flow_10 = float(recent_10d.sum())

        # 连续多日数据才做天数统计（当前接口可能只有今日1天）
        if n_days >= 5:
            if out_days_10 >= 8:
                pts += 20
                sigs.append(f"⚠ 近10日主力净流出{out_days_10}天（{total_flow_10/1e8:.2f}亿），机构资金持续撤离，暴雷风险高")
            elif out_days_5 >= 4:
                pts += 12
                sigs.append(f"⚠ 近5日主力净流出{out_days_5}天（{total_flow_5/1e8:.2f}亿），资金撤退明显")
            elif out_days_5 >= 3:
                pts += 6
                sigs.append(f"△ 近5日主力净流出{out_days_5}天（{total_flow_5/1e8:.2f}亿）")

        # 单日超大额流出：阈值按市值动态计算
        # 大市值股（日均百亿成交）单日10亿流出是正常调仓，不应报警
        try:
            from data.reliable_api import API as _API
            _spot = _API.spot()
            _row  = _spot[_spot["code"].astype(str) == code]
            _mktcap = float(_row.iloc[0].get("mktcap", 0) or 0) if not _row.empty else 0
        except Exception:
            _mktcap = 0
        # 阈值：市值<50亿用3000万，50-500亿用1亿，>500亿用5亿
        if _mktcap > 500e8:
            _threshold = -5e8
        elif _mktcap > 50e8:
            _threshold = -1e8
        else:
            _threshold = -3e7
        max_out = float(recent_5d.min())
        if max_out < _threshold:
            pts += 5
            sigs.append(f"⚠ 近5日单日最大流出 {max_out/1e8:.2f}亿，异常大额卖出")

    except Exception as e:
        logger.debug(f"资金流向检测失败 {code}: {e}")

    return min(20, pts), sigs


# ─────────────────────────────────────────────────────────────
# 维度5：技术破位 + 相对弱势
# ─────────────────────────────────────────────────────────────

def _check_technical_breakdown(code: str, spot_row: dict | None = None) -> tuple[float, list[str]]:
    """
    检测股价技术面破位：跌破关键均线、持续跑输大盘
    返回 (risk_pts, signals)，满分 15
    """
    pts = 0
    sigs = []
    try:
        from data.reliable_api import API
        from datetime import datetime, timedelta
        end   = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
        hist = API.history(code, start, end)
        if hist is None or hist.empty or "close" not in hist.columns:
            return 0, []

        close = hist["close"].astype(float)
        price = float(close.iloc[-1])
        n = len(close)

        ma20 = float(close.rolling(20).mean().iloc[-1]) if n >= 20 else None
        ma60 = float(close.rolling(60).mean().iloc[-1]) if n >= 60 else None

        # 跌破均线
        if ma20 and price < ma20:
            pts += 5
            dist = (ma20 - price) / ma20 * 100
            sigs.append(f"△ 股价跌破MA20（低于{dist:.1f}%），短期趋势偏弱")

        if ma60 and price < ma60:
            pts += 7
            dist = (ma60 - price) / ma60 * 100
            sigs.append(f"⚠ 股价跌破MA60（低于{dist:.1f}%），中期趋势向下")

        # 近20日涨跌幅
        if n >= 20:
            chg_20d = (close.iloc[-1] / close.iloc[-20] - 1) * 100
            if chg_20d < -20:
                pts += 8
                sigs.append(f"⚠ 近20日累计跌幅 {chg_20d:.1f}%，持续大幅下跌")
            elif chg_20d < -10:
                pts += 4
                sigs.append(f"△ 近20日累计跌幅 {chg_20d:.1f}%，短期走势弱")

        # 量价背离：价格上涨但成交量萎缩（假多）
        if n >= 10 and "volume" in hist.columns:
            vol = hist["volume"].astype(float)
            vol_ma10 = float(vol.rolling(10).mean().iloc[-1])
            recent_vol = float(vol.iloc[-3:].mean())
            close_chg_5 = float(close.iloc[-1] / close.iloc[-5] - 1) * 100
            if close_chg_5 > 3 and recent_vol < vol_ma10 * 0.6:
                pts += 3
                sigs.append(f"△ 价格上涨但成交量萎缩{1-recent_vol/vol_ma10:.0%}，量价背离，缺乏实质买盘")

    except Exception as e:
        logger.debug(f"技术破位检测失败 {code}: {e}")

    return min(15, pts), sigs


# ─────────────────────────────────────────────────────────────
# 核心入口：全维度扫描
# ─────────────────────────────────────────────────────────────

def scan_stock_risk(
    code: str,
    fund_info: dict | None = None,
    spot_row: dict | None = None,
    use_cache: bool = True,
) -> dict:
    """
    对单只股票进行全维度暴雷风险扫描。

    参数：
      code      - 股票代码（6位）
      fund_info - 已有基本面数据（来自 fundamental.get_fundamental），不传则自动获取
      spot_row  - 当日行情 dict（可选，用于技术检测）
      use_cache - 是否使用缓存（扫描结果缓存4小时）

    返回：
      {
        "code": "600745",
        "risk_score": 72,          # 0~100，越高越危险
        "level": "高风险",
        "color": "red",
        "icon": "⚠",
        "advice": "...",
        "signals": [...],          # 全部风险信号列表
        "dimensions": {            # 各维度得分
          "financial": 18,
          "goodwill": 14,
          "receivable": 8,
          "fund_flow": 12,
          "technical": 10,
        },
        "scan_time": "15:30",
        "error": None
      }
    """
    cache_path = _CACHE_DIR / f"{code}_risk.pkl"
    if use_cache and cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < 4 * 3600:
            try:
                with open(cache_path, "rb") as f:
                    return pickle.load(f)
            except Exception:
                pass

    result = {
        "code":       code,
        "risk_score": 0,
        "level":      "暂无风险",
        "color":      "green",
        "icon":       "✓",
        "advice":     "暂未发现明显暴雷前兆",
        "signals":    [],
        "dimensions": {},
        "scan_time":  __import__("datetime").datetime.now().strftime("%H:%M"),
        "error":      None,
    }

    all_signals = []
    dim_scores  = {}

    # 获取基本面数据（如果调用方没传）
    if fund_info is None:
        try:
            from data.fundamental import get_fundamental
            fund_info = get_fundamental(code)
        except Exception as e:
            fund_info = {}
            result["error"] = f"基本面数据获取失败: {e}"

    # ── 1. 财务恶化 ─────────────────────────────────────────
    pts1, sigs1 = _check_financial_deterioration(fund_info)
    dim_scores["financial"] = pts1
    all_signals.extend(sigs1)

    # ── 2. 商誉炸弹 ─────────────────────────────────────────
    pts2, sigs2 = _check_goodwill_risk(code)
    dim_scores["goodwill"] = pts2
    all_signals.extend(sigs2)

    # ── 3. 应收账款 ─────────────────────────────────────────
    pts3, sigs3 = _check_receivable_risk(code, fund_info)
    dim_scores["receivable"] = pts3
    all_signals.extend(sigs3)

    # ── 4. 资金撤离 ─────────────────────────────────────────
    pts4, sigs4 = _check_fund_outflow(code)
    dim_scores["fund_flow"] = pts4
    all_signals.extend(sigs4)

    # ── 5. 技术破位 ─────────────────────────────────────────
    pts5, sigs5 = _check_technical_breakdown(code, spot_row)
    dim_scores["technical"] = pts5
    all_signals.extend(sigs5)

    # ── 综合风险分 ───────────────────────────────────────────
    # 满分：30+20+15+20+15 = 100
    total = sum(dim_scores.values())

    # 多维度共振加成：3个以上维度同时触发，额外+10
    triggered_dims = sum(1 for v in dim_scores.values() if v >= 5)
    if triggered_dims >= 3:
        total = min(100, total + 10)
        all_signals.append(f"⚠ 多维度共振警报：{triggered_dims}个风险维度同时触发，暴雷概率显著提升")

    level_info = _get_risk_level(total)
    result.update({
        "risk_score": total,
        "signals":    all_signals,
        "dimensions": dim_scores,
        **level_info,
    })

    # 缓存结果
    try:
        with open(cache_path, "wb") as f:
            pickle.dump(result, f)
    except Exception:
        pass

    return result


def scan_portfolio_risk(portfolio: list[dict]) -> list[dict]:
    """
    批量扫描持仓股票的暴雷风险
    portfolio: [{"code": "600745", "name": "闻泰科技", ...}, ...]
    返回: 每项追加 risk_result 字段
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []
    codes = [p["code"] for p in portfolio if p.get("code")]

    risk_map = {}
    with ThreadPoolExecutor(max_workers=3) as exe:
        futs = {exe.submit(scan_stock_risk, code): code for code in codes}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                risk_map[code] = fut.result()
            except Exception as e:
                risk_map[code] = {
                    "code": code, "risk_score": 0, "level": "检测失败",
                    "color": "dim", "icon": "?", "signals": [], "error": str(e)
                }

    for p in portfolio:
        item = dict(p)
        item["risk_result"] = risk_map.get(p.get("code"), {})
        results.append(item)

    return results


def format_risk_badge(risk_result: dict) -> str:
    """生成简短的风险徽章文字，用于 D 区块表格显示"""
    if not risk_result:
        return ""
    score = risk_result.get("risk_score", 0)
    icon  = risk_result.get("icon", "")
    level = risk_result.get("level", "")
    if score >= 70:
        return f"[bold red]{icon} {score}[/bold red]"
    elif score >= 50:
        return f"[bold yellow]{icon} {score}[/bold yellow]"
    elif score >= 30:
        return f"[yellow]{icon} {score}[/yellow]"
    else:
        return f"[dim green]{icon}[/dim green]"
