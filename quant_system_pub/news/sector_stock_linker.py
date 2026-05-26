"""
板块 → 个股关联模块

给定受影响板块名称列表（如["半导体","芯片"]）和AI给出的个股名称列表，
从全市场实时行情中匹配对应股票代码，并叠加当日表现排序。

不依赖东财EM板块成员接口（该接口在部分网络环境下被拦截）。
改用：1. AI直接给出的公司名称 → 全市场名称模糊匹配
      2. 本地板块关键词 → 全市场名称/代码模糊匹配
"""
import logging
import time

import pandas as pd

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 板块关键词 → 股票名称关键词（用于在全市场行情中做名称匹配）
# ─────────────────────────────────────────────────────────────
SECTOR_NAME_KEYWORDS: dict[str, list[str]] = {
    # AI / 半导体
    "半导体":       ["半导体", "芯片", "集成电路", "晶圆", "光刻"],
    "芯片":         ["芯片", "半导体", "集成电路", "晶圆"],
    "AI芯片":       ["芯片", "算力", "智算"],
    "人工智能":     ["人工智能", "大模型", "智算"],
    "算力":         ["算力", "数据中心", "IDC"],
    "云计算":       ["云计算", "云服务"],
    "大模型":       ["大模型", "人工智能"],
    "光模块":       ["光模块", "光芯片", "光通信"],
    "信创":         ["信创", "国产化", "操作系统", "数据库"],
    "软件国产化":   ["国产", "软件", "信创"],
    "量子计算":     ["量子"],
    "机器视觉":     ["视觉", "相机", "图像"],
    "人形机器人":   ["机器人", "人形"],
    "工业机器人":   ["机器人", "自动化", "伺服"],
    "无人机":       ["无人机", "飞行"],
    "低空经济":     ["低空", "eVTOL", "航空"],
    "卫星互联网":   ["卫星", "通信"],
    "6G":           ["通信", "5G", "6G"],
    # 新能源
    "新能源":       ["新能源", "锂电", "光伏", "风电", "储能"],
    "光伏":         ["光伏", "硅料", "组件", "电池片", "逆变器"],
    "储能":         ["储能", "电池", "液流"],
    "锂电池":       ["锂电", "电池", "正极", "负极", "隔膜", "电解液"],
    "动力电池":     ["锂电", "电池", "动力"],
    "氢能源":       ["氢能", "燃料电池", "制氢"],
    "固态电池":     ["固态电池", "固态"],
    "新能源汽车":   ["新能源汽车", "电动车", "EV"],
    "充电桩":       ["充电桩", "充电站"],
    "自动驾驶":     ["自动驾驶", "辅助驾驶", "激光雷达"],
    "智能汽车":     ["智能汽车", "车联网"],
    # 军工
    "军工":         ["军工", "航空", "船舶", "兵器", "装备"],
    "国防军工":     ["军工", "国防", "航空", "导弹"],
    "航空发动机":   ["航发", "发动机"],
    "船舶":         ["船舶", "造船", "海工"],
    "航空航天":     ["航空", "航天", "火箭"],
    # 消费
    "白酒":         ["白酒", "酒"],
    "食品饮料":     ["食品", "饮料", "乳业", "调味"],
    "消费":         ["消费", "零售", "商超"],
    "旅游":         ["旅游", "酒店", "景区", "免税"],
    "免税":         ["免税"],
    "黄金":         ["黄金", "贵金属"],
    "生猪养殖":     ["养殖", "猪", "肉食"],
    "电商":         ["电商", "零售", "快递"],
    # 医药
    "医药":         ["医药", "制药", "生物", "医疗"],
    "创新药":       ["创新药", "新药", "生物药"],
    "CXO":          ["CXO", "CDMO", "外包"],
    "医疗器械":     ["器械", "医疗设备"],
    "疫苗":         ["疫苗", "生物"],
    "中药":         ["中药", "中医"],
    # 金融
    "银行":         ["银行"],
    "券商":         ["证券", "基金", "投资"],
    "非银金融":     ["保险", "证券", "信托"],
    "保险":         ["保险"],
    # 地产基建
    "房地产":       ["地产", "房地产", "置业"],
    "建材":         ["建材", "水泥", "玻璃", "陶瓷"],
    "建筑":         ["建筑", "工程"],
    "家电":         ["家电", "冰箱", "空调", "洗衣机"],
    "家居":         ["家居", "家具", "装修"],
    "工程机械":     ["工程机械", "挖机", "起重"],
    "钢铁":         ["钢铁", "钢", "铁"],
    "基础建设":     ["基建", "建设", "工程"],
    # 能源资源
    "煤炭":         ["煤炭", "煤"],
    "石油":         ["石油", "石化", "油气"],
    "有色金属":     ["有色", "铜", "铝", "锌", "铅", "镍"],
    "稀土":         ["稀土", "磁材"],
    "锂矿":         ["锂", "碳酸锂"],
    "化工":         ["化工", "化肥", "农药"],
    # 农业
    "农业":         ["农业", "农牧", "种植"],
    "种业":         ["种子", "种业"],
}

# 行情缓存（避免同一次分析重复拉取）
_spot_cache: pd.DataFrame | None = None
_spot_time: float = 0
_SPOT_TTL = 120   # 2分钟


def _get_spot() -> pd.DataFrame:
    global _spot_cache, _spot_time
    now = time.time()
    if _spot_cache is not None and now - _spot_time < _SPOT_TTL:
        return _spot_cache
    try:
        from data.reliable_api import API
        df = API.spot()
        if df is not None and not df.empty:
            _spot_cache = df
            _spot_time  = now
            return df
    except Exception as e:
        logger.debug(f"spot行情拉取失败: {e}")
    return _spot_cache if _spot_cache is not None else pd.DataFrame()


def _is_excluded(code: str, name: str) -> bool:
    if not code:
        return True
    if str(name).__contains__("ST") or str(name).__contains__("退"):
        return True
    # 北交所（流动性差）
    if str(code).startswith(("82", "83", "87", "43", "40", "302", "430")):
        return True
    # 科创板(688)允许，因为半导体/AI龙头多在科创板
    return False


def find_stocks_for_news(
    benefit_sectors: list[str],
    benefit_stocks:  list[str],
    sentiment:       float = 1.0,
    top_n:           int   = 8,
) -> list[dict]:
    """
    根据AI给出的受益板块和个股名称，从全市场行情中匹配具体股票代码。

    benefit_sectors: AI/关键词给出的板块名称列表（如["半导体","算力"]）
    benefit_stocks:  AI直接给出的公司名称列表（如["中芯国际","寒武纪"]）
    sentiment:       情感分（正=利好）
    top_n:           最多返回几只

    返回：[{"code": "002xxx", "name": "xxx", "chg": 2.5, "reason": "半导体板块"}, ...]
    """
    spot = _get_spot()
    if spot.empty:
        return []

    # 标准化列名
    col_code = _find_col(spot, ["code", "代码"])
    col_name = _find_col(spot, ["name", "名称"])
    col_chg  = _find_col(spot, ["chg", "涨跌幅", "change_pct"])
    col_amt  = _find_col(spot, ["amount", "成交额"])

    if not col_code or not col_name:
        return []

    df = spot.copy()
    df["_code"] = df[col_code].astype(str)
    df["_name"] = df[col_name].astype(str)
    df["_chg"]  = pd.to_numeric(df[col_chg], errors="coerce").fillna(0) if col_chg else 0.0
    df["_amt"]  = pd.to_numeric(df[col_amt], errors="coerce").fillna(0) if col_amt else 0.0

    # 过滤无效股（周末缓存数据成交额可能为0，不过滤成交额）
    df = df[df["_code"].str.len() >= 6]
    mask_ex = df.apply(lambda r: _is_excluded(r["_code"], r["_name"]), axis=1)
    df = df[~mask_ex]

    matched: dict[str, dict] = {}   # code → entry

    # ── 1. AI直接给出的个股名称（精确优先）─────────────────
    for stock_name in benefit_stocks:
        if not stock_name or len(stock_name) < 2:
            continue
        hit = df[df["_name"] == stock_name]
        if hit.empty:
            # 尝试从长到短的前缀匹配（最少2字）
            for plen in range(min(len(stock_name), 6), 1, -1):
                prefix = stock_name[:plen]
                hit = df[df["_name"].str.contains(prefix, na=False, regex=False)]
                if not hit.empty:
                    break
        for _, row in hit.head(2).iterrows():
            code = row["_code"]
            if code not in matched:
                matched[code] = {
                    "code":   code,
                    "name":   row["_name"],
                    "chg":    round(float(row["_chg"]), 2),
                    "reason": "AI推荐",
                    "score":  100,
                }

    # ── 2. 板块关键词 → 名称匹配 ─────────────────────────
    sector_hits: list[dict] = []
    for sector in benefit_sectors[:6]:
        kws = SECTOR_NAME_KEYWORDS.get(sector, [sector])
        for kw in kws[:3]:
            if not kw:
                continue
            hit = df[df["_name"].str.contains(kw, na=False, regex=False)]
            for _, row in hit.iterrows():
                code = row["_code"]
                if code in matched:
                    continue
                chg = float(row["_chg"])
                amt = float(row["_amt"])
                # 利好时选涨幅靠前的；利空时选跌幅靠前的
                sort_val = chg if sentiment >= 0 else -chg
                sector_hits.append({
                    "code":     code,
                    "name":     row["_name"],
                    "chg":      round(chg, 2),
                    "reason":   sector,
                    "score":    sort_val + amt / 1e6,  # 涨幅 + 流动性加分
                })

    # 去重：同一股票只保留最高分
    sector_dedup: dict[str, dict] = {}
    for item in sector_hits:
        code = item["code"]
        if code not in sector_dedup or item["score"] > sector_dedup[code]["score"]:
            sector_dedup[code] = item

    # 板块命中按分数降序
    sorted_sector = sorted(sector_dedup.values(), key=lambda x: x["score"], reverse=True)

    # 合并：AI推荐优先 + 板块命中补充
    results = list(matched.values())
    for item in sorted_sector:
        if len(results) >= top_n:
            break
        if item["code"] not in {r["code"] for r in results}:
            results.append(item)

    # 移除 score 字段（仅内部使用）
    for r in results:
        r.pop("score", None)

    return results[:top_n]


def _find_col(df: pd.DataFrame, candidates: list) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    for c in df.columns:
        for cand in candidates:
            if cand.lower() in c.lower():
                return c
    return None
