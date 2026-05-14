"""Agent 4 · QualityScorer — AI conversation scoring via DeepSeek-V3."""

import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import append_memory, hermes_chat, load_memory, report_path, yesterday_str

SYSTEM_PROMPT_TEMPLATE = """你是 Vertu 奢侈品牌的私域销售话术质检专家（AI版）。
Vertu 是全球顶级奢侈手机品牌，客单价极高，客户为高净值人群。

【Vertu 品牌沟通标准】
· 语气：专业优雅，不卑不亢，不过度推销，保持品牌的稀缺感与尊贵感
· 语言：英文为主，语法正确，首字母大写，无拼写错误，措辞精准
· 禁用：yes/No/OK → 改用 "Yes dear," + 重复客户原话
· 主动：引导客户留WhatsApp和邮箱，但不强迫
· 弃单处理：客户弃单后30分钟内跟进（WA优先，无WA则电话/邮件）
· S/A类客户（高意向）：核心监控，需确保响应时效和跟进逻辑正确

【本次评分额外参考】
{MEMORY_CONTEXT}

【返回格式】严格返回JSON，不含其他任何文字：
{{
  "professionalism_score": 0-100,
  "brand_tone_score": 0-100,
  "conversion_score": 0-100,
  "overall_score": 0-100,
  "banned_words_found": ["词1"],
  "issues": ["问题描述1"],
  "highlights": ["亮点描述1"],
  "suggestion": "最重要改进建议（英文）",
  "is_gold_case": true或false,
  "is_problem_case": true或false,
  "memory_update": "建议写入MEMORY.md的内容（如有新发现，否则为空字符串）"
}}"""

WEIGHTS = {"professionalism": 0.35, "brand_tone": 0.30, "conversion": 0.35}


def load_memory_context() -> str:
    """Extract gold cases and recent calibration from MEMORY.md for prompt injection."""
    mem = load_memory("quality-scorer")
    lines = mem.splitlines()
    relevant = []
    capture = False
    for line in lines:
        if "金牌案例库" in line or "评分权重" in line or "待主管确认" in line:
            capture = True
        elif line.startswith("## 【") and capture:
            capture = False
        if capture:
            relevant.append(line)
    return "\n".join(relevant[:40])  # cap at 40 lines to keep prompt concise


def format_conversation(customer_id: str, msgs: list[dict]) -> str:
    """Format a conversation for the scoring prompt."""
    lines = [f"=== 对话 (客户: {customer_id[-6:]}***) ==="]
    for m in msgs[-30:]:  # last 30 messages max
        role = "Sales" if m["actionType"] == "send" else "Customer"
        text = str(m.get("content", "") or m.get("text", "") or m.get("msg", ""))
        if not text or m.get("contentType", 0) != 0:
            continue
        ts = m["chatTime"] // 1000
        lines.append(f"[{role}] {text[:300]}")
    return "\n".join(lines)


def score_conversation(customer_id: str, msgs: list[dict]) -> dict | None:
    """Score one conversation. Returns None if skippable (no text content)."""
    text_msgs = [m for m in msgs if m.get("contentType", 0) == 0]
    outbound = [m for m in text_msgs if m["actionType"] == "send"]
    if not outbound:
        return None  # nothing to score

    memory_context = load_memory_context()
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(MEMORY_CONTEXT=memory_context)
    user_content = format_conversation(customer_id, msgs)

    try:
        raw = hermes_chat(system_prompt, user_content, timeout=120)
        if not raw:
            return {"error": "Hermes returned empty response"}
        raw = re.sub(r"```(?:json)?", "", raw).strip()
        json_match = re.search(r"\{[\s\S]*\}", raw)
        if not json_match:
            return {"error": "no JSON in response", "raw": raw[:200]}
        result = json.loads(json_match.group())
        # Recalculate overall score with weights
        if all(k in result for k in ("professionalism_score", "brand_tone_score", "conversion_score")):
            result["overall_score"] = round(
                result["professionalism_score"] * WEIGHTS["professionalism"]
                + result["brand_tone_score"] * WEIGHTS["brand_tone"]
                + result["conversion_score"] * WEIGHTS["conversion"],
                1,
            )
        result["customer_id"] = customer_id[-6:] + "***"
        return result
    except Exception as e:
        return {"error": str(e)}


def select_conversations(seat_data: dict, max_per_seat: int = 5) -> list[tuple[str, list]]:
    """Select conversations to score: prioritize high-intent signals, cap at max_per_seat."""
    convos = list(seat_data["conversations"].items())

    # Score conversations that have any inbound message mentioning price/interest
    intent_re = re.compile(
        r"(price|how much|cost|interested|want|buy|order|比价|价格|想买)", re.IGNORECASE
    )
    priority = []
    others = []
    for cid, msgs in convos:
        texts = " ".join(
            str(m.get("content", "") or m.get("text", "") or "")
            for m in msgs if m.get("contentType", 0) == 0
        )
        if intent_re.search(texts):
            priority.append((cid, msgs))
        else:
            others.append((cid, msgs))

    selected = priority[:max_per_seat]
    if len(selected) < max_per_seat:
        selected += random.sample(others, min(max_per_seat - len(selected), len(others)))
    return selected


def run_scoring(data: dict) -> dict:
    date_str = data["date"]
    print(f"[QualityScorer] 开始AI话术评分 {date_str}")

    results: dict[str, list] = {}
    gold_cases: list[dict] = []
    problem_cases: list[dict] = []
    memory_updates: list[str] = []

    for account, seat_data in data["seats"].items():
        seat_scores = []
        selected = select_conversations(seat_data, max_per_seat=5)
        print(f"  [{account}] 抽取 {len(selected)} 段对话评分")

        for cid, msgs in selected:
            score = score_conversation(cid, msgs)
            if not score:
                continue
            if "error" in score:
                print(f"    客户{cid[-6:]}***: ❌ 评分失败 — {score['error']}")
                continue
            score["account"] = account
            seat_scores.append(score)
            overall = score.get("overall_score", 0)
            flag = "🥇" if score.get("is_gold_case") else ("⚠️" if score.get("is_problem_case") else "")
            print(f"    客户{score.get('customer_id','?')}: {overall}分 {flag}")

            if score.get("is_gold_case"):
                gold_cases.append(score)
            if score.get("is_problem_case"):
                problem_cases.append(score)
            if score.get("memory_update"):
                memory_updates.append(f"[{account}] {score['memory_update']}")

        results[account] = seat_scores

    output = {
        "date": date_str,
        "seats": results,
        "gold_case_count": len(gold_cases),
        "problem_case_count": len(problem_cases),
    }
    out = report_path(date_str, "score_results.json")
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[QualityScorer] 金牌案例 {len(gold_cases)} | 问题案例 {len(problem_cases)} | 已保存 → {out}")

    _update_scoring_memory(date_str, gold_cases, memory_updates)
    return output


def _update_scoring_memory(
    date_str: str, gold_cases: list[dict], memory_updates: list[str],
) -> None:
    """Persist gold cases and new findings to MEMORY.md."""
    if gold_cases:
        for c in gold_cases:
            append_memory(
                "quality-scorer",
                "金牌案例库",
                f"[{date_str}] 坐席:{c.get('account')} | 综合{c.get('overall_score')}分\n"
                f"亮点: {';'.join(c.get('highlights', []))}\n→ 待主管确认收录",
            )
    if memory_updates:
        append_memory(
            "quality-scorer",
            "待主管确认的新发现",
            "\n".join(memory_updates),
        )


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    date_str = date_arg or yesterday_str()
    data_path = report_path(date_str, "data.json")
    if not data_path.exists():
        print(f"[QualityScorer] data.json not found, run fetch.py first")
        sys.exit(1)
    data = json.loads(data_path.read_text(encoding="utf-8"))
    run_scoring(data)
