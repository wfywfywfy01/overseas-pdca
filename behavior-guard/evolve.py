"""BehaviorGuard 自进化模块 — 由 Nous Hermes 分析今日异常，提炼新规则。"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import append_memory, hermes_chat, load_memory

SYSTEM_PROMPT = """你是Vertu奢侈品销售质检规则专家，严格按JSON格式输出，不加任何多余文字。

输出格式：
{
  "new_banned_words": [
    {"word": "词语", "reason": "原因", "risk": "高或中"}
  ],
  "new_patterns": ["模式描述1"],
  "summary": "一句话总结"
}"""


def run_evolution(guard_results: dict, date_str: str) -> dict:
    # 统计各禁用词命中次数和坐席
    banned_hits: dict[str, dict] = {}
    for account, alerts in guard_results.get("seats", {}).items():
        for a in alerts:
            if a.get("type") != "禁用词":
                continue
            word = a.get("word", "").replace(r"\b", "").replace("\\", "")
            if not word:
                continue
            entry = banned_hits.setdefault(word, {"count": 0, "seats": set(), "examples": []})
            entry["count"] += 1
            entry["seats"].add(account)
            if len(entry["examples"]) < 2:
                entry["examples"].append(a.get("excerpt", "")[:60])

    # 统计异常行为类型
    anomaly_counts: dict[str, int] = {}
    for alerts in guard_results.get("seats", {}).values():
        for a in alerts:
            if a.get("risk") == "高" and a.get("type") != "禁用词":
                anomaly_counts[a["type"]] = anomaly_counts.get(a["type"], 0) + 1

    # 已在词库中的词
    current_memory = load_memory("behavior-guard")
    confirmed_line = ""
    for line in current_memory.splitlines():
        if "已确认禁用词" in line:
            confirmed_line = line
        elif line.startswith("- ") and "已确认" not in current_memory[:current_memory.find(line)]:
            pass

    user_content = f"""日期：{date_str}
高风险预警总数：{guard_results.get('high_alert_count', 0)}

今日禁用词命中统计（词→次数，坐席数，示例）：
{json.dumps(
    {w: {"次数": d["count"], "坐席数": len(d["seats"]), "示例": d["examples"]}
     for w, d in sorted(banned_hits.items(), key=lambda x: -x[1]["count"])[:8]},
    ensure_ascii=False
)}

今日其他高风险异常：
{json.dumps(anomaly_counts, ensure_ascii=False)}

已在词库中的禁用词：yes, no, ok, guaranteed, definitely, sure, cheap, cheapest, discount, ASAP, no problem

请分析：哪些词语应新增到禁用词库？有哪些值得关注的新异常模式？"""

    print("[BehaviorGuard·进化] 调用 Nous Hermes 分析异常模式...")
    raw = hermes_chat(SYSTEM_PROMPT, user_content, timeout=120)
    if not raw:
        return {}

    # 解析 JSON（兼容 markdown 代码块）
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    try:
        m = re.search(r"\{[\s\S]*\}", raw)
        result = json.loads(m.group()) if m else {}
    except Exception:
        print(f"[BehaviorGuard·进化] JSON解析失败，原始输出: {raw[:200]}")
        return {}

    # 标准化 new_banned_words（兼容字符串列表和对象列表）
    raw_words = result.get("new_banned_words", [])
    normalized = []
    for w in raw_words:
        if isinstance(w, str):
            normalized.append({"word": w, "reason": "频繁出现", "risk": "中"})
        elif isinstance(w, dict):
            normalized.append(w)
    result["new_banned_words"] = normalized

    # 过滤已在词库中的词
    already = {"yes", "no", "ok", "guaranteed", "definitely", "sure",
               "cheap", "cheapest", "discount", "asap", "no problem"}
    new_words = [w for w in normalized if w["word"].lower() not in already]

    if new_words:
        entries = "\n".join(
            f'- "{w["word"]}" [{w.get("risk","中")}风险] | {w.get("reason","")}'
            for w in new_words
        )
        append_memory("behavior-guard", "待主管确认（Agent新发现）",
                      f"[{date_str}] Hermes建议新增禁用词：\n{entries}")
        print(f"[BehaviorGuard·进化] 发现 {len(new_words)} 个禁用词候选 → 已写入MEMORY待确认")

    if result.get("new_patterns"):
        entries = "\n".join(f"- {p}" for p in result["new_patterns"])
        append_memory("behavior-guard", "近期新发现模式",
                      f"[{date_str}] Hermes发现：\n{entries}")

    if result.get("summary"):
        print(f"[BehaviorGuard·进化] 摘要: {result['summary']}")

    return result
