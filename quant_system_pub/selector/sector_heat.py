"""
板块热度分析模块
数据来源：同花顺（THS）+ 新浪行业
push2.eastmoney.com 在部分网络环境下不可访问，改用 THS + Sina 接口

评分维度：
  - 今日净流入（权重40%）
  - 今日涨幅（权重35%）
  - 涨停股数量（权重15%）
  - 上涨家数比例（权重10%）
"""
import logging
import time
import pandas as pd

logger = logging.getLogger(__name__)

# THS板块名 → Sina板块label 映射
# Sina行业标签完整列表来自 stock_sector_spot(indicator='新浪行业')
THS_TO_SINA = {
    # 电子/科技
    "半导体":     "new_dzqj",
    "电子化学品":  "new_hghy",
    "元件":       "new_dzqj",
    "其他电子":   "new_dzxx",
    "光学光电子":  "new_dzxx",
    "消费电子":   "new_dzxx",
    "软件开发":   "new_dzxx",
    "计算机设备":  "new_dzxx",
    "通信设备":   "new_dzxx",
    "电子":       "new_dzqj",
    "IT服务":     "new_dzxx",
    "数据中心":   "new_dzxx",
    "人工智能":   "new_dzxx",
    "云计算":     "new_dzxx",
    # 机械/设备
    "自动化设备":  "new_jxhy",
    "机械设备":   "new_jxhy",
    "通用设备":   "new_jxhy",
    "专用设备":   "new_fzjx",
    "仪器仪表":   "new_yqyb",
    "工程机械":   "new_jxhy",
    # 新能源/电力
    "电力设备":   "new_fdsb",
    "电池":       "new_fdsb",
    "光伏":       "new_fdsb",
    "风电":       "new_fdsb",
    "储能":       "new_fdsb",
    "电力":       "new_dlhy",
    "核电":       "new_dlhy",
    "水电":       "new_dlhy",
    # 汽车
    "汽车零部件":  "new_qczz",
    "汽车整车":   "new_qczz",
    "汽车":       "new_qczz",
    # 医药
    "医药生物":   "new_swzz",
    "医疗器械":   "new_ylqx",
    "医药":       "new_swzz",
    "生物制品":   "new_swzz",
    "化学制药":   "new_swzz",
    "中药":       "new_swzz",
    "医疗服务":   "new_swzz",
    "农化用品":   "new_nyhf",
    # 材料/化工
    "化工":       "new_hghy",
    "化学原料":   "new_hghy",
    "化学制品":   "new_hghy",
    "橡胶":       "new_hghy",
    "塑料":       "new_hghy",
    "化学纤维":   "new_fzjx",
    "非金属材料":  "new_jzjc",
    "建筑材料":   "new_jzjc",
    "玻璃":       "new_jzjc",
    "水泥":       "new_jzjc",
    # 有色/黑色金属
    "有色金属":   "new_ysjs",
    "小金属":     "new_ysjs",   # THS小金属→新浪有色金属
    "能源金属":   "new_ysjs",   # 锂/钴/镍等能源金属
    "贵金属":     "new_ysjs",
    "铝":         "new_ysjs",
    "铜":         "new_ysjs",
    "钢铁":       "new_gthy",
    "特钢":       "new_gthy",
    # 能源
    "煤炭":       "new_mthy",
    "煤炭采选":   "new_mthy",
    "石油石化":   "new_syhy",
    "石油":       "new_syhy",
    "油气":       "new_syhy",
    # 军工
    "国防军工":   "new_fjzz",
    "航空航天":   "new_fjzz",
    "军工":       "new_fjzz",
    # 消费
    "食品饮料":   "new_sphy",
    "食品加工":   "new_sphy",
    "白酒":       "new_ljhy",
    "酿酒":       "new_ljhy",
    "美容护理":   "new_fzxl",
    "纺织服装":   "new_fzhy",
    "服装":       "new_fzxl",
    "鞋":         "new_fzxl",
    "家用电器":   "new_jdhy",
    "家电":       "new_jdhy",
    "家具":       "new_jjhy",
    "零售":       "new_sybh",
    "商业贸易":   "new_sybh",
    "商超":       "new_sybh",
    "酒店旅游":   "new_jdly",
    "旅游":       "new_jdly",
    "航空":       "new_jtys",
    "食品":       "new_sphy",
    "乳制品":     "new_slzp",
    # 金融
    "银行":       "new_jrhy",
    "证券":       "new_jrhy",
    "保险":       "new_jrhy",
    "金融":       "new_jrhy",
    # 地产/建筑
    "房地产":     "new_fdc",
    "建筑装饰":   "new_jzjc",
    "建筑":       "new_jzjc",
    # 农业
    "农林牧渔":   "new_nlmy",
    "农业":       "new_nlmy",
    "养殖":       "new_nlmy",
    # 传媒/娱乐
    "传媒":       "new_cmyl",
    "游戏":       "new_cmyl",
    "影视":       "new_cmyl",
    "广播":       "new_cmyl",
    # 基础设施/公用
    "交通运输":   "new_jtys",
    "公路":       "new_glql",
    "铁路":       "new_jtys",
    "港口":       "new_jtys",
    "环保":       "new_hbhy",
    "公用事业":   "new_dlhy",
    "水务":       "new_snhy",
    # 其他
    "印刷包装":   "new_ysbz",
    "包装":       "new_ysbz",
    "造纸":       "new_zzhy",
    "摩托车":     "new_mtc",
    "综合":       "new_zhhy",
    "多元金融":   "new_jrhy",
    "开发区":     "new_kfq",
}


def get_sector_fund_flow(sector_type: str = "行业资金流") -> pd.DataFrame:
    """获取板块资金流排名（通过 reliable_api 自动 failover）"""
    from data.reliable_api import API
    df = API.sector_flow()
    if df.empty:
        return df
    # 映射回原有列名（下游代码依赖）
    rename = {"sector_name": "板块", "flow_yi": "净流入",
              "chg": "涨跌幅", "up": "上涨家数", "down": "下跌家数"}
    return df.rename(columns={k: v for k, v in rename.items() if k in df.columns})


def get_sector_fund_flow_5d(sector_type: str = "行业资金流") -> pd.DataFrame:
    """5日数据暂用今日数据代替（THS接口不提供5日汇总）"""
    return pd.DataFrame()


def get_zt_pool() -> pd.DataFrame:
    """获取今日涨停股池"""
    import akshare as ak
    try:
        df = ak.stock_zt_pool_em(date=pd.Timestamp.today().strftime("%Y%m%d"))
        df.columns = [c.strip() for c in df.columns]
        return df
    except Exception as e:
        logger.warning(f"获取涨停池失败: {e}")
        return pd.DataFrame()


def get_sina_sector_label(ths_name: str) -> str | None:
    """根据THS板块名获取对应Sina标签（用于获取成分股）"""
    # 精确匹配
    if ths_name in THS_TO_SINA:
        return THS_TO_SINA[ths_name]
    # 模糊匹配
    for key, label in THS_TO_SINA.items():
        if key in ths_name or ths_name in key:
            return label
    return None


def _rank01(series: pd.Series) -> pd.Series:
    """0-1 百分位归一化，NaN 补 0.5"""
    mn, mx = series.min(), series.max()
    if mx == mn:
        return series.map(lambda _: 0.5)
    return ((series - mn) / (mx - mn)).fillna(0.5)


def rank_hot_sectors(top_n: int = 5) -> pd.DataFrame:
    """
    返回最热的 top_n 个板块

    返回列：sector_name / sector_type / heat_score /
            flow_today_yi(亿) / flow_5d_yi(亿) / change_pct(%) / zt_count
    """
    df1 = get_sector_fund_flow("行业资金流")

    if df1.empty:
        return pd.DataFrame()

    name_col   = _find_col(df1, ["板块", "名称"])
    flow1_col  = _find_col(df1, ["净流入"])
    change_col = _find_col(df1, ["涨跌幅", "涨幅"])
    up_col     = _find_col(df1, ["上涨家数"])
    dn_col     = _find_col(df1, ["下跌家数"])

    if not name_col:
        logger.warning(f"找不到板块名列，可用列: {list(df1.columns)}")
        return pd.DataFrame()

    df1 = df1.rename(columns={name_col: "sector_name"})
    df1["sector_type"] = "行业资金流"

    df1["flow_today"] = pd.to_numeric(df1.get(flow1_col, 0), errors="coerce").fillna(0) if flow1_col else 0.0
    df1["change_pct"] = pd.to_numeric(df1.get(change_col, 0), errors="coerce").fillna(0) if change_col else 0.0

    if up_col and dn_col:
        up = pd.to_numeric(df1[up_col], errors="coerce").fillna(0)
        dn = pd.to_numeric(df1[dn_col], errors="coerce").fillna(0)
        df1["up_ratio"] = up / (up + dn + 1)
    else:
        df1["up_ratio"] = 0.5

    df1["flow_5d"] = 0.0

    # 涨停股统计
    zt_df = get_zt_pool()
    zt_counts = {}
    if not zt_df.empty:
        sector_col = _find_col(zt_df, ["所属板块", "板块", "行业"])
        if sector_col:
            for _, row in zt_df.iterrows():
                for sec in str(row[sector_col]).split(","):
                    zt_counts[sec.strip()] = zt_counts.get(sec.strip(), 0) + 1

    df1["zt_count"] = df1["sector_name"].map(lambda n: zt_counts.get(n, 0))

    # 综合评分
    df1["s_flow1"]    = _rank01(df1["flow_today"])
    df1["s_change"]   = _rank01(df1["change_pct"])
    df1["s_zt"]       = _rank01(df1["zt_count"])
    df1["s_up_ratio"] = _rank01(df1["up_ratio"])

    df1["heat_score"] = (
        df1["s_flow1"]    * 0.40 +
        df1["s_change"]   * 0.35 +
        df1["s_zt"]       * 0.15 +
        df1["s_up_ratio"] * 0.10
    )

    df1["flow_today_yi"] = df1["flow_today"]
    df1["flow_5d_yi"]    = 0.0

    # 记录Sina标签供下游使用
    # 新浪 sector_flow 直接带有 sina_label 列；THS/EM 数据需要名称映射
    if "sina_label" not in df1.columns:
        df1["sina_label"] = df1["sector_name"].map(get_sina_sector_label)
    else:
        df1["sina_label"] = df1["sina_label"].fillna(df1["sector_name"].map(get_sina_sector_label))

    out = (
        df1
        .sort_values("heat_score", ascending=False)
        .drop_duplicates(subset="sector_name")
        .head(top_n)
        .reset_index(drop=True)
    )
    out.index += 1
    return out[["sector_name", "sector_type", "heat_score",
                "flow_today_yi", "flow_5d_yi", "change_pct", "zt_count", "sina_label"]]


def _find_col(df: pd.DataFrame, candidates: list) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    for c in df.columns:
        for cand in candidates:
            if cand in c or c in cand:
                return c
    return None
