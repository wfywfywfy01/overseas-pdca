"""Agent 0 · Orchestrator — daily QA pipeline controller."""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from config import append_memory, bump_memory_version, hermes_available, report_path, yesterday_str

# Load sub-agents by inserting their dirs into path then importing
def _import_agent(subdir: str, module_file: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        module_file.replace(".py", ""),
        ROOT / subdir / module_file,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_fetcher  = _import_agent("data-fetcher",   "fetch.py")
_checker  = _import_agent("sop-checker",    "check.py")
_guard    = _import_agent("behavior-guard", "guard.py")
_scorer   = _import_agent("quality-scorer", "score.py")
_g_evolve = _import_agent("behavior-guard", "evolve.py")
_s_evolve = _import_agent("sop-checker",    "evolve.py")
_narrate  = _import_agent("orchestrator",   "narrate.py")

fetch_all         = _fetcher.fetch_all
run_sop_check     = _checker.run_sop_check
run_guard         = _guard.run_guard
run_scoring       = _scorer.run_scoring
guard_evolve      = _g_evolve.run_evolution
sop_evolve        = _s_evolve.run_evolution
generate_narrative = _narrate.generate_narrative

GRADE_EMOJI = {"优秀": "🟢", "良好": "🔵", "及格": "🟡", "待改进": "🔴", "N/A": "⚪"}


def seat_display(key: str, seat_data: dict) -> str:
    """Return display name: 中文名(WA账号) if available, else key."""
    name = seat_data.get("name", key) if isinstance(seat_data, dict) else key
    wa_names = seat_data.get("wa_names", []) if isinstance(seat_data, dict) else []
    wa = wa_names[0] if wa_names else ""
    if name and name != key:
        return f"{name}({wa})" if wa else name
    return key


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def score_to_grade(score: float) -> str:
    if score >= 90: return "优秀"
    if score >= 75: return "良好"
    if score >= 60: return "及格"
    return "需关注"


def generate_report(date_str: str, sop: dict, guard: dict, scores: dict,
                    narrative: str = "") -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Vertu 私域质检日报 — {date_str}",
        f"生成时间：{now}  |  CONFIDENTIAL · 仅供内部使用",
        "",
    ]
    if narrative:
        lines += [
            "## 🧠 管理摘要（Nous Hermes）",
            "",
            narrative,
            "",
        ]
    lines += [
        "---",
        "",
        "## 一、SOP硬指标汇总",
        "",
        "| 坐席 | 首回时长 | 私发数 | 老客激活 | 三方私信 | WA动态 | SOP分 |",
        "|------|---------|-------|---------|---------|-------|------|",
    ]

    # 从 data.json 取坐席元数据（用于显示中文名）
    seat_meta = {}
    data_path = report_path(date_str, "data.json")
    if data_path.exists():
        raw_data = json.loads(data_path.read_text(encoding="utf-8"))
        seat_meta = raw_data.get("seats", {})

    seat_rows = []
    for account, s in sop.get("seats", {}).items():
        fr = s.get("first_response", {})
        ds = s.get("daily_sends", {})
        oc = s.get("old_customer_activation", {})
        tp = s.get("third_party_dms", {})
        wa = s.get("wa_dynamics", {})
        sop_score = s.get("sop_score", 0)

        label = seat_display(account, seat_meta.get(account, {}))
        avg_s = fr.get("avg_seconds")
        fr_str = f"{avg_s:.0f}s {GRADE_EMOJI.get(fr.get('grade',''), '')}" if avg_s else "N/A"
        seat_rows.append((sop_score, label, fr_str, ds, oc, tp, wa))

    seat_rows.sort(key=lambda x: -x[0])  # sort by SOP score desc
    for sop_score, account, fr_str, ds, oc, tp, wa in seat_rows:
        lines.append(
            f"| {account} | {fr_str} | "
            f"{ds.get('count',0)} {GRADE_EMOJI.get(ds.get('grade',''),'')} | "
            f"{oc.get('count',0)} {GRADE_EMOJI.get(oc.get('grade',''),'')} | "
            f"{tp.get('count',0)} {GRADE_EMOJI.get(tp.get('grade',''),'')} | "
            f"{'✅' if wa.get('reached') else '❌'}{wa.get('count',0)} | "
            f"**{sop_score}** |"
        )

    lines += [
        "",
        "---",
        "",
        "## 二、异常行为预警",
        "",
    ]

    high_alerts = guard.get("high_alerts", [])
    if high_alerts:
        lines.append(f"### 🚨 高风险预警（{len(high_alerts)} 条）")
        for a in high_alerts:
            ts_ms = a.get("window_start") or a.get("query_time") or a.get("unanswered_since") or a.get("chatTime")
            ts = datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M") if ts_ms else "?"
            acct = a.get("account", "?")
            label = seat_display(acct, seat_meta.get(acct, {}))
            lines.append(
                f"- **{label}** | {a['type']} | "
                f"客户:{str(a.get('customer', a.get('ignored_customer','?')))[-8:]}*** | "
                f"时间:{ts}"
            )
    else:
        lines.append("✅ 今日无高风险预警")

    # Medium risks by seat
    medium_counts: dict[str, int] = {}
    for account, alerts in guard.get("seats", {}).items():
        mid = [a for a in alerts if a.get("risk") == "中"]
        if mid:
            medium_counts[account] = len(mid)
    if medium_counts:
        lines += ["", "### 🟡 中风险行为（汇总）"]
        for account, cnt in sorted(medium_counts.items(), key=lambda x: -x[1]):
            label = seat_display(account, seat_meta.get(account, {}))
            lines.append(f"- {label}: {cnt} 项")

    lines += [
        "",
        "---",
        "",
        "## 三、AI话术评分",
        "",
        "| 坐席 | 专业度 | 品牌调性 | 转化引导 | 综合分 | 等级 |",
        "|------|-------|---------|---------|-------|-----|",
    ]

    for account, seat_scores in scores.get("seats", {}).items():
        if not seat_scores:
            continue
        label = seat_display(account, seat_meta.get(account, {}))
        avg_pro = sum(s.get("professionalism_score", 0) for s in seat_scores) / len(seat_scores)
        avg_brand = sum(s.get("brand_tone_score", 0) for s in seat_scores) / len(seat_scores)
        avg_conv = sum(s.get("conversion_score", 0) for s in seat_scores) / len(seat_scores)
        avg_overall = sum(s.get("overall_score", 0) for s in seat_scores) / len(seat_scores)
        grade = score_to_grade(avg_overall)
        lines.append(
            f"| {label} | {avg_pro:.0f} | {avg_brand:.0f} | {avg_conv:.0f} | "
            f"**{avg_overall:.1f}** | {GRADE_EMOJI.get(grade,'')} {grade} |"
        )

    gold = scores.get("gold_case_count", 0)
    problem = scores.get("problem_case_count", 0)
    if gold or problem:
        lines += [
            "",
            f"🥇 金牌案例 **{gold}** 个  |  ⚠️ 问题案例 **{problem}** 个  ← 详见 score_results.json",
        ]

    lines += [
        "",
        "---",
        "",
        "## 四、自进化更新提示",
        "",
        "本次运行完成后，以下 MEMORY.md 已自动更新：",
        "- `data-fetcher/MEMORY.md`：坐席列表 + API异常记录",
        "- `sop-checker/MEMORY.md`：各指标均值 + Hermes阈值建议",
        "- `behavior-guard/MEMORY.md`：高风险异常模式 + Hermes新禁用词候选",
        "- `quality-scorer/MEMORY.md`：金牌/问题案例 + 新发现",
        "",
        "**待主管审批**：请查看各 Agent 的 MEMORY.md 末尾「待主管确认」区域，回复\"确认\"即可生效。",
        "",
        "---",
        f"*Vertu 私域质检 · 自进化Agent框架 V2.1 (Nous Hermes) · {now}*",
    ]

    return "\n".join(lines)


def run(date_str: str | None = None):
    date_str = date_str or yesterday_str()
    start_t = time.time()
    print(f"\n{'='*60}")
    print(f"[Orchestrator] 启动质检流水线 — {date_str}")
    print(f"{'='*60}\n")

    errors = []

    # Step 1: Fetch data
    try:
        data = fetch_all(date_str)
    except Exception as e:
        print(f"[Orchestrator] ❌ DataFetcher 失败: {e}")
        append_memory("orchestrator", "系统异常记录", f"[{date_str}] DataFetcher | {e} | 未修复")
        return

    # Step 2: SOP Check
    sop_results = {}
    try:
        sop_results = run_sop_check(data)
    except Exception as e:
        print(f"[Orchestrator] ⚠️  SOPChecker 失败，跳过: {e}")
        errors.append(f"SOPChecker: {e}")

    # Step 3: Behavior Guard
    guard_results = {}
    try:
        guard_results = run_guard(data)
    except Exception as e:
        print(f"[Orchestrator] ⚠️  BehaviorGuard 失败，跳过: {e}")
        errors.append(f"BehaviorGuard: {e}")

    # Step 4: Quality Scoring
    score_results = {}
    try:
        score_results = run_scoring(data)
    except Exception as e:
        print(f"[Orchestrator] ⚠️  QualityScorer 失败，跳过: {e}")
        errors.append(f"QualityScorer: {e}")

    # Step 5: Hermes 自进化（有本地模型才跑）
    hermes_on = hermes_available()
    narrative = ""
    if hermes_on:
        print(f"\n[Orchestrator] 🧠 Nous Hermes 在线，开始自进化分析...")
        if guard_results:
            try:
                guard_evolve(guard_results, date_str)
            except Exception as e:
                print(f"[Orchestrator] ⚠️  BehaviorGuard进化失败: {e}")
        if sop_results:
            try:
                sop_evolve(sop_results, date_str)
            except Exception as e:
                print(f"[Orchestrator] ⚠️  SOPChecker进化失败: {e}")
        try:
            narrative = generate_narrative(sop_results, guard_results, score_results, date_str)
        except Exception as e:
            print(f"[Orchestrator] ⚠️  叙述生成失败: {e}")
    else:
        print(f"\n[Orchestrator] ⚪ Nous Hermes 未就绪，跳过自进化（仅规则更新）")

    # Step 7: Generate report
    report_md = generate_report(date_str, sop_results, guard_results, score_results, narrative)
    report_file = report_path(date_str, "daily_report.md")
    report_file.write_text(report_md, encoding="utf-8")

    elapsed = round(time.time() - start_t, 1)
    print(f"\n{'='*60}")
    print(f"[Orchestrator] ✅ 完成 — 耗时 {elapsed}s")
    print(f"[Orchestrator] 报告路径: {report_file}")
    if errors:
        print(f"[Orchestrator] ⚠️  跳过模块: {'; '.join(errors)}")
    print(f"{'='*60}\n")

    # Update orchestrator memory
    seat_count = data.get("seat_count", 0)
    high_alerts = guard_results.get("high_alert_count", 0)
    append_memory(
        "orchestrator",
        "历史运行记录",
        f"[{date_str}] 耗时{elapsed}s | {seat_count}席 | 高风险{high_alerts}条",
    )
    bump_memory_version("orchestrator")

    print(report_md)
    return report_md


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(date_arg)
