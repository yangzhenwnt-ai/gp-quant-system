"""
tdx2db 本地数据库查询层（跨平台）

依赖 tdx2db 工具（https://github.com/jing2uo/tdx2db）将通达信数据导入本地 DuckDB。
提供与 reliable_api.py 相同格式的 DataFrame 输出，作为网络接口的替代第一优先级。

数据库路径优先级（由 core.config_loader.tdx_db_path() 统一处理）：
  1. 环境变量 TDX_DB_PATH
  2. config.local.yaml → tdx_db_path
  3. 各平台常见路径自动探测

表结构（tdx2db v5.0）：
  v_stock_qfq/hfq/bfq — 个股日线（前/后/不复权）
  v_etf_qfq/hfq/bfq   — ETF日线
  raw_basic_daily      — 换手率/市值/涨跌幅
  raw_symbol_name      — 股票名称
  raw_tdx_blocks_*     — 板块信息

symbol 格式：tdx2db 使用 sh/sz 前缀，如 sh600157、sz000001
"""

import logging
import threading
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_conn      = None
_conn_lock = threading.Lock()
_db_path: Optional[str] = None
_available: Optional[bool] = None


def get_conn():
    """获取 DuckDB 只读连接（懒加载，失败时返回 None）"""
    global _conn, _db_path, _available
    if _available is False:
        return None
    if _conn is not None:
        return _conn
    with _conn_lock:
        if _conn is not None:
            return _conn
        try:
            import duckdb
            from core.config_loader import tdx_db_path
            path = tdx_db_path()
            if not path:
                _available = False
                logger.debug("tdx本地库未找到，跳过本地数据源")
                return None
            _conn = duckdb.connect(str(path), read_only=True)
            _db_path = str(path)
            _available = True
            logger.info(f"tdx本地库已连接: {path}")
            return _conn
        except Exception as e:
            _available = False
            logger.debug(f"tdx本地库连接失败: {e}")
            return None


def is_available() -> bool:
    """是否可用（不触发连接，仅返回已知状态）"""
    if _available is None:
        return get_conn() is not None
    return _available is True


# ── symbol 格式转换 ──────────────────────────────────────────

def _to_tdx_symbol(code: str) -> str:
    """6位代码 → tdx2db格式：sh600157 / sz000001"""
    code = code.strip().lstrip("0").zfill(6)[-6:]   # 保证6位
    if code.startswith("6"):
        return f"sh{code}"
    elif code.startswith(("0", "2", "3")):
        return f"sz{code}"
    elif code.startswith(("4", "8")):              # 北交所
        return f"bj{code}"
    else:
        return f"sh{code}"                         # 指数等默认sh


def _to_std_code(symbol: str) -> str:
    """tdx2db格式 → 6位代码"""
    return symbol[2:] if len(symbol) > 6 else symbol


# ── 核心查询：历史日线 ───────────────────────────────────────

def query_history(
    code: str,
    start: str,
    end: str,
    adjust: str = "qfq",
) -> Optional[pd.DataFrame]:
    """
    查询个股历史日线。

    返回 DataFrame（列：date/open/high/low/close/volume/amount/turnover/change_pct）
    失败时返回 None。

    adjust: 'qfq'(前复权) / 'hfq'(后复权) / 'bfq'(不复权)
    """
    conn = get_conn()
    if conn is None:
        return None

    sym = _to_tdx_symbol(code)

    # ETF（51xxxx / 15xxxx / 16xxxx）用 etf 视图，其余用 stock 视图
    if code.startswith(("51", "15", "16", "58")):
        view = f"v_etf_{adjust}"
    else:
        view = f"v_stock_{adjust}"

    sql = f"""
        SELECT
            date,
            open,
            high,
            low,
            close,
            volume,
            amount,
            COALESCE(turnover, 0.0)    AS turnover,
            COALESCE(change_pct, 0.0)  AS change_pct
        FROM {view}
        WHERE symbol = ?
          AND date >= ?
          AND date <= ?
        ORDER BY date
    """
    try:
        df = conn.execute(sql, [sym, start[:10], end[:10]]).df()
        if df.empty:
            return None
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        logger.debug(f"tdx本地库: {code} {start}~{end} → {len(df)}条")
        return df
    except Exception as e:
        logger.debug(f"tdx本地库查询失败 {code}: {e}")
        return None


# ── 查询：全市场股票名称 ─────────────────────────────────────

def query_all_names() -> Optional[pd.DataFrame]:
    """返回 DataFrame(symbol, name, class)，symbol 为 sh/sz 格式"""
    conn = get_conn()
    if conn is None:
        return None
    try:
        df = conn.execute("SELECT symbol, name, class FROM raw_symbol_name").df()
        return df if not df.empty else None
    except Exception as e:
        logger.debug(f"tdx查询names失败: {e}")
        return None


# ── 查询：板块成分股 ────────────────────────────────────────

def query_block_members(block_name: str) -> Optional[pd.DataFrame]:
    """
    按板块名称查询成分股。
    返回 DataFrame(code, name)，code 为6位代码。
    """
    conn = get_conn()
    if conn is None:
        return None
    sql = """
        SELECT
            m.stock_symbol AS symbol,
            n.name
        FROM raw_tdx_blocks_member m
        JOIN raw_tdx_blocks_info i ON m.block_code = i.block_code
        LEFT JOIN raw_symbol_name n ON m.stock_symbol = n.symbol
        WHERE i.block_name = ?
    """
    try:
        df = conn.execute(sql, [block_name]).df()
        if df.empty:
            return None
        df["code"] = df["symbol"].apply(_to_std_code)
        return df[["code", "name"]].drop_duplicates("code").reset_index(drop=True)
    except Exception as e:
        logger.debug(f"tdx查询板块成分失败 {block_name}: {e}")
        return None


# ── 查询：基本面日数据（换手率/市值） ─────────────────────────

def query_basic(code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """
    查询 raw_basic_daily：date/turnover/floatmv/totalmv/change_pct/amplitude
    """
    conn = get_conn()
    if conn is None:
        return None
    sym = _to_tdx_symbol(code)
    sql = """
        SELECT date, turnover, floatmv, totalmv, change_pct, amplitude
        FROM raw_basic_daily
        WHERE symbol = ?
          AND date >= ?
          AND date <= ?
        ORDER BY date
    """
    try:
        df = conn.execute(sql, [sym, start[:10], end[:10]]).df()
        df["date"] = pd.to_datetime(df["date"])
        return df if not df.empty else None
    except Exception as e:
        logger.debug(f"tdx查询basic失败 {code}: {e}")
        return None


# ── 工具：检查数据库最新日期 ────────────────────────────────

def get_latest_date() -> Optional[str]:
    """返回数据库中最新的交易日期（YYYY-MM-DD），用于判断数据是否最新"""
    conn = get_conn()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT MAX(date)::VARCHAR FROM raw_kline_daily WHERE symbol LIKE 'sh%'"
        ).fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def get_db_info() -> dict:
    """返回数据库基本信息，用于状态显示"""
    conn = get_conn()
    if conn is None:
        return {"available": False}
    try:
        latest = get_latest_date()
        row = conn.execute("SELECT COUNT(DISTINCT symbol) FROM raw_kline_daily").fetchone()
        count = row[0] if row else 0
        return {
            "available": True,
            "path":      _db_path,
            "latest":    latest,
            "symbols":   count,
        }
    except Exception as e:
        return {"available": True, "path": _db_path, "error": str(e)}
