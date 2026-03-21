---
name: deribit-options-monitor
version: 2.0.0
description: |
  Deribit BTC/ETH 期权扫描与 Sell Put 收租分析工具。
  Use when user mentions "Deribit", "BTC 期权", "ETH 期权", "DVOL", "Sell Put", "收租", "大宗异动", "卖波动率" or asks "现在 BTC 有什么好的收租机会？" / "ETH 期权怎么样" / "期权波动率健康吗".
allowed-tools:
  - Read
  - Write
  - Edit
  - exec
---

# Deribit Options Monitor Skill (v2.0)

使用 Deribit 公共 API 对 BTC/ETH 期权进行扫描、DVOL 健康度分析、大宗成交监控和 Sell Put 收租机会筛选。

## 支持的币种

- **BTC** (比特币)
- **ETH** (以太坊)

## 功能模块

### 1. DVOL 信号分析
- 基于 Z-Score 的波动率信号判断
- 24h 趋势分析（上涨/下跌/震荡）
- 置信度计算
- 动态阈值（基于 30 天历史波动性自动调整）

### 2. Sell Put 推荐
- APR 排序推荐
- 流动性过滤（bid-ask spread、open_interest）
- 流动性评分（0-100 分）

### 3. 大宗异动监控
- 机构流向标签识别
- 市场情绪分析
- 重点合约标注
- 警告信号检测

### 4. 现货价格
- 实时获取 BTC/ETH 现货价格

## 工作流

默认顺序固定为：

1. `_get_spot_price()` 获取现货价格
2. `get_dvol_signal()` 检查当前是否属于适合卖波动率的环境
3. `get_sell_put_recommendations()` 扫描 7-45 天、合适 Delta 的 Put
4. `get_large_trade_alerts()` 检查机构是否在做反向或对冲动作
5. `run_scan()` 聚合成完整结构化结果
6. `render_report(mode="report")` 输出分析师报告

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
dvol_eth = monitor.get_dvol_signal(currency="ETH")

# 大宗异动
flows_btc = monitor.get_large_trade_alerts(currency="BTC", min_usd_value=500000)
flows_eth = monitor.get_large_trade_alerts(currency="ETH", min_usd_value=500000)

# Sell Put 推荐
ideas_btc = monitor.get_sell_put_recommendations(
    currency="BTC",
    max_delta=0.25,
    min_apr=15.0,
    max_spread_pct=10.0,
    min_open_interest=100.0
)
ideas_eth = monitor.get_sell_put_recommendations(
    currency="ETH",
    max_delta=0.25,
    min_apr=15.0
)

# 完整扫描
scan_btc = monitor.run_scan(currency="BTC")
scan_eth = monitor.run_scan(currency="ETH")

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

# 大宗异动
python3 __init__.py large-trades --currency BTC --min-usd-value 500000
python3 __init__.py large-trades --currency ETH --min-usd-value 500000

# Sell Put 推荐
python3 __init__.py sell-put --currency BTC --max-delta 0.25 --min-apr 15
python3 __init__.py sell-put --currency ETH --max-delta 0.25 --min-apr 15

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
| `--max-delta` | 0.25 | 最大 Delta 绝对值 |
| `--min-apr` | 15.0 | 最小 APR (%) |
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
综合 DVOL 信号 + 大宗成交 + Sell Put 的整体判断

### 2. DVOL 健康度
- 当前 DVOL 值
- 7天 Z-Score
- 趋势（上涨/下跌/震荡）
- 7天/24小时分位数
- 置信度
- 动态阈值
- 信号和建议

### 3. Sell Put 推荐表
按 APR 排序的推荐合约，包含：
- 合约名称
- DTE（到期天数）
- Delta
- APR
- 权利金
- 流动性评分

### 4. 大宗异动分析
- 总成交数/总名义金额
- 市场情绪（看涨/看跌/中性）
- 分类统计（Call/Put 笔数、对冲/权利金笔数）
- 重点合约
- 警告信号

### 5. 风险提示与行动建议

## 数据库

历史数据存储在 SQLite 中：
- `dvol_history`：DVOL 时序数据
- `option_snapshots`：期权快照
- `large_trade_events`：大宗成交事件

## 限制

- 只使用 Deribit 公共 API，不接账户私有持仓
- 未接入 Gamma/Delta 持仓风险检查
- Sell Put 推荐仅针对 Put 期权

## 触发关键词

- "Deribit"
- "BTC 期权"、"ETH 期权"
- "DVOL"、"期权波动率"
- "Sell Put"、"收租"、"卖Put"
- "大宗异动"、"机构行为"
- "现在有什么好的收租机会？"
- "期权波动率健康吗"
