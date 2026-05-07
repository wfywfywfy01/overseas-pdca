# 海外PDCA — Overseas Private Domain & Dealer PDCA System

VERTU 海外部门 PDCA 管理系统 Monorepo

## 结构

```
overseas-pdca/
├── 私域PDCA/
│   └── qa-agents/        # WA 聊天质检 Agent 框架（当前模块）
├── 经销商PDCA/            # 经销商 PDCA 系统（待建设）
└── README.md
```

## 私域PDCA / qa-agents

基于 SalesEpoch WA 聊天记录的每日自动质检系统。

**核心模块：**
- `data-fetcher/` — 从 SalesEpoch API 拉取消息，以 WA 号为主键
- `sop-checker/` — 对照 SOP 硬指标计算触达数、首回时长、老客激活等
- `behavior-guard/` — 禁用词、集中轰炸、选择性回复等异常行为检测
- `quality-scorer/` — Hermes 本地 AI 话术质量评分
- `orchestrator/` — 流水线调度 + 自进化 MEMORY 更新
- `web/` — Flask 质检看板（ECharts 图表，3 标签页）

**运行：**
```bash
cd 私域PDCA/qa-agents
python orchestrator/run.py          # 跑昨日数据
python orchestrator/run.py 2026-05-07  # 指定日期
python web/server.py                # 启动看板 http://localhost:5001
```

**依赖：** Python 3.11+, Ollama (Nous Hermes 2), SalesEpoch API
