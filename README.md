# Deribit Options Monitor

> 📊 **BTC/ETH 期权市场分析师 —— 专业的期权卖方工具**
> 
> **当前版本**: v2.1.0 | **最新更新**: 2026-04-13

Deribit Options Monitor 是一个基于 **Deribit 公共 API** 的期权市场分析工具，专为期权卖方（Sell Put / Sell Call 收租策略）设计。它可以帮你实时监控市场波动率、筛选高性价比收租合约、追踪机构大额异动，让你在复杂的期权市场中找到清晰的交易机会。

**v2.1.0 新增**: ✨ RV 已实现波动率模块 | RV/IV 择时信号 | Sell Call 推荐 | Max Pain 计算 | 8 种流向标签体系

## 🎉 最新版本 (v2.1.0)

### 新增功能
- **RV 已实现波动率模块**: 7d/30d/90d 多周期年化 RV，RV 趋势分析与历史对比
- **RV/IV 交叉信号**: 5 级信号分类，精准识别"低 RV 高 IV"抄底窗口
- **Sell Call 推荐**: 与 Sell Put 对称，支持备兑开仓策略分析
- **Max Pain 计算**: 自动计算当前交割周期最大痛点价格
- **8 种流向标签**: 更精细的大宗交易分类体系

### 优化改进
- 代码重构，消除重复逻辑
- 修复重复 API 请求问题，提升性能
- 改进错误处理与自动降级机制
- 完善数据持久化（新增 rv_history、option_snapshots 表）

详见 [CHANGELOG.md](CHANGELOG.md)

## ✨ 核心功能

### 1. 📈 **DVOL 波动率健康度分析**
- 计算当前 DVOL（Deribit 波动率指数）相对于历史的 Z-Score
- 自动判断趋势（上涨/下跌/震荡）
- 基于 30 天历史计算变异系数，动态调整置信阈值
- 输出 24h 和 7d 百分位排名
- 信号分类：`异常波动(高)` / `高波动率` / `中性偏多` / `中性` / `中性偏空` / `低波动率` / `异常波动(低)`

### 2. 📊 **RV 已实现波动率 & RV/IV 择时**
- 计算 **7d/30d/90d** 年化已实现波动率 (RV)
- **RV/IV 比值** 核心信号，判断期权定价贵贱：
  - `RV/IV < 0.6` → **低 RV 高 IV，抄底窗口**，期权被高估，最适合 Sell Put 收租
  - `RV/IV 0.6-0.8` → 合理区间，正常卖波动率
  - `RV/IV 0.8-1.2` → 回归区间
  - `RV/IV > 1.2` → **RV > IV，警惕卖方风险**，实际波动已经超过期权定价

### 3. 💰 **Sell Put 推荐**
多维度筛选优质 Sell Put 收租机会：
- 按 DTE（到期天数）过滤（默认 7-45 天）
- 流动性过滤（Open Interest + Bid-Ask Spread）
- Delta 控制（默认 `|Delta| ≤ 0.25`）
- APR 最低收益要求（默认 ≥ 15%）
- **流动性评分**（0-100）综合 Spread + Open Interest 排序
- 输出：Strike、DTE、Delta、IV、权利金、APR、盈亏平衡价

### 4. 📞 **Sell Call 推荐**
- 与 Sell Put 对称设计，满足备兑开仓需求
- 默认更保守参数（Delta ≤ 0.20）
- 明确标注风险提示
- 同样的流动性评分和 APR 排序

### 5. 🎯 **大宗异动监控**
追踪 Deribit 最近 24 小时大额期权交易：
- 智能流向标签分类（8 种）：
  - `protective_hedge` - 保护性对冲
  - `premium_collect` - 收取权利金
  - `speculative_put` - 投机做空
  - `call_momentum` - Call 追涨/机构建仓
  - `covered_call` - 备兑卖 Call
  - `call_overwrite` - Call 改仓
  - `call_speculative` - 投机买 Call
- 自动判断市场情绪（看涨/看跌/中性）
- 按严重程度分类（high/medium/info）

### 6. 🎯 **Max Pain 计算**
自动计算当前交割周期的最大痛点价格。

### 7. 💾 **数据持久化**
SQLite 本地缓存历史数据，支持：
- `dvol_history` - DVOL 时序数据
- `rv_history` - RV 波动率历史
- `option_snapshots` - 期权快照
- `large_trade_events` - 大宗成交事件

## 🚀 快速开始

### 环境要求
- Python 3.8+
- requests 库

### 安装

```bash
git clone https://github.com/你的用户名/deribit-options-monitor.git
cd deribit-options-monitor
pip install -r requirements.txt
```

### 使用

#### 健康检查

```bash
python __init__.py doctor
```

#### 获取 RV/IV 信号

```bash
python __init__.py rv --currency BTC
```

输出示例：
```
{
  "currency": "BTC",
  "rv_7d": 42.3,
  "rv_30d": 48.5,
  "rv_90d": 55.2,
  "iv_current": 68.4,
  "rv_iv_ratio": 0.62,
  "signal": "卖波动率机会",
  "confidence": 78,
  "message": "IV高于RV，期权定价偏贵，适合Sell Put收租"
}
```

#### 获取 Sell Put 推荐

```bash
python __init__.py sell-put --currency BTC --max-delta 0.25 --min-apr 15 --top-k 10
```

#### 获取 Sell Call 推荐

```bash
python __init__.py sell-call --currency BTC --max-delta 0.20 --min-apr 15 --top-k 10
```

#### 完整扫描生成中文分析报告

```bash
python __init__.py report --currency BTC --mode report
```

输出完整的中文分析师报告，包含：
1. 市场结论
2. DVOL 健康度
3. RV 实现波动率分析（含 RV/IV 信号）
4. Sell Put 推荐表
5. Sell Call 推荐表
6. 大宗异动分析
7. 策略建议
8. 风险提示

#### 获取大宗异动

```bash
python __init__.py large-trades --currency BTC --min-usd-value 500000
```

#### 获取 DVOL 信号

```bash
python __init__.py dvol --currency BTC
```

#### 完整扫描输出 JSON

```bash
python __init__.py scan --currency BTC --mode json
```

## 📖 输出示例

### 中文报告片段

```
════════════════════════════════════════════════════════════════════════════════
Deribit BTC 期权市场分析师报告
生成时间: 2026-04-07 00:15:00 UTC
当前 BTC 价格: $68250.00
════════════════════════════════════════════════════════════════════════════════

【1. 市场结论】
当前 RV/IV = 0.62，RV 显著低于 IV，期权定价偏高，是较好的 Sell Put 收租窗口。
DVOL 位于 75 百分位，波动率偏高，权利金收入丰厚。

【2. DVOL 波动率健康度】
当前 DVOL: 68.4
7日均值: 61.2
Z-Score: +1.25
趋势: 最近 24h 上涨
结论: 高波动率

【3. RV 实现波动率分析】
7d RV: 42.3
30d RV: 48.5
90d RV: 55.2
当前 IV: 68.4
RV/IV 比值: 0.62
信号: 卖波动率机会
解读: IV显著高于RV，期权定价偏贵，适合Sell Put收租

【4. Sell Put 推荐】
[表格]
合约名称       Strike   DTE  Delta   IV  权利金(USD)   APR  盈亏平衡
BTC-27APR26-70000-P  70000   20  -0.22  68%  $1,250  28%  $68750
...

【5. Sell Call 推荐】
[表格]
⚠️ 注意：Sell Call 理论上存在无限亏损风险，仅适合备兑开仓

...

【6. 大宗异动】
...

【7. 策略建议】
当前是较好的 Sell Put 收租时机，建议：
- 优先选择 Delta 0.15-0.25 的虚值合约
- 控制单张仓位，避免爆仓风险
- 低 RV 高 IV 环境下，卖波动率期望收益为正

【风险提示】
本工具仅供分析参考，不构成投资建议。期权交易风险极大，请谨慎操作。
```

## 🎯 策略理念

### 低 RV 高 IV 抄底择时

| 场景 | RV vs IV | 含义 | 操作建议 |
|------|----------|------|----------|
| **低 RV + 高 IV** | 实际波动小，但市场恐慌定价高 | 期权被高估 | ✅ **最佳 Sell Put 窗口** |
| 高 RV + 高 IV | 实际波动大，定价也贵 | 风险真实存在 | ⚠️ 谨慎，等 RV 回落 |
| 低 RV + 低 IV | 市场平静，权利金薄 | 赔率不够 | 🟡 观望或小仓 |
| 高 RV + 低 IV | 实际波动大但定价便宜 | 期权被低估 | 📈 买方波动率机会 |

## ⚙️ 配置

工具使用 Deribit **公共 API**，**不需要 API Key**，开箱即用。

如果需要自定义数据库路径，可以通过环境变量 `DERIBIT_MONITOR_DB` 指定：

```bash
export DERIBIT_MONITOR_DB=/path/to/your/database.db
```

## 🏗️ 架构

```
deribit-options-monitor/
├── deribit_options_monitor.py    # 核心分析逻辑 (2194 行)
├── __init__.py                   # CLI 入口
├── SKILL.md                      # Skill 说明
├── README.md                     # 项目说明
├── CHANGELOG.md                  # 版本更新日志
├── requirements.txt              # Python 依赖
├── LICENSE                       # MIT 许可证
└── .gitignore                    # Git 忽略
```

### 主要类和方法

| 方法 | 功能 |
|------|------|
| `get_dvol_signal()` | DVOL 波动率信号分析 |
| `get_rv_signal()` | RV 已实现波动率计算 |
| `get_rv_iv_signal()` | RV/IV 交叉信号分析 |
| `get_sell_put_recommendations()` | Sell Put 推荐 |
| `get_sell_call_recommendations()` | Sell Call 推荐 |
| `get_large_trade_alerts()` | 大宗异动监控 |
| `get_max_pain()` | Max Pain 计算 |
| `run_scan()` | 完整并行扫描 |
| `render_report()` | 生成中文报告 |

## 🚀 性能优化

- **Order Book 缓存**：60 秒 TTL，避免重复请求
- **Instrument 解析缓存**：减少重复正则解析
- **并行请求**：使用 ThreadPoolExecutor 并发获取数据
- **分块拉取**：大时间范围数据分块请求，避免 API 限流
- **自动清理**：定期清理过期缓存

## ⚠️ 限制

- **仅公共 API**：不接入私有账户，无法做持仓风险检查
- **无自动交易**：纯分析工具，不执行下单
- **Rate Limit**：Deribit 公共 API 有请求频率限制，请勿频繁调用

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

MIT License - 详见 [LICENSE](LICENSE)

## ⭐  Star 鼓励

如果这个工具对你有帮助，请给个 Star 支持！

## 📜 版本历史

| 版本 | 日期 | 主要更新 |
|------|------|----------|
| **v2.1.0** | 2026-04-13 | ✨ RV 已实现波动率模块 \| RV/IV 择时信号 \| Sell Call 推荐 \| Max Pain 计算 \| 代码重构优化 |
| v2.0.0 | 2026-03-21 | ✨ 双币种支持 (BTC/ETH) \| DVOL 信号增强 \| 流动性评分 \| 报告增强 |
| v1.0.0 | 2026-03-09 | 🎉 初始版本 \| DVOL 分析 \| Sell Put 推荐 \| 大宗异动监控 \| SQLite 存储 |

完整更新历史请查看 [CHANGELOG.md](CHANGELOG.md)

---

### 📊 8 种流向标签说明

| 标签 | 含义 | 典型场景 |
|------|------|----------|
| `protective_hedge` | 保护性对冲 | 持有现货，买入 Put 对冲风险 |
| `premium_collect` | 收取权利金 | 卖方收取权利金策略 |
| `speculative_put` | 投机做空 | 买入 Put 做空市场 |
| `call_momentum` | Call 追涨 | 买入 Call 追涨/机构建仓 |
| `covered_call` | 备兑卖 Call | 持有现货，卖 Call 收取权利金 |
| `call_overwrite` | Call 改仓 | 调整现有 Call 仓位 |
| `call_speculative` | 投机买 Call | 投机性买入 Call |
| `unknown` | 未知 | 无法明确分类的交易 |
