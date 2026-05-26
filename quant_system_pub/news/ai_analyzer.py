"""
AI深度分析模块
对关键消息调用 Ollama 本地模型做深度分析：
  - 消息对哪些板块有影响（利好/利空/中性）
  - 影响逻辑是什么
  - 推荐关注哪类股票/回避哪类股票
  - 紧迫程度（今天就有行情 / 中期布局 / 观察即可）

配置（优先级高→低）：
  1. .env 文件：OLLAMA_BASE_URL / OLLAMA_MODEL
  2. config.local.yaml（暂无此项）
  3. 默认值：http://localhost:11434 / qwen3:4b
"""
import json
import logging
import urllib.request as _ur
import urllib.error

from core.config_loader import env

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = env("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = env("OLLAMA_MODEL",    "qwen3:4b")
TIMEOUT_SEC     = 12

# 连通性缓存：None=未检测，True=可用，False=不可用
_ollama_available: bool | None = None


def check_ollama(base_url: str = None) -> bool:
    """
    检测 Ollama 服务是否可用，结果缓存（进程内只探测一次）。
    不可用时不抛异常，只返回 False。
    """
    global _ollama_available
    if _ollama_available is not None:
        return _ollama_available
    _url = base_url or OLLAMA_BASE_URL
    try:
        req = _ur.Request(f"{_url}/api/tags", method="GET")
        with _ur.urlopen(req, timeout=3) as resp:
            _ollama_available = resp.status == 200
    except Exception:
        _ollama_available = False
    if not _ollama_available:
        logger.info(f"Ollama 不可用（{_url}），AI分析功能已关闭")
    return _ollama_available

SYSTEM_PROMPT = """你是一位专注A股市场的资深分析师，有15年实盘经验。
你的任务是分析财经新闻/快讯对A股市场的影响。

回答必须简洁、直接，用中文，严格按照以下固定格式输出（每行一个字段，不要多余文字，不要思考过程）：

【影响性质】利好 / 利空 / 中性（只选一个）
【紧迫程度】即时（今天就会有行情）/ 短期（1~3天）/ 中期（1~4周）/ 长期布局
【受益板块】板块1、板块2、板块3（最多5个，用顿号分隔，没有就填无）
【受损板块】板块1、板块2（最多3个，用顿号分隔，没有就填无）
【受益股票】股票名称1、股票名称2、股票名称3（直接写A股上市公司名称，最多5家，没有就填无）
【逻辑分析】2句话说清楚为什么这样判断
【操作建议】具体建议（如：关注XX板块龙头，等回调后介入）"""


def analyze_with_ollama(
    title: str,
    content: str,
    base_url: str = None,
    model: str = None,
) -> dict:
    """
    调用 Ollama 本地模型分析单条财经新闻。
    返回结构化结果，不可用时返回空dict。
    """
    _url   = base_url or OLLAMA_BASE_URL
    _model = model    or OLLAMA_MODEL

    if not check_ollama(_url):
        return {}

    user_msg = f"请分析以下财经快讯对A股市场的影响：\n\n标题：{title}\n\n内容：{content}"

    payload = json.dumps({
        "model":  _model,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 400},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
    }, ensure_ascii=False).encode("utf-8")

    try:
        req = _ur.Request(
            f"{_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        text = raw.get("message", {}).get("content", "")
        if not text:
            return {}
        return _parse_ai_response(text)
    except urllib.error.URLError as e:
        logger.debug(f"Ollama连接失败: {e}")
        return {}
    except Exception as e:
        logger.debug(f"Ollama分析失败: {e}")
        return {}


def _parse_ai_response(text: str) -> dict:
    result = {
        "nature":          "",
        "urgency":         "",
        "benefit_sectors": [],
        "harm_sectors":    [],
        "benefit_stocks":  [],
        "logic":           "",
        "suggestion":      "",
        "raw":             text,
    }
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("【影响性质】"):
            result["nature"] = line.replace("【影响性质】", "").strip()
        elif line.startswith("【紧迫程度】"):
            result["urgency"] = line.replace("【紧迫程度】", "").strip()
        elif line.startswith("【受益板块】"):
            val = line.replace("【受益板块】", "").strip()
            result["benefit_sectors"] = [s.strip() for s in val.replace("，", "、").split("、")
                                          if s.strip() and s.strip() != "无"]
        elif line.startswith("【受损板块】"):
            val = line.replace("【受损板块】", "").strip()
            result["harm_sectors"] = [s.strip() for s in val.replace("，", "、").split("、")
                                       if s.strip() and s.strip() != "无"]
        elif line.startswith("【受益股票】"):
            val = line.replace("【受益股票】", "").strip()
            result["benefit_stocks"] = [s.strip() for s in val.replace("，", "、").split("、")
                                         if s.strip() and s.strip() != "无"]
        elif line.startswith("【逻辑分析】"):
            result["logic"] = line.replace("【逻辑分析】", "").strip()
        elif line.startswith("【操作建议】"):
            result["suggestion"] = line.replace("【操作建议】", "").strip()
    return result


# 向后兼容
def analyze_with_ai(title: str, content: str) -> dict:
    return analyze_with_ollama(title, content)
