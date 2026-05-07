# Agents — Orchestrator 调度协议

## 执行顺序（串行，后者依赖前者输出）
1. DataFetcher  → 输出 `reports/{date}/data.json`
2. SOPChecker   → 读取 data.json，输出 `sop_results.json`
3. BehaviorGuard → 读取 data.json，输出 `guard_results.json`
4. QualityScorer → 读取 data.json，输出 `score_results.json`
5. Orchestrator  → 读取全部输出，生成 `daily_report.md`

## 失败重试策略
- DataFetcher: 最多重试3次（已内置），失败则中止并告警
- SOPChecker / BehaviorGuard: 失败则跳过该模块，继续执行后续
- QualityScorer: API失败则跳过，报告中标注"AI评分不可用"

## 数据传递格式
- 所有中间数据以 JSON 文件存储在 `reports/{date}/`
- Agent 之间不直接传递对象，通过磁盘文件解耦

## 并行执行规则
- SOPChecker 和 BehaviorGuard 可并行执行（均只读 data.json）
- QualityScorer 必须在数据拉取完成后执行
- 当前实现：串行执行（简化版），后续可改为 asyncio 并行
