"""
PDCA API — 海外私域 PDCA 操作台后端
FastAPI + asyncpg, 直连 Odoo PostgreSQL

环境变量:
  DATABASE_URL  postgresql://odoo:pass@localhost:5432/odoo17
  USD_TO_CNY    汇率 (默认 7.2)
  TARGETS_FILE  目标配置路径 (默认 ./targets.json)
"""

import asyncio
import json
import math
import os
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ── config ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://odoo:odoo@localhost:5432/odoo17")
USD_TO_CNY   = float(os.getenv("USD_TO_CNY", "7.2"))
TARGETS_FILE = os.getenv("TARGETS_FILE", str(Path(__file__).parent / "targets.json"))

CONFIRMED_STATES = ("sale", "done")

app = FastAPI(title="PDCA API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

pool: asyncpg.Pool | None = None


# ── lifecycle ────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)


@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()


# ── helpers ──────────────────────────────────────────────────────────────────
def to_cny(amount: float, currency_id: int) -> float:
    """Convert any currency amount to CNY."""
    if currency_id == 1:   # USD
        return amount * USD_TO_CNY
    return float(amount)   # CNY (currency_id=6) or others treated as CNY


def wan(cny: float) -> float:
    """CNY → 万元, 2 dp."""
    return round(cny / 10_000, 2)


def pct(done: float, target: float) -> int:
    if not target:
        return 0
    return min(int(done / target * 100), 100)


def load_targets() -> dict:
    try:
        with open(TARGETS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def month_bounds(year: int, month: int) -> tuple[str, str]:
    """Return (start, exclusive_end) as ISO strings for PostgreSQL."""
    _, last_day = monthrange(year, month)
    start = date(year, month, 1).isoformat()
    end   = (date(year, month, last_day) + timedelta(days=1)).isoformat()
    return start, end


def today_cn() -> date:
    """Current date in Asia/Shanghai (UTC+8)."""
    return (datetime.utcnow() + timedelta(hours=8)).date()


def week_of_month(d: date) -> int:
    """Which week of the month (1-based) does d fall in?"""
    return (d.day - 1) // 7 + 1


# ── shared queries ───────────────────────────────────────────────────────────
async def _user_name(uid: int) -> str:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """SELECT rp.name
               FROM res_users ru
               JOIN res_partner rp ON rp.id = ru.partner_id
               WHERE ru.id = $1""",
            uid,
        )
        return row["name"] if row else str(uid)


async def _sales_cny(con: asyncpg.Connection, where: str, args: list) -> float:
    """Sum sales converted to CNY with given WHERE clause."""
    sql = f"""
        SELECT COALESCE(SUM(
            CASE WHEN currency_id = 1 THEN amount_total * {USD_TO_CNY}
                 ELSE amount_total END
        ), 0) AS total
        FROM sale_order
        WHERE state = ANY($1) AND amount_total > 0
          AND {where}
    """
    row = await con.fetchrow(sql, list(CONFIRMED_STATES), *args)
    return float(row["total"])


async def _order_count(con: asyncpg.Connection, where: str, args: list) -> int:
    sql = f"""
        SELECT COUNT(*) AS cnt
        FROM sale_order
        WHERE state = ANY($1) AND amount_total > 0
          AND {where}
    """
    row = await con.fetchrow(sql, list(CONFIRMED_STATES), *args)
    return int(row["cnt"])


async def _team_members(team_id: int) -> list[dict]:
    """Return list of {id, name} for members of the given sales team."""
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT ru.id, rp.name
               FROM res_users ru
               JOIN res_partner rp ON rp.id = ru.partner_id
               WHERE ru.sale_team_id = $1
                 AND ru.active = true
               ORDER BY rp.name""",
            team_id,
        )
        return [{"id": r["id"], "name": r["name"]} for r in rows]


async def _member_sales_cny(con: asyncpg.Connection, user_ids: list[int],
                             start: str, end: str) -> dict[int, float]:
    """Return {user_id: total_cny} for a date range."""
    if not user_ids:
        return {}
    rows = await con.fetch(
        f"""SELECT user_id,
               COALESCE(SUM(
                 CASE WHEN currency_id = 1 THEN amount_total * {USD_TO_CNY}
                      ELSE amount_total END
               ), 0) AS total
            FROM sale_order
            WHERE state = ANY($1)
              AND amount_total > 0
              AND user_id = ANY($2)
              AND date_order AT TIME ZONE 'Asia/Shanghai' >= $3
              AND date_order AT TIME ZONE 'Asia/Shanghai' <  $4
            GROUP BY user_id""",
        list(CONFIRMED_STATES), user_ids, start, end,
    )
    return {r["user_id"]: float(r["total"]) for r in rows}


# ── staff endpoints ──────────────────────────────────────────────────────────
@app.get("/api/pdca/staff/{user_id}/day")
async def staff_day(user_id: int):
    today = today_cn()
    tod_start = today.isoformat()
    tod_end   = (today + timedelta(days=1)).isoformat()
    mon_start, mon_end = month_bounds(today.year, today.month)

    targets = load_targets()
    staff_t  = targets.get("staff", {}).get(str(user_id), {})
    mon_key  = today.strftime("%Y-%m")
    month_target_cny = staff_t.get("months", {}).get(mon_key, staff_t.get("month_default", 250_000))
    year_target_cny  = staff_t.get("year_target", 3_470_000)

    _, days_in_month = monthrange(today.year, today.month)
    day_target_cny   = month_target_cny / days_in_month

    async with pool.acquire() as con:
        today_cny = await _sales_cny(
            con,
            "user_id = $2 AND date_order AT TIME ZONE 'Asia/Shanghai' >= $3 AND date_order AT TIME ZONE 'Asia/Shanghai' < $4",
            [user_id, tod_start, tod_end],
        )
        month_cny = await _sales_cny(
            con,
            "user_id = $2 AND date_order AT TIME ZONE 'Asia/Shanghai' >= $3 AND date_order AT TIME ZONE 'Asia/Shanghai' < $4",
            [user_id, mon_start, mon_end],
        )
        month_orders = await _order_count(
            con,
            "user_id = $2 AND date_order AT TIME ZONE 'Asia/Shanghai' >= $3 AND date_order AT TIME ZONE 'Asia/Shanghai' < $4",
            [user_id, mon_start, mon_end],
        )

        # Team ranking today & month
        rank_rows = await con.fetch(
            f"""SELECT so.user_id,
                   rp.name,
                   COALESCE(SUM(
                     CASE WHEN so.currency_id = 1 THEN so.amount_total * {USD_TO_CNY}
                          ELSE so.amount_total END
                   ), 0) AS total
                FROM sale_order so
                JOIN res_users ru ON ru.id = so.user_id
                JOIN res_partner rp ON rp.id = ru.partner_id
                WHERE so.state = ANY($1)
                  AND so.amount_total > 0
                  AND so.date_order AT TIME ZONE 'Asia/Shanghai' >= $2
                  AND so.date_order AT TIME ZONE 'Asia/Shanghai' <  $3
                  AND ru.sale_team_id = (SELECT sale_team_id FROM res_users WHERE id = $4)
                GROUP BY so.user_id, rp.name
                ORDER BY total DESC
                LIMIT 10""",
            list(CONFIRMED_STATES), mon_start, mon_end, user_id,
        )

    name = await _user_name(user_id)
    perf_rank = [
        {"user_id": r["user_id"], "name": r["name"], "is_me": r["user_id"] == user_id,
         "val_wan": wan(r["total"])}
        for r in rank_rows
    ]

    remaining_days = days_in_month - today.day
    gap_cny = month_target_cny - month_cny
    month_pct = pct(month_cny, month_target_cny)

    return {
        "user": {"id": user_id, "name": name},
        "date": today.isoformat(),
        "kpi": {
            "today_target_wan": wan(day_target_cny),
            "today_done_wan":   wan(today_cny),
            "today_pct":        pct(today_cny, day_target_cny),
            "month_target_wan": wan(month_target_cny),
            "month_done_wan":   wan(month_cny),
            "month_pct":        month_pct,
            "month_orders":     month_orders,
            "remaining_days":   remaining_days,
            "daily_needed_wan": wan(gap_cny / remaining_days) if remaining_days > 0 else 0,
        },
        "ranking": {"perf": perf_rank},
    }


@app.get("/api/pdca/staff/{user_id}/month")
async def staff_month(user_id: int):
    today = today_cn()
    _, days_in_month = monthrange(today.year, today.month)
    mon_start, mon_end = month_bounds(today.year, today.month)

    targets = load_targets()
    staff_t = targets.get("staff", {}).get(str(user_id), {})
    mon_key = today.strftime("%Y-%m")
    month_target_cny = staff_t.get("months", {}).get(mon_key, staff_t.get("month_default", 250_000))

    async with pool.acquire() as con:
        month_cny = await _sales_cny(
            con,
            "user_id = $2 AND date_order AT TIME ZONE 'Asia/Shanghai' >= $3 AND date_order AT TIME ZONE 'Asia/Shanghai' < $4",
            [user_id, mon_start, mon_end],
        )
        month_orders = await _order_count(
            con,
            "user_id = $2 AND date_order AT TIME ZONE 'Asia/Shanghai' >= $3 AND date_order AT TIME ZONE 'Asia/Shanghai' < $4",
            [user_id, mon_start, mon_end],
        )

        # Weekly breakdown
        week_data = []
        week_starts = []
        d = date(today.year, today.month, 1)
        while d.month == today.month:
            week_starts.append(d)
            d += timedelta(days=7)

        for i, ws in enumerate(week_starts):
            we = min(ws + timedelta(days=6), date(today.year, today.month, days_in_month))
            we_excl = we + timedelta(days=1)
            w_target = month_target_cny / len(week_starts)
            if we < today:
                status = "done"
            elif ws <= today <= we:
                status = "current"
            else:
                status = "future"
            if status in ("done", "current"):
                w_cny = await _sales_cny(
                    con,
                    "user_id = $2 AND date_order AT TIME ZONE 'Asia/Shanghai' >= $3 AND date_order AT TIME ZONE 'Asia/Shanghai' < $4",
                    [user_id, ws.isoformat(), we_excl.isoformat()],
                )
            else:
                w_cny = 0.0
            week_data.append({
                "label":  f"W{i+1} · {ws.strftime('%m/%d')}–{we.strftime('%d')}",
                "target_wan": wan(w_target),
                "done_wan":   wan(w_cny),
                "pct":        pct(w_cny, w_target),
                "status":     status,
            })

        # Team ranking this month
        rank_rows = await con.fetch(
            f"""SELECT so.user_id, rp.name,
                   COALESCE(SUM(
                     CASE WHEN so.currency_id = 1 THEN so.amount_total * {USD_TO_CNY}
                          ELSE so.amount_total END
                   ), 0) AS total
                FROM sale_order so
                JOIN res_users ru ON ru.id = so.user_id
                JOIN res_partner rp ON rp.id = ru.partner_id
                WHERE so.state = ANY($1)
                  AND so.amount_total > 0
                  AND so.date_order AT TIME ZONE 'Asia/Shanghai' >= $2
                  AND so.date_order AT TIME ZONE 'Asia/Shanghai' <  $3
                  AND ru.sale_team_id = (SELECT sale_team_id FROM res_users WHERE id = $4)
                GROUP BY so.user_id, rp.name
                ORDER BY total DESC
                LIMIT 10""",
            list(CONFIRMED_STATES), mon_start, mon_end, user_id,
        )

    remaining = days_in_month - today.day
    gap = month_target_cny - month_cny

    return {
        "month": today.strftime("%Y-%m"),
        "kpi": {
            "month_target_wan": wan(month_target_cny),
            "month_done_wan":   wan(month_cny),
            "month_pct":        pct(month_cny, month_target_cny),
            "month_orders":     month_orders,
            "remaining_days":   remaining,
            "daily_needed_wan": wan(gap / remaining) if remaining > 0 else 0,
        },
        "weeks": week_data,
        "ranking": [
            {"user_id": r["user_id"], "name": r["name"],
             "is_me": r["user_id"] == user_id, "val_wan": wan(r["total"])}
            for r in rank_rows
        ],
    }


@app.get("/api/pdca/staff/{user_id}/year")
async def staff_year(user_id: int):
    today = today_cn()
    targets = load_targets()
    staff_t = targets.get("staff", {}).get(str(user_id), {})
    year_target_cny = staff_t.get("year_target", 3_470_000)
    month_targets   = staff_t.get("months", {})

    async with pool.acquire() as con:
        # YTD total
        ytd_cny = await _sales_cny(
            con,
            f"user_id = $2 AND date_order AT TIME ZONE 'Asia/Shanghai' >= '{today.year}-01-01' AND date_order AT TIME ZONE 'Asia/Shanghai' < '{today.year+1}-01-01'",
            [user_id],
        )

        # Monthly actuals Jan–current month
        monthly = []
        for m in range(1, today.month + 1):
            ms, me = month_bounds(today.year, m)
            m_target = month_targets.get(f"{today.year}-{m:02d}",
                                         year_target_cny // 12)
            if date(today.year, m, 1) <= today:
                m_cny = await _sales_cny(
                    con,
                    "user_id = $2 AND date_order AT TIME ZONE 'Asia/Shanghai' >= $3 AND date_order AT TIME ZONE 'Asia/Shanghai' < $4",
                    [user_id, ms, me],
                )
            else:
                m_cny = None
            monthly.append({
                "month": m,
                "quarter": (m - 1) // 3 + 1,
                "target_wan": wan(m_target),
                "done_wan":   wan(m_cny) if m_cny is not None else None,
                "pct": pct(m_cny, m_target) if m_cny is not None else None,
                "status": "current" if m == today.month else ("done" if m < today.month else "future"),
            })
        # Add future months
        for m in range(today.month + 1, 13):
            m_target = month_targets.get(f"{today.year}-{m:02d}", year_target_cny // 12)
            monthly.append({
                "month": m, "quarter": (m - 1) // 3 + 1,
                "target_wan": wan(m_target), "done_wan": None, "pct": None, "status": "future",
            })

    # Quarter summaries
    quarters = []
    for q in range(1, 5):
        months_q = [x for x in monthly if x["quarter"] == q]
        q_target = sum(x["target_wan"] for x in months_q)
        q_done   = sum(x["done_wan"] for x in months_q if x["done_wan"] is not None)
        q_status = "done" if all(x["status"] == "done" for x in months_q) \
                   else "current" if any(x["status"] == "current" for x in months_q) \
                   else "future"
        quarters.append({
            "q": q, "target_wan": round(q_target, 2),
            "done_wan": round(q_done, 2), "pct": pct(q_done, q_target),
            "status": q_status,
        })

    day_of_year = today.timetuple().tm_yday
    expected_pct = int(day_of_year / 365 * 100)

    return {
        "year": today.year,
        "kpi": {
            "year_target_wan": wan(year_target_cny),
            "ytd_done_wan":    wan(ytd_cny),
            "ytd_pct":         pct(ytd_cny, year_target_cny),
            "expected_pct":    expected_pct,
        },
        "quarters": quarters,
        "monthly":  monthly,
    }


# ── manager endpoints ─────────────────────────────────────────────────────────
@app.get("/api/pdca/manager/{team_id}/day")
async def manager_day(team_id: int):
    today = today_cn()
    tod_start = today.isoformat()
    tod_end   = (today + timedelta(days=1)).isoformat()

    members = await _team_members(team_id)
    if not members:
        raise HTTPException(404, "Team not found or has no members")

    targets = load_targets()
    team_t  = targets.get("teams", {}).get(str(team_id), {})
    mon_key = today.strftime("%Y-%m")
    month_target_cny = team_t.get("months", {}).get(mon_key, team_t.get("month_default", 1_220_000))
    _, days_in_month = monthrange(today.year, today.month)
    day_target_cny   = month_target_cny / days_in_month

    uids = [m["id"] for m in members]
    async with pool.acquire() as con:
        today_map = await _member_sales_cny(con, uids, tod_start, tod_end)
        mon_start, mon_end = month_bounds(today.year, today.month)
        month_map = await _member_sales_cny(con, uids, mon_start, mon_end)

    member_cards = []
    for m in members:
        uid = m["id"]
        staff_t  = targets.get("staff", {}).get(str(uid), {})
        m_target = staff_t.get("months", {}).get(mon_key, staff_t.get("month_default", 250_000))
        d_target = m_target / days_in_month
        today_cny = today_map.get(uid, 0.0)
        month_cny = month_map.get(uid, 0.0)
        member_cards.append({
            "user_id":      uid,
            "name":         m["name"],
            "today_wan":    wan(today_cny),
            "today_target_wan": wan(d_target),
            "today_pct":    pct(today_cny, d_target),
            "month_wan":    wan(month_cny),
            "month_target_wan": wan(m_target),
            "month_pct":    pct(month_cny, m_target),
            "warn":         pct(today_cny, d_target) < 40,
        })

    member_cards.sort(key=lambda x: -x["today_wan"])
    team_today = sum(c["today_wan"] for c in member_cards)

    return {
        "team_id": team_id,
        "team_name": team_t.get("name", f"Team {team_id}"),
        "date": today.isoformat(),
        "kpi": {
            "day_target_wan":  wan(day_target_cny),
            "today_total_wan": team_today,
            "today_pct":       pct(int(team_today * 10000), int(day_target_cny)),
        },
        "members": member_cards,
    }


@app.get("/api/pdca/manager/{team_id}/month")
async def manager_month(team_id: int):
    today = today_cn()
    mon_start, mon_end = month_bounds(today.year, today.month)
    _, days_in_month = monthrange(today.year, today.month)

    members = await _team_members(team_id)
    uids    = [m["id"] for m in members]

    targets = load_targets()
    team_t  = targets.get("teams", {}).get(str(team_id), {})
    mon_key = today.strftime("%Y-%m")
    month_target_cny = team_t.get("months", {}).get(mon_key, team_t.get("month_default", 1_220_000))

    async with pool.acquire() as con:
        month_map = await _member_sales_cny(con, uids, mon_start, mon_end)
        team_month_cny = await _sales_cny(
            con,
            "user_id = ANY($2) AND date_order AT TIME ZONE 'Asia/Shanghai' >= $3 AND date_order AT TIME ZONE 'Asia/Shanghai' < $4",
            [uids, mon_start, mon_end],
        )

    member_rows = []
    for m in members:
        uid = m["id"]
        staff_t  = targets.get("staff", {}).get(str(uid), {})
        m_target = staff_t.get("months", {}).get(mon_key, staff_t.get("month_default", 250_000))
        m_cny    = month_map.get(uid, 0.0)
        member_rows.append({
            "user_id":    uid,
            "name":       m["name"],
            "done_wan":   wan(m_cny),
            "target_wan": wan(m_target),
            "pct":        pct(m_cny, m_target),
        })
    member_rows.sort(key=lambda x: -x["done_wan"])

    remaining = days_in_month - today.day
    gap = month_target_cny - team_month_cny
    month_pct = pct(team_month_cny, month_target_cny)

    return {
        "month":   today.strftime("%Y-%m"),
        "team_id": team_id,
        "kpi": {
            "month_target_wan":  wan(month_target_cny),
            "month_done_wan":    wan(team_month_cny),
            "month_pct":         month_pct,
            "remaining_days":    remaining,
            "daily_needed_wan":  wan(gap / remaining) if remaining > 0 else 0,
            "member_count":      len(members),
        },
        "members": member_rows,
    }


@app.get("/api/pdca/manager/{team_id}/year")
async def manager_year(team_id: int):
    today   = today_cn()
    members = await _team_members(team_id)
    uids    = [m["id"] for m in members]

    targets = load_targets()
    team_t  = targets.get("teams", {}).get(str(team_id), {})
    year_target_cny = team_t.get("year_target", 15_950_000)
    month_targets   = team_t.get("months", {})

    async with pool.acquire() as con:
        ytd_cny = await _sales_cny(
            con,
            f"user_id = ANY($2) AND date_order AT TIME ZONE 'Asia/Shanghai' >= '{today.year}-01-01' AND date_order AT TIME ZONE 'Asia/Shanghai' < '{today.year+1}-01-01'",
            [uids],
        )
        member_ytd = await _member_sales_cny(con, uids, f"{today.year}-01-01", f"{today.year+1}-01-01")

    member_rows = []
    for m in members:
        uid = m["id"]
        staff_t = targets.get("staff", {}).get(str(uid), {})
        m_year  = staff_t.get("year_target", year_target_cny // len(members))
        m_cny   = member_ytd.get(uid, 0.0)
        member_rows.append({
            "user_id":       uid,
            "name":          m["name"],
            "ytd_wan":       wan(m_cny),
            "year_target_wan": wan(m_year),
            "pct":           pct(m_cny, m_year),
        })
    member_rows.sort(key=lambda x: -x["ytd_wan"])

    day_of_year  = today.timetuple().tm_yday
    expected_pct = int(day_of_year / 365 * 100)

    return {
        "year":    today.year,
        "team_id": team_id,
        "kpi": {
            "year_target_wan": wan(year_target_cny),
            "ytd_wan":         wan(ytd_cny),
            "ytd_pct":         pct(ytd_cny, year_target_cny),
            "expected_pct":    expected_pct,
        },
        "members": member_rows,
    }


# ── director endpoints ────────────────────────────────────────────────────────
@app.get("/api/pdca/director/day")
async def director_day():
    today     = today_cn()
    tod_start = today.isoformat()
    tod_end   = (today + timedelta(days=1)).isoformat()

    targets = load_targets()
    dept_t  = targets.get("dept", {})
    mon_key = today.strftime("%Y-%m")
    _, days_in_month = monthrange(today.year, today.month)
    month_target_cny = dept_t.get("months", {}).get(mon_key, dept_t.get("month_default", 3_000_000))
    day_target_cny   = month_target_cny / days_in_month

    team_targets = targets.get("teams", {})
    groups = []
    async with pool.acquire() as con:
        for tid_str, tt in team_targets.items():
            today_cny = await _sales_cny(
                con,
                f"""user_id IN (SELECT id FROM res_users WHERE sale_team_id = {int(tid_str)} AND active)
                    AND date_order AT TIME ZONE 'Asia/Shanghai' >= $2
                    AND date_order AT TIME ZONE 'Asia/Shanghai' <  $3""",
                [tod_start, tod_end],
            )
            t_day = tt.get("months", {}).get(mon_key, tt.get("month_default", 1_220_000)) / days_in_month
            groups.append({
                "team_id":   int(tid_str),
                "name":      tt.get("name", f"Group {tid_str}"),
                "today_wan": wan(today_cny),
                "day_target_wan": wan(t_day),
                "today_pct": pct(today_cny, t_day),
                "status":    "ok" if pct(today_cny, t_day) >= 80 else ("warn" if pct(today_cny, t_day) >= 50 else "behind"),
            })

        dept_today = await _sales_cny(
            con,
            "date_order AT TIME ZONE 'Asia/Shanghai' >= $2 AND date_order AT TIME ZONE 'Asia/Shanghai' < $3",
            [tod_start, tod_end],
        )

    return {
        "date": today.isoformat(),
        "kpi": {
            "day_target_wan":  wan(day_target_cny),
            "today_total_wan": wan(dept_today),
            "today_pct":       pct(dept_today, day_target_cny),
        },
        "groups": groups,
    }


@app.get("/api/pdca/director/month")
async def director_month():
    today     = today_cn()
    mon_start, mon_end = month_bounds(today.year, today.month)
    _, days_in_month   = monthrange(today.year, today.month)

    targets = load_targets()
    dept_t  = targets.get("dept", {})
    mon_key = today.strftime("%Y-%m")
    month_target_cny = dept_t.get("months", {}).get(mon_key, dept_t.get("month_default", 3_000_000))

    team_targets = targets.get("teams", {})
    groups = []
    async with pool.acquire() as con:
        dept_month_cny = await _sales_cny(
            con,
            "date_order AT TIME ZONE 'Asia/Shanghai' >= $2 AND date_order AT TIME ZONE 'Asia/Shanghai' < $3",
            [mon_start, mon_end],
        )
        for tid_str, tt in team_targets.items():
            m_cny = await _sales_cny(
                con,
                f"""user_id IN (SELECT id FROM res_users WHERE sale_team_id = {int(tid_str)} AND active)
                    AND date_order AT TIME ZONE 'Asia/Shanghai' >= $2
                    AND date_order AT TIME ZONE 'Asia/Shanghai' <  $3""",
                [mon_start, mon_end],
            )
            t_month = tt.get("months", {}).get(mon_key, tt.get("month_default", 1_220_000))
            groups.append({
                "team_id":    int(tid_str),
                "name":       tt.get("name", f"Group {tid_str}"),
                "month_wan":  wan(m_cny),
                "target_wan": wan(t_month),
                "pct":        pct(m_cny, t_month),
            })

    remaining = days_in_month - today.day
    gap = month_target_cny - dept_month_cny

    return {
        "month":  today.strftime("%Y-%m"),
        "kpi": {
            "month_target_wan":  wan(month_target_cny),
            "month_done_wan":    wan(dept_month_cny),
            "month_pct":         pct(dept_month_cny, month_target_cny),
            "remaining_days":    remaining,
            "daily_needed_wan":  wan(gap / remaining) if remaining > 0 else 0,
        },
        "groups": groups,
    }


@app.get("/api/pdca/director/year")
async def director_year():
    today = today_cn()
    targets = load_targets()
    dept_t  = targets.get("dept", {})
    year_target_cny = dept_t.get("year_target", 40_000_000)
    month_targets   = dept_t.get("months", {})

    async with pool.acquire() as con:
        ytd_cny = await _sales_cny(
            con,
            f"date_order AT TIME ZONE 'Asia/Shanghai' >= '{today.year}-01-01' AND date_order AT TIME ZONE 'Asia/Shanghai' < '{today.year+1}-01-01'",
            [],
        )
        monthly = []
        for m in range(1, 13):
            m_key = f"{today.year}-{m:02d}"
            m_target = month_targets.get(m_key, year_target_cny // 12)
            if date(today.year, m, 1) <= today:
                ms, me = month_bounds(today.year, m)
                m_cny = await _sales_cny(
                    con,
                    "date_order AT TIME ZONE 'Asia/Shanghai' >= $2 AND date_order AT TIME ZONE 'Asia/Shanghai' < $3",
                    [ms, me],
                )
                monthly.append({
                    "month": m, "quarter": (m - 1) // 3 + 1,
                    "target_wan": wan(m_target), "done_wan": wan(m_cny),
                    "pct": pct(m_cny, m_target),
                    "status": "current" if m == today.month else "done",
                })
            else:
                monthly.append({
                    "month": m, "quarter": (m - 1) // 3 + 1,
                    "target_wan": wan(m_target), "done_wan": None, "pct": None, "status": "future",
                })

    quarters = []
    for q in range(1, 5):
        mq = [x for x in monthly if x["quarter"] == q]
        q_target = sum(x["target_wan"] for x in mq)
        q_done   = sum(x["done_wan"] for x in mq if x["done_wan"] is not None)
        q_status = "done" if all(x["status"] == "done" for x in mq) \
                   else "current" if any(x["status"] == "current" for x in mq) \
                   else "future"
        quarters.append({"q": q, "target_wan": round(q_target, 2),
                          "done_wan": round(q_done, 2), "status": q_status})

    day_of_year  = today.timetuple().tm_yday
    expected_pct = int(day_of_year / 365 * 100)

    return {
        "year": today.year,
        "kpi": {
            "year_target_wan": wan(year_target_cny),
            "ytd_done_wan":    wan(ytd_cny),
            "ytd_pct":         pct(ytd_cny, year_target_cny),
            "expected_pct":    expected_pct,
        },
        "quarters": quarters,
        "monthly":  monthly,
    }


# ── utility ──────────────────────────────────────────────────────────────────
@app.get("/api/pdca/users")
async def list_users():
    """Helper: list all active sales users with their team."""
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT ru.id, rp.name, ru.sale_team_id,
                      ct.name AS team_name
               FROM res_users ru
               JOIN res_partner rp ON rp.id = ru.partner_id
               LEFT JOIN crm_team ct ON ct.id = ru.sale_team_id
               WHERE ru.active = true
                 AND ru.sale_team_id IS NOT NULL
               ORDER BY ct.name, rp.name"""
        )
        return [dict(r) for r in rows]


@app.get("/api/pdca/health")
async def health():
    async with pool.acquire() as con:
        ver = await con.fetchval("SELECT version()")
    return {"status": "ok", "db": ver[:40]}
