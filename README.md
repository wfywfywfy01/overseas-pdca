# 海外私域 PDCA 运营控制台

三层级运营管理看板：总监 → 主管 → 员工，纯静态 HTML/CSS/JS，无需构建，GitHub Pages 直接部署。

**Live Demo**: https://wfywfywfy01.github.io/overseas-pdca/pdca-login.html

---

## 架构

```
pdca-login.html      登录页（写入 localStorage session）
pdca-director.html   总监视图  — 全局数据 + 下钻到主管组
pdca-manager.html    主管视图  — 组内成员 + 下钻到员工个人
pdca-day.html        员工视图  — 个人 PDCA + 日/月/年维度
```

### 数据流

```
静态 ALL_USERS / MEMBERS / MONTH_MEMBERS 对象
        ↓ (fallback，GitHub Pages 无 API 时)
uid 参数路由  →  ME = ALL_USERS[uid]  →  KPI / 头部渲染
        ↓ (有 API 时覆盖)
pdca_data.json  →  loadDayData / loadMonthData / loadYearData
```

- URL 参数 `?uid=XXXX` 优先于 session，用于主管下钻到具体员工
- 无 session 且无 uid 时默认演示账号（12849），不跳转登录页
- 所有金额口径对齐：成员月度数据之和 = 主管页组合计（42.3万）

### 下钻链接

| 层级 | 触发 | 目标 |
|------|------|------|
| 总监 → 主管 | 点击成员组卡片 | `pdca-manager.html?team=XXXX` |
| 主管 → 员工（日视图） | 点击成员卡片 / 月进度条 | `pdca-day.html?uid=XXXX` |
| 主管 → 员工（年视图） | 点击年表行 | `pdca-day.html?uid=XXXX` |

下钻通过 `openDrill(url, title)` 在右侧滑出抽屉中以 iframe 加载，不离开当前页面。

### 时间维度切换

每个页面内部通过 `switchTime(period, el)` + `showSec(id, el)` 双层导航：
- 外层：日 / 月 / 年 tab 切换
- 内层：各维度下的功能分区（SOP / KPI / 成员 / 异常等）

---

## 设计思路

### 三层 PDCA 闭环

```
总监   Plan ── 设定 OKR / 月度分配
主管   Do   ── 日常调度 / 异常监控
员工   Check── 日 SOP 打卡 / 数据复盘
       Act  ── AI 日报打分 / 主管介入
```

每一层看板只暴露该角色需要的信息粒度，避免信息过载：
- 总监看"组 KPI + 趋势 + 预测"
- 主管看"成员日进度 + 异常预警"
- 员工看"自己的 SOP / 客户 / 业绩"

### 状态指示设计

- 绿色 `ok`：≥80% 完成率
- 橙色 `warn`：40–79%
- 红色 `behind`：<40%
- 红色警示徽章：当日未达标成员（warn:true）

---

## Claude / AI 集成

### 日报 AI 打分（员工页 → 跟单日报）

员工填写当日日报后，系统调用 Claude API 对日报内容打分（0–100）并给出改进建议：

```javascript
// pdca-day.html — submitReport()
async function submitReport() {
  const content = document.getElementById('report-text').value;
  // 调用后端 /api/score-report，由后端转发 Claude claude-opus-4-7
  const res = await fetch('/api/score-report', {
    method: 'POST',
    body: JSON.stringify({ uid: CURRENT_USER_ID, content })
  });
  const { score, feedback } = await res.json();
  // 渲染评分 + 反馈
}
```

当前演示为 mock 数据（setTimeout 模拟延迟），接入真实 API 只需实现 `/api/score-report` 端点。

### AI 主管分析（主管页 → 日 SOP → Hermes 分析）

基于当日成员数据，Claude 生成团队运营建议：
- 识别异常成员 + 归因
- 预测月末完成率
- 给出干预优先级

```
输入：MEMBERS 数组（today / target / score / bars）
输出：自然语言运营建议 + 行动项
```

演示页已内置静态分析文本，生产环境替换为实时 Claude 调用。

### 数据查询（Vertu CLI + Odoo 集成）

运营数据从 Odoo ERP 通过 `vertu odoo data search` 拉取，写入 `pdca_data.json` 供前端消费：

```bash
vertu odoo data search sale.order \
  --domain "user_id = @me AND state in ['sale','done']" \
  --fields '["name","amount_total","date_order"]' \
  --group-by user_id \
  --output pdca_data.json
```

---

## 本地运行

```bash
# 任意静态服务器即可
npx serve .
# 或
python3 -m http.server 8080
```

访问 `http://localhost:8080/pdca-login.html`，账号密码任意（演示模式）。

## 文件说明

| 文件 | 说明 |
|------|------|
| `pdca-login.html` | 登录页，写入 `localStorage.pdca_session` |
| `pdca-director.html` | 总监视图 |
| `pdca-manager.html` | 主管视图，含成员下钻 |
| `pdca-day.html` | 员工视图，uid 路由，ALL_USERS 静态数据 |

---

## 原有模块

本仓库同时托管 `私域PDCA/qa-agents/` — 基于 SalesEpoch WA 聊天记录的每日自动质检系统（Python + Ollama）。看板演示文件与质检后端相互独立。
