"""
龙头股筛选模块
给定热门板块，从成分股中筛选 3~5 只龙头

数据来源：新浪行业（constituent stocks）+ THS（sector heat）
筛选维度：
  1. 今日涨幅                   强势度（权重30%）
  2. 换手率适中（2%~20%）         流动性合理
  3. 市值筛选（10亿~800亿）       排除微盘壳股和超大蓝筹
  4. 是否涨停 / 连板              强势信号（权重25%）
  5. 市值相对大（板块内领头）      龙头特征（权重20%）
  6. 成交额相对大                 资金聚焦（权重25%）
"""
import logging
import time
import pandas as pd

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 板块成分股（新浪数据源）
# ──────────────────────────────────────────────────────────
def get_sector_stocks_sina(sina_label: str) -> pd.DataFrame:
    """
    通过新浪行业标签获取成分股及今日行情
    返回含 code/name/close/change_pct/turnover/market_cap/amount 的 DataFrame
    """
    from data.reliable_api import API
    import math
    if not sina_label or (isinstance(sina_label, float) and math.isnan(sina_label)):
        return pd.DataFrame()
    try:
        df = API.sector_members_sina(sina_label)
        if df.empty:
            return pd.DataFrame()

        # 列名映射
        rename = {}
        col_map = {
            "code":          ["code", "代码"],
            "name":          ["name", "名称"],
            "close":         ["trade", "最新价"],
            "change_pct":    ["changepercent", "涨跌幅"],
            "turnover":      ["turnoverratio", "换手率"],
            "market_cap":    ["mktcap", "nmc", "市值"],
            "amount":        ["amount", "成交额"],
        }
        for target, srcs in col_map.items():
            for s in srcs:
                if s in df.columns:
                    rename[s] = target
                    break

        df = df.rename(columns=rename)

        # 标准化代码：去掉前缀，补零到6位
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True).str.zfill(6)

        return df
    except Exception as e:
        logger.warning(f"获取板块 [{sina_label}] 成分股失败: {e}")
        return pd.DataFrame()


def get_sector_stocks(sector_name: str, sector_type: str = "行业资金流",
                      sina_label: str = None) -> pd.DataFrame:
    """
    获取板块成分股，优先使用新浪数据
    """
    if sina_label:
        df = get_sector_stocks_sina(sina_label)
        if not df.empty:
            return df

    # 无 Sina 标签时用 reliable_api（带自动重试和缓存）
    from data.reliable_api import API
    return API.sector_members_em(sector_name, is_concept=("概念" in sector_type))


# ──────────────────────────────────────────────────────────
# 涨停池（连板信息）
# ──────────────────────────────────────────────────────────
def get_zt_info() -> dict:
    """
    返回 {股票代码: 连板数} 的字典
    """
    import akshare as ak
    result = {}
    try:
        df = ak.stock_zt_pool_em(date=pd.Timestamp.today().strftime("%Y%m%d"))
        df.columns = [c.strip() for c in df.columns]
        code_col    = _find_col(df, ["代码", "股票代码"])
        lianban_col = _find_col(df, ["连续涨停天数", "连板数", "连板"])
        if code_col and lianban_col:
            for _, row in df.iterrows():
                result[str(row[code_col])] = int(row[lianban_col]) if pd.notna(row[lianban_col]) else 1
    except Exception as e:
        logger.warning(f"获取涨停池失败: {e}")
    return result


# ──────────────────────────────────────────────────────────
# 龙虎榜
# ──────────────────────────────────────────────────────────
def get_lhb_stocks(days: int = 3) -> set:
    """获取最近 N 日龙虎榜股票代码集合"""
    from data.reliable_api import API
    return API.lhb(days=days)


# ──────────────────────────────────────────────────────────
# 核心选股逻辑
# ──────────────────────────────────────────────────────────
def pick_leaders(
    sector_name: str,
    sector_type: str,
    flow_rank_today: pd.DataFrame,
    flow_rank_5d: pd.DataFrame,
    lhb_codes: set,
    zt_info: dict,
    top_n: int = 5,
    sina_label: str = None,
) -> pd.DataFrame:
    """
    在给定板块的成分股中，筛选出龙头股
    返回每行一只股票，含评分和各维度指标
    """
    members = get_sector_stocks(sector_name, sector_type, sina_label)
    if members.empty:
        return pd.DataFrame()

    code_col    = _find_col(members, ["code", "代码", "股票代码"])
    name_col    = _find_col(members, ["name", "名称", "股票名称"])
    price_col   = _find_col(members, ["close", "trade", "最新价", "收盘价"])
    chg_col     = _find_col(members, ["change_pct", "changepercent", "涨跌幅"])
    turn_col    = _find_col(members, ["turnover", "turnoverratio", "换手率"])
    cap_col     = _find_col(members, ["market_cap", "mktcap", "nmc", "流通市值", "总市值"])
    amt_col     = _find_col(members, ["amount", "成交额"])

    if not code_col:
        logger.warning(f"板块 [{sector_name}] 成分股数据缺少代码列")
        return pd.DataFrame()

    members = members.rename(columns={code_col: "code"})
    members["code"] = members["code"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True).str.zfill(6)

    members["name"] = members[name_col].astype(str) if name_col else ""

    # ── 基础过滤：ST / *ST / 退市 / 科创板 / 北交所 ──────────
    # regex=True，一次覆盖 ST、*ST、退市、暂停
    members = members[~members["name"].str.contains(
        r"(^|\*)?ST|退市|退|暂停", na=False, regex=True)]
    members = members[~members["code"].str.startswith("688")]
    members = members[~members["code"].str.match(r"^(82|83|87|43|40)")]

    # ── 价格下限：排除仙股和低价垃圾股 ──────────────────────
    if price_col := next((c for c in members.columns if c in ("price","trade","close","最新价")), None):
        members["price_num"] = pd.to_numeric(members[price_col], errors="coerce").fillna(0)
        members = members[members["price_num"] >= 3.0]

    # ── 暴雷风险过滤 ──────────────────────────────────────────
    # 1. 近5日累计大跌（-15%以上）：主力已经出逃
    if chg_col:
        chg_series = pd.to_numeric(members[chg_col], errors="coerce").fillna(0)
        members = members[chg_series > -15]

    # 2. 换手率过低（<0.3%）：流动性枯竭，机构回避
    #    换手率过高（>20%）：已被游资爆炒，风险极大
    if turn_col:
        members["turnover"] = pd.to_numeric(members[turn_col], errors="coerce")
        members = members[
            members["turnover"].between(0.3, 20) | members["turnover"].isna()
        ]

    # 3. 市值过滤（20亿~500亿）：
    #    - <20亿：微盘壳股，易被操控，财务造假风险高
    #    - >500亿：超大蓝筹，弹性不足，不适合短线
    if cap_col:
        members["market_cap"] = pd.to_numeric(members[cap_col], errors="coerce")
        if members["market_cap"].median() < 1e7:
            members["market_cap"] = members["market_cap"] * 1e4  # 万元→元
        members = members[
            members["market_cap"].between(20e8, 500e8) | members["market_cap"].isna()
        ]

    if members.empty:
        return pd.DataFrame()

    # ── 涨幅评分 ──────────────────────────────────────────
    if chg_col:
        members["change_pct"] = pd.to_numeric(members[chg_col], errors="coerce").fillna(0)
    else:
        members["change_pct"] = 0.0
    members["score_change"] = _rank01(members["change_pct"])

    # ── 成交额评分 ────────────────────────────────────────
    if amt_col:
        members["amount"] = pd.to_numeric(members[amt_col], errors="coerce").fillna(0)
    else:
        members["amount"] = 0.0
    members["score_amount"] = _rank01(members["amount"])

    # ── 市值评分（偏大市值为龙头，但不要最大的）────────────
    if cap_col and "market_cap" in members.columns:
        members["score_cap"] = _rank01(members["market_cap"])
    else:
        members["score_cap"] = 0.5

    # 龙虎榜加分
    members["in_lhb"] = members["code"].isin(lhb_codes).astype(float)

    # ── 连板信息 ──────────────────────────────────────────
    members["lianban"] = members["code"].map(zt_info).fillna(0)

    # ── 判断今日是否涨停（涨幅 >= 9.5%）────────────────────
    if "change_pct" in members.columns:
        members["is_zt"] = members["change_pct"] >= 9.5
    else:
        members["is_zt"] = False

    # ── 次日开板标注 ────────────────────────────────────────
    # 首板(1板)：开板概率 >60%，次日追高风险大   → "次日观察"
    # 2-4板：情绪高位，市值20-80亿可博          → "次日可博" / "次日谨慎"
    # 5板+：炸板风险极大                        → "高风险"
    def _zt_label(row):
        if not row.get("is_zt", False):
            return ""
        lb  = int(row.get("lianban", 0))
        cap = float(row.get("market_cap", 0) or 0)
        if lb <= 1:
            return "次日观察"
        elif lb <= 4:
            return "次日可博" if 20e8 <= cap <= 80e8 else "次日谨慎"
        else:
            return "高风险"

    members["zt_label"] = members.apply(_zt_label, axis=1)

    # ── 连板/涨停评分（区分今日可买 vs 次日策略）────────────
    def _lianban_score(row):
        lb    = int(row.get("lianban", 0))
        is_zt = row.get("is_zt", False)
        if not is_zt:
            return min(lb * 0.08, 0.32)   # 非涨停：历史连板是强势信号
        label = row.get("zt_label", "")
        return {"次日可博": 0.20, "次日观察": 0.05, "次日谨慎": 0.05, "高风险": 0.0}.get(label, 0.0)

    members["score_lianban"] = members.apply(_lianban_score, axis=1)

    # ── 综合评分 ──────────────────────────────────────────
    members["total_score"] = (
        members["score_change"]  * 0.25 +
        members["score_amount"]  * 0.30 +
        members["score_lianban"] * 0.25 +
        members["score_cap"]     * 0.20
    )
    members["total_score"] += members["in_lhb"] * 0.10

    # ── 涨停股降权：今天买不进去，排在可买股后面 ────────────
    # 首板 / 高风险连板压到底，"次日可博"保留但不排最前
    members.loc[members["is_zt"] & (members["lianban"] <= 1),  "total_score"] -= 0.35
    members.loc[members["zt_label"] == "高风险",                "total_score"] -= 0.50

    # ── 暴雷风险扣分 ──────────────────────────────────────
    if "change_pct" in members.columns:
        members.loc[members["change_pct"] < -5, "total_score"] -= 0.30
        members.loc[members["change_pct"] < -8, "total_score"] -= 0.20
    if "turnover" in members.columns:
        members.loc[members["turnover"] > 20,   "total_score"] -= 0.15
    if "market_cap" in members.columns:
        members.loc[members["market_cap"] < 20e8, "total_score"] -= 0.20

    members["total_score"] = members["total_score"].clip(0, 1.1)

    out = members.sort_values("total_score", ascending=False).head(top_n).reset_index(drop=True)
    out.index += 1

    keep_cols = ["code", "name", "total_score", "change_pct",
                 "amount", "in_lhb", "lianban", "is_zt", "zt_label"]
    keep_cols = [c for c in keep_cols if c in out.columns]
    return out[keep_cols]


# ──────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────
def _rank01(series: pd.Series) -> pd.Series:
    mn, mx = series.min(), series.max()
    if mx == mn:
        return series.map(lambda _: 0.5)
    return ((series - mn) / (mx - mn)).fillna(0.5)


def _find_col(df: pd.DataFrame, candidates: list) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    for c in df.columns:
        for cand in candidates:
            if cand in c or c in cand:
                return c
    return None
