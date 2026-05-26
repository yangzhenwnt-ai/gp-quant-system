"""
跨平台配置加载器

优先级（高→低）：
  1. 环境变量（.env 或系统环境）
  2. config.local.yaml（用户私有，gitignore）
  3. config.example.yaml（项目默认值）
  4. 代码内置兜底值

使用：
  from core.config_loader import cfg, env
  cfg("strategy.hold_num")        → 20
  cfg("backtest.commission_rate") → 0.0003
  env("OLLAMA_BASE_URL")          → "http://localhost:11434"
"""

import os
import sys
import platform
from pathlib import Path
from typing import Any

# ── 项目根目录（此文件在 core/ 下，上一层是根）─────────────
ROOT = Path(__file__).parent.parent

# ── 加载 .env（如果存在）────────────────────────────────────
def _load_dotenv():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:   # 不覆盖已有的系统环境变量
                os.environ[k] = v

_load_dotenv()


# ── 加载 YAML 配置（不强依赖 PyYAML，可用内置 json 回退）──
def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        pass
    # 无 PyYAML 时静默跳过，走兜底值
    return {}


_defaults = _load_yaml(ROOT / "config.example.yaml")
_local    = _load_yaml(ROOT / "config.local.yaml")


def _deep_get(d: dict, key_path: str, fallback: Any = None) -> Any:
    """用点号路径取嵌套值，如 'strategy.hold_num'"""
    keys = key_path.split(".")
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return fallback
        cur = cur.get(k)
        if cur is None:
            return fallback
    return cur


def cfg(key: str, fallback: Any = None) -> Any:
    """
    读取配置值，优先 config.local.yaml，其次 config.example.yaml。
    key 用点号分隔，如 'strategy.hold_num'
    """
    v = _deep_get(_local, key)
    if v is not None:
        return v
    v = _deep_get(_defaults, key)
    if v is not None:
        return v
    return fallback


def env(key: str, fallback: str = "") -> str:
    """读取环境变量，已自动加载 .env"""
    return os.environ.get(key, fallback)


# ── 平台检测工具 ────────────────────────────────────────────

def is_windows() -> bool:
    return platform.system() == "Windows"

def is_linux() -> bool:
    return platform.system() == "Linux"

def is_macos() -> bool:
    return platform.system() == "Darwin"


# ── 跨平台路径工具 ──────────────────────────────────────────

def data_dir() -> Path:
    """用户数据目录：~/.quant_system/（三平台统一）"""
    d = Path.home() / ".quant_system"
    d.mkdir(parents=True, exist_ok=True)
    return d

def project_data_dir() -> Path:
    """项目内 data/ 目录"""
    return ROOT / "data"

def cache_dir() -> Path:
    d = ROOT / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d

def reports_dir() -> Path:
    d = ROOT / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d

def tdx_db_path() -> Path | None:
    """
    tdx 本地数据库路径，按优先级：
      1. 环境变量 TDX_DB_PATH
      2. config.local.yaml → tdx_db_path
      3. 各平台常见默认路径
    """
    # 1. 环境变量
    ep = env("TDX_DB_PATH")
    if ep:
        p = Path(ep)
        if p.exists():
            return p

    # 2. config.local.yaml
    cp = cfg("tdx_db_path")
    if cp:
        p = Path(cp)
        if p.exists():
            return p

    # 3. 各平台常见路径
    candidates: list[Path] = []
    if is_windows():
        for drive in ["C", "D", "E"]:
            candidates += [
                Path(f"{drive}:/tdx/tdx.db"),
                Path(f"{drive}:/tdxdb/tdx.db"),
            ]
    # Linux / macOS 统一放 home 下
    candidates += [
        Path.home() / "tdx" / "tdx.db",
        Path.home() / ".quant_system" / "tdx.db",
        Path("/opt/tdx/tdx.db"),
    ]

    for p in candidates:
        if p.exists():
            return p
    return None


# ── 跨平台 stdout 编码修正 ──────────────────────────────────
def fix_stdout_encoding():
    """
    Windows 终端默认 CP936，Rich 输出中文会乱码。
    用 PYTHONIOENCODING=utf-8 启动时此函数为空操作。
    """
    if not is_windows():
        return
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
