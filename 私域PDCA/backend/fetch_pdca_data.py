#!/usr/bin/env python3
"""
fetch_pdca_data.py — 用 vertu CLI 拉 Odoo 销售数据，输出 pdca_data.json
用法: python3 fetch_pdca_data.py
"""
import json
import os
import subprocess
import sys
import tempfile
from calendar import monthrange
from datetime import date, timedelta
from typing import Any

USD_RATE = 7.2


def vertu_search(domain: list[Any], fields: list[str], limit: int = 2000) -> list[dict[str, Any]]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(domain, f)
        tmp = f.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fields, f)
        flds = f.name
    try:
        r = subprocess.run(
            ["vertu", "odoo", "data", "search",
             "--model-name", "sale.order",
             "--domain-file", tmp,
             "--fields-file", flds,
             "--limit", str(limit)],
            capture_output=True, text=True,
        )
        out = r.stdout.strip()
        if not out:
            print(f"  [warn] vertu returned empty: {r.stderr[:200]}", file=sys.stderr)
            return []
        data = json.loads(out)
        return data.get("result", []) if data.get("ok") else []
    finally:
        os.unlink(tmp)
        os.unlink(flds)


def to_cny(amount: float, currency_id: int) -> float:
    return amount * USD_RATE if currency_id == 1 else float(amount)


def agg_by_user(orders: list[dict[str, Any]]) -> dict[tuple[int, str], float]:
    """Returns {(uid, name): total_cny}"""
    out: dict[tuple[int, str], float] = {}
    for o in orders:
        uid   = o["user_id"][0] if o["user_id"] else None
        uname = o["user_id"][1] if o["user_id"] else "未分配"
        if uid is None:
            continue
        cny = to_cny(o["amount_total"], o["currency_id"][0])
        out[(uid, uname)] = out.get((uid, uname), 0) + cny
    return out


def wan(v: float) -> float:
    return round(v / 10_000, 2)


def ranking(by_user: dict[tuple[int, str], float], top: int = 20) -> list[dict[str, Any]]:
    return [
        {"user_id": uid, "name": name, "wan": wan(total)}
        for (uid, name), total in sorted(by_user.items(), key=lambda x: -x[1])[:top]
    ]


def main() -> None:
    today = date.today()
    year  = today.year
    month = today.month

    print("拉取本月数据…", flush=True)
    month_start = f"{year}-{month:02d}-01"
    _, last_day = monthrange(year, month)
    month_end   = (date(year, month, last_day) + timedelta(days=1)).isoformat()

    month_orders = vertu_search(
        [["date_order", ">=", month_start], ["date_order", "<", month_end],
         ["state", "in", ["sale", "done"]], ["amount_total", ">", 0]],
        ["user_id", "amount_total", "currency_id", "date_order"],
    )
    print(f"  本月: {len(month_orders)} 笔")

    print("拉取今日数据…", flush=True)
    tod_start   = today.isoformat()
    today_orders = [o for o in month_orders if o["date_order"][:10] == tod_start]
    print(f"  今日: {len(today_orders)} 笔")

    print("拉取YTD数据…", flush=True)
    ytd_orders = vertu_search(
        [["date_order", ">=", f"{year}-01-01"], ["date_order", "<", month_end],
         ["state", "in", ["sale", "done"]], ["amount_total", ">", 0]],
        ["user_id", "amount_total", "currency_id", "date_order"],
    )
    print(f"  YTD: {len(ytd_orders)} 笔")

    month_by_user = agg_by_user(month_orders)
    today_by_user = agg_by_user(today_orders)
    ytd_by_user   = agg_by_user(ytd_orders)

    # Monthly breakdown (Jan ~ current month)
    monthly_totals: dict[tuple[int, int, str], float] = {}
    for o in ytd_orders:
        m     = int(o["date_order"][5:7])
        uid   = o["user_id"][0] if o["user_id"] else None
        uname = o["user_id"][1] if o["user_id"] else "未分配"
        if uid is None:
            continue
        cny = to_cny(o["amount_total"], o["currency_id"][0])
        monthly_totals[(m, uid, uname)] = monthly_totals.get((m, uid, uname), 0) + cny

    user_monthly: dict[int, dict[str, Any]] = {}
    for (m, uid, name), total in monthly_totals.items():
        if uid not in user_monthly:
            user_monthly[uid] = {"name": name, "months": {}}
        user_monthly[uid]["months"][m] = wan(total)

    dept_monthly: dict[int, float] = {}
    for (m, uid, name), total in monthly_totals.items():
        dept_monthly[m] = dept_monthly.get(m, 0) + total

    output: dict[str, Any] = {
        "generated": today.isoformat(),
        "year":  year,
        "month": month,
        "today": tod_start,
        "dept": {
            "today_wan": wan(sum(today_by_user.values())),
            "month_wan": wan(sum(month_by_user.values())),
            "ytd_wan":   wan(sum(ytd_by_user.values())),
            "month_by_month": [
                {"month": m, "quarter": (m - 1) // 3 + 1, "wan": wan(dept_monthly.get(m, 0))}
                for m in range(1, month + 1)
            ],
        },
        "ranking": {
            "today": ranking(today_by_user),
            "month": ranking(month_by_user),
            "ytd":   ranking(ytd_by_user),
        },
        "users": {
            str(uid): {
                "name":      info["name"],
                "today_wan": wan(today_by_user.get((uid, info["name"]), 0)),
                "month_wan": wan(month_by_user.get((uid, info["name"]), 0)),
                "ytd_wan":   wan(ytd_by_user.get((uid, info["name"]), 0)),
                "monthly":   info["months"],
            }
            for uid, info in user_monthly.items()
        },
    }

    out_path = "/tmp/pdca_data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✓ 输出: {out_path}")
    print(f"  今日部门: {output['dept']['today_wan']}万")
    print(f"  本月部门: {output['dept']['month_wan']}万")
    print(f"  YTD部门:  {output['dept']['ytd_wan']}万")
    print(f"  本月排行 Top5:")
    for r in output["ranking"]["month"][:5]:
        print(f"    {r['name']:12} {r['wan']}万")


if __name__ == "__main__":
    main()
