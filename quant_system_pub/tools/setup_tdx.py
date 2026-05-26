"""
tdx2db 一键安装配置脚本（跨平台：Windows / Linux / macOS）

使用方式：
    python tools/setup_tdx.py

步骤：
  1. 检测平台，下载对应的 tdx2db 二进制
  2. 引导用户确认通达信数据目录（vipdoc）
  3. 执行 tdx2db init 初始化数据库
  4. 验证数据库可用，写入 .env 配置
"""

import os
import sys
import platform
import subprocess
import urllib.request
import zipfile
import tarfile
import shutil
from pathlib import Path

# ── 平台检测 ────────────────────────────────────────────────
_SYSTEM = platform.system()   # Windows / Linux / Darwin
_MACHINE = platform.machine().lower()   # x86_64 / arm64 / aarch64

# ── tdx2db 版本与下载地址 ────────────────────────────────────
TDX2DB_VERSION = "v2026.5a"
_BASE = f"https://github.com/jing2uo/tdx2db/releases/download/{TDX2DB_VERSION}"

def _binary_url() -> tuple[str, str]:
    """返回 (下载URL, 文件名)"""
    if _SYSTEM == "Windows":
        return f"{_BASE}/tdx2db_Windows_x86_64.zip", "tdx2db_Windows_x86_64.zip"
    elif _SYSTEM == "Darwin":
        return f"{_BASE}/tdx2db_Darwin_arm64.tar.gz", "tdx2db_Darwin_arm64.tar.gz"
    else:  # Linux
        arch = "arm64" if _MACHINE in ("arm64", "aarch64") else "x86_64"
        fn = f"tdx2db_Linux_{arch}.tar.gz"
        return f"{_BASE}/{fn}", fn

def _exe_name() -> str:
    return "tdx2db.exe" if _SYSTEM == "Windows" else "tdx2db"

# ── 安装目录（跨平台默认路径）──────────────────────────────
def _default_install_dir() -> Path:
    if _SYSTEM == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "tdx2db"
    else:
        return Path.home() / ".local" / "bin"

def _default_db_path() -> Path:
    return Path.home() / ".quant_system" / "tdx.db"

# ── 通达信 vipdoc 常见路径 ──────────────────────────────────
def _tdx_vipdoc_candidates() -> list[Path]:
    if _SYSTEM == "Windows":
        candidates = []
        for drive in ["C", "D", "E"]:
            candidates += [
                Path(f"{drive}:/TDX/vipdoc"),
                Path(f"{drive}:/通达信/vipdoc"),
                Path(f"{drive}:/zd_hsgt/vipdoc"),
                Path(f"{drive}:/new_tdx/vipdoc"),
            ]
        return candidates
    elif _SYSTEM == "Darwin":
        return [
            Path.home() / "TDX" / "vipdoc",
            Path("/Applications/TDX/vipdoc"),
        ]
    else:  # Linux（通常通过 Wine 运行）
        return [
            Path.home() / ".wine" / "drive_c" / "TDX" / "vipdoc",
            Path.home() / "TDX" / "vipdoc",
        ]


def step1_download_binary(install_dir: Path) -> Path:
    print(f"\n[1/4] 下载 tdx2db {TDX2DB_VERSION}（{_SYSTEM}/{_MACHINE}）...")
    install_dir.mkdir(parents=True, exist_ok=True)
    exe = install_dir / _exe_name()

    if exe.exists():
        print(f"  已存在：{exe}，跳过下载")
        return exe

    url, fname = _binary_url()
    archive = install_dir / fname
    print(f"  下载地址：{url}")

    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(archive, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        print(f"\r  进度: {downloaded/total*100:.1f}%", end="")
        print()
    except Exception as e:
        print(f"\n  下载失败: {e}")
        print(f"  请手动下载: {url}")
        print(f"  解压后将 {_exe_name()} 放到: {install_dir}")
        sys.exit(1)

    # 解压
    if fname.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as z:
            z.extractall(install_dir)
    else:
        with tarfile.open(archive, "r:gz") as t:
            t.extractall(install_dir)
    archive.unlink(missing_ok=True)

    # 找到可执行文件
    if not exe.exists():
        found = list(install_dir.rglob(_exe_name()))
        if found:
            shutil.move(str(found[0]), str(exe))
        else:
            print(f"  错误：解压后未找到 {_exe_name()}")
            sys.exit(1)

    if _SYSTEM != "Windows":
        exe.chmod(exe.stat().st_mode | 0o111)   # +x

    print(f"  已安装：{exe}")
    return exe


def step2_find_vipdoc() -> str:
    print(f"\n[2/4] 查找通达信数据目录（vipdoc）...")
    for p in _tdx_vipdoc_candidates():
        if p.exists() and any(p.iterdir()):
            print(f"  自动找到：{p}")
            ans = input("  使用此目录？(Y/n) ").strip().lower()
            if ans in ("", "y", "yes"):
                return str(p)

    print("  请手动输入通达信 vipdoc 目录路径")
    if _SYSTEM == "Windows":
        print("  （通常是 C:\\TDX\\vipdoc 或 D:\\通达信\\vipdoc）")
    else:
        print("  （通常是 ~/.wine/drive_c/TDX/vipdoc）")

    path = input("  路径: ").strip().strip('"').strip("'")
    if not Path(path).exists():
        print(f"  目录不存在：{path}")
        sys.exit(1)
    return path


def step3_init_db(exe: Path, vipdoc_dir: str, db_path: Path) -> None:
    print(f"\n[3/4] 初始化数据库...")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  数据库路径：{db_path}")
    print(f"  数据来源：{vipdoc_dir}")
    print(f"  （首次初始化约需 30~120 秒）")

    db_uri = f"duckdb://{db_path}"
    cmd = [str(exe), "init", "--dburi", db_uri, "--dayfiledir", vipdoc_dir]
    print(f"\n  执行: {' '.join(cmd)}\n")

    try:
        proc = subprocess.run(cmd, timeout=300)
        if proc.returncode != 0:
            print(f"\n  tdx2db init 返回非0: {proc.returncode}")
            sys.exit(1)
    except subprocess.TimeoutExpired:
        print("\n  超时（5分钟），请检查 vipdoc 目录是否正确")
        sys.exit(1)
    except Exception as e:
        print(f"\n  执行失败: {e}")
        sys.exit(1)


def step4_verify_and_write_env(db_path: Path) -> None:
    print(f"\n[4/4] 验证并写入配置...")
    root = Path(__file__).parent.parent
    sys.path.insert(0, str(root))
    os.environ["TDX_DB_PATH"] = str(db_path)

    try:
        from data.tdx_local import get_db_info, query_history
        info = get_db_info()
        if not info.get("available"):
            print(f"  验证失败：{info}")
            return

        print(f"  股票数量：{info.get('symbols', '?')} 只")
        print(f"  最新日期：{info.get('latest', '?')}")

        from datetime import datetime, timedelta
        end   = datetime.today().strftime("%Y%m%d")
        start = (datetime.today() - timedelta(days=30)).strftime("%Y%m%d")
        df = query_history("000001", start, end)
        if df is not None and not df.empty:
            print(f"  测试查询 000001：{len(df)} 条，最新收盘 {df['close'].iloc[-1]:.2f}")
    except Exception as e:
        print(f"  验证异常: {e}")
        return

    # 写入 .env
    env_file = root / ".env"
    env_content = ""
    if env_file.exists():
        with open(env_file, encoding="utf-8") as f:
            lines = [l for l in f.readlines() if not l.startswith("TDX_DB_PATH=")]
        env_content = "".join(lines)
    env_content += f"\nTDX_DB_PATH={db_path}\n"
    with open(env_file, "w", encoding="utf-8") as f:
        f.write(env_content)

    print(f"\n  ✓ 成功！TDX_DB_PATH 已写入 .env")
    print(f"  仪表盘状态栏将显示 [tdx✓{info.get('latest')}]")


def print_cron_tip(exe: Path, db_path: Path) -> None:
    db_uri = f"duckdb://{db_path}"
    if _SYSTEM == "Windows":
        sched = "Windows 任务计划程序，每天 17:30 执行"
    else:
        sched = "crontab -e 添加：30 17 * * 1-5"
    print(f"""
─────────────────────────────────────────────────
每日数据更新（收盘后运行）：

  {exe} cron --dburi "{db_uri}"

建议设置自动定时任务：{sched}
─────────────────────────────────────────────────
""")


if __name__ == "__main__":
    print("=" * 52)
    print("tdx2db 接入配置向导（跨平台）")
    print(f"当前平台：{_SYSTEM} {_MACHINE}")
    print("=" * 52)

    install_dir = _default_install_dir()
    db_path     = _default_db_path()

    # 允许命令行覆盖
    if len(sys.argv) > 1:
        install_dir = Path(sys.argv[1])
    if len(sys.argv) > 2:
        db_path = Path(sys.argv[2])

    exe    = step1_download_binary(install_dir)
    vipdoc = step2_find_vipdoc()
    step3_init_db(exe, vipdoc, db_path)
    step4_verify_and_write_env(db_path)
    print_cron_tip(exe, db_path)
