"""Agent 1 · DataFetcher — pull all seat messages from SalesEpoch API.

主键设计：以员工的 WhatsApp 号码（whatsId）为主键，而非 SalesEpoch 登录账号。
一个 SalesEpoch 账号下可能有多个员工用自己的 WA 号发消息，按 WA 号拆分后
再通过 seats_config.json 映射到员工姓名。
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    AGENTS_DIR, TENANT_ID, api_post, api_post_paged,
    append_memory, report_path, yesterday_str, day_range_ms,
)

ACTION_SEND    = 1
ACTION_RECEIVE = 2


def load_seats_config() -> dict:
    """Load phone→{name, wa_names} mapping from seats_config.json."""
    p = AGENTS_DIR / "seats_config.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def normalize_phone(raw: str) -> str:
    """Strip all non-digits."""
    return re.sub(r"[^0-9]", "", raw)


def get_all_seats() -> list[dict]:
    data = api_post("/group-dispatch-api/user/listUserByTenantId", {})
    seats = data.get("data", [])
    if not isinstance(seats, list):
        seats = []
    print(f"[DataFetcher] 获取到 {len(seats)} 个 SalesEpoch 账号")
    return seats


def normalize_message(m: dict) -> dict:
    action = m.get("actionType", 0)
    return {
        "chatTime":      m.get("chatTime", 0),
        "actionType":    "send" if action == ACTION_SEND else "receive",
        "actionTypeRaw": action,
        "contentType":   m.get("contentType", 0),
        "content":       m.get("content", ""),
        "friendWhatsId": m.get("friendWhatsId", ""),
        "friendName":    m.get("friendName", ""),
        "whatsId":       m.get("whatsId", ""),
        "username":      m.get("username", ""),
        "chatType":      m.get("chatType", 1),
        "msgId":         m.get("msgId", ""),
    }


def fetch_seat_messages(account: str, date_str: str) -> list[dict]:
    start_ms, end_ms = day_range_ms(date_str)
    messages = api_post_paged(
        "/wscrm-bus-api/open/message/msgPage",
        {"tenantId": TENANT_ID},
        page_size=100,
        info_filters={
            "agentAccount": account,
            "startChatTime": start_ms,
            "endChatTime":   end_ms,
        },
    )
    return [normalize_message(m) for m in messages]


def group_by_customer(messages: list[dict], agent_phones: set | None = None) -> dict[str, list[dict]]:
    """Group messages by customer phone, filtering out noise:
    - chatType=2  : group chats (friendWhatsId is an 18-digit group ID)
    - inter-agent : friendWhatsId is another agent's WA number
    - unknown     : empty friendWhatsId
    """
    convos: dict[str, list[dict]] = {}
    for m in messages:
        if m.get("chatType") == 2:
            continue
        cid = m.get("friendWhatsId", "")
        if not cid or len(cid) > 15:  # group IDs are 18 digits
            continue
        if agent_phones and cid in agent_phones:
            continue  # skip inter-agent messages
        convos.setdefault(cid, []).append(m)
    for cid in convos:
        convos[cid].sort(key=lambda x: x["chatTime"])
    return convos


def split_by_wa_account(messages: list[dict], seats_cfg: dict) -> dict[str, list[dict]]:
    """
    把一个 SalesEpoch 账号下的消息按 agent whatsId 拆分。
    只保留在 seats_config.json 中有姓名的 WA 号。
    key = 归一化电话号码。
    """
    by_wa: dict[str, list[dict]] = {}
    for m in messages:
        raw_wid = m.get("whatsId", "")
        if not raw_wid:
            continue
        wid = normalize_phone(raw_wid)
        if wid not in seats_cfg:
            continue  # 不在名单里，跳过
        by_wa.setdefault(wid, []).append(m)
    return by_wa


def _process_seat(
    seat: dict, date_str: str, seats_cfg: dict,
    agent_phones: set[str], wa_seats: dict, failed: list,
) -> None:
    """Fetch messages for one SalesEpoch seat and merge into wa_seats."""
    account = seat.get("userName") or seat.get("username", "")
    if not account:
        return
    try:
        messages = fetch_seat_messages(account, date_str)
        if not messages:
            return
        if seats_cfg:
            by_wa = split_by_wa_account(messages, seats_cfg)
            for wid, wa_msgs in by_wa.items():
                cfg = seats_cfg[wid]
                if wid not in wa_seats:
                    wa_seats[wid] = {
                        "phone": wid, "name": cfg["name"],
                        "wa_names": cfg.get("wa_names", []),
                        "se_account": account,
                        "message_count": 0,
                        "conversations": {}, "raw_messages": [],
                    }
                wa_seats[wid]["raw_messages"].extend(wa_msgs)
                wa_seats[wid]["message_count"] += len(wa_msgs)
                for cid, msgs in group_by_customer(wa_msgs, agent_phones).items():
                    wa_seats[wid]["conversations"].setdefault(cid, []).extend(msgs)
                print(f"  [{cfg['name']} / {wid}] +{len(wa_msgs)} 条")
        else:
            convos = group_by_customer(messages)
            wa_seats[account] = {
                "phone": account, "name": account, "wa_names": [],
                "se_account": account, "message_count": len(messages),
                "conversations": convos, "raw_messages": messages,
            }
            print(f"  [{account}] {len(messages)} 条消息")
    except Exception as e:
        print(f"  [{account}] 拉取失败: {e}")
        failed.append(account)
        append_memory("data-fetcher", "API异常记录", f"[{date_str}] {account} | {e}")


def fetch_all(date_str: str | None = None) -> dict:
    date_str = date_str or yesterday_str()
    print(f"[DataFetcher] 开始拉取 {date_str} 数据")

    seats_cfg = load_seats_config()
    if seats_cfg:
        print(f"[DataFetcher] 已加载名单：{len(seats_cfg)} 个 WA 号，"
              f"{len({v['name'] for v in seats_cfg.values()})} 名员工")
    else:
        print("[DataFetcher] ⚠️  seats_config.json 未找到，将拉取全部账号")

    se_accounts  = get_all_seats()
    wa_seats: dict[str, dict] = {}
    failed: list[str] = []
    agent_phones: set[str] = set(seats_cfg.keys()) if seats_cfg else set()

    for seat in se_accounts:
        _process_seat(seat, date_str, seats_cfg, agent_phones, wa_seats, failed)

    for wid, seat_data in wa_seats.items():
        for cid in seat_data["conversations"]:
            seat_data["conversations"][cid].sort(key=lambda x: x["chatTime"])

    active = sum(1 for s in wa_seats.values() if s["message_count"] > 0)
    print(f"[DataFetcher] 完成: {active} 个 WA 账号有消息 / 共 {len(wa_seats)} 个")

    result = {
        "date":        date_str,
        "fetched_at":  datetime.now().isoformat(),
        "seat_count":  len(wa_seats),
        "seats":       wa_seats,
        "failed_seats": failed,
    }
    out = report_path(date_str, "data.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DataFetcher] 数据已保存 → {out}")
    append_memory(
        "data-fetcher", "坐席账号记录",
        f"[{date_str}] WA账号 {len(wa_seats)} 个，活跃 {active} 个",
    )
    return result


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    fetch_all(date_arg)
