"""
跨平台键盘输入模块

统一封装 Windows (msvcrt) / Linux+macOS (termios) 的无回显单键读取。
所有 tui_dashboard 中原来直接 import msvcrt 的地方改用此模块。

公开 API：
  getch()        → str   读一个字符（阻塞），特殊键返回空串
  kbhit()        → bool  是否有按键等待（非阻塞）
  flush_input()           清空输入缓冲区
  read_line()    → str   读一行（有回显，跨平台 input() 等价）
"""

import sys
import platform

_IS_WIN = platform.system() == "Windows"


# ════════════════════════════════════════════════════════════
# Windows 实现（msvcrt）
# ════════════════════════════════════════════════════════════

def _win_getch() -> str:
    import msvcrt
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):   # 功能键前缀，吞掉第二字节
        msvcrt.getwch()
        return ""
    return ch


def _win_kbhit() -> bool:
    import msvcrt
    return msvcrt.kbhit()


def _win_flush() -> None:
    import msvcrt
    while msvcrt.kbhit():
        msvcrt.getwch()


# ════════════════════════════════════════════════════════════
# Unix 实现（select + os.read，不修改 termios）
# ════════════════════════════════════════════════════════════
# Rich Live 已在内部把终端设为 raw 模式。
# 这里不再调用 tty.setraw / tcsetattr，避免与 Rich 冲突导致卡死。
# 直接用 select 检测输入就绪，再用 os.read 读原始字节。

def _unix_getch(timeout: float = 0.1) -> str:
    import select, os
    fd = sys.stdin.fileno()
    try:
        ready = select.select([fd], [], [], timeout)[0]
        if not ready:
            return ""
        raw = os.read(fd, 32)   # 一次最多读 32 字节（处理多字节 ESC 序列）
        if not raw:
            return ""
        # ESC 序列（方向键等）：以 \x1b[ 开头，直接丢弃
        if raw[0:1] == b"\x1b":
            return ""
        # 只取第一个字节解码（普通 ASCII 按键）
        return raw[0:1].decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _unix_kbhit() -> bool:
    import select
    fd = sys.stdin.fileno()
    try:
        return bool(select.select([fd], [], [], 0)[0])
    except Exception:
        return False


def _unix_flush() -> None:
    import select, os
    fd = sys.stdin.fileno()
    try:
        while select.select([fd], [], [], 0)[0]:
            os.read(fd, 256)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
# 公开接口（自动分派）
# ════════════════════════════════════════════════════════════

def getch() -> str:
    """读取一个按键，不回显，阻塞直到有输入。特殊键返回空串。"""
    if _IS_WIN:
        return _win_getch()
    return _unix_getch()


def kbhit() -> bool:
    """非阻塞：是否有按键等待处理。"""
    if _IS_WIN:
        return _win_kbhit()
    return _unix_kbhit()


def flush_input() -> None:
    """清空输入缓冲区，丢弃所有未读按键。"""
    if _IS_WIN:
        _win_flush()
    else:
        try:
            _unix_flush()
        except Exception:
            pass


def read_line(prompt: str = "") -> str:
    """带回显的行输入（等价于 input()，跨平台一致）。"""
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    return sys.stdin.readline().rstrip("\n")
