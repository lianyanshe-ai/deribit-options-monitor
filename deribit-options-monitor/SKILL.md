---
name: deribit-options-monitor
version: 2.1.0
description: |
  Deribit BTC/ETH 期权扫描与 Sell Put/Sell Call 收租分析工具。
  支持 DVOL 波动率分析、RV/IV 择时、大宗异动监控、Max Pain 计算。
  Use when user mentions "Deribit", "BTC 期权", "ETH 期权", "DVOL", "Sell Put", "Sell Call", "收租", "大宗异动", "卖波动率", "RV/IV" or asks "现在 BTC 有什么好的收租机会？" / "ETH 期权怎么样" / "期权波动率健康吗".
allowed-tools:
  - Read
  - Write
  - Edit
  - exec
---

# Deribit Options Monitor Skill (v2.1)

使用 Deribit 公共 API 对 BTC/ETH 期权进行扫描、DVOL 健康度分析、RV/IV 择时、大宗成交监控和 Sell Put/Sell Call 收租机会筛选。

## 支持的币种

- **BTC** (比特币)
- **ETH** (以太坊)

## 功能模块

### 1. DVOL 信号分析
- 基于 Z-Score 的波动率信号判断
- 24h 趋势分析（上涨/下跌/震荡）
- 置信度计算
- 动态阈值（基于 30 天历史波动性自动调整）

### 2. RV 已实现波动率 & RV/IV 择时 **(新增)**
- 计算 **7d/30d/90d** 年化已实现波动率
- **RV/IV 比值** 判断期权贵贱，识别抄底窗口
  - `RV/IV < 0.6` → 低 RV 高 IV，**黄金卖波动率窗口**
  - `RV/IV > 1.2` → RV 超过 IV，**警惕卖方风险**

### 3. Sell Put 推荐
- APR 排序推荐
- 流动性过滤（bid-ask spread、open_interest）
- 流动性评分（0-100 分）

### 4. Sell Call 推荐 **(新增)**
- 与 Sell Put 对称设计，满足备兑开仓需求
- 默认更保守参数（max_delta = 0.20）
- 同样的流动性评分和 APR 排序
- ⚠️ 报告中明确标注风险提示

### 5. 大宗异动监控
- 机构流向标签识别（8 种标签）
- 市场情绪分析
- 重点合约标注
- 警告信号检测

### 6. Max Pain 计算 **(新增)**
- 自动计算当前交割周期的最大痛点价格

### 7. 现货价格
- 实时获取 BTC/ETH 现货价格

## 工作流

默认顺序固定为：

1. 并行执行：`get_dvol_signal()` + `get_rv_signal()` + `get_large_trade_alerts()`
2. `get_rv_iv_signal()` 计算 RV/IV 交叉信号
3. `get_sell_put_recommendations()` 扫描 Put 期权
4. `get_sell_call_recommendations()` 扫描 Call 期权
5. `get_max_pain()` 计算最大痛点
6. `run_scan()` 聚合成完整结构化结果
7. `render_report(mode="report")` 输出分析师报告

## Python 用法

```python
from pathlib import Path
import sys

skill_dir = Path("/path/to/deribit-options-monitor")
sys.path.insert(0, str(skill_dir))
from deribit_options_monitor import DeribitOptionsMonitor

monitor = DeribitOptionsMonitor()

# 健康检查
doctor = monitor.doctor()

# DVOL 信号
dvol_btc = monitor.get_dvol_signal(currency="BTC")

# RV 信号
rv_btc = monitor.get_rv_signal(currency="BTC")

# RV/IV 信号
rv_iv_btc = monitor.get_rv_iv_signal(currency="BTC")

# 大宗异动
flows_btc = monitor.get_large_trade_alerts(currency="BTC", min_usd_value=500000)

# Sell Put 推荐
ideas_btc = monitor.get_sell_put_recommendations(
    currency="BTC",
    max_delta=0.25,
    min_apr=15.0,
    max_spread_pct=10.0,
    min_open_interest=100.0
)

# Sell Call 推荐
call_ideas_btc = monitor.get_sell_call_recommendations(
    currency="BTC",
    max_delta=0.20,
    min_apr=15.0
)

# Max Pain
max_pain = monitor.get_max_pain(currency="BTC")

# 完整扫描
scan_btc = monitor.run_scan(currency="BTC")

# 报告生成
report = monitor.render_report(mode="report", scan_data=scan_btc)
alert = monitor.render_report(mode="alert", scan_data=scan_btc)
json_output = monitor.render_report(mode="json", scan_data=scan_btc)
```

## CLI 用法

```bash
# 健康检查
python3 __init__.py doctor

# DVOL 信号
python3 __init__.py dvol --currency BTC
python3 __init__.py dvol --currency ETH

# RV/IV 信号 (新增)
python3 __init__.py rv --currency BTC
python3 __init__.py rv --currency ETH

# Sell Put 推荐
python3 __init__.py sell-put --currency BTC --max-delta 0.25 --min-apr 15
python3 __init__.py sell-put --currency ETH --max-delta 0.25 --min-apr 15

# Sell Call 推荐 (新增)
python3 __init__.py sell-call --currency BTC --max-delta 0.20 --min-apr 15
python3 __init__.py sell-call --currency ETH --max-delta 0.20 --min-apr 15

# 大宗异动
python3 __init__.py large-trades --currency BTC --min-usd-value 500000
python3 __init__.py large-trades --currency ETH --min-usd-value 500000

# 完整扫描
python3 __init__.py scan --currency BTC
python3 __init__.py scan --currency ETH

# 生成报告
python3 __init__.py report --currency BTC --mode report
python3 __init__.py report --currency ETH --mode report
python3 __init__.py report --currency BTC --mode alert
python3 __init__.py report --currency BTC --mode json
```

## CLI 参数

| 参数 | 默认值 | 说明 |
|------|---------|------|
| `--currency` | BTC | 币种 (BTC/ETH) |
| `--min-usd-value` | 500000 | 大宗成交最小 USD 金额 |
| `--lookback-minutes` | 60 | 大宗成交回溯分钟数 |
| `--max-delta` | 0.25 | Sell Put 最大 Delta 绝对值 |
| `--max-delta-sell-call` | 0.20 | Sell Call 最大 Delta |
| `--min-apr` | 15.0 | Sell Put 最小 APR (%) |
| `--min-apr-sell-call` | 15.0 | Sell Call 最小 APR (%) |
| `--min-dte` | 7 | 最小到期天数 |
| `--max-dte` | 45 | 最大到期天数 |
| `--top-k` | 5 | 推荐合约数量 |
| `--max-spread-pct` | 10.0 | 最大 bid-ask spread (%) |
| `--min-open-interest` | 100.0 | 最小未平仓合约数 |
| `--mode` | report | 输出模式 (report/json/alert) |

## 输出说明

- `report`：中文分析师报告，适合直接阅读
- `json`：结构化结果，适合自动化消费
- `alert`：短告警文本，适合交给 cron / Telegram 管道

## 报告内容

### 1. 市场结论
综合 RV/IV + DVOL 信号 + 大宗成交的整体判断

### 2. DVOL 健康度
- 当前 DVOL 值
- 7天 Z-Score
- 趋势（上涨/下跌/震荡）
- 7天/24小时分位数
- 置信度
- 动态阈值
- 信号和建议

### 3. RV 实现波动率分析 **(新增)**
- 7d/30d/90d RV 值
- 当前 IV
- RV/IV 比值
- 信号解读（是否适合卖波动率）

### 4. Sell Put 推荐表
按 APR + 流动性评分排序：
- 合约名称
- Strike
- DTE（到期天数）
- Delta
- IV
- 权利金
- APR
- 盈亏平衡价
- 流动性评分

### 5. Sell Call 推荐表 **(新增)**
与 Sell Put 相同结构，默认参数更保守

### 6. 大宗异动分析
- 总成交数/总名义金额
- 市场情绪（看涨/看跌/中性）
- 分类统计（Call/Put 笔数、对冲/权利金笔数）
- 重点合约
- 警告信号

### 7. Max Pain **(新增)**
当前交割周期最大痛点价格

### 8. 策略建议与风险提示

## 数据库

历史数据存储在 SQLite 中：
- `dvol_history`：DVOL 时序数据
- `rv_history`：RV 已实现波动率历史
- `option_snapshots`：期权快照
- `large_trade_events`：大宗成交事件

## 升级日志

### v2.1.0 (2026-04-07)
- ✨ 新增 **RV 已实现波动率**模块
- ✨ 新增 **RV/IV 比值信号**，识别"低 RV 高 IV"抄底窗口
- ✨ 新增 **Sell Call 推荐**模块，支持备兑开仓
- ✨ 新增 **Max Pain 计算**
- ♻️ 重构代码，提取通用方法，消除 Sell Put/Sell Call 重复代码
- 🐛 修复重复 API 请求问题
- 🐛 修复参数传递问题
- 📝 报告新增 RV 分析章节和 Sell Call 章节

### v2.0.0
- 初始版本，支持 DVOL、Sell Put、大宗异动

## 限制

- 只使用 Deribit 公共 API，不接账户私有持仓
- 未接入 Gamma/Delta 持仓风险检查

## 触发关键词

- "Deribit"
- "BTC 期权"、"ETH 期权"
- "DVOL"、"期权波动率"
- "Sell Put"、"Sell Call"、"收租"、"卖Put"、"卖Call"
- "RV/IV"、"已实现波动率"
- "大宗异动"、"机构行为"
- "Max Pain"
- "现在有什么好的收租机会？"
- "期权波动率健康吗"
