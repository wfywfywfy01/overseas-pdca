"""Agent 2 · SOPChecker — calculate 6 SOP KPI metrics."""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import append_memory, report_path, yesterday_str

# ── Grading thresholds — aligned with SOP (海外私域+SOP梳理 2026-05-07) ───────
THRESHOLDS = {
    "first_response_s": {"excellent": 30, "good": 180, "pass": 600},
    # 触达总数：目标50个/日（唯一客户）
    "daily_sends":      {"excellent": 50, "good": 40, "pass": 30},
    # 老客触达：50个中分配20个/日
    "old_customer":     {"excellent": 25, "good": 20, "pass": 15},
    # 三方私信：≥40条/日
    "third_party_dms":  {"excellent": 50, "good": 40, "pass": 30},
    "wa_dynamics":      {"target": 3},
    # 弃单联系时效：15分钟内（SOP 2026-05-07）
    "abandoned_response_min": {"max": 15},
    "abandoned_days":   {"max": 15},
}


def grade_metric(value: float, key: str) -> str:
    t = THRESHOLDS.get(key, {})
    if key == "first_response_s":
        if value <= t["excellent"]:  return "优秀"
        if value <= t["good"]:       return "良好"
        if value <= t["pass"]:       return "及格"
        return "待改进"
    if key == "daily_sends":
        if value >= t["excellent"]:  return "优秀"
        if value >= t["good"]:       return "良好"
        if value >= t["pass"]:       return "及格"
        return "待改进"
    if key == "old_customer":
        if value >= t["excellent"]:  return "优秀"
        if value >= t["good"]:       return "良好"
        if value >= t["pass"]:       return "及格"
        return "待改进"
    if key == "third_party_dms":
        if value >= t["excellent"]:  return "优秀"
        if value >= t["good"]:       return "良好"
        if value >= t["pass"]:       return "及格"
        return "待改进"
    return "N/A"


# ── Metric calculators ────────────────────────────────────────────────────────

WORK_HOUR_START = 9   # 北京时间工作开始
WORK_HOUR_END   = 18  # 北京时间工作结束


def calc_first_response(seat_data: dict) -> dict:
    """Average first-response time for conversations where customer's first message
    arrived during business hours (09:00–18:00). Overnight messages are excluded
    because agents cannot reasonably respond in 30s to 3am messages.
    """
    times = []
    skipped_offhours = 0
    for customer_id, msgs in seat_data["conversations"].items():
        first_in = next((m for m in msgs if m["actionType"] == "receive"), None)
        if not first_in:
            continue
        # Only count messages received during business hours
        recv_hour = datetime.fromtimestamp(first_in["chatTime"] / 1000).hour
        if not (WORK_HOUR_START <= recv_hour < WORK_HOUR_END):
            skipped_offhours += 1
            continue
        t_in = first_in["chatTime"]
        first_out = next(
            (m for m in msgs if m["actionType"] == "send" and m["chatTime"] > t_in),
            None,
        )
        if first_out:
            delta_s = (first_out["chatTime"] - t_in) / 1000
            times.append(delta_s)

    if not times:
        return {
            "avg_seconds": None, "under_30s_count": 0, "total_convos": 0,
            "under_30s_rate": None, "skipped_offhours": skipped_offhours, "grade": "N/A",
        }

    avg = sum(times) / len(times)
    under_30 = sum(1 for t in times if t <= 30)
    rate = under_30 / len(times)
    return {
        "avg_seconds": round(avg, 1),
        "under_30s_count": under_30,
        "total_convos": len(times),
        "under_30s_rate": round(rate, 3),
        "skipped_offhours": skipped_offhours,
        "grade": grade_metric(avg, "first_response_s"),
    }


def calc_daily_sends(seat_data: dict) -> dict:
    """Count unique customers contacted today (触达总数, SOP target: 50/day)."""
    count = sum(
        1
        for customer_id, msgs in seat_data["conversations"].items()
        if any(m["actionType"] in ("send", 1) for m in msgs)
    )
    return {"count": count, "grade": grade_metric(count, "daily_sends")}


def calc_old_customer_activation(seat_data: dict, known_customers: set) -> dict:
    """Count unique existing customers contacted today."""
    contacted_today = set(seat_data["conversations"].keys())
    old_activated = contacted_today & known_customers
    count = len(old_activated)
    return {
        "count": count,
        "activated_customers": list(old_activated),
        "grade": grade_metric(count, "old_customer"),
    }


def calc_third_party_dms(seat_data: dict) -> dict:
    """Count messages that came from a third-party channel source."""
    count = sum(
        1
        for m in seat_data["raw_messages"]
        if m.get("channelSource") or m.get("sourceType") not in (None, "", "whatsapp", "wa")
    )
    # Fallback: if API doesn't tag channel, count messages with explicit thirdParty flag
    if count == 0:
        count = sum(1 for m in seat_data["raw_messages"] if m.get("thirdParty") or m.get("isThirdParty"))
    return {"count": count, "grade": grade_metric(count, "third_party_dms")}


def calc_wa_dynamics(seat_data: dict) -> dict:
    """Count WA broadcast/status messages sent today."""
    count = sum(
        1
        for m in seat_data["raw_messages"]
        if m.get("messageType") in ("broadcast", "status", "group_broadcast")
        and m["actionType"] == "send"
    )
    return {"count": count, "target": THRESHOLDS["wa_dynamics"]["target"], "reached": count >= THRESHOLDS["wa_dynamics"]["target"]}


def calc_abandoned_orders(seat_data: dict) -> dict:
    """
    Check customers tagged as 'abandoned order' who haven't been contacted in 15+ days.
    Uses customer stage/tag fields from message metadata.
    """
    overdue = []
    today_ms = datetime.now().timestamp() * 1000
    max_gap_ms = THRESHOLDS["abandoned_days"]["max"] * 86400 * 1000

    for customer_id, msgs in seat_data["conversations"].items():
        tags = msgs[0].get("customerTags", []) or msgs[0].get("tags", [])
        stage = msgs[0].get("customerStage", "") or msgs[0].get("stage", "")
        is_abandoned = any(
            t in str(tag).lower() for tag in tags for t in ("abandon", "弃单", "abandoned")
        ) or "弃单" in str(stage) or "abandon" in str(stage).lower()
        if not is_abandoned:
            continue
        last_contact_ms = max(m["chatTime"] for m in msgs)
        gap_ms = today_ms - last_contact_ms
        if gap_ms > max_gap_ms:
            overdue.append({
                "customer": customer_id,
                "days_since_contact": round(gap_ms / 86400000, 1),
            })

    return {"overdue_count": len(overdue), "overdue_customers": overdue}


# ── Main ──────────────────────────────────────────────────────────────────────

def run_sop_check(data: dict) -> dict:
    date_str = data["date"]
    print(f"[SOPChecker] 开始检查 {date_str} SOP指标")

    # Build known-customer set from MEMORY (simplified: use all customers seen across all seats)
    all_customers: set[str] = set()
    for seat_data in data["seats"].values():
        all_customers.update(seat_data["conversations"].keys())
    # A customer is "old" if they appear in more than one seat or are in memory
    # Simplified: treat any customer with prior messages as old (production: compare with historical DB)

    results: dict[str, dict] = {}
    seat_summaries = []

    for account, seat_data in data["seats"].items():
        r: dict = {
            "account": account,
            "first_response": calc_first_response(seat_data),
            "daily_sends": calc_daily_sends(seat_data),
            "old_customer_activation": calc_old_customer_activation(seat_data, all_customers),
            "third_party_dms": calc_third_party_dms(seat_data),
            "wa_dynamics": calc_wa_dynamics(seat_data),
            "abandoned_orders": calc_abandoned_orders(seat_data),
        }

        # Composite SOP score (each metric contributes equally)
        grades = [
            r["first_response"]["grade"],
            r["daily_sends"]["grade"],
            r["old_customer_activation"]["grade"],
            r["third_party_dms"]["grade"],
        ]
        grade_map = {"优秀": 4, "良好": 3, "及格": 2, "待改进": 1, "N/A": 0}
        scored = [grade_map[g] for g in grades if g != "N/A"]
        r["sop_score"] = round(sum(scored) / len(scored) * 25, 1) if scored else 0

        results[account] = r
        seat_summaries.append(
            f"  {account}: 回复{r['first_response'].get('avg_seconds','?')}s "
            f"| 私发{r['daily_sends']['count']} "
            f"| 老客{r['old_customer_activation']['count']} "
            f"| SOP分{r['sop_score']}"
        )

    print("[SOPChecker] 结果:\n" + "\n".join(seat_summaries))

    output = {"date": date_str, "seats": results}
    out = report_path(date_str, "sop_results.json")
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SOPChecker] 已保存 → {out}")

    # Update MEMORY with today's averages
    if results:
        avg_sends = sum(v["daily_sends"]["count"] for v in results.values()) / len(results)
        append_memory(
            "sop-checker",
            "各指标历史均值",
            f"[{date_str}] 平均私发数={avg_sends:.1f}",
        )

    return output


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    date_str = date_arg or yesterday_str()
    data_path = report_path(date_str, "data.json")
    if not data_path.exists():
        print(f"[SOPChecker] data.json not found at {data_path}, run fetch.py first")
        sys.exit(1)
    data = json.loads(data_path.read_text(encoding="utf-8"))
    run_sop_check(data)
