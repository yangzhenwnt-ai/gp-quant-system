"""
多区块实时仪表盘 —— 基于 rich 库
布局：
  ┌─ A 大盘情绪 ──┐ ┌─ B 实时快讯 ──────────────┐
  │               │ │                             │
  └───────────────┘ └─────────────────────────────┘
  ┌─ C 盘中异动 ──┐ ┌─ D 持仓跟踪 ──────────────┐
  │               │ │                             │
  └───────────────┘ └─────────────────────────────┘
  ┌─ E 热门选股（整行）──────────────────────────┐
  │                                               │
  └───────────────────────────────────────────────┘

运行：
  python tui_dashboard.py                  # 默认10分钟全局刷新
  python tui_dashboard.py --interval 5     # 5分钟刷新
  python tui_dashboard.py --no-pick        # 跳过选股（更快）
"""
import sys, os, time, argparse, warnings
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

# 跨平台编码修正（Windows 终端默认 CP936）
from core.config_loader import fix_stdout_encoding
fix_stdout_encoding()

# 跨平台键盘输入
from core.keyboard import getch, kbhit, flush_input, _IS_WIN

import logging
logging.basicConfig(level=logging.WARNING)
warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)
import pandas as pd

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.columns import Columns
from rich import box

console = Console()

# ── 缓存，避免每次都重新拉取慢数据 ──────────────────────────
_cache = {
    "pulse":     {"data": None, "ts": 0},
    "news":      {"data": None, "ts": 0},
    "scan":      {"data": None, "ts": 0},
    "portfolio": {"data": None, "ts": 0},
    "pick":      {"data": None, "ts": 0},
}

def is_trading():
    n = datetime.now()
    if n.weekday() >= 5: return False
    h, m = n.hour, n.minute
    return (9*60+15 <= h*60+m <= 11*60+30) or (13*60 <= h*60+m <= 15*60)


# ════════════════════════════════════════════════════════════
# A 区块：大盘情绪
# ════════════════════════════════════════════════════════════
def fetch_pulse():
    try:
        from market.market_pulse import run_market_pulse
        return run_market_pulse()
    except Exception as e:
        return {"error": str(e)}

def render_pulse(data) -> Panel:
    from rich.console import Group
    TITLE = "[bold bright_white on blue] A [/][bold cyan] 大盘情绪[/]"
    if not data or "error" in data:
        return Panel(Text(f"  ✗ {data.get('error','无数据') if data else '无数据'}", style="red"),
                     title=TITLE, border_style="blue")

    s   = data["sentiment"]
    ov  = data["overview"]
    br  = data["breadth"]
    vol = data["volume"]

    score = int(s["score"])
    filled = score // 5
    lvl_style = "bold green" if score >= 55 else ("bold yellow" if score >= 40 else "bold red")
    bar = Text()
    bar.append("  ")
    bar.append("█" * filled, style="green")
    bar.append("░" * (20 - filled), style="bright_black")
    bar.append(f" {score:3d}/100  ")
    bar.append(f"{s['emoji']} {s['level']}", style=lvl_style)
    bar.append(f"\n  {s['advice']}\n", style="yellow")

    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold bright_black",
                padding=(0, 1), show_edge=False)
    tbl.add_column("指数", width=9)
    tbl.add_column("现价",  justify="right", width=8)
    tbl.add_column("涨跌",  justify="right", width=8)
    tbl.add_column("MA20",  justify="right", width=8)
    tbl.add_column("",      width=3)
    for info in ov.values():
        chg = info.get("change_pct", 0)
        cs  = "green" if chg >= 0 else "red"
        arrow = "[green]▲[/]" if info.get("above_ma20") else "[red]▼[/]"
        tbl.add_row(
            info["name"],
            f"{info['close']:.2f}",
            f"[{cs}]{chg:+.2f}%[/]",
            f"[bright_black]{info['ma20']:.2f}[/]",
            arrow,
        )

    zt = br.get("zt", 0); dt = br.get("dt", 0)
    up = br.get("up", 0); dn = br.get("down", 0)
    amt   = vol.get("total_amount_yi", 0)
    north = vol.get("north_flow_yi", 0)
    zt_list = br.get("zt_list", [])

    foot = Text()
    foot.append("\n  ")
    foot.append(f"涨停 {zt}", style="green bold"); foot.append("  ")
    foot.append(f"跌停 {dt}", style="red bold");   foot.append("    ")
    foot.append(f"↑{up}", style="green");          foot.append(" / ")
    foot.append(f"↓{dn}", style="red")
    if amt:
        ac = "green" if amt >= 8000 else ("yellow" if amt >= 5000 else "red")
        ns = "green" if north >= 0 else "red"
        foot.append(f"\n  成交 "); foot.append(f"{int(amt)}亿", style=ac)
        foot.append("   北向 ");  foot.append(f"{north:+.1f}亿", style=ns)
    if zt_list:
        names = "  ".join(n for _, n in zt_list[:5])
        foot.append(f"\n  [bright_black]涨停:[/] [green]{names}[/]")

    return Panel(Group(bar, tbl, foot), title=TITLE, border_style="blue")


# ════════════════════════════════════════════════════════════
# B 区块：实时快讯
# ════════════════════════════════════════════════════════════

# AI分析结果缓存：title_hash → ai_result dict
_news_ai_cache: dict[str, dict] = {}

def _ai_enrich_item(item: dict) -> None:
    """在后台线程中用 Ollama 分析一条新闻，结果写入 item['ai']。"""
    title = item.get("title", "")
    key   = title[:80]
    if key in _news_ai_cache:
        item["ai"] = _news_ai_cache[key]
        return
    try:
        from news.ai_analyzer import analyze_with_ollama
        from news.sector_stock_linker import find_stocks_for_news
        ai = analyze_with_ollama(title, title)
        if ai:
            stocks = find_stocks_for_news(
                benefit_sectors = ai.get("benefit_sectors", []) + item.get("sectors", []),
                benefit_stocks  = ai.get("benefit_stocks",  []),
                sentiment       = item.get("sent", 1.0),
                top_n           = 6,
            )
            ai["stocks"] = stocks
        _news_ai_cache[key] = ai
        item["ai"] = ai
    except Exception as e:
        item["ai"] = {}

def fetch_news(max_age=40):
    try:
        from news.news_fetcher import fetch_latest_news
        from news.keyword_map import match_sectors, score_sentiment, score_urgency
        news_df = fetch_latest_news(max_age_minutes=max_age)
        items = []
        if news_df.empty:
            return items
        for _, row in news_df.iterrows():
            title   = str(row.get("title", "")).strip()
            content = str(row.get("content", title))
            source  = str(row.get("source", ""))
            ts      = str(row.get("time", ""))[:16]
            if not title: continue
            full    = title + " " + content
            sectors = match_sectors(full)
            sent    = score_sentiment(full)
            urg     = score_urgency(full)
            if not sectors and urg < 2: continue
            item = {"ts": ts, "source": source, "title": title,
                    "sectors": sectors, "sent": sent, "urg": urg, "ai": {}}
            # 加入已有AI缓存
            key = title[:80]
            if key in _news_ai_cache:
                item["ai"] = _news_ai_cache[key]
            items.append(item)
            if len(items) >= 15: break

        # 对紧急度≥2的新闻，后台异步AI分析（不阻塞刷新）
        import threading
        for item in items:
            if item.get("urg", 0) >= 2 and not item.get("ai"):
                t = threading.Thread(target=_ai_enrich_item, args=(item,), daemon=True)
                t.start()

        return items
    except Exception as e:
        return [{"error": str(e)}]

def render_news(items) -> Panel:
    TITLE = "[bold bright_white on orange3] B [/][bold yellow] 实时快讯[/]"
    t = Text()
    if not items:
        t.append("  近40分钟无重要快讯", style="bright_black")
        return Panel(t, title=TITLE, border_style="orange3")
    if items and "error" in items[0]:
        t.append(f"  ✗ {items[0]['error']}", style="red")
        return Panel(t, title=TITLE, border_style="orange3")

    for item in items[:12]:
        sent = item["sent"]; urg = item["urg"]
        if sent > 0.3:
            sent_icon, sent_s = "▲", "green"
        elif sent < -0.3:
            sent_icon, sent_s = "▼", "red"
        else:
            sent_icon, sent_s = "·", "bright_black"
        urg_s = "bold red" if urg >= 3 else ("yellow" if urg >= 2 else "bright_black")

        t.append(f"  {item['ts'][-5:]} ", style="bright_black")
        t.append(sent_icon, style=sent_s)
        t.append(" ")
        if urg >= 3:
            t.append("❗", style=urg_s)
        t.append(f"{item['title'][:55]}\n")
        ai = item.get("ai", {})
        ai_stocks = ai.get("stocks", []) if ai else []
        if ai_stocks:
            codes_str = " ".join(s.get("code","") for s in ai_stocks[:4])
            t.append(f"    ├ AI股: {codes_str}\n", style="cyan dim")
        if item.get("sectors"):
            t.append(f"    └ {', '.join(item['sectors'][:4])}\n", style="bright_black")

    return Panel(t, title=TITLE, border_style="orange3")


# ════════════════════════════════════════════════════════════
# C 区块：盘中异动
# ════════════════════════════════════════════════════════════
def fetch_scan():
    if not is_trading():
        return {"off": True}
    try:
        from market.intraday_scanner import run_intraday_scan
        return run_intraday_scan()
    except Exception as e:
        return {"error": str(e)}

def render_scan(data) -> Panel:
    TITLE = "[bold bright_white on dark_violet] C [/][bold magenta] 盘中异动[/]"
    t = Text()
    if not data:
        t.append("  无数据", style="bright_black")
        return Panel(t, title=TITLE, border_style="dark_violet")
    if "off" in data:
        t.append("  💤 非交易时段", style="bright_black")
        return Panel(t, title=TITLE, border_style="dark_violet")
    if "error" in data:
        t.append(f"  ✗ {data['error']}", style="red")
        return Panel(t, title=TITLE, border_style="dark_violet")

    zt = data.get("zt_stocks"); dt = data.get("dt_stocks")
    vs = data.get("vol_surge"); fs = data.get("fund_surge")

    has_content = False
    if zt is not None and not zt.empty:
        names = " ".join(str(r.get("name","")) for _, r in zt.head(8).iterrows())
        t.append(f"  🔴 涨停 ", style="bold green"); t.append(f"{len(zt)}只", style="bold green")
        t.append(f"  {names}\n", style="green")
        has_content = True
    if dt is not None and not dt.empty:
        names = " ".join(str(r.get("name","")) for _, r in dt.head(5).iterrows())
        t.append(f"  🟢 跌停 ", style="bold red"); t.append(f"{len(dt)}只", style="bold red")
        t.append(f"  {names}\n", style="red")
        has_content = True

    if vs is not None and not vs.empty:
        t.append("\n  ⚡ 放量拉升（量比>3x）\n", style="bold yellow")
        for _, r in vs.head(5).iterrows():
            chg = r.get("change_pct", 0)
            cs = "green" if chg > 0 else "red"
            t.append(f"    {r.get('code',''):<8} {str(r.get('name','')):<8}")
            t.append(f" [{cs}]{chg:+.2f}%[/{cs}]")
            t.append(f"  {r.get('vol_ratio',0):.1f}x\n", style="yellow")
        has_content = True

    if fs is not None and not fs.empty:
        t.append("\n  💰 超大单净流入\n", style="bold cyan")
        for _, r in fs.head(5).iterrows():
            chg  = r.get("change_pct", 0)
            flow = r.get("super_flow", 0)
            cs = "green" if chg > 0 else "red"
            t.append(f"    {r.get('code',''):<8} {str(r.get('name','')):<8}")
            t.append(f" [{cs}]{chg:+.2f}%[/{cs}]")
            t.append(f"  +{flow/1e8:.2f}亿\n", style="cyan")
        has_content = True

    if not has_content:
        t.append("  暂无异动", style="bright_black")

    return Panel(t, title=TITLE, border_style="dark_violet")


# ════════════════════════════════════════════════════════════
# D 区块：持仓跟踪
# ════════════════════════════════════════════════════════════
def fetch_portfolio():
    try:
        from market.portfolio_tracker import analyze_portfolio, load_portfolio
        p = load_portfolio()
        if not p:
            return []
        # loader=None → analyze_portfolio 只用实时行情，不尝试历史数据
        reports = analyze_portfolio(loader=None)
        return reports
    except Exception as e:
        logger.warning(f"fetch_portfolio 失败: {e}", exc_info=True)
        return [{"error": str(e)}]

# 持仓风险扫描缓存（后台异步更新，不阻塞渲染）
_portfolio_risk_cache: dict[str, dict] = {}   # code -> risk_result

def _refresh_portfolio_risk(reports: list[dict], sync: bool = False):
    """
    对持仓股票做暴雷扫描，结果写入 _portfolio_risk_cache。
    sync=True  → 同步执行（启动时用，确保首帧就有数据）
    sync=False → 后台线程（刷新时用，不阻塞界面）
    """
    from data.risk_scanner import scan_stock_risk

    def _worker():
        for r in (reports or []):
            code = r.get("code", "")
            if not code:
                continue
            try:
                result = scan_stock_risk(code, use_cache=True)
                _portfolio_risk_cache[code] = result
            except Exception:
                pass

    if sync:
        try:
            _worker()
        except Exception:
            pass
    else:
        import threading
        threading.Thread(target=_worker, daemon=True).start()


def render_portfolio(reports) -> Panel:
    from rich.console import Group
    TITLE = "[bold bright_white on green] D [/][bold green] 持仓跟踪[/]"
    if not reports:
        t = Text("  暂无持仓  按 D 添加", style="bright_black")
        return Panel(t, title=TITLE, border_style="green")
    if reports and "error" in reports[0]:
        t = Text(f"  ✗ {reports[0]['error']}", style="red")
        return Panel(t, title=TITLE, border_style="green")

    total_cost = sum(r["cost"] * r["shares"] for r in reports)
    total_val  = sum(r["mkt_val"] for r in reports)
    total_pnl  = total_val - total_cost
    pct        = total_pnl / total_cost * 100 if total_cost else 0
    pnl_s      = "bold green" if total_pnl >= 0 else "bold red"

    # 检查是否有高风险持仓
    high_risk_codes = [
        code for code, rr in _portfolio_risk_cache.items()
        if rr.get("risk_score", 0) >= 50
    ]

    header = Text()
    header.append(f"  市值 ¥{total_val:,.0f}   盈亏 ", style="bright_black")
    header.append(f"¥{total_pnl:+,.0f} ({pct:+.1f}%)", style=pnl_s)
    if high_risk_codes:
        header.append(f"   ⚠ {len(high_risk_codes)}只高风险", style="bold red")
    header.append("\n")

    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold bright_black",
                padding=(0, 1), show_edge=False, expand=False)
    tbl.add_column("代码",    width=7)
    tbl.add_column("名称",    width=7)
    tbl.add_column("成本",    justify="right", width=7)
    tbl.add_column("现价",    justify="right", width=7)
    tbl.add_column("今涨",    justify="right", width=7)
    tbl.add_column("盈亏",    justify="right", width=8)
    tbl.add_column("建议/风险", width=11)

    for r in reports:
        ps  = "green" if r["pnl_pct"]    >= 0 else "red"
        cs  = "green" if r.get("change_pct", 0) >= 0 else "red"
        ac  = "green" if r["advice_color"] == "green" else (
              "red"   if r["advice_color"] == "red"   else "yellow")
        advice_icon = {"继续持有": "✓", "建议减仓": "△", "注意观察": "?", "立刻清仓": "X"}.get(r["advice"], "·")

        # 风险徽章（嵌入建议列）
        rr = _portfolio_risk_cache.get(r["code"], {})
        rscore = rr.get("risk_score", -1)
        if rscore < 0:
            risk_str = "[dim]—[/]"
        elif rscore >= 70:
            risk_str = f"[bold red]⚠暴{rscore}[/]"
        elif rscore >= 50:
            risk_str = f"[yellow]△危{rscore}[/]"
        elif rscore >= 30:
            risk_str = f"[yellow]!注{rscore}[/]"
        else:
            risk_str = f"[dim green]✓安全[/]"

        tbl.add_row(
            r["code"],
            str(r["name"])[:5],
            f"{r['cost']:.2f}",
            f"{r['price']:.2f}",
            f"[{cs}]{r.get('change_pct',0):+.1f}%[/]",
            f"[{ps}]{r['pnl_pct']:+.1f}%[/]",
            f"[{ac}]{advice_icon}[/] {risk_str}",
        )
        # 信号提示行：暴雷信号优先，否则显示卖出信号
        rr_sigs = [s for s in rr.get("signals", []) if s.startswith("⚠")]
        if rr_sigs:
            tbl.add_row("", f"[bold red]{rr_sigs[0][:48]}[/]", "", "", "", "", "")
        else:
            for urgency, reason in (r.get("sell_signals") or [])[:1]:
                icon  = "⚠" if urgency == "SELL_NOW" else "△"
                style = "red" if urgency == "SELL_NOW" else "yellow"
                tbl.add_row("", f"[{style}]{icon} {reason[:46]}[/]", "", "", "", "", "")

    foot = Text.from_markup("\n  [bright_black]按 D 管理持仓 · R 风险详扫 · I 个股分析  ┃  ✓安全 !关注 △中危 ⚠暴雷[/]")
    return Panel(Group(header, tbl, foot), title=TITLE, border_style="green")


# ════════════════════════════════════════════════════════════
# E 区块：热门选股
# ════════════════════════════════════════════════════════════
_pick_cache = {"text": None, "ts": 0}

def fetch_pick(market_score=50, pick_interval_min=10):
    """
    E区块：热门板块龙头股
    缓存10分钟（原30分钟），加入实时量能因子让排名盘中动态变化
    """
    global _pick_cache
    now = time.time()
    if _pick_cache["text"] and (now - _pick_cache["ts"]) < pick_interval_min * 60:
        return _pick_cache["text"]

    if market_score < 35:
        return [{"warn": "大盘情绪极弱，今日不建议追涨"}]

    try:
        from selector.sector_heat import rank_hot_sectors
        from selector.stock_picker import pick_leaders, get_lhb_stocks, get_zt_info
        from market.timing import evaluate_timing
        from data.reliable_api import API
        import re

        today      = datetime.today().strftime("%Y-%m-%d")
        data_start = (pd.Timestamp.today() - pd.DateOffset(months=8)).strftime("%Y-%m-%d")

        hot = pd.DataFrame()
        for _attempt in range(4):
            hot = rank_hot_sectors(top_n=3)
            if not hot.empty:
                break
            wait = 2 ** _attempt
            logger.warning(f"板块数据为空，{wait}s后重试(第{_attempt+1}次)")
            time.sleep(wait)
        if hot.empty:
            return [{"error": "板块数据获取失败，请稍后重试"}]

        lhb_codes = get_lhb_stocks(days=3)
        zt_info   = get_zt_info()

        # 获取实时行情（用于量能二次排序）
        spot_df = API.spot()
        spot_map = {}
        if not spot_df.empty:
            for _, row in spot_df.iterrows():
                spot_map[str(row.get("code", ""))] = {
                    "vol_ratio": float(row.get("vol_ratio", 1) or 1),
                    "turnover":  float(row.get("turnover",  0) or 0),
                    "amount":    float(row.get("amount",    0) or 0),
                    "price":     float(row.get("price",     0) or 0),
                }

        empty_df = pd.DataFrame()
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # ── 并行获取各板块龙头 ────────────────────────────────
        def _fetch_sector(row):
            sname  = row["sector_name"]
            stype  = row.get("sector_type", "行业资金流")
            slabel = row.get("sina_label", None)
            leaders = pick_leaders(sname, stype, empty_df, empty_df,
                                   lhb_codes, zt_info, top_n=5, sina_label=slabel)
            return sname, stype, slabel, row, leaders

        sector_rows = [row for _, row in hot.iterrows()]
        with ThreadPoolExecutor(max_workers=len(sector_rows)) as pool:
            sector_futures = {pool.submit(_fetch_sector, r): r for r in sector_rows}
            sector_results = {}
            for fut in as_completed(sector_futures):
                try:
                    sname, stype, slabel, row, leaders = fut.result()
                    sector_results[sname] = (row, leaders)
                except Exception as e:
                    logger.warning(f"板块并行获取失败: {e}")

        # ── 二次量能排序，收集需要获取历史的股票 ────────────────
        # {code: (stock_dict, sector_name)}
        history_needed = {}
        sector_top3 = {}   # sector_name → [(realtime_score, stock, rt, price, vr)]

        for sname, (row, leaders) in sector_results.items():
            if leaders is None or leaders.empty:
                sector_top3[sname] = []
                continue
            scored = []
            for _, stock in leaders.iterrows():
                code  = stock["code"]
                rt    = spot_map.get(code, {})
                vr    = rt.get("vol_ratio", 1)
                turn  = rt.get("turnover",  0)
                price = rt.get("price",     0)
                vol_bonus  = 10 if 1.5 <= vr <= 3.0 else (5 if 1.0 <= vr < 1.5 else 0)
                turn_bonus = 8 if 1 <= turn <= 5 else 0
                realtime_score = float(stock.get("total_score", 0)) * 100 + vol_bonus + turn_bonus
                scored.append((realtime_score, stock, rt, price, vr))
            scored.sort(key=lambda x: -x[0])
            top3 = scored[:3]
            sector_top3[sname] = top3
            for _, stock, rt, price, vr in top3:
                code = stock["code"]
                if code not in history_needed:
                    history_needed[code] = stock

        # ── 并行获取历史K线 ───────────────────────────────────
        def _fetch_hist(code):
            try:
                hist = API.history(code, data_start, today)
                if hist is not None and len(hist) >= 60:
                    return code, evaluate_timing(code, hist)
            except Exception:
                pass
            return code, {}

        timing_map = {}
        if history_needed:
            with ThreadPoolExecutor(max_workers=min(len(history_needed), 4)) as pool:
                hist_futures = {pool.submit(_fetch_hist, code): code for code in history_needed}
                for fut in as_completed(hist_futures):
                    try:
                        code, timing_ev = fut.result()
                        timing_map[code] = timing_ev
                    except Exception:
                        pass

        # ── 按原板块排名组装结果 ──────────────────────────────
        results = []
        for rank, row in hot.iterrows():
            sname = row["sector_name"]
            sector_info = {
                "sector": sname,
                "flow":   row["flow_today_yi"],
                "heat":   row["heat_score"],
                "stocks": [],
            }
            top3 = sector_top3.get(sname, [])
            for rs, stock, rt, price, vr in top3:
                code    = stock["code"]
                name    = stock.get("name", "")
                chg     = stock.get("change_pct", 0)
                lianban  = int(stock.get("lianban", 0))
                in_lhb   = bool(stock.get("in_lhb", False))
                is_zt    = bool(stock.get("is_zt", False))
                zt_label = str(stock.get("zt_label", ""))
                sector_info["stocks"].append({
                    "code":      code,
                    "name":      name,
                    "price":     price,
                    "chg":       chg,
                    "vol_ratio": vr,
                    "lianban":   lianban,
                    "in_lhb":    in_lhb,
                    "is_zt":     is_zt,
                    "zt_label":  zt_label,
                    "timing":    timing_map.get(code, {}),
                })
            results.append(sector_info)

        _pick_cache["text"] = results
        _pick_cache["ts"]   = time.time()
        return results
    except Exception as e:
        logger.warning(f"fetch_pick 失败: {e}", exc_info=True)
        return [{"error": str(e)}]

def render_pick(data, pick_interval_min=10) -> Panel:
    from rich.console import Group
    TITLE = "[bold bright_white on cyan] E [/][bold cyan] 热门板块选股[/]"
    age_min = int((time.time() - _pick_cache["ts"]) / 60) if _pick_cache["ts"] else 0
    nxt = max(0, pick_interval_min - age_min)

    if not data:
        return Panel(Text("  ⏳ 加载中...", style="bright_black"), title=TITLE, border_style="cyan")
    if data and "error" in data[0]:
        return Panel(Text(f"  ✗ {data[0]['error']}", style="red"), title=TITLE, border_style="cyan")
    if data and "warn" in data[0]:
        return Panel(Text(f"  ⚠ {data[0]['warn']}", style="yellow"), title=TITLE, border_style="cyan")

    cache_note = f"{nxt}分钟后更新" if nxt > 0 else "即将更新"

    # 每个板块用一个 Table，横排展示所有板块
    tables = []
    for sector in data:
        flow   = sector.get("flow", 0)
        heat   = sector.get("heat", 0)
        flow_s = "green" if flow > 0 else "red"

        tbl = Table(
            title=f"[bold cyan]{sector['sector']}[/]  [bright_black]热度{heat:.2f}[/]  "
                  f"[{flow_s}]{flow:+.1f}亿[/]",
            box=box.SIMPLE_HEAD, show_header=True,
            header_style="bold bright_black", padding=(0, 1),
            title_justify="left", show_edge=True, border_style="bright_black",
        )
        tbl.add_column("代码",  width=7)
        tbl.add_column("名称",  width=7)
        tbl.add_column("涨跌",  justify="right", width=6)
        tbl.add_column("量比",  justify="right", width=5)
        tbl.add_column("标签",  width=10)

        for stock in sector.get("stocks", []):
            chg = float(stock.get("chg", 0))
            vr  = float(stock.get("vol_ratio", 1))
            lb  = stock.get("lianban", 0)
            lhb = stock.get("in_lhb", False)
            ev  = stock.get("timing", {})

            cs   = "green" if chg >= 0 else "red"
            vr_s = "green" if 1.5 <= vr <= 3.0 else ("yellow" if vr > 3 else "bright_black")

            zt_label = stock.get("zt_label", "")
            is_zt    = stock.get("is_zt", False)

            tags = []
            # 涨停标注优先展示（最重要的信息）
            if zt_label == "次日可博":
                tags.append("[bold yellow]涨停·次日可博[/]")
            elif zt_label == "次日观察":
                tags.append("[yellow]涨停·次日观察[/]")
            elif zt_label == "次日谨慎":
                tags.append("[dim yellow]涨停·谨慎[/]")
            elif zt_label == "高风险":
                tags.append("[red]涨停·高风险[/]")
            elif is_zt:
                tags.append("[yellow]涨停[/]")
            elif lb >= 2:
                tags.append(f"[yellow]{lb}连板[/]")
            if lhb:       tags.append("[magenta]龙虎[/]")
            if not zt_label and isinstance(ev, dict) and ev.get("rating"):
                rc = ev.get("rating_color", "bright_black")
                tags.append(f"[{rc}]{ev['rating']}[/]")

            tbl.add_row(
                stock.get("code", ""),
                str(stock.get("name", ""))[:6],
                f"[{cs}]{chg:+.1f}%[/]",
                f"[{vr_s}]{vr:.1f}[/]",
                " ".join(tags) if tags else "[bright_black]—[/]",
            )
        tables.append(tbl)

    foot = Text.from_markup(f"\n  [bright_black]按 E 详情 · I 个股分析 · W 加观察池 · {cache_note}[/]")
    return Panel(Group(Columns(tables, equal=True, expand=True), foot),
                 title=TITLE, border_style="cyan")


# ════════════════════════════════════════════════════════════
# F 区块：全市场优质股扫描
# ════════════════════════════════════════════════════════════
_quality_cache = {"data": None, "ts": 0}

def fetch_quality(interval_min: int = 60):
    """获取全市场优质股扫描结果，间隔60分钟刷新（历史数据拉取较慢）"""
    global _quality_cache
    now = time.time()
    if _quality_cache["data"] and (now - _quality_cache["ts"]) < interval_min * 60:
        return _quality_cache["data"]
    try:
        from selector.quality_scanner import scan_quality_stocks
        result = scan_quality_stocks(mode="both", top_n=15)
        if result:
            _quality_cache["data"] = result
            _quality_cache["ts"]   = now
        return result
    except Exception as e:
        logger.warning(f"优质股扫描失败: {e}", exc_info=True)
        err = {"error": str(e)}
        # 如果有旧缓存，优先返回旧数据而不是错误
        if _quality_cache["data"]:
            return _quality_cache["data"]
        return err


def _risk_badge_cached(code: str) -> str:
    """读取风险缓存返回 Rich 标记徽章；无缓存时返回空字符串"""
    try:
        import pickle
        cp = Path(__file__).parent / "cache" / "risk" / f"{code}_risk.pkl"
        if cp.exists():
            rr = pickle.loads(cp.read_bytes())
            sc = rr.get("risk_score", 0)
            if sc >= 70: return f"[bold red]⚠{sc}[/]"
            if sc >= 50: return f"[yellow]△{sc}[/]"
            if sc >= 30: return f"[yellow]!{sc}[/]"
            return f"[dim green]✓[/]"
    except Exception:
        pass
    return ""


def render_quality(data, interval_min: int = 60) -> Panel:
    from rich.console import Group
    TITLE = "[bold bright_white on magenta] F [/][bold magenta] 全市场优质股[/]"

    age_min = int((time.time() - _quality_cache["ts"]) / 60) if _quality_cache["ts"] else 0
    nxt     = max(0, interval_min - age_min)
    scan_ts = (f"扫描于 {datetime.fromtimestamp(_quality_cache['ts']).strftime('%H:%M')}  "
               f"{nxt}分钟后更新") if _quality_cache["ts"] else ""

    if not data:
        t = Text(f"  ⏳ 首次扫描中（约需1-2分钟）...  {scan_ts}", style="bright_black")
        return Panel(t, title=TITLE, border_style="magenta")
    if "error" in data:
        t = Text(f"  ✗ {data['error']}", style="red")
        return Panel(t, title=TITLE, border_style="magenta")

    momentum = data.get("momentum", pd.DataFrame())
    value    = data.get("value",    pd.DataFrame())

    def _make_tbl(df, label, hdr_style, show_rs=False):
        tbl = Table(
            title=label, box=box.SIMPLE_HEAD, show_header=True,
            header_style=f"bold {hdr_style}", padding=(0, 1),
            title_justify="left", show_edge=True, border_style="bright_black",
        )
        tbl.add_column("代码",  width=7)
        tbl.add_column("名称",  width=6)
        tbl.add_column("现价",  justify="right", width=6)
        tbl.add_column("今涨",  justify="right", width=6)
        tbl.add_column("评分",  justify="right", width=5)
        tbl.add_column("均线" if not show_rs else "偏MA20", justify="right", width=6)
        tbl.add_column("风险", width=5)

        if df is None or df.empty:
            tbl.add_row("—", "暂无", "", "", "", "", "")
            return tbl

        for _, r in df.head(10).iterrows():
            chg   = r.get("chg", 0)
            score = r.get("total_score", 0)
            code  = str(r.get("code", ""))
            cs    = "green" if chg >= 0 else "red"
            sc    = "green" if score >= 70 else ("yellow" if score >= 60 else "bright_black")
            if show_rs:
                rs   = r.get("rs_pct", 0)
                rs_s = "cyan" if -3 < rs < 5 else ("yellow" if rs < 10 else "bright_black")
                ma_col = f"[{rs_s}]{rs:+.1f}%[/]"
            else:
                ma_col = "[green]多头[/]" if r.get("ma_align") else "[bright_black]—[/]"
            risk_badge = _risk_badge_cached(code)
            tbl.add_row(
                code,
                str(r.get("name", ""))[:5],
                f"{r.get('price', 0):.2f}",
                f"[{cs}]{chg:+.1f}%[/]",
                f"[{sc}]{score:.0f}[/]",
                ma_col,
                risk_badge,
            )
        return tbl

    tbl_m = _make_tbl(momentum, "[green]▲ 强势股（突破+资金）[/]", "green", show_rs=False)
    tbl_v = _make_tbl(value,    "[cyan]◆ 低位股（低估+趋势）[/]", "cyan",  show_rs=True)

    foot = Text.from_markup(f"\n  [bright_black]按 F 详情 · I 个股分析(顺带扫描暴雷风险) · W 加观察池  ·  {scan_ts}[/]")
    return Panel(
        Group(Columns([tbl_m, tbl_v], equal=True, expand=True), foot),
        title=TITLE, border_style="magenta",
    )


def print_quality_detail(quality_data):
    """F区块详情页：完整输出所有候选股，含风险收益比过滤+连续上榜+仓位建议"""
    if not quality_data:
        console.print("[yellow]扫描仍在后台进行中（约需2-5分钟，正在拉取历史数据）...[/]")
        console.print("[dim]提示：按 Q 返回主界面，稍后再按 F 查看结果[/]")
        return
    if "error" in quality_data:
        console.print(f"[red]{quality_data['error']}[/]"); return

    momentum = quality_data.get("momentum", pd.DataFrame())
    value    = quality_data.get("value",    pd.DataFrame())
    scan_time = quality_data.get("scan_time", "--:--")

    # 连续上榜数据
    try:
        from data.tracker import get_consecutive_picks, record_picks
        consecutive = get_consecutive_picks(days=5)
    except Exception:
        consecutive = {}

    def _levels(price, ma5, ma20, mode="momentum"):
        """计算买入区、止损、目标、风险收益比"""
        if mode == "momentum":
            buy = round(ma5 * 1.005, 2)
            stop = round(ma20 * 0.97, 2)
            target = round(price * 1.18, 2)
        else:
            buy = round(ma20 * 1.005, 2)
            stop = round(ma20 * 0.97, 2)
            target = round(price * 1.13, 2)
        gain = target - buy
        loss = buy - stop
        rr   = round(gain / loss, 2) if loss > 0 else 0
        return buy, stop, target, rr

    def _pos_label(price, ma5, chg):
        gap = (price - ma5) / ma5 * 100 if ma5 else 0
        if chg > 4:
            return "今日大涨等回踩", "yellow"
        if gap <= 1.5:
            return "紧贴MA5 可建仓", "green"
        if gap <= 3.5:
            return "略高MA5 等回踩", "cyan"
        return "远离MA5 暂观望", "red"

    def _position_size(rr: float, score: float, pos_label: str) -> str:
        """根据盈亏比+评分给出仓位建议"""
        if "暂观望" in pos_label:
            return "0%"
        if rr >= 2.5 and score >= 85:
            return "10~15%"
        if rr >= 2.0 and score >= 75:
            return "5~10%"
        if rr >= 1.5:
            return "3~5%"
        return "观望"

    console.print(f"\n[dim]扫描时间: {scan_time}   最低盈亏比门槛: 1.5[/]\n")

    def _render_section(df, mode, title, strategy_hint, header_color):
        if df is None or df.empty:
            return
        console.print(f"[bold {header_color}]{title}[/]")
        console.print(f"[dim]  {strategy_hint}[/]\n")

        tbl = Table(box=box.ROUNDED, show_header=True, header_style=f"bold {header_color}")
        tbl.add_column("#",       width=3,  justify="right")
        tbl.add_column("代码",    width=8)
        tbl.add_column("名称",    width=7)
        tbl.add_column("现价",    justify="right", width=7)
        tbl.add_column("今涨",    justify="right", width=7)
        tbl.add_column("综合分",  justify="right", width=6)
        tbl.add_column("暴雷风险",width=7)
        tbl.add_column("建议买入",justify="right", width=7)
        tbl.add_column("止损价",  justify="right", width=7)
        tbl.add_column("目标价",  justify="right", width=7)
        tbl.add_column("盈亏比",  justify="right", width=6)
        tbl.add_column("建议仓位",justify="right", width=8)
        tbl.add_column("当前位置",width=14)
        tbl.add_column("连续上榜",justify="right", width=8)

        # 自动记录到tracker
        track_list = []
        row_num = 0
        for _, r in df.iterrows():
            price = float(r.get("price", 0))
            ma5   = float(r.get("ma5",   0)) or price
            ma20  = float(r.get("ma20",  0)) or price * 0.95
            chg   = float(r.get("chg",   0))
            sc    = float(r.get("total_score", 0))
            code  = str(r.get("code", ""))

            buy, stop, target, rr = _levels(price, ma5, ma20, mode)

            # 风险收益比 < 1.5 降级显示（不过滤，让用户自己判断）
            rr_c = "green" if rr >= 2.0 else ("yellow" if rr >= 1.5 else "red")
            sc_s = "green" if sc >= 80 else "yellow"
            chg_s = "green" if chg > 0 else "red"
            pos_label, pos_color = _pos_label(price, ma5, chg)
            pos_size = _position_size(rr, sc, pos_label)
            streak = consecutive.get(code, 0)
            streak_s = f"[bold yellow]{streak}天[/]" if streak >= 2 else "[dim]首次[/]"

            risk_badge = _risk_badge_cached(code) or "[dim]—[/]"
            row_num += 1
            tbl.add_row(
                str(row_num),
                code,
                str(r.get("name", ""))[:6],
                f"{price:.2f}",
                f"[{chg_s}]{chg:+.1f}%[/]",
                f"[{sc_s}]{sc:.0f}[/]",
                risk_badge,
                f"[cyan]{buy:.2f}[/]",
                f"[red]{stop:.2f}[/]",
                f"[green]{target:.2f}[/]",
                f"[{rr_c}]{rr:.1f}[/]",
                f"[green]{pos_size}[/]" if pos_size not in ("0%", "观望") else f"[dim]{pos_size}[/]",
                f"[{pos_color}]{pos_label}[/]",
                streak_s,
            )

            track_list.append({
                "code": code, "name": str(r.get("name", "")),
                "price": price, "score": sc,
                "stop_loss": stop, "target": target, "risk_reward": rr,
            })

        console.print(tbl)

        # 静默写入tracker
        try:
            record_picks(track_list, source=mode)
        except Exception:
            pass

    _render_section(
        momentum, "momentum",
        "▲ 强势股（技术面突破 + 资金配合）",
        "策略：回踩MA5附近买入 | 跌破MA20止损 | 盈亏比<1.5为红色警告",
        "green",
    )
    _render_section(
        value, "value",
        "\n◆ 低位价值股（MA20附近 + 趋势改善）",
        "策略：分批建仓 | 跌破MA20下方3%止损 | 盈亏比<1.5为红色警告",
        "cyan",
    )

    console.print("\n[dim]  盈亏比=潜在收益÷潜在亏损，建议只买盈亏比≥1.5的票（黄色以上）[/dim]")
    console.print("[dim]  连续上榜≥2天说明主力持续运作，胜率更高[/dim]")
    console.print("[dim]  暴雷风险：✓安全  !关注(30+)  △中危(50+)  ⚠暴雷(70+)  —=未扫描（按I键扫描个股）[/dim]")

    # W 快捷键：加入观察池
    all_q_stocks = []
    for df, mode in [(momentum, "momentum"), (value, "value")]:
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            price = float(r.get("price", 0))
            ma5   = float(r.get("ma5",   0)) or price
            ma20  = float(r.get("ma20",  0)) or price * 0.95
            if mode == "momentum":
                stop   = round(ma20 * 0.97, 2)
                target = round(price * 1.18, 2)
            else:
                stop   = round(ma20 * 0.97, 2)
                target = round(price * 1.13, 2)
            all_q_stocks.append({
                "code": str(r.get("code", "")),
                "name": str(r.get("name", "")),
                "price": price,
                "stop_loss": stop,
                "target": target,
                "source": mode,
            })

    if all_q_stocks:
        console.print("\n[dim]  按 [bold]W[/] 将股票加入观察池，输入编号后回车；其他键返回[/]")
        for i, s in enumerate(all_q_stocks, 1):
            console.print(f"  [dim]{i}.[/] {s['code']} {s['name']}  {s['price']:.2f}")
        try:
            ch = getch()
            if ch.lower() == "w":
                console.print("\n  输入编号（1-{n}）: ".format(n=len(all_q_stocks)), end="")
                idx_str = ""
                while True:
                    c = getch()
                    if c == "": continue
                    if c in ("\r", "\n"):
                        break
                    if c == "\x08" and idx_str:
                        idx_str = idx_str[:-1]
                        console.print("\b \b", end="")
                    elif c.isdigit():
                        idx_str += c
                        console.print(c, end="")
                console.print()
                try:
                    idx = int(idx_str)
                    if 1 <= idx <= len(all_q_stocks):
                        s = all_q_stocks[idx - 1]
                        from data.watchlist import add_watch
                        ok = add_watch(
                            code=s["code"], name=s["name"], price=s["price"],
                            source=s["source"],
                            stop_loss=s["stop_loss"], target=s["target"],
                        )
                        if ok:
                            console.print(f"  [green]✓ 已加入观察池: {s['code']} {s['name']}[/]")
                        else:
                            console.print(f"  [yellow]该股票已在观察池中[/]")
                    else:
                        console.print("  [dim]编号超出范围[/]")
                except ValueError:
                    console.print("  [dim]无效编号[/]")
                import time as _t2; _t2.sleep(1.5)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════
# 布局构建
# ════════════════════════════════════════════════════════════
def render_advisor(advice: dict) -> Panel:
    from rich.console import Group
    TITLE = "[bold bright_white on yellow] G [/][bold yellow] 今日操作建议[/]"

    if not advice:
        return Panel(Text("  ⏳ 计算中...", style="bright_black"), title=TITLE, border_style="yellow")

    env     = advice.get("market_env", "—")
    score   = advice.get("market_score", 0)
    pos     = advice.get("position_advice", "—")
    ec      = advice.get("env_color", "white")
    summary = advice.get("summary", "")
    gen_at  = advice.get("generated_at", "")

    # ── 状态行 ─────────────────────────────────────────────
    bar = Text()
    bar.append("  大盘 ", style="bright_black")
    bar.append(f"{env}", style=f"bold {ec}")
    bar.append(f" {score}/100", style=ec)
    bar.append("   仓位 ", style="bright_black")
    pos_s = "bold green" if "%" in pos and pos != "空仓" else "bold red"
    bar.append(f"{pos}", style=pos_s)
    bar.append(f"   {gen_at}\n", style="bright_black")
    bar.append(f"  {summary}\n", style="bold")

    # ── 可建仓 ─────────────────────────────────────────────
    buylist   = advice.get("buylist",   [])
    watchlist = advice.get("watchlist", [])

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True,
                header_style="bold bright_black", padding=(0, 1),
                show_edge=True, border_style="bright_black")
    tbl.add_column("",      width=2)
    tbl.add_column("代码",  width=7)
    tbl.add_column("名称",  width=7)
    tbl.add_column("买入",  justify="right", width=7)
    tbl.add_column("止损",  justify="right", width=7)
    tbl.add_column("目标",  justify="right", width=7)
    tbl.add_column("RR",    justify="right", width=4)
    tbl.add_column("状态",  width=12)

    for item in buylist[:5]:
        rr     = item.get("risk_reward", 0)
        streak = item.get("consecutive", 0)
        rr_c   = "green" if rr >= 2 else "yellow"
        state  = f"[bold yellow]{streak}天连续[/]" if streak >= 2 else "[green]可建仓[/]"
        tbl.add_row(
            "✅", item["code"], str(item["name"])[:6],
            f"[cyan]{item['buy_price']:.2f}[/]",
            f"[red]{item['stop_loss']:.2f}[/]",
            f"[green]{item['target']:.2f}[/]",
            f"[{rr_c}]{rr:.1f}[/]",
            state,
        )
    for item in watchlist[:3]:
        tbl.add_row(
            "👀", item["code"], str(item["name"])[:6],
            f"[bright_black]{item['buy_price']:.2f}[/]", "—", "—", "—",
            f"[cyan]{item['position_label'][:10]}[/]",
        )

    if not buylist and not watchlist:
        tbl.add_row("—", "", "今日暂无满足条件的标的", "", "", "", "", "")

    # ── 胜率摘要 ───────────────────────────────────────────
    wr_line = Text()
    try:
        from data.tracker import calc_winrate
        wr = calc_winrate(days=30)
        if wr.get("total", 0) > 0:
            w5   = wr.get("win_5d") or 0
            avg5 = wr.get("avg_pnl_5d") or 0
            w5_c = "green" if w5 >= 60 else ("yellow" if w5 >= 50 else "red")
            a5_c = "green" if avg5 > 0 else "red"
            best = wr.get("best")
            wr_line.append("\n  📊 近30日  ", style="bright_black")
            wr_line.append(f"5日胜率 {w5:.0f}%", style=w5_c)
            wr_line.append(f"  均盈亏 {avg5:+.1f}%", style=a5_c)
            wr_line.append(f"  {wr['total']}条记录", style="bright_black")
            if best:
                wr_line.append(f"  最佳 [green]{best['name']} +{best['pnl_5d']:.1f}%[/]")
    except Exception:
        pass

    foot = Text.from_markup("\n  [bright_black]按 G 详情 · I 个股分析 · S 收盘总结 · R 持仓风险扫描[/]")
    return Panel(Group(bar, tbl, wr_line, foot), title=TITLE, border_style="yellow")


# ════════════════════════════════════════════════════════════
# K 区块：大盘暴跌错杀反弹扫描
# ════════════════════════════════════════════════════════════
_kill_cache: dict = {"data": None, "ts": 0}


def fetch_kill(index_chg: float, trigger: float = -2.0, interval_min: int = 30) -> dict:
    """错杀反弹扫描，间隔30分钟刷新，大盘未跌够直接返回未触发结果"""
    global _kill_cache
    now = time.time()
    # 大盘未达阈值，不扫描（但保留上次触发的缓存供查看）
    if index_chg > trigger:
        if _kill_cache["data"]:
            return _kill_cache["data"]   # 返回上次的结果
        return {"triggered": False, "index_chg": index_chg, "stocks": [],
                "scan_time": datetime.now().strftime("%H:%M:%S")}
    # 已触发，且缓存还新鲜
    if _kill_cache["data"] and (now - _kill_cache["ts"]) < interval_min * 60:
        return _kill_cache["data"]
    try:
        from selector.mistaken_kill import scan_mistaken_kill
        result = scan_mistaken_kill(index_chg=index_chg, trigger_threshold=trigger)
        if result.get("triggered"):
            _kill_cache["data"] = result
            _kill_cache["ts"]   = now
        return result
    except Exception as e:
        logger.warning(f"错杀扫描失败: {e}")
        return _kill_cache.get("data") or {
            "triggered": True, "index_chg": index_chg,
            "stocks": [], "scan_time": "—", "error": str(e),
        }


def render_kill(data: dict) -> Panel:
    """K 区块面板（嵌入总览右侧，常驻显示）"""
    from rich.console import Group
    TITLE = "[bold bright_white on red] K [/][bold red] 错杀反弹[/]"

    if not data or not data.get("triggered"):
        idx_chg = data.get("index_chg", 0) if data else 0
        msg = Text(
            f"  今日大盘跌幅 {idx_chg:+.2f}%，未达触发条件（≥2%跌幅）\n"
            "  大盘暴跌日自动激活，扫描被错杀的优质股",
            style="dim",
        )
        return Panel(msg, title=TITLE, border_style="red")

    stocks = data.get("stocks", [])
    idx_chg = data.get("index_chg", 0)
    scan_time = data.get("scan_time", "—")

    header = Text(
        f"  ⚡ 大盘今跌 {idx_chg:+.2f}%  扫描时间 {scan_time}"
        f"  共找到 {len(stocks)} 只疑似错杀股\n",
        style="bold red",
    )

    if not stocks:
        msg = Text("  暂未找到符合条件的错杀股（质量筛选严格，宁缺毋滥）", style="dim")
        return Panel(Group(header, msg), title=TITLE, border_style="red")

    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold", padding=(0, 1))
    tbl.add_column("代码", width=7)
    tbl.add_column("名称", width=7)
    tbl.add_column("今跌",   justify="right", width=7)
    tbl.add_column("量比",   justify="right", width=5)
    tbl.add_column("60日涨", justify="right", width=7)
    tbl.add_column("评分",   justify="right", width=5)

    for s in stocks[:8]:   # 总览只显示前8只
        chg   = s.get("chg", 0)
        vr    = s.get("vol_ratio", 0)
        g60   = s.get("gain_60d", 0)
        sc    = s.get("score", 0)
        sc_c  = "green" if sc >= 70 else ("yellow" if sc >= 50 else "dim")
        vr_s  = f"[green]{vr:.1f}[/]" if vr < 0.8 else (f"[yellow]{vr:.1f}[/]" if vr < 1.5 else f"[red]{vr:.1f}[/]")
        tbl.add_row(
            s["code"], str(s.get("name", ""))[:6],
            f"[red]{chg:+.1f}%[/]",
            vr_s,
            f"{g60:+.1f}%",
            f"[{sc_c}]{sc:.0f}[/]",
        )

    foot = Text.from_markup("\n  [bright_black]按 K 查看详情及次日策略[/]")
    return Panel(Group(header, tbl, foot), title=TITLE, border_style="red")


def print_kill_detail(data: dict):
    """K 区块详情页：完整错杀股列表 + 次日操作策略"""
    if not data:
        console.print("  [dim]无数据[/]")
        return

    if not data.get("triggered"):
        idx_chg = data.get("index_chg", 0)
        console.print(f"  [dim]今日大盘跌幅 {idx_chg:+.2f}%，未达到触发条件（需跌 ≥ 2%）[/]")
        console.print("  [dim]大盘暴跌日此页自动激活[/]")
        return

    stocks = data.get("stocks", [])
    idx_chg = data.get("index_chg", 0)
    scan_time = data.get("scan_time", "—")

    console.print(f"\n  [bold red]⚡ 大盘今跌 {idx_chg:+.2f}%  |  扫描时间 {scan_time}[/]")
    console.print(f"  [bold]共筛出 {len(stocks)} 只疑似被错杀优质股（明日观察机会）[/]\n")

    if not stocks:
        console.print("  [dim]本次扫描未找到符合条件的错杀股。条件：\n"
                      "  · 当日跌幅 4~9.4%（未跌停）\n"
                      "  · 量比 < 2.0（缩量被动下跌）\n"
                      "  · 近60日涨幅 < 35%（非炒高股）\n"
                      "  · 价格仍在 MA20 附近（趋势未破）[/]")
        return

    # 完整列表
    tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan", padding=(0, 1))
    tbl.add_column("#",     width=3, justify="right")
    tbl.add_column("代码",  width=8)
    tbl.add_column("名称",  width=9)
    tbl.add_column("现价",  justify="right", width=7)
    tbl.add_column("今跌",  justify="right", width=7)
    tbl.add_column("量比",  justify="right", width=6)
    tbl.add_column("60日涨", justify="right", width=7)
    tbl.add_column("MA20",  justify="right", width=7)
    tbl.add_column("MA60",  justify="right", width=7)
    tbl.add_column("评分",  justify="right", width=5)

    for i, s in enumerate(stocks, 1):
        chg  = s.get("chg", 0)
        vr   = s.get("vol_ratio", 0)
        g60  = s.get("gain_60d", 0)
        sc   = s.get("score", 0)
        ma20 = s.get("ma20")
        ma60 = s.get("ma60")
        price = s.get("price", 0)
        sc_c = "green" if sc >= 70 else ("yellow" if sc >= 50 else "dim")
        vr_s = f"[green]{vr:.1f}[/]" if vr < 0.8 else (f"[yellow]{vr:.1f}[/]" if vr < 1.5 else f"[red]{vr:.1f}[/]")
        # MA20 距离标注
        ma20_s = f"{ma20:.2f}" if ma20 else "—"
        if ma20 and price >= ma20:
            ma20_s = f"[green]{ma20:.2f}[/]"
        elif ma20:
            ma20_s = f"[yellow]{ma20:.2f}[/]"
        ma60_s = f"{ma60:.2f}" if ma60 else "—"

        tbl.add_row(
            str(i), s["code"], str(s.get("name", ""))[:8],
            f"{price:.2f}",
            f"[red]{chg:+.1f}%[/]",
            vr_s,
            f"{g60:+.1f}%",
            ma20_s, ma60_s,
            f"[{sc_c}]{sc:.0f}[/]",
        )
    console.print(tbl)

    # 逐股策略
    console.print(f"\n[bold cyan]【次日操作策略】[/]")
    for s in stocks:
        name  = str(s.get("name", ""))
        code  = s["code"]
        strat = s.get("strategy", "")
        price = s.get("price", 0)
        ma20  = s.get("ma20")
        # 简单止损/目标计算
        stop   = round(price * 0.95, 2)   # -5% 止损
        target = round(price * 1.10, 2)   # +10% 目标（错杀反弹）
        console.print(f"\n  [bold]{name}（{code}）[/]  现价 {price}  止损 [red]{stop}[/]  目标 [green]{target}[/]")
        console.print(f"  [dim]{strat}[/]")

    console.print(f"\n[bold yellow]【注意事项】[/]")
    console.print("  [dim]· 错杀反弹是短线策略，次日若不涨立刻止损，不拖延")
    console.print("  · 严格止损 -5%，不恋战；获利 +8~10% 即止盈")
    console.print("  · 开盘前5分钟观察集合竞价，低开超 2% 放弃介入")
    console.print("  · 大盘若次日继续大跌，所有信号失效，不操作[/]")


def print_advisor_detail(advice: dict):
    """G 区块详情页：完整操作建议 + 胜率报告"""
    if not advice:
        console.print("[dim]计算中...[/]"); return

    env   = advice.get("market_env", "—")
    score = advice.get("market_score", 0)
    pos   = advice.get("position_advice", "—")
    ec    = advice.get("env_color", "white")

    console.print(f"\n[bold]大盘环境: [{ec}]{env}[/]  情绪分: {score}/100  "
                  f"建议仓位: [green]{pos}[/][/bold]")

    skip = advice.get("skip_reason")
    if skip:
        console.print(f"\n[red]{skip}[/]\n")
        return

    # 可买清单详细
    buylist = advice.get("buylist", [])
    if buylist:
        console.print("\n[bold green]━━ 今日可建仓清单（按优先级排序）━━[/]")
        tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold green")
        tbl.add_column("#",       width=3, justify="right")
        tbl.add_column("代码",    width=8)
        tbl.add_column("名称",    width=8)
        tbl.add_column("建议买入",justify="right", width=8)
        tbl.add_column("止损价",  justify="right", width=7)
        tbl.add_column("目标价",  justify="right", width=7)
        tbl.add_column("盈亏比",  justify="right", width=6)
        tbl.add_column("建议仓位",justify="right", width=8)
        tbl.add_column("连续上榜",justify="right", width=8)
        tbl.add_column("来源",    width=8)
        for i, item in enumerate(buylist, 1):
            rr = item.get("risk_reward", 0)
            rr_c = "green" if rr >= 2 else "yellow"
            streak = item.get("consecutive", 0)
            streak_s = f"[bold yellow]{streak}天[/]" if streak >= 2 else "[dim]首次[/]"
            src_map = {"momentum": "强势股", "value": "价值股", "pick": "热门板块"}
            src = src_map.get(item.get("source", ""), item.get("source", ""))
            # 仓位根据盈亏比和评分
            sc = item.get("score", 0)
            if rr >= 2.5 and sc >= 85:
                psize = "10~15%"
            elif rr >= 2.0 and sc >= 75:
                psize = "5~10%"
            else:
                psize = "3~5%"
            tbl.add_row(
                str(i),
                item["code"], item["name"][:6],
                f"[cyan]{item['buy_price']:.2f}[/]",
                f"[red]{item['stop_loss']:.2f}[/]",
                f"[green]{item['target']:.2f}[/]",
                f"[{rr_c}]{rr:.1f}[/]",
                f"[green]{psize}[/]",
                streak_s,
                f"[dim]{src}[/]",
            )
        console.print(tbl)
        console.print("[dim]  注：仓位建议为单笔最大仓位，总持仓不超过建议总仓位[/dim]")

    watchlist = advice.get("watchlist", [])
    if watchlist:
        console.print("\n[bold cyan]━━ 关注等回踩（今日涨幅过大，等明日或下一个交易日）━━[/]")
        for item in watchlist:
            console.print(f"  {item['code']} {item['name']:<8} "
                          f"[cyan]{item['position_label']}[/]  "
                          f"回踩到 {item['buy_price']:.2f} 附近再考虑")

    # 胜率完整报告
    try:
        from data.tracker import calc_winrate
        console.print("\n[bold yellow]━━ 选股系统胜率报告（近30天）━━[/]")
        for src, label in [("momentum","强势股"), ("value","价值股"), ("pick","热门板块"), (None,"全部")]:
            wr = calc_winrate(source=src, days=30)
            if wr.get("total", 0) == 0:
                continue
            w1 = wr.get("win_1d"); w3 = wr.get("win_3d"); w5 = wr.get("win_5d")
            a5 = wr.get("avg_pnl_5d")
            ht = wr.get("hit_target"); hs = wr.get("hit_stop")
            w5_c = "green" if (w5 or 0) >= 60 else ("yellow" if (w5 or 0) >= 50 else "red")
            a5_c = "green" if (a5 or 0) > 0 else "red"
            console.print(
                f"  [{label}] 共{wr['total']}条  "
                f"1日胜率{w1:.0f}%  3日胜率{w3:.0f}%  "
                f"5日胜率[{w5_c}]{w5:.0f}%[/]  "
                f"5日均盈亏[{a5_c}]{a5:+.1f}%[/]  "
                f"达标{ht:.0f}%  止损{hs:.0f}%"
                if all(x is not None for x in [w1,w3,w5,a5,ht,hs])
                else f"  [{label}] 数据收集中..."
            )
            best  = wr.get("best")
            worst = wr.get("worst")
            if best:
                console.print(f"    最佳: [green]{best['name']}+{best['pnl_5d']:.1f}%[/]  "
                              f"最差: [red]{worst['name']}{worst['pnl_5d']:+.1f}%[/]")
    except Exception as e:
        console.print(f"[dim]胜率统计加载失败: {e}[/]")


# ════════════════════════════════════════════════════════════
# W 区块：观察池
# ════════════════════════════════════════════════════════════

def render_watchlist() -> Panel:
    from rich.console import Group
    TITLE = "[bold bright_white on blue] W [/][bold blue] 观察池[/]"
    try:
        from data.watchlist import get_all_stats
        stats = get_all_stats()
    except Exception as e:
        return Panel(Text(f"  ✗ {e}", style="red"), title=TITLE, border_style="blue")

    if not stats:
        return Panel(Text("  暂无观察股  按 W 添加", style="bright_black"),
                     title=TITLE, border_style="blue")

    tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold bright_black",
                padding=(0, 1), show_edge=False)
    tbl.add_column("代码",  width=7)
    tbl.add_column("名称",  width=7)
    tbl.add_column("加入价", justify="right", width=7)
    tbl.add_column("现价",  justify="right", width=7)
    tbl.add_column("盈亏",  justify="right", width=8)
    tbl.add_column("最高",  justify="right", width=7)
    tbl.add_column("距止损", justify="right", width=7)
    tbl.add_column("天/共", justify="right", width=6)
    tbl.add_column("趋势",  width=6)

    for s in stats:
        ps    = "green" if s["cur_pnl"] >= 0 else "red"
        mx_s  = "green" if s["max_gain"] > 0 else "bright_black"
        ds    = s.get("dist_stop")
        ds_s  = "green" if (ds or 0) > 5 else ("yellow" if (ds or 0) > 0 else "red")
        streak = s.get("streak", 0)
        if streak >= 2:
            tr = f"[green]↑{streak}天[/]"
        elif streak <= -2:
            tr = f"[red]↓{abs(streak)}天[/]"
        else:
            tr = "[bright_black]—[/]"

        tbl.add_row(
            s["code"],
            str(s["name"])[:6],
            f"{s['add_price']:.2f}",
            f"{s['cur_price']:.2f}",
            f"[{ps}]{s['cur_pnl']:+.1f}%[/]",
            f"[{mx_s}]{s['max_gain']:+.1f}%[/]",
            f"[{ds_s}]{ds:+.1f}%[/]" if ds is not None else "—",
            f"{s['days']}/{s['track_days']}",
            tr,
        )

    foot = Text.from_markup("\n  [bright_black]按 W 管理观察池 · I 个股分析 · 在 E/F 详情页输入 W 添加股票[/]")
    return Panel(Group(tbl, foot), title=TITLE, border_style="blue")


def print_watchlist_detail():
    """W 区块详情页：走势图 + 管理操作"""
    from data.watchlist import get_all_stats, add_watch, remove_watch, get_watching

    TITLE = "[bold blue]W  观察池管理[/]"

    def _show() -> dict:
        stats = get_all_stats()
        console.print(f"\n[bold blue]{'─'*70}[/]")
        console.print(f"[bold blue]  观察池  共 {len(stats)} 只[/]")
        console.print(f"[bold blue]{'─'*70}[/]\n")

        if not stats:
            console.print("  [bright_black]暂无观察股[/]\n")
            console.print("[bold yellow]━━ 操作 ━━[/]")
            console.print("  输入 [green]A[/] 添加股票  |  直接回车返回")
            return {}

        idx_map = {}
        for i, s in enumerate(stats, 1):
            ps   = "green" if s["cur_pnl"] >= 0 else "red"
            ds   = s.get("dist_stop")
            ds_s = "green" if (ds or 0) > 5 else ("yellow" if (ds or 0) > 0 else "red")
            streak = s.get("streak", 0)
            tr = (f"[green]↑{streak}天[/]" if streak >= 2
                  else f"[red]↓{abs(streak)}天[/]" if streak <= -2 else "—")

            console.print(
                f"  [bold]{i}.[/] {s['code']} [bold]{s['name'][:6]}[/]  "
                f"加入 {s['add_price']:.2f}  现价 {s['cur_price']:.2f}  "
                f"盈亏 [{ps}]{s['cur_pnl']:+.1f}%[/]  "
                f"最高 [green]{s['max_gain']:+.1f}%[/]  "
                f"距止损 [{ds_s}]{ds:+.1f}%[/]  "
                f"跟踪 {s['days']}/{s['track_days']}天  {tr}"
                if ds is not None else
                f"  [bold]{i}.[/] {s['code']} [bold]{s['name'][:6]}[/]  "
                f"加入 {s['add_price']:.2f}  现价 {s['cur_price']:.2f}  "
                f"盈亏 [{ps}]{s['cur_pnl']:+.1f}%[/]  跟踪 {s['days']}/{s['track_days']}天"
            )

            # 迷你走势图（用 sparkline 字符）
            snaps = s.get("snapshots", [])
            if len(snaps) >= 3:
                pnls = [sp["pnl"] for sp in snaps[-15:]]
                mn, mx = min(pnls), max(pnls)
                chars = " ▁▂▃▄▅▆▇█"
                def _bar(v):
                    if mx == mn: return "▄"
                    idx = int((v - mn) / (mx - mn) * 8)
                    return chars[min(idx, 8)]
                spark = "".join(_bar(p) for p in pnls)
                spark_s = "green" if pnls[-1] >= 0 else "red"
                console.print(f"     [{spark_s}]{spark}[/]  "
                               f"[bright_black]{snaps[0]['date']} → {snaps[-1]['date']}  "
                               f"止损{s.get('stop_loss', 0) or 0:.2f}  目标{s.get('target', 0) or 0:.2f}[/]")
                if s.get("note"):
                    console.print(f"     [bright_black]备注: {s['note']}[/]")
            idx_map[str(i)] = s["code"]

        console.print()
        console.print("[bold yellow]━━ 操作 ━━[/]")
        console.print("  输入[bold red]序号[/]删除  |  [bold green]A[/] 添加  |  直接回车返回")
        return idx_map

    def _read_line(prompt=""):
        if prompt: console.print(prompt, end="")
        buf = ""
        while True:
            ch = getch()
            if ch == "": continue
            if ch in ("\r", "\n"): console.print(); return buf.strip()
            if ch == "\x03": raise KeyboardInterrupt
            if ch == "\x1b": buf = ""; console.print(); return ""
            if ch == "\x08":
                if buf: buf = buf[:-1]; console.print("\b \b", end="")
            elif ch.isprintable():
                console.print(ch, end=""); buf += ch

    idx_map = _show()

    while True:
        try:
            cmd = _read_line("\n> ").upper()
        except KeyboardInterrupt:
            return

        if not cmd:
            break

        if cmd == "A":
            console.print("[green]添加股票（格式：代码 名称 现价 [跟踪天数=20] [备注]）[/]")
            console.print("[dim]例：600519 贵州茅台 1800 30 白酒龙头[/]")
            try:
                line = _read_line("> ")
            except KeyboardInterrupt:
                return
            parts = line.split(None, 4)
            if len(parts) >= 3:
                try:
                    code2  = parts[0]
                    name2  = parts[1]
                    price2 = float(parts[2])
                    days2  = int(parts[3]) if len(parts) > 3 else 20
                    note2  = parts[4]      if len(parts) > 4 else ""
                    ok = add_watch(code2, name2, price2, source="manual",
                                   track_days=days2, note=note2)
                    if ok:
                        console.print(f"[green]✓ 已添加 {code2} {name2}[/]")
                    else:
                        console.print(f"[yellow]{code2} 已在观察池中[/]")
                    idx_map = _show()
                except Exception as e:
                    console.print(f"[red]格式错误: {e}[/]")
            else:
                console.print("[red]至少需要：代码 名称 现价[/]")

        elif cmd in idx_map:
            code = idx_map[cmd]
            watching = get_watching()
            entry = next((w for w in watching if w["code"] == code), None)
            name  = entry["name"] if entry else code
            console.print(f"  确认删除 [red]{name}({code})[/]？Y 确认，其他键取消")
            confirm = getch().upper()
            console.print()
            if confirm == "Y":
                remove_watch(code, reason="manual")
                console.print(f"[green]✓ 已移除 {name}({code})[/]")
                idx_map = _show()
            else:
                console.print("[dim]已取消[/]")
        else:
            break


def print_closing_summary_detail():
    """S区块：收盘复盘总结详情页"""
    console.print("[dim]  正在生成收盘总结，请稍候...[/]")
    try:
        from market.closing_summary import generate_closing_summary
        summary = generate_closing_summary()
    except Exception as e:
        console.print(f"[red]生成失败: {e}[/]")
        return

    text = summary.get("full_text", "无内容")
    score = summary.get("score", 0)
    gen_at = summary.get("generated_at", "")

    # 强度颜色
    if score >= 75:
        score_color = "green"
    elif score >= 50:
        score_color = "yellow"
    else:
        score_color = "red"

    console.print(f"  [dim]生成时间: {gen_at}   市场强度: [{score_color}]{score}/100[/][/dim]\n")

    # 按行输出，对关键词上色
    for line in text.splitlines():
        if line.startswith("─") or line.startswith("  ─"):
            console.print(f"[dim]{line}[/]")
        elif line.startswith("【") and line.endswith("】"):
            console.print(f"\n[bold cyan]{line}[/]")
        elif line.strip().startswith("►"):
            console.print(f"[yellow]{line}[/]")
        elif line.strip().startswith("◆"):
            console.print(f"[green]{line}[/]")
        elif line.strip().startswith("·"):
            console.print(f"[cyan]{line}[/]")
        elif "大涨" in line or "上涨" in line or "偏多" in line:
            console.print(f"[green]{line}[/]")
        elif "大跌" in line or "下跌" in line or "偏弱" in line or "弱势" in line:
            console.print(f"[red]{line}[/]")
        else:
            console.print(line)


def print_risk_scan_detail(port_data):
    """
    R 键：持仓暴雷扫描全报告
    对所有持仓股票进行完整的暴雷风险检测，显示详细信号
    """
    console.print("  [dim]正在对持仓股票进行暴雷风险扫描...[/]")
    codes = [r.get("code", "") for r in (port_data or []) if r.get("code")]
    if not codes:
        console.print("  [dim]暂无持仓，无需扫描[/]")
        return

    from data.risk_scanner import scan_stock_risk, RISK_LEVELS

    sep = "─" * 62
    has_high_risk = False

    for r in (port_data or []):
        code = r.get("code", "")
        name = r.get("name", "—")
        if not code:
            continue

        console.print(f"\n[bold]{sep}[/]")
        console.print(f"[bold white]  {code}  {name}[/]  [dim]持仓成本 {r.get('cost',0):.2f}  现价 {r.get('price',0):.2f}  盈亏 {r.get('pnl_pct',0):+.1f}%[/]")

        risk = scan_stock_risk(code, use_cache=False)
        rscore = risk.get("risk_score", 0)
        rlevel = risk.get("level", "—")
        rdims  = risk.get("dimensions", {})
        rsigs  = risk.get("signals", [])

        # 风险分条形图
        bar_f = rscore // 5
        if rscore >= 70:
            bar_s = "bold red"
            has_high_risk = True
        elif rscore >= 50:
            bar_s = "yellow"
        elif rscore >= 30:
            bar_s = "yellow"
        else:
            bar_s = "green"

        console.print(
            f"  风险评分  [{bar_s}]{'█'*bar_f}{'░'*(20-bar_f)}[/]  [{bar_s}]{rscore}/100  {rlevel}[/]"
        )

        # 各维度细项
        dim_labels = {"financial": "财务恶化", "goodwill": "商誉炸弹",
                      "receivable": "应收账款", "fund_flow": "资金撤离", "technical": "技术破位"}
        dim_line = []
        for k, label in dim_labels.items():
            v = rdims.get(k, 0)
            if v >= 8:
                dim_line.append(f"[red]{label}:{v}[/]")
            elif v >= 3:
                dim_line.append(f"[yellow]{label}:{v}[/]")
            else:
                dim_line.append(f"[dim]{label}:0[/]")
        console.print("  " + "  ".join(dim_line))

        # 具体信号
        if rsigs:
            for sig in rsigs:
                if sig.startswith("⚠"):
                    console.print(f"  [bold red]{sig}[/]")
                elif sig.startswith("△"):
                    console.print(f"  [yellow]{sig}[/]")
                else:
                    console.print(f"  [dim]{sig}[/]")
        else:
            console.print("  [dim green]未发现明显暴雷前兆[/]")

        # 操作建议
        advice = risk.get("advice", "")
        if rscore >= 70:
            console.print(f"  [bold red]操作建议：立即减仓或清仓，{advice}[/]")
        elif rscore >= 50:
            console.print(f"  [yellow]操作建议：建议减仓，{advice}[/]")
        elif rscore >= 30:
            console.print(f"  [yellow]操作建议：设好止损，{advice}[/]")

    console.print(f"\n[bold]{sep}[/]")
    if has_high_risk:
        console.print("[bold red]\n  ⚠⚠⚠ 有高风险持仓，请尽快处理！⚠⚠⚠[/]")
    else:
        console.print("[dim green]\n  持仓整体风险可控[/]")


def build_layout(pulse_data, news_data, scan_data, port_data, pick_data, quality_data,
                 advice_data, interval, pick_interval, no_pick, last_update,
                 next_refresh_ts=0, kill_data=None):
    pa = render_pulse(pulse_data)
    pb = render_news(news_data)
    pc = render_scan(scan_data)
    pd_ = render_portfolio(port_data)
    pe = render_pick(pick_data, pick_interval) if not no_pick else Panel(
        Text("  已跳过选股模块（--no-pick）", style="dim"),
        title="[bold cyan]E  热门选股[/]", border_style="cyan")
    pf = render_quality(quality_data)
    pg = render_advisor(advice_data)
    pw = render_watchlist()
    pk = render_kill(kill_data)

    nxt = datetime.fromtimestamp(next_refresh_ts).strftime("%H:%M:%S") if next_refresh_ts else "--:--"
    trading_s = "[green]● 交易中[/]" if is_trading() else "[bright_black]○ 休市[/]"
    # tdx本地库状态
    try:
        from data.tdx_local import get_db_info
        _tdx = get_db_info()
        if _tdx.get("available"):
            _latest = _tdx.get("latest", "?")
            tdx_s = f"[green]tdx✓{_latest}[/][bright_black] ┃[/] "
        else:
            tdx_s = "[bright_black]tdx✗ ┃[/] "
    except Exception:
        tdx_s = ""
    # Ollama AI 状态
    try:
        from news.ai_analyzer import check_ollama, _ollama_available
        if _ollama_available is None:
            ai_s = ""  # 未检测，不显示
        elif _ollama_available:
            ai_s = f"[green]AI✓[/][bright_black] ┃[/] "
        else:
            ai_s = f"[bright_black]AI✗ ┃[/] "
    except Exception:
        ai_s = ""
    status = (f"[bright_black] 更新{last_update} 刷新{nxt} [/]{trading_s}"
              f"[bright_black]  ┃  [/]{tdx_s}{ai_s}"
              f"[bright_black]A大盘 B快讯 C异动 D持仓 E热股 F优质 G建议 K错杀 W观察池"
              f"  [bold white]I个股分析 R风险扫描 S收盘总结[/][bright_black]  Q退出[/]")

    layout = Layout()
    layout.split_column(
        Layout(name="top",    ratio=5),
        Layout(name="bottom", ratio=5),
        Layout(name="pick",   ratio=4),
        Layout(name="mid",    ratio=5),
        Layout(name="lower",  ratio=5),
        Layout(name="status", size=1),
    )
    layout["top"].split_row(
        Layout(pa, name="pulse", ratio=1),
        Layout(pb, name="news",  ratio=1),
    )
    layout["bottom"].split_row(
        Layout(pc, name="scan",       ratio=1),
        Layout(pd_, name="portfolio", ratio=1),
    )
    layout["pick"].update(pe)
    layout["mid"].split_row(
        Layout(pf, name="quality",   ratio=3),
        Layout(pw, name="watchlist", ratio=2),
    )
    layout["lower"].split_row(
        Layout(pk, name="kill",    ratio=3),
        Layout(pg, name="advisor", ratio=2),
    )
    layout["status"].update(Text.from_markup(status, justify="center"))

    return layout


# ════════════════════════════════════════════════════════════
# I 区块：个股全方位分析详情页
# ════════════════════════════════════════════════════════════

def print_stock_analysis_detail():
    """
    I 键：输入股票代码，输出全方位分析报告
    涵盖：实时行情 / 技术面 / 基本面 / 估值 / 资金面
    """
    code = _read_line("  请输入股票代码（6位，ESC取消）: ").strip()
    if not code:
        return
    code = code.zfill(6)

    console.print(f"\n  [dim]正在分析 {code}，拉取多维度数据...[/]")

    # ── 1. 实时行情 ──────────────────────────────────────────
    name, price, chg, amount, vol_ratio, turnover = "—", 0.0, 0.0, 0.0, 0.0, 0.0
    try:
        from data.reliable_api import API
        spot = API.spot()
        row = spot[spot["code"].astype(str).str.strip() == code.strip()]
        if not row.empty:
            r = row.iloc[0]
            name     = str(r.get("name", "—")).strip()
            price    = float(r.get("price", 0) or 0)
            chg      = float(r.get("chg", 0) or 0)
            amount   = float(r.get("amount", 0) or 0)
            vol_ratio = float(r.get("vol_ratio", 0) or 0)
            turnover = float(r.get("turnover", 0) or 0)
    except Exception as e:
        console.print(f"  [yellow]行情数据获取失败: {e}[/]")

    # ── 2. 历史行情 + 技术面 ─────────────────────────────────
    hist_signals = []
    ma5 = ma10 = ma20 = ma60 = None
    rs_20 = None
    vol_ma5 = None
    try:
        from data.reliable_api import API
        from datetime import timedelta
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
        hist = API.history(code, start, end)
        if not hist.empty and "close" in hist.columns:
            close_s = hist["close"].astype(float)
            ma5  = close_s.tail(5).mean()
            ma10 = close_s.tail(10).mean()
            ma20 = close_s.tail(20).mean()
            ma60 = close_s.tail(60).mean() if len(close_s) >= 60 else None

            # 相对强度：近20日涨幅 vs 上证
            if len(close_s) >= 20:
                rs_20 = (close_s.iloc[-1] / close_s.iloc[-20] - 1) * 100

            # 量能：近5日均量
            if "volume" in hist.columns:
                vol_s = hist["volume"].astype(float)
                vol_ma5 = vol_s.tail(5).mean()

            # 技术信号
            if ma5 and ma10 and ma20:
                if ma5 > ma10 > ma20:
                    hist_signals.append("✓ 均线多头排列（MA5>MA10>MA20），趋势向上")
                elif ma5 < ma10 < ma20:
                    hist_signals.append("✗ 均线空头排列（MA5<MA10<MA20），趋势向下")
                else:
                    hist_signals.append("· 均线交叉震荡，方向待确认")

            if ma60 and price > 0:
                dist = (price - ma60) / ma60 * 100
                if dist > 20:
                    hist_signals.append(f"· 距MA60偏离 +{dist:.1f}%，高位风险")
                elif dist < -10:
                    hist_signals.append(f"✓ 距MA60偏离 {dist:.1f}%，低位区间")

            if rs_20 is not None:
                if rs_20 > 10:
                    hist_signals.append(f"✓ 近20日涨幅 +{rs_20:.1f}%，相对强势")
                elif rs_20 < -10:
                    hist_signals.append(f"✗ 近20日涨幅 {rs_20:.1f}%，相对弱势")
    except Exception as e:
        hist_signals.append(f"· 历史行情获取失败: {e}")

    # ── 3. 基本面 ────────────────────────────────────────────
    console.print("  [dim]拉取基本面数据...[/]")
    from data.fundamental import get_fundamental, get_fundamental_score, enrich_with_price, get_pe_pb_signal
    fund_info = get_fundamental(code)
    if price > 0:
        fund_info = enrich_with_price(fund_info, price)
    fund_score, fund_signals = get_fundamental_score(fund_info)
    pe_sigs = get_pe_pb_signal(fund_info.get("pe"), fund_info.get("pb"),
                               profit_yoy=fund_info.get("profit_yoy"))

    # ── 4. 资金流向 ──────────────────────────────────────────
    fund_flow_signals = []
    try:
        from data.reliable_api import API
        ff = API.fund_flow(code, days=5)
        if ff is not None and not ff.empty:
            # 找主力净流入列
            main_col = next((c for c in ff.columns if "主力" in c and "净" in c), None)
            if main_col:
                main_5d = float(ff[main_col].sum())
                latest_main = float(ff.iloc[-1][main_col]) if not ff.empty else 0
                if main_5d > 0:
                    fund_flow_signals.append(f"✓ 5日主力净流入 +{main_5d/1e8:.2f}亿，机构资金持续买入")
                elif main_5d < 0:
                    fund_flow_signals.append(f"✗ 5日主力净流出 {main_5d/1e8:.2f}亿，主力在撤退")
                if latest_main > 0:
                    fund_flow_signals.append(f"✓ 最近一日主力净流入 +{latest_main/1e8:.2f}亿")
    except Exception:
        pass

    # ── 5. 涨停池 / 龙虎榜 ──────────────────────────────────
    lhb_signal = []
    try:
        from data.reliable_api import API
        lhb_codes = API.lhb(days=5)
        if code in lhb_codes:
            lhb_signal.append("✓ 近5日上龙虎榜，游资/机构高度关注")
    except Exception:
        pass

    # ══════════════════════════════════════════════════════════
    # 输出报告
    # ══════════════════════════════════════════════════════════
    console.print()
    sep = "─" * 62
    console.print(f"[bold cyan]{sep}[/]")
    chg_color = "green" if chg >= 0 else "red"
    console.print(f"[bold white]  {code}  {name}[/]   "
                  f"[bold {chg_color}]{price:.2f}  {chg:+.2f}%[/]")
    console.print(f"[dim]{sep}[/]")

    # 行情快照
    console.print(f"\n[bold cyan]【一、实时行情】[/]")
    console.print(f"  现价 {price:.2f}   涨跌 [{chg_color}]{chg:+.2f}%[/]")
    if amount > 0:
        console.print(f"  成交额 {amount/10000:.2f}亿   换手率 {turnover:.2f}%   量比 {vol_ratio:.2f}x")
    if ma5:
        ma_color = lambda p, m: "green" if p > m else "red"
        console.print(f"  MA5={ma5:.2f}[{ma_color(price,ma5)}]{'↑' if price>ma5 else '↓'}[/]  "
                      f"MA10={ma10:.2f}  MA20={ma20:.2f}"
                      + (f"  MA60={ma60:.2f}" if ma60 else ""))

    # 技术面
    console.print(f"\n[bold cyan]【二、技术面分析】[/]")
    for sig in hist_signals:
        color = "green" if sig.startswith("✓") else ("red" if sig.startswith("✗") else "dim")
        console.print(f"  [{color}]{sig}[/]")
    if not hist_signals:
        console.print("  · 历史数据不足")

    # 基本面
    console.print(f"\n[bold cyan]【三、基本面分析】[/]")
    report_date = fund_info.get("report_date", "—")
    console.print(f"  [dim]数据期：{report_date}[/]")

    # 关键财务数字
    roe = fund_info.get("roe")
    profit_yoy = fund_info.get("profit_yoy")
    revenue_yoy = fund_info.get("revenue_yoy")
    debt_ratio = fund_info.get("debt_ratio")
    net_margin = fund_info.get("net_margin")
    net_profit = fund_info.get("net_profit")
    revenue = fund_info.get("revenue")

    tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    tbl.add_column("指标", style="dim")
    tbl.add_column("数值", justify="right")
    tbl.add_column("指标", style="dim")
    tbl.add_column("数值", justify="right")

    def _fmt(v, suffix="", na="—"):
        return f"{v:.2f}{suffix}" if v is not None else na

    tbl.add_row(
        "ROE", _fmt(roe, "%"),
        "净利润增速", _fmt(profit_yoy, "%")
    )
    tbl.add_row(
        "营收增速", _fmt(revenue_yoy, "%"),
        "资产负债率", _fmt(debt_ratio, "%")
    )
    tbl.add_row(
        "净利率", _fmt(net_margin, "%"),
        "每股收益", _fmt(fund_info.get("eps"), "元")
    )
    if net_profit:
        tbl.add_row(
            "净利润", f"{net_profit/1e8:.2f}亿",
            "营收", f"{revenue/1e8:.2f}亿" if revenue else "—"
        )
    console.print(tbl)

    # 基本面信号
    fs_bar = int(fund_score / 5)
    fs_color = "green" if fund_score >= 70 else ("yellow" if fund_score >= 50 else "red")
    console.print(f"  基本面评分  [{fs_color}]{'█'*fs_bar}{'░'*(20-fs_bar)}[/]  [{fs_color}]{fund_score}/100[/]")
    for sig in fund_signals:
        color = "green" if sig.startswith("✓") else ("red" if sig.startswith("✗") else "dim")
        console.print(f"  [{color}]{sig}[/]")

    # 估值
    console.print(f"\n[bold cyan]【四、估值分析】[/]")
    pe = fund_info.get("pe")
    pb = fund_info.get("pb")
    bvps = fund_info.get("bvps")
    eps = fund_info.get("eps")
    if pe or pb:
        console.print(f"  PE={pe if pe else '—'}x   PB={pb if pb else '—'}x   "
                      f"每股净资产={_fmt(bvps, '元')}")
    for sig in pe_sigs:
        color = "green" if sig.startswith("✓") else ("red" if sig.startswith("✗") else "dim")
        console.print(f"  [{color}]{sig}[/]")
    if not pe_sigs:
        console.print("  · 估值数据不足（EPS/净资产缺失）")

    # 资金流向
    if fund_flow_signals or lhb_signal:
        console.print(f"\n[bold cyan]【五、资金与筹码】[/]")
        for sig in lhb_signal + fund_flow_signals:
            color = "green" if sig.startswith("✓") else ("red" if sig.startswith("✗") else "dim")
            console.print(f"  [{color}]{sig}[/]")

    # ── 7. 暴雷风险检测 ─────────────────────────────────────
    console.print(f"\n[bold cyan]【五、暴雷风险检测】[/]")
    console.print("  [dim]正在检测商誉炸弹 / 财务恶化 / 资金撤离...[/]")
    risk_result = {}
    try:
        from data.risk_scanner import scan_stock_risk
        risk_result = scan_stock_risk(code, fund_info=fund_info)
        rscore  = risk_result.get("risk_score", 0)
        rlevel  = risk_result.get("level", "—")
        rcolor  = risk_result.get("color", "dim")
        radvice = risk_result.get("advice", "")
        rdims   = risk_result.get("dimensions", {})

        # 风险分条形图
        risk_bar_filled = rscore // 5
        # 条用红色表示
        bar_color = "bold red" if rscore >= 70 else ("yellow" if rscore >= 30 else "green")
        console.print(
            f"  暴雷风险  [{bar_color}]{'█'*risk_bar_filled}{'░'*(20-risk_bar_filled)}[/]  "
            f"[{bar_color}]{rscore}/100  {rlevel}[/]"
        )

        # 各维度得分
        dim_labels = {"financial": "财务恶化", "goodwill": "商誉炸弹",
                      "receivable": "应收账款", "fund_flow": "资金撤离", "technical": "技术破位"}
        dim_parts = []
        for k, label in dim_labels.items():
            v = rdims.get(k, 0)
            if v > 0:
                c = "red" if v >= 8 else "yellow"
                dim_parts.append(f"[{c}]{label}:{v}[/]")
            else:
                dim_parts.append(f"[dim]{label}:0[/]")
        console.print("  维度得分  " + "  ".join(dim_parts))
        console.print(f"  [dim]{radvice}[/]")

        # 风险信号
        rsigs = risk_result.get("signals", [])
        if rsigs:
            console.print()
            for sig in rsigs:
                if sig.startswith("⚠"):
                    console.print(f"  [bold red]{sig}[/]")
                elif sig.startswith("△"):
                    console.print(f"  [yellow]{sig}[/]")
                else:
                    console.print(f"  [dim]{sig}[/]")
        else:
            console.print("  [dim green]未发现明显暴雷前兆[/]")
    except Exception as e:
        console.print(f"  [dim]暴雷检测失败: {e}[/]")

    # 综合结论
    console.print(f"\n[bold cyan]【六、综合结论】[/]")
    all_positive = sum(1 for s in (fund_signals + hist_signals + fund_flow_signals + pe_sigs) if s.startswith("✓"))
    all_negative = sum(1 for s in (fund_signals + hist_signals + fund_flow_signals + pe_sigs) if s.startswith("✗"))

    if fund_info.get("error"):
        console.print(f"  [yellow]基本面数据获取失败: {fund_info['error']}，以下仅供参考[/]")

    risk_score = risk_result.get("risk_score", 0)

    # 技术面状态：均线多头=强势，可提升权重
    tech_strong = any("多头排列" in s or "相对强势" in s for s in hist_signals)
    tech_weak   = any("空头排列" in s or "跌破MA" in s for s in hist_signals)

    # 综合评分逻辑：
    #   技术强势时：技术面权重 55%，基本面 45%（基本面有季报滞后，技术反映当前共识）
    #   技术弱势时：基本面权重 55%，技术面 45%
    #   暴雷风险高时：无论技术多强，综合得分上限压制
    tech_score = min(100, max(0, 50 + all_positive * 4 - all_negative * 6))
    if tech_strong:
        raw = fund_score * 0.40 + tech_score * 0.60
    elif tech_weak:
        raw = fund_score * 0.60 + tech_score * 0.40
    else:
        raw = fund_score * 0.50 + tech_score * 0.50

    # 暴雷风险惩罚（风险越高扣越多，但不完全归零）
    risk_penalty = risk_score * 0.25
    invest_score = round(min(100, max(0, raw - risk_penalty)), 1)

    tc = "green" if invest_score >= 70 else ("yellow" if invest_score >= 50 else "red")
    console.print(f"  投资综合评分  [{tc}]{invest_score}/100[/]  "
                  f"（基本面{fund_score:.0f} / 技术{tech_score:.0f} / 暴雷风险{risk_score}）")

    if risk_score >= 70:
        console.print(f"  [bold red]结论：暴雷风险极高，强烈建议规避，不应持仓[/]")
    elif risk_score >= 50:
        console.print(f"  [yellow]结论：中等暴雷风险，不建议新增仓位，已持仓建议减仓[/]")
    elif invest_score >= 72:
        console.print("  [green]结论：基本面扎实，暴雷风险低，具备投资价值[/]")
        console.print("  [green]建议：可关注回调买点，控制仓位[/]")
    elif invest_score >= 55:
        console.print("  [yellow]结论：整体尚可，有部分风险项，需选择合适时机介入[/]")
    elif invest_score >= 40:
        console.print("  [yellow]结论：存在明显短板，谨慎介入，设好止损[/]")
    else:
        console.print("  [red]结论：风险项较多，暂不建议介入[/]")

    if all_negative > 0:
        console.print(f"\n  [red]主要投资风险（{all_negative}项）：[/]")
        for s in (fund_signals + hist_signals + pe_sigs):
            if s.startswith("✗"):
                console.print(f"    [red]{s}[/]")

    console.print(f"\n[dim]{sep}[/]")


# ════════════════════════════════════════════════════════════
# 详情页：全屏显示单个区块完整内容（支持上下滚动）
# ════════════════════════════════════════════════════════════

def _read_line(prompt: str = "") -> str:
    """模块级逐字符读取一行（支持退格/ESC/Ctrl+C），供各详情页共用"""
    if prompt:
        console.print(prompt, end="")
    buf = ""
    while True:
        ch = getch()
        if ch == "": continue
        if ch in ("\r", "\n"):
            console.print(); return buf.strip()
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x1b":
            console.print(); return ""
        if ch == "\x08":
            if buf:
                buf = buf[:-1]; console.print("\b \b", end="")
        elif ch.isprintable():
            console.print(ch, end=""); buf += ch


def show_detail(title: str, content_fn, args, no_wait: bool = False):
    """全屏打印完整内容，按任意键返回总览"""
    console.clear()
    console.print(f"\n[bold cyan]{'─'*60}[/]")
    hint = "（操作完成后自动返回）" if no_wait else "（按任意键返回总览）"
    console.print(f"[bold cyan]  {title}  详情  {hint}[/]")
    console.print(f"[bold cyan]{'─'*60}[/]\n")
    try:
        content_fn()
    except Exception as e:
        console.print(f"[red]获取失败: {e}[/]")
    if not no_wait:
        console.print(f"\n[dim]{'─'*60}[/]")
        console.print("[dim]按任意键返回总览...[/]")
        # 等待短暂时间，确保之前的按键事件（如触发详情的那次按键）
        # 不会被误读为"返回"，避免秒退
        time.sleep(0.3)
        flush_input()
        getch()


def print_pulse_detail(pulse_data):
    if not pulse_data or "error" in pulse_data:
        console.print("[red]数据获取失败[/]")
        return
    s, ov, br, vol = pulse_data["sentiment"], pulse_data["overview"], pulse_data["breadth"], pulse_data["volume"]
    score = s["score"]
    filled = int(score / 5)
    bar = "[green]" + "█"*filled + "[/][dim]" + "░"*(20-filled) + "[/]"
    lvl = "green" if score >= 55 else ("yellow" if score >= 40 else "red")
    console.print(f"  情绪评分  [{bar}]  {score}/100")
    console.print(f"  [{lvl}]{s['emoji']} {s['level']}[/]  {s['advice']}\n")

    tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
    tbl.add_column("指数");  tbl.add_column("现价", justify="right")
    tbl.add_column("涨跌", justify="right"); tbl.add_column("MA5", justify="right")
    tbl.add_column("MA20", justify="right"); tbl.add_column("趋势")
    for info in ov.values():
        chg = info.get("change_pct", 0)
        cs  = f"[green]{chg:+.2f}%[/]" if chg >= 0 else f"[red]{chg:+.2f}%[/]"
        ma5 = "[green]✓[/]" if info.get("above_ma5") else "[red]✗[/]"
        ma20= "[green]✓[/]" if info.get("above_ma20") else "[red]✗[/]"
        trend = "[green]多头↑[/]" if info.get("above_ma20") else "[red]空头↓[/]"
        tbl.add_row(info["name"], f"{info['close']:.2f}", cs,
                    f"{info['ma5']:.2f}{ma5}", f"{info['ma20']:.2f}{ma20}", trend)
    console.print(tbl)

    zt = br.get("zt",0); dt = br.get("dt",0)
    up = br.get("up",0); dn = br.get("down",0)
    amt = vol.get("total_amount_yi",0); north = vol.get("north_flow_yi",0)
    console.print(f"\n  涨停 [green]{zt}[/] / 跌停 [red]{dt}[/]   上涨 [green]{up}[/] / 下跌 [red]{dn}[/]")
    if amt:
        ac = "green" if amt>=8000 else ("yellow" if amt>=5000 else "red")
        console.print(f"  两市成交 [{ac}]{int(amt)}亿[/]   北向 {'[green]' if north>0 else '[red]'}{north:+.1f}亿[/]")
    zt_list = br.get("zt_list", [])
    if zt_list:
        console.print(f"\n  今日全部涨停 ({len(zt_list)}只):")
        names = "  ".join([f"[green]{n}[/]" for _, n in zt_list])
        console.print(f"  {names}")


def print_news_detail(news_data):
    if not news_data:
        console.print("[dim]暂无重要快讯[/]")
        return
    if "error" in news_data[0]:
        console.print(f"[red]{news_data[0]['error']}[/]")
        return

    console.print("[bold cyan]B 实时快讯 — AI深度分析[/]\n")

    for idx, item in enumerate(news_data, 1):
        sent = item.get("sent", 0); urg = item.get("urg", 1)
        ss = "green" if sent > 0.3 else ("red" if sent < -0.3 else "dim")
        si = "▲利好" if sent > 0.3 else ("▼利空" if sent < -0.3 else "→中性")
        us = "bold red" if urg >= 3 else ("yellow" if urg >= 2 else "dim")
        ui = "❗重磅" if urg >= 3 else ("⚡中" if urg >= 2 else "低")

        console.print(f"  [{us}]{idx:02d}. [{ui}][/]  [dim]{item.get('ts','')}[/]  [{ss}]{si}[/]")
        console.print(f"  [bold]{item['title']}[/]")

        # 关键词板块
        sectors = item.get("sectors", [])
        if sectors:
            console.print(f"  [yellow dim]关键词板块: {', '.join(sectors[:5])}[/]")

        # AI分析结果
        ai = item.get("ai", {})
        if ai:
            nature  = ai.get("nature", "")
            urgency = ai.get("urgency", "")
            b_sec   = ai.get("benefit_sectors", [])
            h_sec   = ai.get("harm_sectors", [])
            logic   = ai.get("logic", "")
            suggest = ai.get("suggestion", "")
            stocks  = ai.get("stocks", [])

            if nature:
                nc = "green" if "利好" in nature else ("red" if "利空" in nature else "dim")
                console.print(f"  [dim]AI分析:[/] [{nc}]{nature}[/]  [dim]{urgency}[/]")
            if b_sec:
                console.print(f"  [dim]受益板块:[/] [green]{', '.join(b_sec[:5])}[/]")
            if h_sec:
                console.print(f"  [dim]受损板块:[/] [red]{', '.join(h_sec[:3])}[/]")
            if logic:
                console.print(f"  [dim]逻辑: {logic[:80]}[/]")
            if suggest:
                console.print(f"  [cyan]操作: {suggest[:80]}[/]")

            # 关联个股表格
            if stocks:
                console.print()
                tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold dim",
                            padding=(0, 1))
                tbl.add_column("代码",  width=8)
                tbl.add_column("名称",  width=10)
                tbl.add_column("今日涨跌", justify="right", width=9)
                tbl.add_column("关联原因", width=12)
                for s in stocks:
                    chg = s.get("chg", 0)
                    chg_s = f"[green]+{chg:.2f}%[/]" if chg > 0 else (
                            f"[red]{chg:.2f}%[/]"   if chg < 0 else f"[dim]{chg:.2f}%[/]")
                    reason = s.get("reason", "")
                    tbl.add_row(s.get("code",""), s.get("name",""), chg_s, reason)
                console.print(tbl)
        elif urg >= 2:
            try:
                from news.ai_analyzer import _ollama_available
                if _ollama_available is False:
                    console.print(f"  [bright_black]AI不可用（未配置Ollama，见 .env 的 OLLAMA_BASE_URL）[/]")
                else:
                    console.print("  [dim]AI分析中（稍后刷新查看）...[/]")
            except Exception:
                pass

        console.print()


def print_scan_detail(scan_data):
    if not scan_data or "off" in scan_data:
        console.print("[dim]非交易时段[/]"); return
    if "error" in scan_data:
        console.print(f"[red]{scan_data['error']}[/]"); return

    zt = scan_data.get("zt_stocks"); dt = scan_data.get("dt_stocks")
    vs = scan_data.get("vol_surge"); fs = scan_data.get("fund_surge")

    if zt is not None and not zt.empty:
        console.print(f"[green bold]涨停股 ({len(zt)}只)[/]")
        tbl = Table(box=box.SIMPLE, show_header=False)
        tbl.add_column("代码"); tbl.add_column("名称")
        for _, r in zt.iterrows():
            tbl.add_row(str(r.get("code","")), f"[green]{r.get('name','')}[/]")
        console.print(tbl)

    if dt is not None and not dt.empty:
        console.print(f"\n[red bold]跌停股 ({len(dt)}只)[/]")
        tbl = Table(box=box.SIMPLE, show_header=False)
        tbl.add_column("代码"); tbl.add_column("名称")
        for _, r in dt.iterrows():
            tbl.add_row(str(r.get("code","")), f"[red]{r.get('name','')}[/]")
        console.print(tbl)

    if vs is not None and not vs.empty:
        console.print(f"\n[yellow bold]放量拉升异动（量比>3x，共{len(vs)}只）[/]")
        tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        tbl.add_column("代码"); tbl.add_column("名称"); tbl.add_column("涨幅", justify="right"); tbl.add_column("量比", justify="right")
        for _, r in vs.iterrows():
            chg = r.get("change_pct",0)
            cs = "green" if chg>0 else "red"
            tbl.add_row(str(r.get("code","")), r.get("name",""),
                        f"[{cs}]{chg:+.2f}%[/]", f"{r.get('vol_ratio',0):.1f}x")
        console.print(tbl)

    if fs is not None and not fs.empty:
        console.print(f"\n[cyan bold]超大单净流入（共{len(fs)}只）[/]")
        tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold")
        tbl.add_column("代码"); tbl.add_column("名称"); tbl.add_column("涨幅", justify="right"); tbl.add_column("净流入", justify="right")
        for _, r in fs.iterrows():
            chg = r.get("change_pct",0); flow = r.get("super_flow",0)
            cs = "green" if chg>0 else "red"
            tbl.add_row(str(r.get("code","")), r.get("name",""),
                        f"[{cs}]{chg:+.2f}%[/]", f"[cyan]{flow/1e8:+.2f}亿[/]")
        console.print(tbl)


def fetch_stock_fund_flow(code: str) -> dict:
    """获取单只股票的主力资金流向（通过 reliable_api 直连 push2.eastmoney.com）"""
    from data.reliable_api import API
    result = {}
    try:
        df = API.fund_flow(code)
        if df is None or df.empty:
            result["error"] = "暂无资金流数据（接口返回空）"
            return result
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "date":      str(r.get("date", ""))[:10],
                "close":     0.0,   # push2接口无收盘价，用0占位
                "chg":       0.0,
                "main_net":  float(r.get("main_net",  0) or 0),
                "super_net": float(r.get("super_net", 0) or 0),
                "big_net":   float(r.get("big_net",   0) or 0),
                "main_pct":  0.0,
            })
        result["history"] = rows
        out_days = 0
        for row in reversed(rows):
            if row["main_net"] < 0:
                out_days += 1
            else:
                break
        result["out_days"] = out_days
        result["main_5d"] = sum(r["main_net"] for r in rows)
    except Exception as e:
        result["error"] = str(e)
    return result


def print_portfolio_detail(port_data):
    from market.portfolio_tracker import remove_position, add_position, load_portfolio

    def _show(port_data):
        """渲染持仓表格，返回 {序号: code} 映射"""
        if not port_data:
            console.print("[dim]暂无持仓记录[/]")
            return {}
        if isinstance(port_data, list) and port_data and "error" in port_data[0]:
            console.print(f"[red]{port_data[0]['error']}[/]")
            return {}

        total_cost = sum(r["cost"] * r["shares"] for r in port_data)
        total_val  = sum(r["mkt_val"] for r in port_data)
        total_pnl  = total_val - total_cost
        pct = total_pnl / total_cost * 100 if total_cost else 0
        ps  = "green" if total_pnl >= 0 else "red"
        console.print(f"  总成本 ¥{total_cost:,.0f}   当前市值 ¥{total_val:,.0f}   "
                      f"总盈亏 [{ps}]¥{total_pnl:+,.0f} ({pct:+.2f}%)[/]\n")

        tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
        tbl.add_column("#",      width=3,  justify="right")
        tbl.add_column("代码",   width=8)
        tbl.add_column("名称",   width=8)
        tbl.add_column("成本",   justify="right", width=7)
        tbl.add_column("现价",   justify="right", width=7)
        tbl.add_column("今涨",   justify="right", width=7)
        tbl.add_column("盈亏%",  justify="right", width=8)
        tbl.add_column("持天",   justify="right", width=5)
        tbl.add_column("市值",   justify="right", width=10)
        tbl.add_column("建议",   width=8)
        tbl.add_column("暴雷风险", width=9)

        idx_map = {}
        for i, r in enumerate(port_data, 1):
            ps2 = "green" if r["pnl_pct"] > 0 else "red"
            cs  = "green" if r["change_pct"] > 0 else "red"
            ac  = {"green": "green", "red": "red"}.get(r["advice_color"], "yellow")

            rr     = _portfolio_risk_cache.get(r["code"], {})
            rscore = rr.get("risk_score", -1)
            if rscore < 0:
                risk_cell = "[dim]未扫描[/]"
            elif rscore >= 70:
                risk_cell = f"[bold red]⚠ 暴雷 {rscore}[/]"
            elif rscore >= 50:
                risk_cell = f"[yellow]△ 中危 {rscore}[/]"
            elif rscore >= 30:
                risk_cell = f"[yellow]! 关注 {rscore}[/]"
            else:
                risk_cell = f"[green]✓ 安全[/]"

            tbl.add_row(
                str(i), r["code"], r["name"],
                f"{r['cost']:.2f}", f"{r['price']:.2f}",
                f"[{cs}]{r['change_pct']:+.2f}%[/]",
                f"[{ps2}]{r['pnl_pct']:+.2f}%[/]",
                f"{r['hold_days']}天",
                f"¥{r['mkt_val']:,.0f}",
                f"[{ac}]{r['advice']}[/]",
                risk_cell,
            )
            idx_map[str(i)] = r["code"]
        console.print(tbl)

        # 卖出信号 + 暴雷警告
        for r in port_data:
            rr      = _portfolio_risk_cache.get(r["code"], {})
            rr_sigs = [s for s in rr.get("signals", []) if s.startswith("⚠")]
            sigs    = r.get("sell_signals", [])

            if rr_sigs or sigs:
                console.print(f"\n  [bold]{r['name']}({r['code']}) 信号:[/]")
                for msg in rr_sigs[:2]:
                    console.print(f"    [bold red]{msg}[/]")
                for urgency, reason in sigs:
                    if urgency == "SELL_NOW":
                        console.print(f"    [red]🚨 {reason}[/]")
                    elif urgency == "REDUCE":
                        console.print(f"    [yellow]⚠️  {reason}[/]")
                    elif urgency == "WATCH":
                        console.print(f"    [dim]👀 {reason}[/]")

        return idx_map

    from market.portfolio_tracker import analyze_portfolio

    # 用列表包装，使内层函数可修改
    state = {"port": port_data}

    def _refresh():
        try:
            state["port"] = analyze_portfolio()
        except Exception as e:
            console.print(f"[red]刷新持仓失败: {e}[/]")
            state["port"] = []
        idx = _show(state["port"])
        # 无论有无持仓都显示操作菜单（空仓时可添加）
        console.print("\n[dim]  暴雷风险说明：✓安全  !关注(30+)  △中危(50+)  ⚠暴雷(70+)  未扫描=按R键执行全仓风险扫描[/]")
        console.print("\n[bold yellow]━━ 快速操作 ━━[/]")
        console.print("  输入[bold red]序号[/]删除持仓  |  输入[bold green]A[/]新增（只需代码，自动识别名称）  |  回车返回")
        return idx

    def _read_line(prompt=""):
        """逐字符读取一行，支持退格，回车结束"""
        if prompt:
            console.print(prompt, end="")
        buf = ""
        while True:
            ch = getch()
            if ch == "": continue   # 特殊键，已由 getch() 处理
            if ch in ("\r", "\n"):
                console.print()
                return buf.strip()
            if ch == "\x03":   # Ctrl+C
                raise KeyboardInterrupt
            if ch == "\x1b":   # ESC：清空当前行重新输入
                if buf:
                    console.print("\r" + " " * (len(prompt) + len(buf) + 2) + "\r" + prompt, end="")
                    buf = ""
                continue
            if ch == "\x08":   # Backspace
                if buf:
                    buf = buf[:-1]
                    console.print("\b \b", end="")
            elif ch.isprintable():   # 只接受可打印字符
                console.print(ch, end="")
                buf += ch

    idx_map = _refresh()

    while True:
        try:
            cmd = _read_line("\n> ").upper()
        except KeyboardInterrupt:
            return

        if not cmd:
            break

        if cmd == "A":
            # ── 逐字段交互式录入 ──────────────────────────────
            try:
                # 1. 股票代码
                code2 = _read_line("  股票代码（ESC取消）: ").strip()
                if not code2:
                    continue

                # 自动查询股票名称
                name2 = ""
                console.print(f"  [dim]正在查询 {code2}...[/]", end="")
                try:
                    from data.reliable_api import API
                    spot = API.spot()
                    if not spot.empty:
                        row = spot[spot["code"].astype(str).str.strip() == code2.strip()]
                        if not row.empty:
                            name2 = str(row.iloc[0].get("name", "")).strip()
                except Exception:
                    pass
                # 退格清掉"正在查询"那行
                console.print(f"\r  [dim]{'':30}[/]\r", end="")

                if name2:
                    console.print(f"  识别到：[bold cyan]{code2} {name2}[/]")
                    confirm_name = _read_line(f"  股票名称（回车确认，或手动输入修改）: ").strip()
                    if confirm_name:
                        name2 = confirm_name
                else:
                    console.print(f"  [yellow]未能自动识别，请手动输入名称[/]")
                    name2 = _read_line("  股票名称: ").strip()
                    if not name2:
                        console.print("[dim]已取消[/]")
                        continue

                # 2. 成本价
                cost_s = _read_line("  买入成本价: ").strip()
                if not cost_s:
                    console.print("[dim]已取消[/]"); continue
                cost2 = float(cost_s)

                # 3. 持仓股数
                shares_s = _read_line("  持仓股数: ").strip()
                if not shares_s:
                    console.print("[dim]已取消[/]"); continue
                shares2 = int(shares_s)

                add_position(code2, name2, cost2, shares2)
                console.print(f"[green]✓ 已添加 {code2} {name2}  成本{cost2}  {shares2}股[/]")
                idx_map = _refresh()
                if not idx_map:
                    break

            except ValueError as e:
                console.print(f"[red]输入格式错误: {e}[/]")
            except KeyboardInterrupt:
                return

        elif cmd in idx_map:
            code = idx_map[cmd]
            name = next((r["name"] for r in state["port"] if r["code"] == code), code)
            console.print(f"  确认删除 [red]{name}({code})[/]？按 Y 确认，其他键取消")
            confirm = getch().upper()
            console.print()
            if confirm == "Y":
                remove_position(code)
                console.print(f"[green]✓ 已删除 {name}({code})[/]")
                idx_map = _refresh()
                if not idx_map:
                    break
            else:
                console.print("[dim]已取消[/]")

        else:
            break  # 不认识的输入直接返回

    # 主力资金分析
    console.print(f"\n[bold cyan]{'─'*60}[/]")
    console.print("[bold cyan]  主力资金分析（近5日）[/]")
    console.print(f"[bold cyan]{'─'*60}[/]")
    console.print("[dim]  正在拉取主力资金数据...[/]")

    for r in state["port"]:
        code = r["code"]; name = r["name"]
        ff = fetch_stock_fund_flow(code)
        console.print(f"\n  [bold]{name}（{code}）[/]")

        if "error" in ff:
            console.print(f"    [dim]资金流数据暂不可用（{ff['error'][:40]}）[/]")
            continue
        if not ff.get("history"):
            console.print(f"    [dim]暂无资金流数据（北交所或数据源未覆盖）[/]")
            continue

        rows = ff.get("history", [])
        out_days = ff.get("out_days", 0)
        main_5d  = ff.get("main_5d", 0)

        # 近5日明细表
        ftbl = Table(box=box.SIMPLE, show_header=True, header_style="bold", padding=(0,1))
        ftbl.add_column("日期",    width=11)
        ftbl.add_column("收盘价",  justify="right", width=8)
        ftbl.add_column("涨跌",    justify="right", width=7)
        ftbl.add_column("主力净流入", justify="right", width=12)
        ftbl.add_column("超大单",  justify="right", width=12)
        ftbl.add_column("大单",    justify="right", width=10)
        ftbl.add_column("主力占比", justify="right", width=8)

        for row in rows:
            mn  = row["main_net"];  ms = "green" if mn>=0 else "red"
            sn  = row["super_net"]; ss = "green" if sn>=0 else "red"
            bn  = row["big_net"];   bs = "green" if bn>=0 else "red"
            cs  = "green" if row["chg"]>=0 else "red"
            ftbl.add_row(
                row["date"],
                f"{row['close']:.2f}",
                f"[{cs}]{row['chg']:+.2f}%[/]",
                f"[{ms}]{mn/1e8:+.2f}亿[/]",
                f"[{ss}]{sn/1e8:+.2f}亿[/]",
                f"[{bs}]{bn/1e8:+.2f}亿[/]",
                f"[{ms}]{row['main_pct']:+.2f}%[/]",
            )
        console.print(ftbl)

        # 综合判断
        m5s = "green" if main_5d>=0 else "red"
        console.print(f"    5日主力累计: [{m5s}]{main_5d/1e8:+.2f}亿[/]", end="")
        if out_days >= 3:
            console.print(f"   [red bold]⚠️  主力已连续{out_days}日净流出，注意风险[/]")
        elif out_days >= 1:
            console.print(f"   [yellow]连续{out_days}日净流出，保持观察[/]")
        elif main_5d > 0:
            console.print(f"   [green]主力持续流入，多头信号[/]")
        else:
            console.print()


def print_pick_detail(pick_data):
    if not pick_data:
        console.print("[dim]加载中...[/]"); return
    if "error" in pick_data[0]:
        console.print(f"[red]{pick_data[0]['error']}[/]"); return
    if "warn" in pick_data[0]:
        console.print(f"[yellow]{pick_data[0]['warn']}[/]"); return

    for sector in pick_data:
        fs = "green" if sector.get("flow", 0) > 0 else "red"
        console.print(f"\n[bold cyan]▶ 板块: {sector['sector']}[/]  "
                      f"热度{sector.get('heat', 0):.3f}  "
                      f"今日流入[{fs}]{sector.get('flow', 0):+.1f}亿[/]")
        console.print("─" * 70)

        stocks = sector.get("stocks", [])
        if not stocks:
            console.print("  [dim]暂无符合条件的龙头股[/]")
            continue

        tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 1))
        tbl.add_column("代码",     width=8)
        tbl.add_column("名称",     width=8)
        tbl.add_column("现价",     justify="right", width=7)
        tbl.add_column("今涨",     justify="right", width=7)
        tbl.add_column("量比",     justify="right", width=6)
        tbl.add_column("暴雷风险", width=7)
        tbl.add_column("建议买入", justify="right", width=8)
        tbl.add_column("止损价",   justify="right", width=7)
        tbl.add_column("目标价",   justify="right", width=7)
        tbl.add_column("盈亏比",   justify="right", width=6)
        tbl.add_column("评级",     width=8)
        tbl.add_column("52周位置", justify="right", width=8)
        tbl.add_column("标签",     width=12)

        for stock in stocks:
            code  = stock.get("code", "")
            name  = str(stock.get("name", ""))[:6]
            price = float(stock.get("price", 0))
            chg   = float(stock.get("chg",   0))
            vr    = float(stock.get("vol_ratio", 1))
            lb    = int(stock.get("lianban", 0))
            lhb   = bool(stock.get("in_lhb", False))
            ev    = stock.get("timing", {})

            cs   = "green" if chg > 0 else "red"
            vr_s = "green" if 1.5 <= vr <= 3.0 else ("red" if vr > 5 else "dim")

            if isinstance(ev, dict) and not ev.get("error"):
                buy_z  = ev.get("buy_zone")
                stop   = ev.get("stop_loss", "—")
                tgt1   = ev.get("target1",   "—")
                rr     = ev.get("risk_reward", 0)
                rating = ev.get("rating", "—")
                rc     = ev.get("rating_color", "dim")
                pos    = ev.get("position_pct")
                buy_s  = f"{buy_z[0]:.2f}" if buy_z else "—"
                rr_c   = "green" if rr >= 2 else ("yellow" if rr >= 1.5 else "red")
                pos_s  = f"{pos:.0f}%" if pos is not None else "—"
            else:
                buy_s = "—"; stop = "—"; tgt1 = "—"
                rr = 0; rating = "—"; rc = "dim"; rr_c = "dim"; pos_s = "—"

            zt_label = stock.get("zt_label", "")
            is_zt    = stock.get("is_zt", False)

            tags = []
            if zt_label:
                zt_col = {"次日可博": "bold yellow", "次日观察": "yellow",
                          "次日谨慎": "dim yellow", "高风险": "bold red"}.get(zt_label, "yellow")
                tags.append(f"[{zt_col}]涨停·{zt_label}[/]")
            elif lb >= 2:
                tags.append(f"[yellow]{lb}连板[/]")
            if lhb:
                tags.append("[magenta]龙虎榜[/]")
            tag_str = " ".join(tags)
            tag_col = "bold yellow" if tags else "dim"

            risk_badge = _risk_badge_cached(code) or "[dim]—[/]"
            tbl.add_row(
                code, name,
                f"{price:.2f}" if price else "—",
                f"[{cs}]{chg:+.1f}%[/]",
                f"[{vr_s}]{vr:.1f}[/]",
                risk_badge,
                f"[cyan]{buy_s}[/]",
                f"[red]{stop}[/]"   if stop != "—" else "—",
                f"[green]{tgt1}[/]" if tgt1 != "—" else "—",
                f"[{rr_c}]{rr:.1f}[/]" if rr else "—",
                f"[{rc}]{rating}[/]",
                pos_s,
                f"[{tag_col}]{tag_str}[/]" if tag_str else "[dim]—[/]",
            )

        console.print(tbl)

        for stock in stocks:
            ev = stock.get("timing", {})
            if not isinstance(ev, dict) or ev.get("error"):
                continue
            sigs = ev.get("signals", [])
            if sigs:
                console.print(f"  [dim]{stock.get('code')} {stock.get('name')} 信号:[/]")
                for icon, msg in sigs[:4]:
                    console.print(f"    {icon} [dim]{msg}[/]")
        console.print()

    console.print("[dim]  暴雷风险：✓安全  !关注(30+)  △中危(50+)  ⚠暴雷(70+)  —=未扫描（按I键扫描个股）[/dim]\n")

    # 汇总所有股票供 W 快捷键使用
    all_stocks = []
    for sector in pick_data:
        for s in sector.get("stocks", []):
            all_stocks.append(s)

    if all_stocks:
        console.print("[dim]  按 [bold]W[/] 将股票加入观察池，输入编号后回车；其他键返回[/]")
        for i, s in enumerate(all_stocks, 1):
            console.print(f"  [dim]{i}.[/] {s.get('code')} {s.get('name')}  "
                          f"{s.get('price', 0):.2f}")
        try:
            ch = getch()
            if ch.lower() == "w":
                console.print("\n  输入编号（1-{n}）: ".format(n=len(all_stocks)), end="")
                idx_str = ""
                while True:
                    c = getch()
                    if c == "": continue
                    if c in ("\r", "\n"):
                        break
                    if c == "\x08" and idx_str:
                        idx_str = idx_str[:-1]
                        console.print("\b \b", end="")
                    elif c.isdigit():
                        idx_str += c
                        console.print(c, end="")
                console.print()
                try:
                    idx = int(idx_str)
                    if 1 <= idx <= len(all_stocks):
                        s = all_stocks[idx - 1]
                        from data.watchlist import add_watch
                        ev = s.get("timing", {}) or {}
                        ok = add_watch(
                            code=str(s.get("code", "")),
                            name=str(s.get("name", "")),
                            price=float(s.get("price", 0)),
                            source="pick",
                            stop_loss=ev.get("stop_loss") if isinstance(ev, dict) else None,
                            target=ev.get("target1") if isinstance(ev, dict) else None,
                        )
                        if ok:
                            console.print(f"  [green]✓ 已加入观察池: {s.get('code')} {s.get('name')}[/]")
                        else:
                            console.print(f"  [yellow]该股票已在观察池中[/]")
                    else:
                        console.print("  [dim]编号超出范围[/]")
                except ValueError:
                    console.print("  [dim]无效编号[/]")
                import time as _t; _t.sleep(1.5)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════
# 观察池自动入池逻辑
# ════════════════════════════════════════════════════════════

def _auto_watch_pick(pick: list):
    """E区块：热门选股龙头自动入观察池（每日最多2只，门槛更严）。"""
    if not pick or not isinstance(pick, list):
        return
    if pick and ("error" in pick[0] or "warn" in pick[0]):
        return
    try:
        from data.watchlist import add_watch, can_auto_add
    except Exception:
        return
    if not can_auto_add(daily_limit=5):
        return
    added = []
    candidates = []
    for sector in pick:
        for s in sector.get("stocks", []):
            score = float(s.get("score", 0))
            chg   = float(s.get("chg", 0))
            price = float(s.get("price", 0))
            ev    = s.get("timing") or {}
            rr    = float(ev.get("risk_reward", 0)) if isinstance(ev, dict) else 0
            # 门槛：评分≥85、涨幅1~7%（不追高）、盈亏比≥2、非涨停
            if score < 85 or not (1.0 < chg < 7.0) or rr < 2.0:
                continue
            candidates.append((score, s, ev))
    # 按评分排序，只取最高的2只
    candidates.sort(key=lambda x: -x[0])
    for score, s, ev in candidates[:2]:
        if not can_auto_add(daily_limit=5):
            break
        stop = ev.get("stop_loss") if isinstance(ev, dict) else None
        tgt  = ev.get("target1")   if isinstance(ev, dict) else None
        ok = add_watch(
            code=str(s.get("code", "")), name=str(s.get("name", "")),
            price=float(s.get("price", 0)), source="pick",
            stop_loss=stop, target=tgt,
        )
        if ok:
            added.append(f"{s.get('code')} {s.get('name')}")
    if added:
        logger.info(f"E区块自动入观察池: {', '.join(added)}")


def _auto_watch_quality(quality: dict):
    """F区块：优质股扫描，每日最多3只，取综合评分最高且满足严格条件的。"""
    if not quality or not isinstance(quality, dict) or "error" in quality:
        return
    try:
        from data.watchlist import add_watch, can_auto_add
    except Exception:
        return
    if not can_auto_add(daily_limit=5):
        return
    candidates = []
    for df, mode in [
        (quality.get("momentum"), "momentum"),
        (quality.get("value"),    "value"),
    ]:
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            sc       = float(r.get("total_score", 0))
            ma_align = bool(r.get("ma_align", False))
            price    = float(r.get("price", 0))
            ma5      = float(r.get("ma5",  0)) or price
            ma20     = float(r.get("ma20", 0)) or price * 0.95
            chg      = float(r.get("chg",  0))
            rs       = float(r.get("rs_pct", 0))
            # 门槛更严：评分≥85、均线多头、涨幅0~7%、近期强势但不过热
            if sc < 85 or not ma_align or chg < 0 or chg > 7:
                continue
            if rs > 20:   # 距MA20超20%，追高风险
                continue
            buy    = ma5  * 1.005
            stop   = ma20 * 0.97
            target = price * (1.18 if mode == "momentum" else 1.13)
            loss   = buy - stop
            rr     = (target - buy) / loss if loss > 0 else 0
            if rr < 2.0:
                continue
            candidates.append((sc, mode, r, stop, target))
    # 按评分取最高的3只
    candidates.sort(key=lambda x: -x[0])
    added = []
    for sc, mode, r, stop, target in candidates[:3]:
        if not can_auto_add(daily_limit=5):
            break
        ok = add_watch(
            code=str(r.get("code", "")), name=str(r.get("name", "")),
            price=float(r.get("price", 0)), source=mode,
            stop_loss=round(stop, 3), target=round(target, 3),
        )
        if ok:
            added.append(f"{r.get('code')} {r.get('name')}")
    if added:
        logger.info(f"F区块自动入观察池: {', '.join(added)}")


def _auto_watch(advice: dict):
    """G区块：操作建议 buylist 中 priority=1 且 RR≥2.0 且大盘可操作的自动入池。"""
    if not advice or "error" in advice:
        return
    if not advice.get("can_operate", False):
        return
    try:
        from data.watchlist import add_watch
    except Exception:
        return
    added = []
    for item in advice.get("buylist", []):
        if item.get("priority") != 1:
            continue
        if item.get("risk_reward", 0) < 2.0:
            continue
        ok = add_watch(
            code=str(item.get("code", "")),
            name=str(item.get("name", "")),
            price=float(item.get("price", 0)),
            source=item.get("source", "advisor"),
            stop_loss=item.get("stop_loss"),
            target=item.get("target"),
        )
        if ok:
            added.append(f"{item.get('code')} {item.get('name')}")
    if added:
        logger.info(f"G区块自动入观察池: {', '.join(added)}")


# ════════════════════════════════════════════════════════════
# 主循环
# ════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="A股多区块实时仪表盘")
    parser.add_argument("--interval",      type=int, default=10,
                        help="全局刷新间隔（分钟），默认10")
    parser.add_argument("--pick-interval", type=int, default=30,
                        help="选股刷新间隔（分钟），默认30")
    parser.add_argument("--no-pick",       action="store_true",
                        help="跳过选股模块（更快）")
    args = parser.parse_args()

    console.print("[bold cyan]  A股量化仪表盘启动中，首次加载约1-3分钟...[/]")

    pulse_data = port_data = scan_data = pick_data = quality_data = advice_data = kill_data = None
    news_data  = []
    last_update = "--:--"

    # 首次加载
    pulse_score = 50
    pulse_data = fetch_pulse()
    if isinstance(pulse_data, dict) and "sentiment" in pulse_data:
        pulse_score = pulse_data["sentiment"]["score"]
    news_data  = fetch_news()
    scan_data  = fetch_scan()
    port_data  = fetch_portfolio()
    # 启动时同步扫描持仓暴雷风险（确保首帧就有风险列数据）
    console.print("[dim]  正在扫描持仓暴雷风险...[/]")
    _refresh_portfolio_risk(port_data, sync=True)
    if not args.no_pick:
        pick_data = fetch_pick(pulse_score, args.pick_interval)
        _auto_watch_pick(pick_data)

    # 观察池：先清理再拍快照
    try:
        from data.watchlist import take_snapshot as _wl_snapshot, purge_watchlist
        purge_watchlist(max_watch=20, max_loss_pct=-10.0)
        _wl_snapshot()
    except Exception as _e:
        logger.debug(f"watchlist 初始化失败: {_e}")

    # 优质股扫描 + 操作建议 在后台运行，不阻塞首屏
    import threading as _threading
    _quality_result = [None]
    _advice_result  = [None]

    def _bg_quality():
        try:
            q = fetch_quality(interval_min=60)
            _quality_result[0] = q
            _auto_watch_quality(q)
        except Exception as e:
            logger.warning(f"后台优质股扫描线程异常: {e}")
            _quality_result[0] = {"error": str(e)}

    def _bg_advice():
        # 等优质股扫描完成再计算建议（最多等180秒）
        import time as _t
        for _ in range(180):
            if _quality_result[0] is not None:
                break
            _t.sleep(1)
        try:
            from data.tracker import get_consecutive_picks
            from market.daily_advisor import build_daily_advice
            consecutive = get_consecutive_picks(days=5)
            adv = build_daily_advice(
                pulse_data, _quality_result[0], pick_data, consecutive
            )
            _advice_result[0] = adv
            _auto_watch(adv)
        except Exception as e:
            logger.warning(f"操作建议计算失败: {e}")
            _advice_result[0] = {"error": str(e)}

    _threading.Thread(target=_bg_quality, daemon=True).start()
    _threading.Thread(target=_bg_advice,  daemon=True).start()
    last_update = datetime.now().strftime("%H:%M:%S")

    # 详情页定义（按键 -> (标题, 打印函数)）
    detail_map = {
        "a": ("A 大盘情绪",   lambda: print_pulse_detail(pulse_data)),
        "b": ("B 实时快讯",   lambda: print_news_detail(news_data)),
        "c": ("C 盘中异动",   lambda: print_scan_detail(scan_data)),
        "d": ("D 持仓跟踪",   lambda: print_portfolio_detail(port_data)),
        "e": ("E 热门选股",   lambda: print_pick_detail(pick_data)),
        "f": ("F 全市场优质股", lambda: print_quality_detail(
            quality_data or _quality_result[0] or _quality_cache.get("data")
        )),
        "g": ("G 今日操作建议", lambda: print_advisor_detail(
            advice_data or _advice_result[0]
        )),
        "w": ("W 观察池",       lambda: print_watchlist_detail()),
        "s": ("S 收盘总结",     lambda: print_closing_summary_detail()),
        "i": ("I 个股分析",     lambda: print_stock_analysis_detail()),
        "r": ("R 持仓风险扫描", lambda: print_risk_scan_detail(port_data)),
        "k": ("K 错杀反弹",     lambda: print_kill_detail(kill_data)),
    }

    def redraw():
        nonlocal advice_data, kill_data
        # 同步后台结果
        if _advice_result[0] is not None and advice_data is None:
            advice_data = _advice_result[0]
        # 获取大盘跌幅用于错杀扫描（pulse_data["overview"]["sh"]["change_pct"]）
        _idx_chg = 0.0
        try:
            if isinstance(pulse_data, dict):
                _idx_chg = float(
                    pulse_data.get("overview", {}).get("sh", {}).get("change_pct", 0) or 0
                )
        except Exception:
            pass
        kill_data = fetch_kill(_idx_chg)
        layout = build_layout(pulse_data, news_data, scan_data, port_data,
                              pick_data, quality_data, advice_data,
                              args.interval, args.pick_interval,
                              args.no_pick, last_update, next_refresh,
                              kill_data=kill_data)
        console.clear()
        console.print(layout)

    def refresh_data():
        nonlocal pulse_data, news_data, scan_data, port_data
        nonlocal pick_data, quality_data, advice_data, kill_data
        nonlocal last_update, pulse_score
        console.clear()
        console.print("[dim]  正在刷新数据...[/]")
        pulse_data = fetch_pulse()
        if isinstance(pulse_data, dict) and "sentiment" in pulse_data:
            pulse_score = pulse_data["sentiment"]["score"]
        news_data  = fetch_news()
        scan_data  = fetch_scan()
        port_data  = fetch_portfolio()
        _refresh_portfolio_risk(port_data)
        if not args.no_pick:
            pick_data = fetch_pick(pulse_score, args.pick_interval)
            _auto_watch_pick(pick_data)
        quality_data = fetch_quality(interval_min=60)
        _auto_watch_quality(quality_data)
        # 同步更新操作建议
        try:
            from data.tracker import get_consecutive_picks
            from market.daily_advisor import build_daily_advice
            advice_data = build_daily_advice(
                pulse_data, quality_data, pick_data, get_consecutive_picks(days=5)
            )
            _auto_watch(advice_data)
        except Exception as e:
            logger.warning(f"刷新操作建议失败: {e}")
        # 更新观察池：清理 + 快照
        try:
            from data.watchlist import take_snapshot as _wl_snap, purge_watchlist
            purge_watchlist(max_watch=20, max_loss_pct=-10.0)
            _wl_snap()
        except Exception as _e:
            logger.debug(f"watchlist 刷新失败: {_e}")
        last_update = datetime.now().strftime("%H:%M:%S")

    next_refresh = time.time() + args.interval * 60
    redraw()

    while True:
        try:
            # 后台优质股扫描完成后同步结果
            if _quality_result[0] is not None and quality_data is None:
                quality_data = _quality_result[0]
                redraw()

            # 检查按键
            # Linux/macOS 下 Rich Live 会接管终端，kbhit() 的 select 可能失效，
            # 因此统一用带超时的 getch()（0.1s），既不阻塞也不依赖 kbhit。
            if _IS_WIN:
                ch = getch() if kbhit() else ""
            else:
                ch = getch()   # 内部有 0.5s select 超时，不会永久阻塞

            if ch:
                flush_input()
                if ch == "\x03" or ch.lower() == "q":  # Ctrl+C 或 Q
                    break
                elif ch.lower() in detail_map:
                    key = ch.lower()
                    title, fn = detail_map[key]
                    show_detail(title, fn, args, no_wait=(key == "d"))
                    redraw()
                    continue

            # 定时刷新数据
            if time.time() >= next_refresh:
                refresh_data()
                next_refresh = time.time() + args.interval * 60
                redraw()
            elif int(time.time()) % 30 == 0:
                redraw()

        except KeyboardInterrupt:
            break
        except Exception as e:
            console.print(f"[red]异常: {e}[/]")
            time.sleep(3)
            redraw()

    console.print("[yellow]  已退出仪表盘[/]")


if __name__ == "__main__":
    main()
