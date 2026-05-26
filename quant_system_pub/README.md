# A股量化仪表盘

基于终端（TUI）的 A 股实时监控与量化分析系统。

**核心功能：**
- 大盘情绪实时评分（沪深创三大指数 + 涨跌停/量能/北向）
- 持仓跟踪（止盈止损预警、技术恶化信号）
- 盘中异动扫描（量价突变、大单监控）
- 多源实时快讯 + Ollama 本地 AI 分析 + 受益股自动关联
- 热门板块龙头选股、优质股扫描（动量 + 价值双维度）
- 错杀反弹扫描（大盘跌 ≥2% 自动触发）
- 持仓风险量化（7 维度 0-100 评分）
- 观察池管理（持续跟踪推荐股）
- 收盘总结 + 每日策略建议
- 个股深度分析（技术面 + 基本面 + AI 解读）
- 本地回测引擎（事件驱动，月度再平衡）

支持 **Windows / Linux / macOS**，数据全部来自公开接口，无需账号鉴权。

---

## 安装运行

### 前提

- Python 3.10 或以上（推荐 3.12+）
- 能访问公网（腾讯/东财行情接口）

### 第一步：获取代码

```bash
git clone <repo_url>
cd quant_system_pub
```

### 第二步：安装依赖

```bash
pip install -r requirements.txt
```

> macOS / Linux 如果 pip 指向 Python 2，请用 `pip3` 或 `python3 -m pip`

### 第三步：配置环境变量

```bash
cp .env.example .env
```

用任意编辑器打开 `.env`，按需填写：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OLLAMA_BASE_URL` | Ollama 服务地址（B 块 AI 分析用） | `http://localhost:11434` |
| `OLLAMA_MODEL` | 使用的模型名 | `qwen3:4b` |
| `TDX_DB_PATH` | 本地 tdx2db 数据库路径（可选，见下文） | 空（走网络） |
| `LOG_LEVEL` | 日志级别 | `WARNING` |

> 不配置 Ollama 也能正常运行，只是新闻块没有 AI 分析。

### 第四步：初始化持仓（可选）

```bash
cp data/my_portfolio.example.json data/my_portfolio.json
```

编辑 `data/my_portfolio.json`，填入真实持仓（也可以先不填，仪表盘 D 键可以随时增减）：

```json
{
  "600036": {
    "name": "招商银行",
    "cost": 38.00,
    "shares": 500,
    "buy_date": "2025-01-01",
    "stop_loss": 35.00,
    "target": 46.00,
    "note": "备注"
  }
}
```

### 第五步：运行

```bash
python tui_dashboard.py
```

Linux / macOS 若默认 python 指向 Python 2：

```bash
python3 tui_dashboard.py
```

建议最大化终端窗口，仪表盘宽度不足会压缩布局。

---

## 仪表盘快捷键

| 按键 | 功能 | 说明 |
|------|------|------|
| `A` | 大盘情绪详情 | 情绪评分、指数走势、涨跌停分布、北向资金 |
| `B` | 实时快讯 + AI 分析 | 新闻列表、AI 解读、受益股票代码 |
| `C` | 盘中异动详情 | 量价突变、大单、封板/炸板列表 |
| `D` | 持仓跟踪 | 盈亏、止盈止损状态、可增删仓位 |
| `E` | 热门选股详情 | 热门板块 + 龙头股 |
| `F` | 优质股扫描 | 动量股 + 价值股双维度筛选 |
| `G` | 每日策略建议 | 仓位建议、重点关注股 |
| `K` | 错杀反弹扫描 | 大盘跌 ≥2% 时自动触发，寻找超跌反弹机会 |
| `W` | 观察池管理 | 跟踪推荐股、查看跟踪进度 |
| `I` | 个股深度分析 | 输入股票代码，获取技术+基本面+AI 分析 |
| `R` | 持仓风险扫描 | 7 维度量化风险评分（财务/技术/资金等） |
| `S` | 收盘总结 | 今日复盘 + 明日前瞻 |
| `Q` | 退出 | — |

---

## 可选：接入本地数据库（大幅加速）

不配置时系统自动使用腾讯行情网络接口，历史查询约 1~3 秒/次。  
接入本地 tdx2db 数据库后，历史查询变为毫秒级，且数据量从约 640 条扩展至全量历史。

**前提：** 本机安装了通达信（需要其 `vipdoc` 日线文件）

```bash
python tools/setup_tdx.py
```

脚本自动完成：
1. 检测系统平台，下载对应 tdx2db 二进制
2. 引导选择通达信 `vipdoc` 目录
3. 初始化 DuckDB 本地数据库
4. 验证数据完整性，写入 `TDX_DB_PATH` 到 `.env`

**每日收盘后更新数据：**

```bash
# Linux / macOS
~/.local/bin/tdx2db cron --dburi "duckdb://~/.quant_system/tdx.db"

# Windows（路径由 setup_tdx.py 完成后提示）
%LOCALAPPDATA%\tdx2db\tdx2db.exe cron --dburi "duckdb://C:/Users/<用户名>/.quant_system/tdx.db"
```

建议设置系统定时任务（crontab 或 Windows 任务计划程序），在每天 17:30 自动执行。

---

## 自定义策略参数（可选）

```bash
cp config.example.yaml config.local.yaml
```

编辑 `config.local.yaml` 可覆盖默认参数，例如：

```yaml
backtest:
  initial_capital: 500000   # 初始资金
  commission_rate: 0.0003   # 手续费

strategy:
  hold_num: 10              # 持仓股数
  stop_loss: -0.08          # 止损线

mistaken_kill:
  index_drop_threshold: -2.0  # 触发错杀扫描的指数跌幅
```

所有可配置项见 `config.example.yaml`，未配置项自动使用默认值。

---

## 目录结构

```
quant_system_pub/
├── core/
│   ├── config_loader.py      # 跨平台配置（.env + YAML 三级优先级）
│   └── keyboard.py           # 跨平台键盘输入（Windows msvcrt / Unix termios）
├── data/
│   ├── reliable_api.py       # 统一数据层（腾讯/东财/tdx，自动 failover）
│   ├── tdx_local.py          # 本地 DuckDB 查询（tdx2db）
│   ├── watchlist.py          # 观察池持久化
│   ├── risk_scanner.py       # 持仓风险量化
│   ├── fundamental.py        # 基本面数据
│   └── my_portfolio.example.json
├── market/
│   ├── market_pulse.py       # 大盘情绪评分
│   ├── portfolio_tracker.py  # 持仓跟踪 + 预警
│   ├── intraday_scanner.py   # 盘中异动扫描
│   ├── daily_advisor.py      # 每日策略建议
│   └── closing_summary.py    # 收盘总结
├── news/
│   ├── news_fetcher.py       # 多源快讯抓取（财联社/新浪）
│   ├── ai_analyzer.py        # Ollama AI 深度分析
│   └── sector_stock_linker.py # 新闻→受益股关联
├── selector/
│   ├── mistaken_kill.py      # 错杀反弹扫描
│   ├── quality_scanner.py    # 优质股筛选（动量+价值）
│   └── stock_picker.py       # 热门板块龙头选股
├── factors/                  # 技术/基本面因子计算
├── strategy/                 # ML 选股模型
├── backtest/
│   └── engine.py             # 事件驱动回测引擎
├── tools/
│   └── setup_tdx.py          # tdx2db 一键安装（跨平台）
├── tui_dashboard.py          # 主入口
├── config.py                 # 全局配置（读取 config_loader）
├── config.example.yaml       # 参数模板（可提交 git）
├── .env.example              # 环境变量模板（可提交 git）
├── requirements.txt
└── .gitignore
```

---

## 数据来源

| 数据类型 | 来源 | 说明 |
|----------|------|------|
| 实时行情（全市场） | 腾讯行情 HTTP | 5200+ 只，无需鉴权 |
| 历史日线 | 腾讯 HTTP / tdx2db 本地 | 优先本地，自动降级网络 |
| 资金流向 | 东方财富 push2 | 当日主力/超大单净流入 |
| 涨跌停数据 | 东方财富 | 实时更新 |
| 北向资金 | 东方财富 | 实时 |
| 新闻快讯 | 财联社 + 新浪财经 | 近 40 分钟实时快讯 |
| AI 分析 | Ollama 本地模型 | 需自行部署，不部署不影响其他功能 |

---

## 平台兼容性

| 功能 | Windows | Linux | macOS |
|------|:-------:|:-----:|:-----:|
| 仪表盘主体 | ✓ | ✓ | ✓ |
| 键盘交互 | msvcrt | termios | termios |
| 中文显示 | ✓（UTF-8 自动修正） | ✓ | ✓ |
| tdx2db 本地库 | ✓ | ✓ | ✓ arm64 |
| Ollama AI | ✓ | ✓ | ✓ |
| SSH 远程运行 | — | ✓ | ✓ |

---

## 常见问题

**Q: 不安装 Ollama 能用吗？**  
A: 能。Ollama 只影响 B 块新闻 AI 分析，其余功能完全正常。

**Q: 没有通达信数据怎么办？**  
A: 不需要。默认走腾讯行情网络接口，历史查询稍慢（1~3 秒），但功能完整。

**Q: 终端显示乱码怎么办？**  
A: Windows 下用 `python tui_dashboard.py` 启动时系统会自动切换到 UTF-8 输出。如仍乱码，在终端先执行 `chcp 65001`，或用 Windows Terminal / VSCode 终端运行。

**Q: 运行时报 `ModuleNotFoundError`？**  
A: 检查依赖是否完整安装：`pip install -r requirements.txt`，注意 pip 对应的 Python 版本需要 ≥ 3.10。

**Q: 盘中运行还是收盘后也能看？**  
A: 两者都可以。非交易时段行情数据为收盘价，其他分析功能（风险扫描、回测等）不受时段限制。

---

## 免责声明

本项目仅供学习、研究和技术交流使用。

- 本系统输出的所有分析结果、选股建议、买卖信号均**不构成任何投资建议**
- 股市有风险，投资需谨慎，入市须自担风险
- 历史数据与回测结果不代表未来收益，量化模型存在失效风险
- 请在充分了解相关风险的前提下，结合自身风险承受能力做出独立判断
- 作者及贡献者对使用本项目造成的任何直接或间接损失不承担任何责任

**投资有风险，入市需谨慎。**
