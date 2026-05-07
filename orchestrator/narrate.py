"""Orchestrator 叙述模块 — 由 Nous Hermes 生成有洞察的日报摘要段落。"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import hermes_chat

SYSTEM_PROMPT = """你是Vertu英国海外事业部私域销售质检主管助理。
根据今日数据，直接输出管理摘要正文（纯文字，不加标题，不超过150字）：
- 第一句：点出最严重问题，点名坐席和数字
- 第二句：肯定表现最好的坐席（如有）
- 第三句：给出今日最重要的1条行动建议
语气直接专业，不废话。"""


def generate_narrative(sop_results: dict, guard_results: dict,
                       score_results: dict, date_str: str) -> str:
    """Generate an AI narrative summary for the daily report."""

    # 找今日最差/最好坐席
    sop_seats = sop_results.get("seats", {})
    active = {k: v for k, v in sop_seats.items() if v.get("daily_sends", {}).get("count", 0) > 0}
    top = max(active.items(), key=lambda x: x[1].get("sop_score", 0)) if active else None
    bottom = min(active.items(), key=lambda x: x[1].get("sop_score", 0)) if active else None

    # AI评分最差
    score_seats = score_results.get("seats", {})
    worst_ai = None
    worst_score = 999
    for account, scores in score_seats.items():
        if scores:
            avg = sum(s.get("overall_score", 0) for s in scores) / len(scores)
            if avg < worst_score:
                worst_score = avg
                worst_ai = (account, avg)

    high_alerts = guard_results.get("high_alert_count", 0)
    problem_cases = score_results.get("problem_case_count", 0)

    # 高风险预警按坐席汇总
    alert_by_seat: dict[str, list[str]] = {}
    for a in guard_results.get("high_alerts", [])[:20]:
        seat = a.get("account", "?")
        alert_by_seat.setdefault(seat, []).append(a.get("type", ""))

    user_content = f"""日期：{date_str}

关键数据：
- 活跃坐席：{len(active)}/33
- 高风险预警：{high_alerts} 条
- AI问题案例：{problem_cases} 个
- SOP最高分坐席：{top[0] if top else 'N/A'}（{top[1].get('sop_score',0) if top else 0}分，私发{top[1].get('daily_sends',{}).get('count',0) if top else 0}条）
- SOP最低分活跃坐席：{bottom[0] if bottom else 'N/A'}（{bottom[1].get('sop_score',0) if bottom else 0}分）
- AI话术最低分：{worst_ai[0] if worst_ai else 'N/A'}（综合{worst_ai[1]:.1f}分 if worst_ai else 0）

高风险预警按坐席：
{json.dumps({k: list(set(v)) for k, v in alert_by_seat.items()}, ensure_ascii=False)}

请生成管理摘要段落。"""

    narrative = hermes_chat(SYSTEM_PROMPT, user_content, timeout=90)
    return narrative.strip() if narrative else ""
