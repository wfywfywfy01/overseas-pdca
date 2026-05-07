"""Agent 3 · BehaviorGuard — banned words + 9-type anomaly detection."""

import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import append_memory, load_memory, report_path, yesterday_str

# ── Banned word definitions (seed list; MEMORY.md is authoritative) ───────────

BANNED_HIGH = [
    r"\byes\b", r"\bno\b", r"\bok\b",
    r"\bguaranteed\b", r"\bdefinitely\b", r"\bsure\b",
    r"\bcheap(est)?\b", r"\bdiscount\b",
]
BANNED_MID = [
    r"\bsorry\b.*\bsorry\b",  # repeated sorry
    r"\bASAP\b",
    r"\bno problem\b",
]


def load_banned_words_from_memory() -> tuple[list[str], list[str]]:
    """Parse confirmed banned words from behavior-guard/MEMORY.md."""
    mem = load_memory("behavior-guard")
    high, mid = list(BANNED_HIGH), list(BANNED_MID)
    in_confirmed = False
    for line in mem.splitlines():
        if "已确认禁用词" in line:
            in_confirmed = True
        elif "待主管确认" in line:
            in_confirmed = False
        if in_confirmed and line.startswith("- "):
            words_part = line.split("→")[0].replace("- ", "").strip()
            for w in words_part.split(","):
                w = w.strip().strip("'\"").rstrip("?")
                if not w:
                    continue
                pat = r"\b" + re.escape(w) + r"\b"
                if "[高" in line and pat not in high:
                    high.append(pat)
                elif "[中" in line and pat not in mid:
                    mid.append(pat)
    return high, mid


# ── Detectors ─────────────────────────────────────────────────────────────────

def scan_banned_words(seat_data: dict, high_patterns: list, mid_patterns: list) -> list[dict]:
    hits = []
    for customer_id, msgs in seat_data["conversations"].items():
        for m in msgs:
            if m["actionType"] != "send" or m.get("contentType", 0) != 0:
                continue
            text = str(m.get("content", "") or m.get("text", "") or m.get("msg", ""))
            if not text:
                continue
            for pat in high_patterns:
                if re.search(pat, text, re.IGNORECASE):
                    hits.append({
                        "risk": "高",
                        "type": "禁用词",
                        "word": pat,
                        "customer": customer_id,
                        "chatTime": m["chatTime"],
                        "excerpt": text[:120],
                    })
            for pat in mid_patterns:
                if re.search(pat, text, re.IGNORECASE):
                    hits.append({
                        "risk": "中",
                        "type": "禁用词",
                        "word": pat,
                        "customer": customer_id,
                        "chatTime": m["chatTime"],
                        "excerpt": text[:120],
                    })
    return hits


def detect_bombing(seat_data: dict, threshold: int = 5, window_ms: int = 3_600_000) -> list[dict]:
    """1h内对同一客户发>threshold条 → 集中轰炸."""
    alerts = []
    for customer_id, msgs in seat_data["conversations"].items():
        sent = sorted(
            [m for m in msgs if m["actionType"] == "send"],
            key=lambda x: x["chatTime"],
        )
        for i, m in enumerate(sent):
            window = [x for x in sent[i:] if x["chatTime"] - m["chatTime"] <= window_ms]
            if len(window) > threshold:
                alerts.append({
                    "risk": "高",
                    "type": "集中轰炸",
                    "customer": customer_id,
                    "count": len(window),
                    "window_start": m["chatTime"],
                })
                break
    return alerts


def detect_selective_reply(seat_data: dict, gap_ms: int = 1_800_000) -> list[dict]:
    """在回某客户的同时，有其他客户的消息超30分钟未回."""
    alerts = []
    convos = seat_data["conversations"]
    # For each customer with unanswered inbound, check if agent was active elsewhere
    for cid, msgs in convos.items():
        unanswered = [m for m in msgs if m["actionType"] == "receive"]
        if not unanswered:
            continue
        first_unread = unanswered[0]["chatTime"]
        replied = any(
            m["actionType"] == "send" and m["chatTime"] > first_unread
            for m in msgs
        )
        if replied:
            continue
        # Check if agent was sending to someone else during this gap
        for other_cid, other_msgs in convos.items():
            if other_cid == cid:
                continue
            active_elsewhere = any(
                m["actionType"] == "send"
                and first_unread <= m["chatTime"] <= first_unread + gap_ms
                for m in other_msgs
            )
            if active_elsewhere:
                alerts.append({
                    "risk": "高",
                    "type": "选择性回复",
                    "ignored_customer": cid,
                    "served_customer": other_cid,
                    "unanswered_since": first_unread,
                })
                break
    return alerts


def detect_high_intent_neglect(seat_data: dict, gap_ms: int = 7_200_000) -> list[dict]:
    """客户发出询价/比价消息后>2小时无回复."""
    intent_keywords = re.compile(
        r"(price|how much|cost|quote|interested|buy|purchase|order|want|need|looking for|比价|价格|多少钱|想买|要买)",
        re.IGNORECASE,
    )
    alerts = []
    for cid, msgs in seat_data["conversations"].items():
        for m in msgs:
            if m["actionType"] != "receive":
                continue
            text = str(m.get("content", "") or m.get("text", "") or "")
            if not intent_keywords.search(text):
                continue
            t_query = m["chatTime"]
            replied_in_time = any(
                x["actionType"] == "send" and t_query < x["chatTime"] <= t_query + gap_ms
                for x in msgs
            )
            if not replied_in_time:
                alerts.append({
                    "risk": "高",
                    "type": "高意向客户冷落",
                    "customer": cid,
                    "query_time": t_query,
                    "excerpt": text[:120],
                })
    return alerts


def detect_bulk_send(seat_data: dict, threshold: int = 10, window_ms: int = 300_000) -> list[dict]:
    """同5分钟窗口内对>=threshold个不同客户发送相同内容."""
    sent_msgs = [
        m for msgs in seat_data["conversations"].values()
        for m in msgs
        if m["actionType"] == "send" and m.get("contentType", 0) == 0
    ]
    sent_msgs.sort(key=lambda x: x["chatTime"])
    alerts = []
    for i, m in enumerate(sent_msgs):
        text = str(m.get("content", "") or m.get("text", "") or "")
        if not text or len(text) < 20:
            continue
        window = [
            x for x in sent_msgs[i:]
            if x["chatTime"] - m["chatTime"] <= window_ms
            and str(x.get("content", "") or x.get("text", "") or "")[:50] == text[:50]
        ]
        unique_customers = set(x.get("friendWhatsId", "") for x in window)
        if len(unique_customers) >= threshold:
            alerts.append({
                "risk": "高",
                "type": "批量群发嫌疑",
                "customer_count": len(unique_customers),
                "window_start": m["chatTime"],
                "excerpt": text[:80],
            })
    return alerts


def detect_zombie_followup(seat_data: dict, days: int = 3) -> list[dict]:
    """连续>=days天对同一客户发消息但零回复."""
    alerts = []
    for cid, msgs in seat_data["conversations"].items():
        sent_days = set(
            datetime.fromtimestamp(m["chatTime"] / 1000).date()
            for m in msgs if m["actionType"] == "send"
        )
        received_any = any(m["actionType"] == "receive" for m in msgs)
        if len(sent_days) >= days and not received_any:
            alerts.append({
                "risk": "中",
                "type": "僵尸跟进",
                "customer": cid,
                "days_sent": len(sent_days),
            })
    return alerts


def detect_zero_outreach(seat_data: dict) -> list[dict]:
    """工作日全天无任何主动发送消息."""
    has_any_send = any(
        m["actionType"] == "send" for m in seat_data.get("raw_messages", [])
    )
    if not has_any_send:
        return [{"risk": "中", "type": "全天零触达"}]
    return []


# ── Main ──────────────────────────────────────────────────────────────────────

def run_guard(data: dict) -> dict:
    date_str = data["date"]
    print(f"[BehaviorGuard] 开始异常检测 {date_str}")

    high_patterns, mid_patterns = load_banned_words_from_memory()
    results: dict[str, list] = {}
    all_high_alerts = []
    new_patterns: list[str] = []

    for account, seat_data in data["seats"].items():
        alerts: list[dict] = []

        alerts += scan_banned_words(seat_data, high_patterns, mid_patterns)
        alerts += detect_bombing(seat_data)
        alerts += detect_selective_reply(seat_data)
        alerts += detect_high_intent_neglect(seat_data)
        alerts += detect_bulk_send(seat_data)
        alerts += detect_zombie_followup(seat_data)
        alerts += detect_zero_outreach(seat_data)

        # Deduplicate per-seat alerts
        seen: set = set()
        deduped_alerts: list[dict] = []
        for a in alerts:
            k = (a.get("type"), a.get("customer"), a.get("chatTime"), a.get("window_start"))
            if k not in seen:
                seen.add(k)
                deduped_alerts.append(a)
        alerts = deduped_alerts

        for a in alerts:
            a["account"] = account
            if a["risk"] == "高":
                all_high_alerts.append(a)
                print(f"  ⚠️  [高风险] {account} | {a['type']} | 客户:{a.get('customer','?')}")

        results[account] = alerts

    # Deduplicate high alerts (same account+type+customer+time)
    seen_keys: set = set()
    deduped: list[dict] = []
    for a in all_high_alerts:
        k = (a.get("account"), a.get("type"), a.get("customer"), a.get("chatTime"), a.get("window_start"))
        if k not in seen_keys:
            seen_keys.add(k)
            deduped.append(a)
    all_high_alerts = deduped

    output = {
        "date": date_str,
        "seats": results,
        "high_alert_count": len(all_high_alerts),
        "high_alerts": all_high_alerts,
    }
    out = report_path(date_str, "guard_results.json")
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[BehaviorGuard] 高风险预警 {len(all_high_alerts)} 条，已保存 → {out}")

    if all_high_alerts:
        append_memory(
            "behavior-guard",
            "异常模式库",
            f"[{date_str}] 高风险预警 {len(all_high_alerts)} 条: "
            + "; ".join(f"{a['type']}({a['account']})" for a in all_high_alerts[:5]),
        )

    return output


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    date_str = date_arg or yesterday_str()
    data_path = report_path(date_str, "data.json")
    if not data_path.exists():
        print(f"[BehaviorGuard] data.json not found, run fetch.py first")
        sys.exit(1)
    data = json.loads(data_path.read_text(encoding="utf-8"))
    run_guard(data)
