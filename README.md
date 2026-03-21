# deribit-options-monitor

Deribit BTC/ETH 期权分析工具，支持 DVOL 信号、Sell Put 推荐、大宗异动监控。

## 功能特性

- **DVOL 信号分析**: Z-Score + 趋势判断 + 动态阈值 + 置信度
- **Sell Put 推荐**: APR 排序 + 流动性过滤 (spread/OI)
- **大宗异动监控**: Call/Put 流向分析 + 市场情绪判断
- **双币种支持**: BTC 和 ETH
- **三种输出模式**: report (中文报告) / json / alert (短告警)

## 环境要求

- `Python 3.10+`
- `requests` Python 包

## 仓库结构

```text
.
├── README.md
├── CHANGELOG.md
├── LICENSE
├── install.sh
├── .gitignore
└── deribit-options-monitor/
    ├── SKILL.md
    ├── __init__.py
    ├── deribit_options_monitor.py
    └── agents/openai.yaml
```

## 安装方法

### 方法一：脚本安装

```bash
git clone https://github.com/你的用户名/deribit-options-monitor.git
cd deribit-options-monitor
bash install.sh
```

### 方法二：手动安装

```bash
mkdir -p ~/.openclaw/workspace/skills
cp -R deribit-options-monitor ~/.openclaw/workspace/skills/
```

## 快速验证

```bash
python3 ~/.openclaw/workspace/skills/deribit-options-monitor/__init__.py doctor
python3 ~/.openclaw/workspace/skills/deribit-options-monitor/__init__.py report --currency BTC --mode alert
```

## 使用示例

```bash
# DVOL 信号
python3 __init__.py dvol --currency BTC
python3 __init__.py dvol --currency ETH

# Sell Put 推荐
python3 __init__.py sell-put --currency BTC --max-delta 0.25 --min-apr 15

# 大宗异动
python3 __init__.py large-trades --currency BTC --min-usd-value 500000

# 完整报告
python3 __init__.py report --currency BTC --mode report
python3 __init__.py report --currency ETH --mode report
```

## 说明

- 本工具只使用 Deribit 公共 API，不需要 API Key
- 本地数据缓存写入 `~/.openclaw/workspace/skills/deribit-options-monitor/.cache/`
