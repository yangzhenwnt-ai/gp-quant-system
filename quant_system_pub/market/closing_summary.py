"""
收盘总结模块

从量化视角生成当日复盘报告，覆盖：
  1. 大盘定性（指数强弱 + 均线结构 + 市场特征）
  2. 市场宽度（涨跌比 + 涨跌停 + 连板效应）
  3. 板块轮动（今日主线 + 热度评分 + 强弱排序）
  4. 量能结构（总成交 + 换手分布 + 量比分析）
  5. 资金性质（大单/散户主导 + 板块集中度）
  6. 操作建议（明日预判 + 策略倾向）

调用：
  from market.closing_summary import generate_closing_summary
  summary = generate_closing_summary()   # 返回结构化 dict
  text = summary["full_text"]            # 完整文字报告
"""
import logging
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 辅助：指数位置描述
# ─────────────────────────────────────────────────────────────

def _index_position(info: dict) -> str:
    """根据均线结构描述指数所处位置"""
    above = [
        ("MA5",  info.get("above_ma5",  False)),
        ("MA10", info.get("above_ma10", False)),
        ("MA20", info.get("above_ma20", False)),
        ("MA60", info.get("above_ma60", False)),
    ]
    n_above = sum(1 for _, v in above if v)
    if n_above == 4:
        return "多头排列（站上全部均线）"
    elif n_above == 3:
        return "偏多（站上MA5/10/20，MA60待确认）"
    elif n_above == 2:
        return "震荡（站上MA5/10，MA20承压）"
    elif n_above == 1:
        return "偏弱（仅站上MA5，短期反弹）"
    else:
        return "空头排列（全线跌破均线）"


def _chg_desc(chg: float) -> str:
    """量化涨跌幅的文字描述"""
    if chg >= 2.0:   return f"大涨 {chg:+.2f}%"
    elif chg >= 0.5: return f"上涨 {chg:+.2f}%"
    elif chg >= 0:   return f"微涨 {chg:+.2f}%"
    elif chg >= -0.5: return f"微跌 {chg:+.2f}%"
    elif chg >= -2.0: return f"下跌 {chg:+.2f}%"
    else:             return f"大跌 {chg:+.2f}%"


def _market_character(up: int, down: int, zt: int, dt: int,
                      median_chg: float, total: int) -> str:
    """判断今日市场性质"""
    up_ratio = up / (up + down) if (up + down) > 0 else 0.5
    zt_ratio = zt / total if total > 0 else 0

    if zt_ratio > 0.03 and up_ratio > 0.65:
        return "普涨行情（大多数股票上涨，赚钱效应极强）"
    elif zt_ratio > 0.02 and up_ratio > 0.55:
        return "强势行情（热点板块领涨，赚钱效应良好）"
    elif up_ratio > 0.55 and median_chg > 0.3:
        return "温和上涨（多数飘红，市场情绪积极）"
    elif abs(up_ratio - 0.5) < 0.05:
        return "分化震荡（涨跌参半，方向不明朗）"
    elif up_ratio < 0.40 and dt > 5:
        return "弱势下跌（多数下跌，资金避险情绪升温）"
    elif up_ratio < 0.35 and dt > 10:
        return "恐慌杀跌（跌停数量多，建议回避风险）"
    else:
        return "结构性行情（指数平稳，个股分化明显）"


def _lianban_analysis(zt_pool_df: pd.DataFrame) -> dict:
    """连板梯队分析"""
    result = {"max_lianban": 0, "lianban_list": [], "first_board": 0, "high_board": 0}
    if zt_pool_df is None or zt_pool_df.empty:
        return result

    # 找连板次数列
    lb_col = next((c for c in zt_pool_df.columns
                   if any(k in c for k in ["连板", "连续", "连涨"])), None)
    name_col = next((c for c in zt_pool_df.columns
                     if any(k in c for k in ["名称", "股票名称"])), None)
    code_col = next((c for c in zt_pool_df.columns
                     if any(k in c for k in ["代码", "股票代码"])), None)

    if lb_col is None:
        return result

    zt_pool_df = zt_pool_df.copy()
    zt_pool_df["lb"] = pd.to_numeric(zt_pool_df[lb_col], errors="coerce").fillna(1)

    result["max_lianban"] = int(zt_pool_df["lb"].max())
    result["first_board"] = int((zt_pool_df["lb"] == 1).sum())
    result["high_board"]  = int((zt_pool_df["lb"] >= 3).sum())

    # 高位连板股
    high = zt_pool_df[zt_pool_df["lb"] >= 2].sort_values("lb", ascending=False)
    items = []
    for _, r in high.head(8).iterrows():
        name = str(r.get(name_col, "")) if name_col else ""
        code = str(r.get(code_col, "")) if code_col else ""
        lb   = int(r["lb"])
        items.append({"code": code, "name": name, "lianban": lb})
    result["lianban_list"] = items
    return result


# ─────────────────────────────────────────────────────────────
# 核心：生成收盘总结
# ─────────────────────────────────────────────────────────────

def generate_closing_summary() -> dict:
    """
    生成收盘总结，返回 dict：
      {
        "date": "2026-05-13",
        "sections": {                   # 各板块结构化数据
          "index": {...},
          "breadth": {...},
          "sectors": {...},
          "volume": {...},
          "lianban": {...},
          "outlook": {...},
        },
        "full_text": "...",            # 完整可读报告文字
        "score": int,                  # 今日市场强度评分 0-100
        "generated_at": "15:05"
      }
    """
    today = datetime.today().strftime("%Y-%m-%d")
    now   = datetime.now().strftime("%H:%M")

    sections = {}
    errors   = []

    # ── 1. 全市场行情 ─────────────────────────────────────────
    try:
        from data.reliable_api import API
        spot = API.spot()
        chg_ser = pd.to_numeric(spot["chg"], errors="coerce").dropna()
        amt_ser = pd.to_numeric(spot["amount"], errors="coerce").fillna(0)
        vr_ser  = pd.to_numeric(spot["vol_ratio"], errors="coerce").fillna(0)

        total   = len(spot)
        up      = int((chg_ser > 0).sum())
        down    = int((chg_ser < 0).sum())
        flat    = total - up - down
        zt_cnt  = int((chg_ser >= 9.8).sum())
        dt_cnt  = int((chg_ser <= -9.8).sum())
        up5     = int((chg_ser >= 5).sum())   # 涨幅>5%强势股数
        dn5     = int((chg_ser <= -5).sum())  # 跌幅>5%弱势股数

        median_chg  = float(chg_ser.median())
        mean_chg    = float(chg_ser.mean())
        total_amt   = float(amt_ser.sum()) / 10000   # 万元→亿元
        avg_vr      = float(vr_ser[vr_ser > 0].median())

        # 成交额分布：哪个区间股票最多
        buckets = {
            "<5000万": int((amt_ser < 5000).sum()),
            "5000万~2亿": int(amt_ser.between(5000, 20000).sum()),
            "2亿~10亿":  int(amt_ser.between(20000, 100000).sum()),
            ">10亿":     int((amt_ser >= 100000).sum()),
        }

        sections["volume"] = {
            "total_amt_yi":  round(total_amt, 1),
            "avg_vol_ratio": round(avg_vr, 2),
            "amt_buckets":   buckets,
            "up5": up5, "dn5": dn5,
        }
        sections["breadth"] = {
            "total": total, "up": up, "down": down, "flat": flat,
            "zt": zt_cnt, "dt": dt_cnt,
            "up_ratio": round(up / (up + down) * 100, 1) if (up + down) > 0 else 50,
            "median_chg": round(median_chg, 2),
            "mean_chg":   round(mean_chg, 2),
            "character":  _market_character(up, down, zt_cnt, dt_cnt, median_chg, total),
        }
    except Exception as e:
        errors.append(f"行情数据: {e}")
        spot = pd.DataFrame()
        sections["breadth"] = {"up": 0, "down": 0, "zt": 0, "dt": 0,
                               "up_ratio": 50, "median_chg": 0, "mean_chg": 0,
                               "character": "数据获取失败"}
        sections["volume"]  = {"total_amt_yi": 0, "avg_vol_ratio": 1}

    # ── 2. 大盘指数 ───────────────────────────────────────────
    try:
        from market.market_pulse import run_market_pulse
        pulse = run_market_pulse()
        ov    = pulse.get("overview", {})
        sent  = pulse.get("sentiment", {})

        idx_data = {}
        for key, label in [("sh", "上证"), ("sz", "深成"), ("cyb", "创业板")]:
            info = ov.get(key, {})
            idx_data[label] = {
                "close":      info.get("close", 0),
                "chg":        round(info.get("change_pct", 0), 2),
                "ma5":        info.get("ma5", 0),
                "ma20":       info.get("ma20", 0),
                "position":   _index_position(info),
                "chg_desc":   _chg_desc(info.get("change_pct", 0)),
            }
        sections["index"] = {
            "indices":   idx_data,
            "score":     int(sent.get("score", 50)),
            "level":     sent.get("level", "—"),
            "zt_count":  sent.get("zt_count", 0),
            "dt_count":  sent.get("dt_count", 0),
        }
    except Exception as e:
        errors.append(f"指数数据: {e}")
        sections["index"] = {"indices": {}, "score": 50, "level": "未知"}

    # ── 3. 涨停池：连板梯队 ───────────────────────────────────
    try:
        from data.reliable_api import API
        zt_pool_df = API.zt_pool()
        sections["lianban"] = _lianban_analysis(zt_pool_df)
    except Exception as e:
        errors.append(f"涨停池: {e}")
        sections["lianban"] = {"max_lianban": 0, "lianban_list": [],
                               "first_board": 0, "high_board": 0}

    # ── 4. 板块热度 ───────────────────────────────────────────
    try:
        from selector.sector_heat import rank_hot_sectors
        hot = rank_hot_sectors(top_n=10)
        top_sectors = []
        cold_sectors = []
        if not hot.empty:
            for _, r in hot.head(5).iterrows():
                top_sectors.append({
                    "name": str(r["sector_name"]),
                    "chg":  round(float(r["change_pct"]), 2),
                    "zt":   int(r.get("zt_count", 0)),
                    "heat": round(float(r["heat_score"]), 3),
                })
            # 全量行业数据拿最弱的（用sector_flow）
            from data.reliable_api import API
            sf = API.sector_flow()
            if not sf.empty and "chg" in sf.columns:
                worst = sf.sort_values("chg").head(5)
                for _, r in worst.iterrows():
                    cold_sectors.append({
                        "name": str(r["sector_name"]),
                        "chg":  round(float(r["chg"]), 2),
                    })

        sections["sectors"] = {
            "top":   top_sectors,
            "cold":  cold_sectors,
            "leader": top_sectors[0]["name"] if top_sectors else "—",
        }
    except Exception as e:
        errors.append(f"板块数据: {e}")
        sections["sectors"] = {"top": [], "cold": [], "leader": "—"}

    # ── 5. 明日预判 ───────────────────────────────────────────
    score = sections["index"].get("score", 50)
    up_r  = sections["breadth"].get("up_ratio", 50)
    zt    = sections["breadth"].get("zt", 0)
    dt    = sections["breadth"].get("dt", 0)
    med   = sections["breadth"].get("median_chg", 0)

    # 综合评分（市场强度）
    strength = min(100, max(0, int(
        score * 0.4 +
        (up_r - 50) * 1.0 +
        min(zt, 150) * 0.15 -
        dt * 2 +
        med * 5 +
        50
    )))

    if strength >= 75:
        outlook_label  = "偏多"
        outlook_action = "大盘强势，明日可积极关注热门板块龙头，轻仓跟进"
        outlook_risk   = "注意高位股回调风险，避免追高"
    elif strength >= 60:
        outlook_label  = "中性偏多"
        outlook_action = "行情健康，选择强势板块核心票，控制仓位30~50%"
        outlook_risk   = "注意板块轮动节奏，追热点须控制成本"
    elif strength >= 45:
        outlook_label  = "震荡"
        outlook_action = "市场方向不明，建议持股观望，不追高"
        outlook_risk   = "缩量整理时避免频繁操作，等待方向确认"
    elif strength >= 30:
        outlook_label  = "偏弱"
        outlook_action = "控制仓位，减少持仓暴露，现金为王"
        outlook_risk   = "避免补仓被套，严格执行止损"
    else:
        outlook_label  = "弱势"
        outlook_action = "建议空仓观望，等待恐慌情绪释放后的企稳信号"
        outlook_risk   = "切勿抄底，等市场出现明确企稳再行动"

    sections["outlook"] = {
        "label":  outlook_label,
        "action": outlook_action,
        "risk":   outlook_risk,
        "strength": strength,
    }

    # ── 6. 生成完整文字报告 ───────────────────────────────────
    full_text = _build_text(today, sections, errors)

    return {
        "date":         today,
        "sections":     sections,
        "full_text":    full_text,
        "score":        strength,
        "generated_at": now,
        "errors":       errors,
    }


def _build_text(date: str, s: dict, errors: list) -> str:
    """把结构化数据拼成可读报告"""
    lines = []
    a = lines.append

    # ── 标题 ─────────────────────────────────────────────────
    a(f"{'─'*62}")
    a(f"  {date}  A股收盘复盘")
    a(f"{'─'*62}")

    # ── 1. 大盘走势 ───────────────────────────────────────────
    a("\n【一、大盘走势】")
    idx = s.get("index", {})
    for name, info in idx.get("indices", {}).items():
        close = info.get("close", 0)
        desc  = info.get("chg_desc", "—")
        pos   = info.get("position", "—")
        ma20  = info.get("ma20", 0)
        ma5   = info.get("ma5", 0)
        dist_ma20 = (close - ma20) / ma20 * 100 if ma20 else 0
        a(f"  {name}  {close:.2f}  {desc}")
        a(f"      均线结构：{pos}")
        a(f"      MA5={ma5:.1f}  MA20={ma20:.1f}  距MA20 {dist_ma20:+.1f}%")

    score = idx.get("score", 50)
    level = idx.get("level", "—")
    a(f"\n  市场情绪评分：{score}/100  [{level}]")

    # ── 2. 市场宽度 ───────────────────────────────────────────
    a("\n【二、市场宽度（全市场 A 股）】")
    br = s.get("breadth", {})
    up    = br.get("up", 0)
    down  = br.get("down", 0)
    flat  = br.get("flat", 0)
    total = br.get("total", up + down + flat)
    zt    = br.get("zt", 0)
    dt    = br.get("dt", 0)
    up_r  = br.get("up_ratio", 50)
    med   = br.get("median_chg", 0)
    mean  = br.get("mean_chg", 0)
    char  = br.get("character", "—")

    a(f"  上涨 {up} 只 / 下跌 {down} 只 / 平盘 {flat} 只  （上涨占比 {up_r:.1f}%）")
    a(f"  涨停 {zt} 只 / 跌停 {dt} 只  涨跌比 {zt}:{dt}")
    a(f"  个股涨幅中位数 {med:+.2f}%  均值 {mean:+.2f}%")

    up5 = s.get("volume", {}).get("up5", 0)
    dn5 = s.get("volume", {}).get("dn5", 0)
    if up5 or dn5:
        a(f"  涨幅>5% 强势股 {up5} 只 / 跌幅>5% 弱势股 {dn5} 只")

    a(f"\n  市场性质：{char}")

    # ── 3. 连板梯队 ───────────────────────────────────────────
    a("\n【三、连板梯队（赚钱效应核心指标）】")
    lb = s.get("lianban", {})
    first = lb.get("first_board", 0)
    high  = lb.get("high_board", 0)
    maxlb = lb.get("max_lianban", 0)
    lb_list = lb.get("lianban_list", [])

    a(f"  今日首板 {first} 只 / 3板以上 {high} 只 / 最高板数 {maxlb} 板")
    if lb_list:
        a("  高位连板股：")
        for item in lb_list:
            bar = "█" * min(item["lianban"], 10)
            a(f"    {item['code']} {item['name']:<8} {item['lianban']}连板  {bar}")

    # 连板梯队解读
    if maxlb >= 5:
        a("  ► 连板龙头高位运行，赚钱效应极强，市场情绪高亢")
        a("    注意：高位板承接意愿是关键，一旦炸板可能引发情绪退潮")
    elif maxlb >= 3:
        a("  ► 连板梯队健康，3板以上龙头稳住，情绪有支撑")
    elif maxlb >= 2:
        a("  ► 连板高度偏低，热点持续性待观察，轻仓参与")
    else:
        a("  ► 市场无强势连板龙头，赚钱效应较弱")

    # ── 4. 板块轮动 ───────────────────────────────────────────
    a("\n【四、板块轮动】")
    sec = s.get("sectors", {})
    top  = sec.get("top", [])
    cold = sec.get("cold", [])

    if top:
        a("  今日强势板块（按热度排名）：")
        for i, t in enumerate(top, 1):
            zt_s = f" 涨停{t['zt']}只" if t["zt"] > 0 else ""
            a(f"    {i}. {t['name']:<16} 涨幅 {t['chg']:+.2f}%{zt_s}")
        leader = top[0]["name"]
        a(f"\n  主线板块：{leader}")
        # 板块集中度判断
        if len(top) >= 2:
            chg_gap = top[0]["chg"] - top[1]["chg"]
            if chg_gap > 1.5:
                a(f"  板块集中度高，资金聚焦在{leader}，主线清晰")
            else:
                a("  多板块齐涨，资金分散，结构性机会为主")

    if cold:
        a("\n  今日弱势板块：")
        for c in cold[:3]:
            a(f"    {c['name']:<16} 涨幅 {c['chg']:+.2f}%")

    # ── 5. 量能结构 ───────────────────────────────────────────
    a("\n【五、量能结构】")
    vol = s.get("volume", {})
    total_amt = vol.get("total_amt_yi", 0)
    avg_vr    = vol.get("avg_vol_ratio", 1)
    buckets   = vol.get("amt_buckets", {})

    if total_amt > 0:
        a(f"  全市场成交额：{total_amt:.0f} 亿元")
        if total_amt > 15000:
            a("  ► 天量级别，情绪极度亢奋，需警惕见顶风险")
        elif total_amt > 10000:
            a("  ► 万亿成交，资金活跃，行情有持续性")
        elif total_amt > 7000:
            a("  ► 成交活跃，市场参与热情高")
        elif total_amt > 4000:
            a("  ► 成交温和，行情延续性有限")
        else:
            a("  ► 缩量，市场观望情绪浓厚")

    if avg_vr > 0:
        a(f"  全市场量比中位数：{avg_vr:.2f}x")
        if avg_vr > 2.0:
            a("  ► 放量明显，主力资金积极参与")
        elif avg_vr > 1.2:
            a("  ► 量比适中，行情健康")
        else:
            a("  ► 缩量整理，等待方向选择")

    if buckets:
        a("  个股成交额分布：")
        for label, cnt in buckets.items():
            pct = cnt / (total) * 100 if (total := sum(buckets.values())) > 0 else 0
            a(f"    {label:<14} {cnt:>5} 只  ({pct:.1f}%)")

    # ── 6. 量化解读 ───────────────────────────────────────────
    a("\n【六、量化综合解读】")
    strength = s.get("outlook", {}).get("strength", 50)
    bar_filled = strength // 5
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    a(f"  市场强度：[{bar}] {strength}/100")

    # 综合判断逻辑
    _insights = []

    # 涨跌结构
    if up_r >= 65 and zt >= 80:
        _insights.append("全面上涨格局，资金做多意愿强烈，赚钱效应极佳")
    elif up_r >= 60 and zt >= 40:
        _insights.append("多头占优，热点板块维持强势，做多逻辑未破坏")
    elif up_r < 45 and dt >= 10:
        _insights.append("多空分歧加剧，下跌股数量偏多，需控制风险")
    elif abs(up_r - 50) < 8:
        _insights.append("涨跌均衡，市场处于横盘蓄势阶段")

    # 连板效应
    if maxlb >= 5:
        _insights.append(f"连板龙头高达{maxlb}板，情绪高峰期，警惕情绪反转")
    elif maxlb >= 3:
        _insights.append(f"连板梯队完整（最高{maxlb}板），热点持续性强")
    elif first < 20 and maxlb <= 1:
        _insights.append("首板数量偏少，市场缺乏赚钱效应，不宜激进")

    # 成交量
    if total_amt > 10000:
        _insights.append(f"万亿成交支撑，市场流动性充裕")
    elif total_amt < 3000 and total_amt > 0:
        _insights.append(f"成交低迷（{total_amt:.0f}亿），机构观望，短期方向不明")

    # 板块集中
    if top and top[0]["chg"] > 3:
        _insights.append(f"{top[0]['name']}领涨超3%，主线明确，跟进龙头策略有效")

    for ins in _insights:
        a(f"  ◆ {ins}")

    # ── 7. 明日策略 ───────────────────────────────────────────
    a("\n【七、明日策略】")
    out = s.get("outlook", {})
    a(f"  市场倾向：{out.get('label', '—')}")
    a(f"  操作建议：{out.get('action', '—')}")
    a(f"  风险提示：{out.get('risk', '—')}")

    # 具体方向
    if top:
        a(f"\n  重点关注板块：")
        for t in top[:3]:
            a(f"    · {t['name']}  今日涨幅 {t['chg']:+.2f}%，明日观察能否持续")

    a(f"\n  复盘核心问题：")
    a(f"    1. 今日{sec.get('leader','主线板块')}是否持续？明日是否有接力资金？")
    a(f"    2. 连板龙头明日高开低走还是继续封板？是情绪转折信号？")
    a(f"    3. 大盘成交能否维持？缩量则谨慎，放量则积极。")

    if errors:
        a(f"\n  [部分数据获取失败，结果仅供参考: {'; '.join(errors[:2])}]")

    a(f"\n{'─'*62}")

    return "\n".join(lines)
