"""Vertu QA Dashboard — Flask backend"""

import json
from pathlib import Path
from flask import Flask, jsonify, render_template, abort

app = Flask(__name__)
REPORTS_DIR = Path(__file__).parent.parent / "reports"


def load(date: str, name: str) -> dict:
    p = REPORTS_DIR / date / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def narrative(date: str) -> str:
    p = REPORTS_DIR / date / "daily_report.md"
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8")
    capturing, lines = False, []
    for line in text.splitlines():
        if "管理摘要" in line:
            capturing = True
            continue
        if capturing:
            if line.startswith("---"):
                break
            if line.strip():
                lines.append(line.strip())
    return " ".join(lines)


@app.get("/")
def index():
    dates = sorted(
        [d.name for d in REPORTS_DIR.iterdir() if d.is_dir()],
        reverse=True,
    )
    return render_template("dashboard.html", dates=dates)


@app.get("/api/report/<date>")
def report(date: str):
    if not (REPORTS_DIR / date).exists():
        abort(404)
    data   = load(date, "data")
    sop    = load(date, "sop_results")
    guard  = load(date, "guard_results")
    scores = load(date, "score_results")

    seat_meta = data.get("seats", {})

    def label(key: str) -> str:
        s = seat_meta.get(key, {})
        name = s.get("name", key)
        wa   = (s.get("wa_names") or [""])[0]
        if name and name != key:
            return f"{name}({wa})" if wa else name
        return key

    # SOP 表
    sop_rows = []
    for key, s in sop.get("seats", {}).items():
        fr = s.get("first_response", {})
        ds = s.get("daily_sends", {})
        oc = s.get("old_customer_activation", {})
        sop_rows.append({
            "label":        label(key),
            "fr_avg":       round(fr.get("avg_seconds") or 0),
            "fr_grade":     fr.get("grade", "N/A"),
            "fr_n":         fr.get("total_convos", 0),
            "fr_skipped":   fr.get("skipped_offhours", 0),
            "sends":        ds.get("count", 0),
            "sends_grade":  ds.get("grade", "N/A"),
            "old_cust":     oc.get("count", 0),
            "old_grade":    oc.get("grade", "N/A"),
            "wa_reached":   s.get("wa_dynamics", {}).get("reached", False),
            "wa_count":     s.get("wa_dynamics", {}).get("count", 0),
            "sop_score":    s.get("sop_score", 0),
        })
    sop_rows.sort(key=lambda r: -r["sop_score"])

    # 高风险预警
    from datetime import datetime
    alerts_out = []
    for a in guard.get("high_alerts", []):
        ts_ms = a.get("window_start") or a.get("query_time") or \
                a.get("unanswered_since") or a.get("chatTime")
        ts = datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M") if ts_ms else "?"
        acct = a.get("account", "?")
        cust = str(a.get("customer") or a.get("ignored_customer") or "?")
        alerts_out.append({
            "label":    label(acct),
            "type":     a.get("type", ""),
            "customer": cust[-8:] + "***",
            "time":     ts,
            "risk":     a.get("risk", "高"),
        })

    # 中风险汇总
    mid_counts = []
    for acct, als in guard.get("seats", {}).items():
        mid = [a for a in als if a.get("risk") == "中"]
        if mid:
            mid_counts.append({"label": label(acct), "count": len(mid)})
    mid_counts.sort(key=lambda x: -x["count"])

    # AI 评分
    score_rows = []
    for key, seat_scores in scores.get("seats", {}).items():
        if not seat_scores:
            continue
        n = len(seat_scores)
        avg = lambda f: round(sum(s.get(f, 0) for s in seat_scores) / n, 1)
        overall = avg("overall_score")
        grade = ("优秀" if overall >= 90 else "良好" if overall >= 75
                 else "及格" if overall >= 60 else "需关注")
        score_rows.append({
            "label":   label(key),
            "pro":     avg("professionalism_score"),
            "brand":   avg("brand_tone_score"),
            "conv":    avg("conversion_score"),
            "overall": overall,
            "grade":   grade,
            "n":       n,
        })
    score_rows.sort(key=lambda r: -r["overall"])

    return jsonify({
        "date":           date,
        "narrative":      narrative(date),
        "active_seats":   sum(1 for s in sop.get("seats", {}).values()
                              if s.get("daily_sends", {}).get("count", 0) > 0),
        "total_seats":    len(sop.get("seats", {})),
        "high_alerts":    guard.get("high_alert_count", 0),
        "problem_cases":  scores.get("problem_case_count", 0),
        "gold_cases":     scores.get("gold_case_count", 0),
        "sop_rows":       sop_rows,
        "alerts":         alerts_out,
        "mid_counts":     mid_counts,
        "score_rows":     score_rows,
    })


if __name__ == "__main__":
    app.run(port=5001, debug=False)
