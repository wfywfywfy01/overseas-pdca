"""SOPChecker 自进化模块 — 由 Nous Hermes 对比历史均值，建议阈值校准。"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import append_memory, hermes_chat, load_memory

SYSTEM_PROMPT = """你是Vertu私域销售SOP指标分析专家，严格按JSON格式输出，不加任何多余文字。

输出格式：
{
  "threshold_adjustments": [
    {"metric": "指标名", "suggestion": "建议", "reason": "数据依据"}
  ],
  "top_seat": "今日表现最佳坐席名",
  "worst_issue": "最需改进的问题（一句话）",
  "summary": "整体评价（一句话）"
}"""


def run_evolution(sop_results: dict, date_str: str) -> dict:
    seats = sop_results.get("seats", {})
    active = {k: v for k, v in seats.items()
              if v.get("daily_sends", {}).get("count", 0) > 0}

    seat_stats = [
        {
            "account": account,
            "first_response_avg_s": s.get("first_response", {}).get("avg_seconds"),
            "under_30s_rate": s.get("first_response", {}).get("under_30s_rate"),
            "daily_sends": s.get("daily_sends", {}).get("count", 0),
            "old_customers": s.get("old_customer_activation", {}).get("count", 0),
            "sop_score": s.get("sop_score", 0),
        }
        for account, s in sorted(active.items(),
                                  key=lambda x: -x[1].get("sop_score", 0))
    ]

    history = _extract_history(load_memory("sop-checker"))

    user_content = f"""日期：{date_str}
活跃坐席：{len(active)}/{len(seats)}

各坐席SOP数据（按评分降序）：
{json.dumps(seat_stats, ensure_ascii=False, indent=2)}

历史均值记录：
{history}

当前SOP标准：首回≤30s, 私发≥30条, 老客激活15-30个

请分析数据，给出阈值调整建议和整体评价。"""

    print("[SOPChecker·进化] 调用 Nous Hermes 分析指标趋势...")
    raw = hermes_chat(SYSTEM_PROMPT, user_content, timeout=120)
    if not raw:
        return {}

    raw = re.sub(r"```(?:json)?", "", raw).strip()
    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        result = json.loads(m.group()) if m else {}
    except Exception:
        print(f"[SOPChecker·进化] JSON解析失败: {raw[:200]}")
        return {}

    if result.get("threshold_adjustments"):
        entries = "\n".join(
            f'- {t["metric"]}: {t["suggestion"]} | 原因: {t["reason"]}'
            for t in result["threshold_adjustments"]
        )
        append_memory("sop-checker", "阈值校准记录",
                      f"[{date_str}] Hermes建议（待主管确认）：\n{entries}")

    if result.get("summary"):
        print(f"[SOPChecker·进化] 摘要: {result['summary']}")

    return result


def _extract_history(memory: str) -> str:
    lines = [l for l in memory.splitlines()
             if "均值" in l or "2026-" in l or "私发" in l]
    return "\n".join(lines[-8:]) or "暂无历史记录"
