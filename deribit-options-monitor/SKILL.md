---
name: deribit-options-monitor
version: 1.0.0
description: |
  Deribit BTC 期权扫描与 Sell Put 收租分析工具。Use when user mentions "Deribit", "BTC 期权", "DVOL", "Sell Put", "收租", "大宗异动", "卖波动率" or asks "现在 BTC 有什么好的收租机会？" / "看下 Deribit BTC 期权" / "期权波动率健康吗".
allowed-tools:
  - Read
  - Write
  - Edit
  - exec
---

# Deribit Options Monitor Skill

用 Deribit 公共 API 做 `BTC` 期权扫描、DVOL 健康度分析、大宗成交监控和 Sell Put 收租机会筛选。

## 工作流

默认顺序固定为：

1. `get_dvol_signal()` 检查当前是否属于适合卖波动率的环境
2. `get_sell_put_recommendations()` 扫描 7-45 天、合适 Delta 的 Put
3. `get_large_trade_alerts()` 检查机构是否在做反向或对冲动作
4. `run_scan()` 聚合成完整结构化结果
5. `render_report(mode="report")` 输出分析师报告

## Python 用法

```python
from pathlib import Path
import sys

skill_dir = Path("/path/to/deribit-options-monitor")
sys.path.insert(0, str(skill_dir))
from deribit_options_monitor import DeribitOptionsMonitor

monitor = DeribitOptionsMonitor()

doctor = monitor.doctor()
dvol = monitor.get_dvol_signal(currency="BTC")
flows = monitor.get_large_trade_alerts(currency="BTC", min_usd_value=500000)
ideas = monitor.get_sell_put_recommendations(currency="BTC", max_delta=0.25, min_apr=15.0)
scan = monitor.run_scan(currency="BTC")
report = monitor.render_report(mode="report", scan_data=scan)
alert = monitor.render_report(mode="alert", scan_data=scan)
```

## CLI 用法

```bash
python3 {skill_dir}/__init__.py doctor
python3 {skill_dir}/__init__.py dvol --currency BTC
python3 {skill_dir}/__init__.py large-trades --currency BTC --min-usd-value 500000
python3 {skill_dir}/__init__.py sell-put --currency BTC --max-delta 0.25 --min-apr 15
python3 {skill_dir}/__init__.py scan --currency BTC
python3 {skill_dir}/__init__.py report --currency BTC --mode report
python3 {skill_dir}/__init__.py report --currency BTC --mode alert
```

## 输出说明

- `report`：中文分析师报告，适合直接阅读
- `json`：结构化结果，适合自动化消费
- `alert`：短告警文本和 `alerts[]`，适合交给 cron / Telegram 管道

## 默认假设

- v1 只支持 `BTC`
- 只用 Deribit 公共 API，不接账户私有持仓
- `min_usd_value` 按 `amount × index_price` 的标的名义金额过滤
- 历史数据会写入本地 SQLite，用于 DVOL 分位和均值回归分析
