# deribit-options-monitor

这是一个给 OpenClaw 用的 Deribit BTC 期权分析 skill。

它可以做这些事：

- 检查 BTC DVOL 波动率环境
- 扫描 Sell Put 收租机会
- 监控 Deribit 大宗期权成交
- 输出 `report`、`json`、`alert` 三种结果

## 适合谁

如果你已经在用 OpenClaw，想直接通过 GitHub 开源代码安装一个 Deribit 期权监控 skill，这个仓库就是给你用的。

## 环境要求

安装前请先确认本机已经有：

- `OpenClaw`
- `Python 3`
- `requests` Python 包

## 仓库结构

```text
.
├── README.md
├── install.sh
├── .gitignore
└── deribit-options-monitor/
    ├── SKILL.md
    ├── __init__.py
    ├── deribit_options_monitor.py
    └── agents/openai.yaml
```

## 安装方法

### 方法一：最适合小白

先下载仓库，然后执行一条安装命令：

```bash
git clone https://github.com/lianyanshe-ai/deribit-options-monitor.git
cd deribit-options-monitor
bash install.sh
```

安装完成后，skill 会被复制到：

```bash
~/.openclaw/workspace/skills/deribit-options-monitor
```

### 方法二：手动安装

如果你不想运行脚本，也可以手动复制：

```bash
mkdir -p ~/.openclaw/workspace/skills
cp -R deribit-options-monitor ~/.openclaw/workspace/skills/
```

## 安装后验证

执行下面两条命令，确认 skill 正常：

```bash
python3 ~/.openclaw/workspace/skills/deribit-options-monitor/__init__.py doctor
python3 ~/.openclaw/workspace/skills/deribit-options-monitor/__init__.py report --currency BTC --mode alert
```

如果你看到 `ok: true`，说明 Deribit 接口和本地 SQLite 都工作正常。

## 使用示例

```bash
python3 ~/.openclaw/workspace/skills/deribit-options-monitor/__init__.py dvol --currency BTC
python3 ~/.openclaw/workspace/skills/deribit-options-monitor/__init__.py sell-put --currency BTC --max-delta 0.25 --min-apr 15
python3 ~/.openclaw/workspace/skills/deribit-options-monitor/__init__.py large-trades --currency BTC --min-usd-value 500000
python3 ~/.openclaw/workspace/skills/deribit-options-monitor/__init__.py report --currency BTC --mode report
```

## 说明

- 本 skill 只使用 Deribit 公共 API，不需要 API Key。
- 当前版本只支持 `BTC`。
- 本地历史缓存会写入 `~/.openclaw/workspace/skills/deribit-options-monitor/.cache/`。
