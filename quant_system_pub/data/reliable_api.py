"""
可靠数据访问层 reliable_api.py

解决问题：akshare EM（东方财富）API 频繁 RemoteDisconnected，
程序每次都等超时、每次都要手动补接口。

设计：
  1. 每类数据配置多个备用接口，按稳定性排序
     - 全市场行情：腾讯直连HTTP(最稳) → akshare腾讯 → akshare EM(最不稳)
     - 板块资金流：THS → EM → 新浪
     - 历史日线：EM → 腾讯
     - 板块成分股：EM → THS
  2. 接口健康追踪：失败1次 → 冷却30分钟，跳过等超时
  3. 线程超时：每个接口都有硬超时限制，不会无限等待
  4. 磁盘缓存兜底：所有接口都挂时返回最近缓存
  5. 所有模块通过本层获取数据，不直接调用 akshare

用法：
  from data.reliable_api import API
  df = API.spot()                    # 全市场实时行情
  df = API.history('600157', ...)    # 个股历史日线
  df = API.sector_flow()             # 板块资金流
  ...
"""

import time
import pickle
import logging
import threading
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Callable, Any, Optional

import requests
import pandas as pd

logger = logging.getLogger(__name__)

# ── 缓存目录 ────────────────────────────────────────────────
_CACHE_DIR = Path(__file__).parent.parent / "data" / "api_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_LOCK = threading.Lock()       # 并发写缓存时防止文件损坏
_HIST_SEM   = threading.Semaphore(4) # 历史K线并发上限：同时最多4个请求

# ── 接口健康状态 {接口名: {"fails": int, "cooldown_until": float}} ──
_health: dict[str, dict] = {}
_FAIL_THRESHOLD = 1       # 失败1次立刻切换备用，不反复等超时
_COOLDOWN_SEC   = 1800    # 冷却30分钟后自动重试


def _call_with_timeout(fn: Callable, timeout: float = 8.0):
    """在线程中执行 fn，超时则抛 TimeoutError"""
    result = [None]
    exc    = [None]

    def _run():
        try:
            result[0] = fn()
        except Exception as e:
            exc[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"接口超时（>{timeout}s）")
    if exc[0] is not None:
        raise exc[0]
    return result[0]


# ════════════════════════════════════════════════════════════
# 内部工具
# ════════════════════════════════════════════════════════════

def _cache_path(key: str) -> Path:
    safe = key.replace("/", "_").replace(":", "_").replace(" ", "_")
    return _CACHE_DIR / f"{safe}.pkl"


def _load_cache(key: str, max_age_sec: int) -> Optional[pd.DataFrame]:
    p = _cache_path(key)
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if age > max_age_sec:
        return None
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _load_cache_any_age(key: str, max_stale_sec: int = 0) -> Optional[pd.DataFrame]:
    """
    兜底：返回磁盘上的旧缓存。
    max_stale_sec>0 时，超过该时长的缓存视为无效（比如周末旧资金流数据），返回 None。
    """
    p = _cache_path(key)
    if not p.exists():
        return None
    age_sec = time.time() - p.stat().st_mtime
    if max_stale_sec > 0 and age_sec > max_stale_sec:
        age_min = int(age_sec / 60)
        logger.warning(f"[缓存过期跳过] {key}  缓存已 {age_min} 分钟，超过上限 {max_stale_sec//60} 分钟，不使用")
        return None
    try:
        with open(p, "rb") as f:
            data = pickle.load(f)
        age_min = int(age_sec / 60)
        logger.warning(f"[缓存兜底] {key}  数据来自 {age_min} 分钟前的缓存")
        return data
    except Exception:
        return None


def _save_cache(key: str, df: pd.DataFrame):
    try:
        with _CACHE_LOCK:
            with open(_cache_path(key), "wb") as f:
                pickle.dump(df, f)
    except Exception as e:
        logger.debug(f"写缓存失败 {key}: {e}")


def _is_healthy(name: str) -> bool:
    h = _health.get(name)
    if not h:
        return True
    if h["fails"] < _FAIL_THRESHOLD:
        return True
    if time.time() > h.get("cooldown_until", 0):
        # 冷却结束，重置
        _health[name] = {"fails": 0, "cooldown_until": 0}
        return True
    return False


def _mark_fail(name: str):
    h = _health.setdefault(name, {"fails": 0, "cooldown_until": 0})
    h["fails"] += 1
    if h["fails"] >= _FAIL_THRESHOLD:
        h["cooldown_until"] = time.time() + _COOLDOWN_SEC
        cd_min = _COOLDOWN_SEC // 60
        logger.warning(f"[接口降级] {name} 失败{h['fails']}次，冷却{cd_min}分钟")


def _mark_ok(name: str):
    _health[name] = {"fails": 0, "cooldown_until": 0}


def _try_sources(
    cache_key: str,
    cache_ttl: int,
    sources: list[tuple[str, Callable, float]],   # (名称, fn, 超时秒)
    validator: Callable[[pd.DataFrame], bool] = None,
    max_stale_sec: int = 0,   # 兜底缓存最大年龄限制，0=不限制
) -> pd.DataFrame:
    """
    按顺序尝试各数据源，成功则缓存并返回，全败则返回磁盘缓存。
    sources: [(接口名, 无参lambda, 超时秒), ...]
    max_stale_sec: 兜底缓存的最大年龄（秒），超过则返回空 DataFrame 而非旧数据
    """
    if validator is None:
        validator = lambda df: df is not None and not df.empty

    # 先查新鲜缓存
    cached = _load_cache(cache_key, cache_ttl)
    if cached is not None and validator(cached):
        return cached

    for item in sources:
        name, fn = item[0], item[1]
        timeout  = item[2] if len(item) > 2 else 10.0
        if not _is_healthy(name):
            logger.debug(f"[跳过冷却] {name}")
            continue
        try:
            df = _call_with_timeout(fn, timeout)
            if validator(df):
                _mark_ok(name)
                _save_cache(cache_key, df)
                return df
            else:
                _mark_fail(name)
        except Exception as e:
            _mark_fail(name)
            logger.warning(f"[接口失败] {name}: {type(e).__name__} {str(e)[:55]}")

    # 所有接口失败 → 兜底缓存（受 max_stale_sec 限制）
    stale = _load_cache_any_age(cache_key, max_stale_sec=max_stale_sec)
    if stale is not None:
        return stale

    logger.debug(f"[全部失败] {cache_key} 无可用数据源且无缓存")
    return pd.DataFrame()


# ════════════════════════════════════════════════════════════
# 腾讯/新浪 直连 HTTP 接口（无需 akshare，稳定性最高）
# ════════════════════════════════════════════════════════════

# 腾讯行情字段索引（qt.gtimg.cn 返回的~分割数组）
# 完整字段参考：https://cloud.tencent.com/developer/article/2088494
_TX_IDX = {
    "name":   1,   # 股票名称
    "open":   5,   # 今日开盘
    "close":  4,   # 昨日收盘（用于计算涨跌幅）
    "price":  3,   # 当前价格
    "high":   33,  # 今日最高
    "low":    34,  # 今日最低
    "volume": 36,  # 成交量（手）
    "amount": 37,  # 成交额（元）
    "turnover": 38,# 换手率
    "vol_ratio": 49, # 量比（部分没有，fallback 0）
}

_TX_HEADERS = {
    "Referer": "https://gu.qq.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# A股所有代码前缀规则
_SH_PREFIXES = ("60", "68", "51", "58", "50", "56", "57", "11", "12", "01", "00")
_SZ_PREFIXES = ("00", "30", "15", "16", "18", "03", "02")


def _code_to_tx(code: str) -> str:
    """将6位股票代码转为腾讯格式：sh600519 / sz000001"""
    c = str(code).zfill(6)
    if c.startswith(("60", "688", "51", "58", "11", "12")):
        return f"sh{c}"
    return f"sz{c}"


def _fetch_tencent_batch(codes: list[str]) -> list[dict]:
    """用腾讯 qt.gtimg.cn 批量获取行情，每批最多80只"""
    if not codes:
        return []
    symbols = ",".join(_code_to_tx(c) for c in codes)
    url = f"http://qt.gtimg.cn/q={symbols}"
    resp = requests.get(url, headers=_TX_HEADERS, timeout=10)
    resp.encoding = "gbk"
    rows = []
    for line in resp.text.splitlines():
        m = re.match(r'v_([a-z]{2})(\d{6})="([^"]*)"', line)
        if not m:
            continue
        code = m.group(2)
        parts = m.group(3).split("~")
        def _f(idx, default=0.0):
            try: return float(parts[idx]) if idx < len(parts) else default
            except: return default
        prev = _f(4)
        price = _f(3)
        chg = round((price - prev) / prev * 100, 2) if prev else 0.0
        rows.append({
            "code":     code,
            "name":     parts[1] if len(parts) > 1 else "",
            "price":    price,
            "chg":      chg,
            "high":     _f(33),
            "low":      _f(34),
            "volume":   _f(36),
            "amount":   _f(37),
            "turnover": _f(38),
            "vol_ratio":_f(49),
        })
    return rows


def _get_all_stock_codes() -> list[str]:
    """获取全市场A股代码列表（先查缓存，再从akshare取）"""
    cache_key = "all_stock_codes"
    p = _cache_path(cache_key)
    # 代码列表24小时内有效
    if p.exists() and (time.time() - p.stat().st_mtime) < 86400:
        try:
            with open(p, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        codes = df.iloc[:, 0].astype(str).str.zfill(6).tolist()
        with open(p, "wb") as f:
            pickle.dump(codes, f)
        return codes
    except Exception as e:
        logger.warning(f"获取全市场代码列表失败: {e}")
        return []


def _fetch_tencent_all_stocks() -> pd.DataFrame:
    """
    通过腾讯直连HTTP接口获取全市场实时行情
    分批（每批80只）请求，合并结果
    """
    codes = _get_all_stock_codes()
    if not codes:
        return pd.DataFrame()

    all_rows = []
    batch_size = 80
    for i in range(0, len(codes), batch_size):
        batch = codes[i: i + batch_size]
        try:
            rows = _fetch_tencent_batch(batch)
            all_rows.extend(rows)
        except Exception as e:
            logger.debug(f"腾讯行情批次 {i//batch_size} 失败: {e}")
        # 避免请求过快
        if i + batch_size < len(codes):
            time.sleep(0.05)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    # 过滤价格为0的（停牌/退市）
    df = df[df["price"] > 0].reset_index(drop=True)
    return df


def _sina_urllib(url: str, encoding: str = "gbk", timeout: int = 8) -> bytes:
    """用标准库 urllib 发 GET 请求（绕开 requests 连接池被东财/新浪断连的问题）"""
    import urllib.request as _ur
    req = _ur.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer":    "https://finance.sina.com.cn/",
        "Connection": "close",
    })
    with _ur.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_sina_sector_flow() -> pd.DataFrame:
    """
    新浪财经行业板块数据（直连HTTP，用 urllib 稳定）
    返回标准化列：sector_name / sina_label / flow_yi / chg / up / down
    格式：hangye_ZA01,农业,15,9.74,0.06,0.62,...
    字段：[0]=label [1]=名称 [2]=股票数 [3]=均价 [4]=涨跌额 [5]=涨跌幅%
    """
    try:
        raw = _sina_urllib("http://money.finance.sina.com.cn/q/view/newFLJK.php?param=hs_s")
        text = raw.decode("gbk", errors="replace")
        rows = []
        for item in re.findall(r'"([^"]+)"', text):
            fields = item.split(",")
            if len(fields) < 6 or not fields[0].startswith("hangye_"):
                continue
            try:
                rows.append({
                    "sector_name": fields[1],
                    "sina_label":  fields[0],
                    "chg":         float(fields[5]),
                    "flow_yi":     0.0,
                    "up":          0,
                    "down":        0,
                })
            except (ValueError, IndexError):
                continue
        if rows:
            return pd.DataFrame(rows)
    except Exception as e:
        logger.debug(f"新浪板块接口失败: {e}")
    return pd.DataFrame()


def _fetch_sina_sector_members(label: str) -> pd.DataFrame:
    """
    新浪行业成分股 + 实时行情（urllib 直连，不依赖 requests/akshare）
    label: 新浪行业代码，如 hangye_ZC35
    返回列：code / name / price / chg / turnover / amount
    """
    import json as _json
    try:
        # 先拿总数
        count_raw = _sina_urllib(
            f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
            f"/Market_Center.getHQNodeStockCount?node={label}",
            timeout=6,
        )
        count_text = count_raw.decode("gbk", errors="replace").strip().strip('"')
        total = int(count_text) if count_text.isdigit() else 200

        # 分页拉取（每页100条，最多3页）
        rows = []
        for page in range(1, min(4, total // 100 + 2)):
            page_raw = _sina_urllib(
                f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
                f"/Market_Center.getHQNodeData"
                f"?page={page}&num=100&sort=changepercent&asc=0&node={label}&symbol=&_s_r_a=page",
                timeout=10,
            )
            page_data = _json.loads(page_raw.decode("gbk", errors="replace"))
            if not page_data:
                break
            rows.extend(page_data)

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df.columns = [c.strip() for c in df.columns]
        # 标准化列名
        rename = {
            "symbol": "raw_code", "name": "name",
            "trade": "price", "changepercent": "chg",
            "turnoverratio": "turnover", "amount": "amount",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if "raw_code" in df.columns:
            df["code"] = df["raw_code"].str.replace(r"^(sh|sz)", "", regex=True)
        for col in ("price", "chg", "turnover", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return df
    except Exception as e:
        logger.debug(f"新浪成分股 HTTP {label} 失败: {e}")
    return pd.DataFrame()


# ════════════════════════════════════════════════════════════
# 公开 API 接口
# ════════════════════════════════════════════════════════════

class _API:
    """统一数据访问层，所有方法都有多源 failover + 缓存兜底"""

    # ── 全市场实时行情 ──────────────────────────────────────
    def spot(self) -> pd.DataFrame:
        """
        全市场实时行情
        返回列(标准化)：code / name / price / chg / high / low / volume / amount / turnover / vol_ratio
        主力：腾讯直连HTTP（最稳定）→ akshare腾讯 → akshare EM（最不稳定，最后用）
        """
        import akshare as ak

        def _tencent_http():
            return _fetch_tencent_all_stocks()

        def _tencent_ak():
            df = ak.stock_zh_a_spot()
            df.columns = [c.strip() for c in df.columns]
            return self._norm_spot_tx(df)

        def _em():
            df = ak.stock_zh_a_spot_em()
            df.columns = [c.strip() for c in df.columns]
            return self._norm_spot_em(df)

        return _try_sources(
            cache_key="spot_all",
            cache_ttl=60,
            sources=[
                ("spot_tx_http", _tencent_http, 50.0),  # 腾讯直连HTTP，分批请求需要时间
                ("spot_tx_ak",   _tencent_ak,   25.0),  # akshare腾讯接口
                ("spot_em",      _em,            8.0),   # EM 不稳定，短超时兜底
            ],
        )

    def _norm_spot_em(self, df: pd.DataFrame) -> pd.DataFrame:
        col = lambda *names: next((c for c in df.columns if any(n in c for n in names)), None)
        code_c = col("代码"); name_c = col("名称"); price_c = col("最新价")
        chg_c  = col("涨跌幅"); high_c = col("最高"); low_c = col("最低")
        vol_c  = col("成交量"); amt_c  = col("成交额"); turn_c = col("换手率")
        vr_c   = col("量比")
        out = pd.DataFrame()
        if code_c:
            out["code"]     = df[code_c].astype(str).str.strip()
            out["name"]     = df[name_c].astype(str) if name_c else ""
            out["price"]    = pd.to_numeric(df[price_c],  errors="coerce") if price_c else 0
            out["chg"]      = pd.to_numeric(df[chg_c],    errors="coerce") if chg_c   else 0
            out["high"]     = pd.to_numeric(df[high_c],   errors="coerce") if high_c  else 0
            out["low"]      = pd.to_numeric(df[low_c],    errors="coerce") if low_c   else 0
            out["volume"]   = pd.to_numeric(df[vol_c],    errors="coerce") if vol_c   else 0
            out["amount"]   = pd.to_numeric(df[amt_c],    errors="coerce") if amt_c   else 0
            out["turnover"] = pd.to_numeric(df[turn_c],   errors="coerce") if turn_c  else 0
            out["vol_ratio"]= pd.to_numeric(df[vr_c],     errors="coerce") if vr_c    else 0
        return out

    def _norm_spot_tx(self, df: pd.DataFrame) -> pd.DataFrame:
        col = lambda *names: next((c for c in df.columns if any(n in c for n in names)), None)
        code_c = col("代码"); name_c = col("名称"); price_c = col("最新价")
        chg_amt_c = col("涨跌额"); prev_c = col("昨收")
        high_c = col("最高"); low_c = col("最低")
        vol_c  = col("成交量"); amt_c  = col("成交额")
        out = pd.DataFrame()
        if code_c:
            codes = df[code_c].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True).str.zfill(6)
            prev  = pd.to_numeric(df[prev_c], errors="coerce") if prev_c else pd.Series(0.0, index=df.index)
            chg_a = pd.to_numeric(df[chg_amt_c], errors="coerce") if chg_amt_c else pd.Series(0.0, index=df.index)
            out["code"]     = codes
            out["name"]     = df[name_c].astype(str) if name_c else ""
            out["price"]    = pd.to_numeric(df[price_c], errors="coerce") if price_c else 0
            out["chg"]      = (chg_a / prev.replace(0, float("nan")) * 100).fillna(0)
            out["high"]     = pd.to_numeric(df[high_c],  errors="coerce") if high_c else 0
            out["low"]      = pd.to_numeric(df[low_c],   errors="coerce") if low_c  else 0
            out["volume"]   = pd.to_numeric(df[vol_c],   errors="coerce") if vol_c  else 0
            out["amount"]   = pd.to_numeric(df[amt_c],   errors="coerce") if amt_c  else 0
            out["turnover"] = 0.0
            out["vol_ratio"]= 0.0
        return out

    # ── 个股实时行情（单只）────────────────────────────────
    def spot_one(self, code: str) -> dict:
        """获取单只股票实时行情，返回 dict"""
        import akshare as ak

        def _from_full_spot():
            df = self.spot()
            if df.empty:
                return pd.DataFrame()
            row = df[df["code"] == code]
            return row

        def _xq():
            prefix = "SH" if code.startswith("6") else "SZ"
            df = ak.stock_individual_spot_xq(symbol=f"{prefix}{code}")
            df.columns = [c.strip() for c in df.columns]
            return df

        # 先从全量行情里找
        df_full = self.spot()
        if not df_full.empty:
            row = df_full[df_full["code"] == code]
            if not row.empty:
                r = row.iloc[0]
                result = {k: r.get(k, 0) for k in ["code","name","price","chg","high","low","volume","amount","turnover","vol_ratio"]}
                # 补充雪球的PE/PB/52周数据
                try:
                    prefix = "SH" if code.startswith("6") else "SZ"
                    xq = ak.stock_individual_spot_xq(symbol=f"{prefix}{code}")
                    kv = dict(zip(xq["item"], xq["value"]))
                    def v(k):
                        try: return float(kv.get(k) or 0)
                        except: return 0.0
                    result["high52"]  = v("52周最高")
                    result["low52"]   = v("52周最低")
                    result["pe"]      = v("市盈率(动)")
                    result["pb"]      = v("市净率")
                    result["turnover"]= v("周转率") or result["turnover"]
                except Exception:
                    result["high52"] = result["low52"] = result["pe"] = result["pb"] = 0.0
                return result

        return {"error": f"找不到 {code} 的实时行情"}

    # ── 个股历史日线 ────────────────────────────────────────
    def history(self, code: str, start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
        """
        个股历史日线，标准化列：date/open/high/low/close/volume/amount
        优先级：tdx本地DuckDB → 腾讯直连HTTP → EM akshare
        """
        import akshare as ak

        cache_key = f"hist_{code}_{start}_{end}_{adjust}"

        def _tdx_local():
            from data.tdx_local import query_history
            df = query_history(code, start, end, adjust)
            if df is None or df.empty:
                raise ValueError("tdx本地库无数据")
            return df

        def _tencent_http():
            """腾讯直连HTTP历史K线，不经过akshare/py_mini_racer"""
            import urllib.request as _ur
            import json as _json
            import re as _re
            prefix = "sh" if code.startswith("6") else "sz"
            url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                   f"?_var=kline_dayqfq&param={prefix}{code},day,,,640,qfq")
            req = _ur.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://gu.qq.com/",
            })
            with _HIST_SEM:   # 限流：同时最多4个并发历史请求
                with _ur.urlopen(req, timeout=15) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
            m = _re.search(r"=(\{.*\})", raw, _re.DOTALL)
            if not m:
                raise ValueError("腾讯历史K线解析失败")
            d = _json.loads(m.group(1))
            stock_key = f"{prefix}{code}"
            stock_data = d["data"].get(stock_key, {})
            klines = stock_data.get("qfqday") or stock_data.get("day", [])
            if not klines:
                raise ValueError("腾讯历史K线数据为空")
            rows = []
            for bar in klines:
                # [日期, 开盘, 收盘, 最高, 最低, 成交量]
                rows.append({
                    "date":   pd.to_datetime(bar[0]),
                    "open":   float(bar[1]),
                    "close":  float(bar[2]),
                    "high":   float(bar[3]),
                    "low":    float(bar[4]),
                    "volume": float(bar[5]),
                    "amount": 0.0,
                })
            df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
            # 过滤日期范围
            s, e = pd.to_datetime(start), pd.to_datetime(end)
            return df[(df["date"] >= s) & (df["date"] <= e)].reset_index(drop=True)

        def _em():
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start.replace("-",""), end_date=end.replace("-",""),
                adjust=adjust,
            )
            df.columns = [c.strip() for c in df.columns]
            return self._norm_hist(df, style="em")

        return _try_sources(
            cache_key=cache_key,
            cache_ttl=3600 * 12,   # 12小时内复用（日线数据变化慢）
            sources=[
                ("hist_tdx",     _tdx_local,     2.0),   # 本地DuckDB，毫秒级
                ("hist_tencent", _tencent_http, 20.0),   # 腾讯HTTP备用
                ("hist_em",      _em,            12.0),   # EM最后兜底
            ],
            validator=lambda df: df is not None and len(df) >= 5,
        )

    def _norm_hist(self, df: pd.DataFrame, style: str) -> pd.DataFrame:
        if style == "em":
            rename = {"日期":"date","开盘":"open","收盘":"close","最高":"high",
                      "最低":"low","成交量":"volume","成交额":"amount",
                      "涨跌幅":"pct_chg","换手率":"turnover"}
            df = df.rename(columns={k:v for k,v in rename.items() if k in df.columns})
        # 腾讯接口：列名可能是 "date" 或 "datetime"，统一处理
        if "date" not in df.columns:
            date_col = next((c for c in df.columns if "date" in c.lower()), None)
            if date_col:
                df = df.rename(columns={date_col: "date"})
            else:
                raise KeyError(f"找不到日期列，现有列: {list(df.columns)}")
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    # ── 板块资金流 ──────────────────────────────────────────
    def sector_flow(self) -> pd.DataFrame:
        """
        行业板块资金流排名
        标准化列：sector_name / chg / flow_yi(亿) / up / down
        主力：THS（同花顺）→ EM（东财）→ 新浪行业涨跌幅
        """
        import akshare as ak

        def _ths():
            df = ak.stock_board_industry_summary_ths()
            df.columns = [c.strip() for c in df.columns]
            return self._norm_sector(df, style="ths")

        def _em():
            df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
            df.columns = [c.strip() for c in df.columns]
            return self._norm_sector(df, style="em")

        def _sina():
            return _fetch_sina_sector_flow()

        return _try_sources(
            cache_key="sector_flow",
            cache_ttl=300,
            sources=[
                # 新浪直连最稳定（无主力资金数据，但涨跌幅 100% 可用）
                ("sector_sina", _sina,  8.0),
                # 以下两个接口在部分网络环境不稳定，作为备用
                ("sector_ths",  _ths,  12.0),
                ("sector_em",   _em,    8.0),
            ],
        )

    def _norm_sector(self, df: pd.DataFrame, style: str) -> pd.DataFrame:
        out = pd.DataFrame()
        col = lambda *ns: next((c for c in df.columns if any(n in c for n in ns)), None)
        if style == "ths":
            name_c = col("板块"); flow_c = col("净流入")
            chg_c  = col("涨跌幅"); up_c = col("上涨"); dn_c = col("下跌")
            out["sector_name"] = df[name_c].astype(str) if name_c else ""
            out["flow_yi"]  = pd.to_numeric(df[flow_c], errors="coerce").fillna(0) if flow_c else 0
            out["chg"]      = pd.to_numeric(df[chg_c],  errors="coerce").fillna(0) if chg_c  else 0
            out["up"]       = pd.to_numeric(df[up_c],   errors="coerce").fillna(0) if up_c   else 0
            out["down"]     = pd.to_numeric(df[dn_c],   errors="coerce").fillna(0) if dn_c   else 0
        elif style == "em":
            name_c = col("名称"); flow_c = col("主力净流入","净额")
            chg_c  = col("涨跌幅")
            out["sector_name"] = df[name_c].astype(str) if name_c else ""
            flow = pd.to_numeric(df[flow_c], errors="coerce").fillna(0) if flow_c else 0
            out["flow_yi"] = flow / 1e8   # 元→亿
            out["chg"]     = pd.to_numeric(df[chg_c], errors="coerce").fillna(0) if chg_c else 0
            out["up"]      = 0
            out["down"]    = 0
        return out

    # ── 个股主力资金流 ──────────────────────────────────────
    def fund_flow(self, code: str) -> pd.DataFrame:
        """
        个股主力资金流，标准化列：date/main_net/super_net/big_net/mid_net/small_net
        主力：东财 push2 直连HTTP（无 py_mini_racer，返回今日数据）
        push2his（历史）在部分网络被封，不使用。
        """
        cache_key = f"fundflow_{code}"

        def _em_push2_http():
            import urllib.request as _ur
            import json as _json
            # 北交所（302/430/83/87开头）东财push2不支持，直接返回空
            if code.startswith(("302", "430", "83", "87", "88")):
                return pd.DataFrame()
            market = 1 if code.startswith("6") else 0
            url = (f"https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
                   f"?klt=101&fields1=f1,f2,f3,f7"
                   f"&fields2=f51,f52,f53,f54,f55,f56,f57"
                   f"&secid={market}.{code}&_={int(time.time()*1000)}")
            req = _ur.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer":    "https://data.eastmoney.com/",
            })
            with _ur.urlopen(req, timeout=8) as resp:
                d = _json.loads(resp.read())
            klines = d.get("data", {}).get("klines", []) if d.get("data") else []
            if not klines:
                return pd.DataFrame()
            rows = []
            for item in klines:
                parts = item.split(",")
                if len(parts) < 6:
                    continue
                rows.append({
                    "date":      parts[0],
                    "main_net":  float(parts[1]) if parts[1] else 0.0,
                    "super_net": float(parts[2]) if parts[2] else 0.0,
                    "big_net":   float(parts[3]) if parts[3] else 0.0,
                    "mid_net":   float(parts[4]) if parts[4] else 0.0,
                    "small_net": float(parts[5]) if parts[5] else 0.0,
                })
            return pd.DataFrame(rows)

        return _try_sources(
            cache_key=cache_key,
            cache_ttl=300,
            sources=[("fundflow_em_http", _em_push2_http, 8.0)],
            validator=lambda df: df is not None and len(df) > 0,
            max_stale_sec=8 * 3600,
        )

    # ── 涨停池 ──────────────────────────────────────────────
    def zt_pool(self, date: str = None) -> pd.DataFrame:
        import akshare as ak
        date = date or datetime.today().strftime("%Y%m%d")
        cache_key = f"zt_{date}"

        def _em():
            df = ak.stock_zt_pool_em(date=date)
            df.columns = [c.strip() for c in df.columns]
            return df

        # 空结果（非交易日/今日无涨停）是正常情况，不触发降级
        return _try_sources(cache_key=cache_key, cache_ttl=300,
                            sources=[("zt_em", _em, 10.0)],
                            validator=lambda df: df is not None)

    # ── 跌停池 ──────────────────────────────────────────────
    def dt_pool(self, date: str = None) -> pd.DataFrame:
        import akshare as ak
        date = date or datetime.today().strftime("%Y%m%d")
        cache_key = f"dt_{date}"

        def _em():
            df = ak.stock_zt_pool_dtgc_em(date=date)
            df.columns = [c.strip() for c in df.columns]
            return df

        # 空结果（今日无跌停股）是正常情况，不触发接口降级
        return _try_sources(cache_key=cache_key, cache_ttl=300,
                            sources=[("dt_em", _em, 10.0)],
                            validator=lambda df: df is not None)

    # ── 大盘指数日线 ────────────────────────────────────────
    def index_daily(self, symbol: str) -> pd.DataFrame:
        """大盘指数历史日线（sh000001/sz399001/sz399006），腾讯直连HTTP，无py_mini_racer"""
        cache_key = f"index_{symbol}"

        def _tencent_http():
            import urllib.request as _ur
            import json as _json
            import re as _re
            url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                   f"?_var=kline_dayqfq&param={symbol},day,,,640,qfq")
            req = _ur.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://gu.qq.com/",
            })
            with _ur.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            m = _re.search(r"=(\{.*\})", raw, _re.DOTALL)
            if not m:
                raise ValueError("腾讯指数K线解析失败")
            d = _json.loads(m.group(1))
            stock_data = d["data"].get(symbol, {})
            klines = stock_data.get("day") or stock_data.get("qfqday", [])
            if not klines:
                raise ValueError("腾讯指数K线数据为空")
            rows = [{"date": pd.to_datetime(bar[0]),
                     "open": float(bar[1]), "close": float(bar[2]),
                     "high": float(bar[3]), "low":   float(bar[4]),
                     "volume": float(bar[5])}
                    for bar in klines]
            return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)

        return _try_sources(cache_key=cache_key, cache_ttl=3600,
                            sources=[("index_tx", _tencent_http, 15.0)])

    # ── 市场活跃度（涨跌家数）──────────────────────────────
    def market_activity(self) -> dict:
        """返回 {up, down, zt, dt} 当日涨跌家数，从 spot 数据统计"""
        try:
            spot = self.spot()
            if spot.empty:
                return {"up": 0, "down": 0, "zt": 0, "dt": 0}
            chg = pd.to_numeric(spot.get("chg", pd.Series(dtype=float)), errors="coerce").fillna(0)
            up  = int((chg > 0).sum())
            dn  = int((chg < 0).sum())
            zt  = int((chg >= 9.5).sum())
            dt  = int((chg <= -9.5).sum())
            return {"up": up, "down": dn, "zt": zt, "dt": dt}
        except Exception as e:
            logger.warning(f"市场活跃度获取失败: {e}")
            return {"up": 0, "down": 0, "zt": 0, "dt": 0}

    # ── 北向资金 ────────────────────────────────────────────
    def north_flow(self) -> float:
        """北向资金今日净流入（亿元），正=流入"""
        import akshare as ak
        # 方法1：分钟实时累计（列名可能乱码，按位置取）
        try:
            df = ak.stock_hsgt_fund_min_em(symbol="北向资金")
            if not df.empty and len(df.columns) >= 3:
                # 列顺序：日期/时间/沪股通/深股通/北向资金合计 —— 取最后一列
                val = pd.to_numeric(df.iloc[-1, -1], errors="coerce")
                if not pd.isna(val) and val != 0:
                    return round(float(val), 2)
        except Exception:
            pass
        # 方法2：汇总表，用列位置取净买额（index=5，单位亿元）
        # 行0=沪股通, 行2=深股通（行1/3是沪股通(沪)/深股通(沪)子项，去重）
        try:
            df = ak.stock_hsgt_fund_flow_summary_em()
            if df.empty or len(df.columns) < 6:
                return 0.0
            net_col = df.columns[5]   # 成交净买额
            rows = df.iloc[[0, 2]] if len(df) >= 3 else df.iloc[:2]
            total = pd.to_numeric(rows[net_col], errors="coerce").sum()
            return round(float(total), 2) if not pd.isna(total) else 0.0
        except Exception as e:
            logger.warning(f"北向资金获取失败: {e}")
            return 0.0

    # ── 龙虎榜 ──────────────────────────────────────────────
    def lhb(self, days: int = 3) -> set:
        """返回近N日龙虎榜股票代码集合（并行请求，无sleep）"""
        import akshare as ak
        from concurrent.futures import ThreadPoolExecutor, as_completed
        today = datetime.today()

        def _fetch_one(i) -> list:
            date_str  = (today - timedelta(days=i)).strftime("%Y%m%d")
            cache_key = f"lhb_{date_str}"
            cached = _load_cache(cache_key, 86400)
            if cached is not None:
                return cached
            try:
                df = ak.stock_lhb_detail_em(start_date=date_str, end_date=date_str)
                if df.empty:
                    return []
                df.columns = [c.strip() for c in df.columns]
                code_col = next((c for c in df.columns if "代码" in c), None)
                if not code_col:
                    return []
                day_codes = df[code_col].astype(str).tolist()
                _save_cache(cache_key, day_codes)
                return day_codes
            except Exception as e:
                logger.debug(f"龙虎榜 {date_str} 获取失败: {e}")
                return []

        codes: set = set()
        with ThreadPoolExecutor(max_workers=days) as pool:
            futures = {pool.submit(_fetch_one, i): i for i in range(days)}
            for fut in as_completed(futures):
                try:
                    codes.update(fut.result())
                except Exception:
                    pass
        return codes

    # ── 新浪板块成分股 ──────────────────────────────────────
    def sector_members_sina(self, label: str) -> pd.DataFrame:
        """通过新浪标签获取板块成分股"""
        import akshare as ak
        import math
        if not label or (isinstance(label, float) and math.isnan(label)):
            return pd.DataFrame()
        cache_key = f"members_sina_{label}"

        def _sina():
            df = ak.stock_sector_detail(sector=label)
            if df is None or df.empty:
                return pd.DataFrame()
            df.columns = [c.strip() for c in df.columns]
            return df

        return _try_sources(cache_key=cache_key, cache_ttl=1800,
                            sources=[("members_sina", _sina, 12.0)])

    # ── 东方财富板块成分股 ──────────────────────────────────
    def sector_members_em(self, name: str, is_concept: bool = False) -> pd.DataFrame:
        """
        板块成分股：新浪直连HTTP（主力）→ 东财EM（备用）
        """
        import akshare as ak
        cache_key = f"members_em_{name}"

        def _sina_http():
            # 新浪行业成分股（直连HTTP，稳定）
            if is_concept:
                return pd.DataFrame()
            from selector.sector_heat import get_sina_sector_label
            label = get_sina_sector_label(name)
            if not label:
                return pd.DataFrame()
            return _fetch_sina_sector_members(label)

        def _em():
            if is_concept:
                df = ak.stock_board_concept_cons_em(symbol=name)
            else:
                df = ak.stock_board_industry_cons_em(symbol=name)
            if df is None or df.empty:
                return pd.DataFrame()
            df.columns = [c.strip() for c in df.columns]
            return df

        return _try_sources(cache_key=cache_key, cache_ttl=1800,
                            sources=[
                                ("members_sina_http", _sina_http, 12.0),
                                ("members_em",        _em,        10.0),
                            ])

    # ── 个股资金流排名 ──────────────────────────────────────
    def fund_flow_rank(self) -> pd.DataFrame:
        """
        今日个股资金流排名（超大单/大单）
        EM接口在部分网络环境必挂，直接跳过，用实时行情合成（稳定可靠）
        """
        cache_key = "fund_flow_rank_today"

        def _spot_fallback():
            spot = self.spot()
            if spot.empty:
                return pd.DataFrame()
            df = spot[spot["amount"] > 0].copy()
            df = df.rename(columns={"code": "代码", "name": "名称",
                                    "chg": "涨跌幅", "amount": "成交额"})
            df["主力净流入-净额"] = (
                pd.to_numeric(df["成交额"], errors="coerce").fillna(0) *
                pd.to_numeric(df["涨跌幅"], errors="coerce").fillna(0) / 100
            )
            df["代码"] = df["代码"].astype(str).str.zfill(6)
            return df[["代码", "名称", "涨跌幅", "主力净流入-净额"]].reset_index(drop=True)

        return _try_sources(cache_key=cache_key, cache_ttl=180,
                            sources=[
                                ("ffrank_spot", _spot_fallback, 30.0),
                            ])

    # ── 健康状态报告 ────────────────────────────────────────
    def health_report(self) -> str:
        if not _health:
            return "所有接口正常"
        lines = []
        for name, h in _health.items():
            if h["fails"] >= _FAIL_THRESHOLD:
                cd = max(0, int((h["cooldown_until"] - time.time()) / 60))
                lines.append(f"  [降级] {name}  失败{h['fails']}次  冷却剩余{cd}分钟")
            elif h["fails"] > 0:
                lines.append(f"  [警告] {name}  失败{h['fails']}次")
        return "\n".join(lines) if lines else "所有接口正常"


# 全局单例
API = _API()

# 已知在部分网络环境必然失败的接口，启动时直接标记冷却，静默跳过
# sector_ths/sector_em/members_em 依赖东财/同花顺，在 ISP 级别被拦截，无法解决
# members_sina_http 已被新 urllib 版替代，旧接口废弃
# hist_em（个股历史日线EM）保留作为 hist_tencent 失败时的备用，不预先禁用
_KNOWN_UNSTABLE = ["members_em", "sector_ths", "sector_em", "members_sina_http"]
for _iface in _KNOWN_UNSTABLE:
    _health[_iface] = {"fails": _FAIL_THRESHOLD, "cooldown_until": time.time() + _COOLDOWN_SEC}
