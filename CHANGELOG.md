# Changelog

All notable changes to this project will be documented in this file.

## [2.1.0] - 2026-04-13

### Added
- **RV 已实现波动率模块**:
  - 7d/30d/90d 多周期年化 RV 计算
  - RV 趋势分析与历史对比
  - RV 百分位排名评估
- **RV/IV 交叉信号分析**:
  - RV/IV 比值计算，判断期权定价贵贱
  - 5 级信号分类：抄底窗口 (<0.6) / 卖波动率机会 (0.6-0.8) / 合理区间 (0.8-1.2) / RV 偏高 (1.2-1.5) / 危险区域 (>1.5)
  - "低 RV 高 IV" 抄底窗口识别
- **Sell Call 推荐模块**:
  - 与 Sell Put 对称的推荐逻辑
  - 默认更保守参数 (Delta ≤ 0.20)
  - 支持备兑开仓策略分析
- **Max Pain 最大痛点计算**:
  - 自动计算当前交割周期最大痛点价格
  - 期权持仓分布分析
- **新 CLI 命令**:
  - `rv` -- RV 已实现波动率信号
  - `sell-call` -- Sell Call 推荐

### Changed
- **代码重构**:
  - 消除 Sell Put/Sell Call 重复代码，提取公共逻辑
  - 优化 instrument 名称解析逻辑
  - 改进参数传递机制，修复潜在 bug
- **性能优化**:
  - 修复重复 API 请求问题
  - 优化并行请求策略
  - 改进缓存 TTL 机制
- **报告增强**:
  - 新增 RV/IV 信号展示
  - 新增 Sell Call 推荐部分
  - 新增 Max Pain 数据展示
  - 完善市场解读与策略建议

### Fixed
- 修复参数传递错误导致的部分功能异常
- 修复重复 API 请求造成的性能问题
- 修复 Sell Call 推荐中的边界条件处理

### Technical Improvements
- 代码行数从 ~1500 行扩展至 2194 行，功能更完善
- 数据库表新增：`rv_history`、`option_snapshots`
- 改进错误处理与自动降级机制
- 新增 8 种流向标签体系：protective_hedge、premium_collect、speculative_put、call_momentum、covered_call、call_overwrite、call_speculative、unknown

---

## [2.0.0] - 2026-03-21

### Added
- **双币种支持**: 现在支持 BTC 和 ETH 期权分析
- **DVOL 信号增强**:
  - Z-Score 波动率分析
  - 趋势判断 (上涨/下跌/震荡)
  - 动态阈值 (基于 30 天历史自动调整)
  - 置信度计算
- **Sell Put 推荐增强**:
  - 流动性过滤 (bid-ask spread ≤10%)
  - 最低 open_interest 要求 (≥100)
  - 流动性评分 (0-100 分)
- **大宗异动监控增强**:
  - Call 期权大宗成交分析
  - 流向标签 (保护性对冲/Call追涨/收取权利金等)
  - 市场情绪判断
  - 重点合约解读
- **报告增强**:
  - 市场解读 (DVOL 变化分析)
  - 策略建议
  - 风险提示

### Fixed
- Instrument name 解析 Bug (支持单双位数日期)
- 大宗交易过滤丢失 Call 问题
- 百分位数计算逻辑
- 缓存 TTL 机制

### Changed
- 代码优化:
  - 添加 instrument 解析缓存
  - 添加缓存自动清理机制
  - 提取常量减少硬编码

---

## [1.0.0] - 2026-03-09

### Added
- DVOL 恐慌指数获取与分析
- Sell Put 收租推荐
- 大宗异动监控
- SQLite 数据存储
- CLI 命令行工具
