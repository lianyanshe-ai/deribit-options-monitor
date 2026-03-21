# Changelog

All notable changes to this project will be documented in this file.

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
