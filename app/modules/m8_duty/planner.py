"""预约生成与现场分工：把「已约时段」分给当天真能到场的同事。

两件事分开做，规则都在这里，便于单测钉死：

1. **预约（booking）**：名录里可排程的展商按原对接人分组，按优先级与展位顺序
   在开放时间内依次约时段（20 分钟一档，中间留步行分钟）。预约只认名录，
   不看当天谁请假：这就是「已约时段」，不随人员离场而作废。
2. **分工（assignment）**：每个预约先给原对接人；原对接人名单外、当天不在、
   晚到、提前离馆或行程赶不上时，由能及时到场（窗口盖住时段、行程接得上）
   的同事接管；没人接得下的预约标「无人接管」，仍然保留在预约记录里。

判定顺序固定，结果与输入行序无关：原对接人 → 接管候选（已接管次数少的优先，
其次到站早的，最后按工号）→ 无人接管。完成记录是事实：已完成的预约不再重排，
只按记录定人；续跑时上一版已完成的预约原样带过来。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config
from ..m2_booth import geometry, normalize
from ..m3_windows import windows
from ..m4_route import distance
from . import roster

DWELL_MIN = config.DWELL_EXHIBITOR_MIN
GRACE_MIN = config.APPOINTMENT_GRACE_MIN
ENTRY = config.DEFAULT_ENTRY

OWNER = "OWNER"
TAKEOVER = "TAKEOVER"
DONE = "DONE"
UNCOVERED = "UNCOVERED"
ROLE_LABELS = config.DUTY_ROLE_LABELS


def day_of_hall(hall: str) -> int:
    """馆 → 参观日；与 M4 一致，1 号馆群第一天、2 号馆群第二天。"""
    return 1 if str(hall).endswith("1") else 2


def appointment_id(ref: str) -> str:
    return f"AP-{ref}"


def takeover_id(appointment: str) -> str:
    return f"T-{appointment}"


# ---------- 预约生成 ----------
def build_appointments(exhibitors: list[dict],
                       people: list[dict]) -> tuple[list[dict], list[dict]]:
    """可排程展商 → 预约；返回 (预约, 未能约上的展商)。"""
    team_by_name = {person["name"]: person
                    for person in people if not person.get("external")}
    groups: dict[tuple, list[dict]] = {}
    skipped: list[dict] = []
    for record in exhibitors:
        # dup / isolated 已在名录账里记过，不参与预约；unplaced 要单独说明为什么没约上
        if record.get("status") not in ("ok", "unplaced"):
            continue
        position = position_of(record)
        if record.get("status") == "unplaced" or position is None:
            skipped.append({"ref": record.get("ex_id") or "",
                            "name": record.get("name") or "",
                            "hall": record.get("hall") or "",
                            "code": "P_NO_POSITION",
                            "note": "面积型或没有网格坐标，无法预约"})
            continue
        day = day_of_hall(record["hall"])
        owner_name = (record.get("owner") or "").strip()
        person = team_by_name.get(owner_name)
        owner_key = person["id"] if person else ("X-" + (owner_name or "未指派"))
        groups.setdefault((day, owner_key, owner_name), []).append({
            "ref": record["ex_id"], "name": record["name"], "hall": record["hall"],
            "booth_code": record.get("booth_code") or "",
            "priority": int(record.get("priority") or 5),
            "pos": position, "owner_name": owner_name,
            "owner_id": person["id"] if person else "",
            "owner_external": person is None,
            "row_ref": record.get("row_ref") or "",
        })

    appointments: list[dict] = []
    for (day, _owner_key, _owner_name), records in groups.items():
        records.sort(key=lambda item: (item["priority"], _hall_index(item["hall"]),
                                       item["booth_code"], item["ref"]))
        open_min, close_min = config.SHOW_HOURS[day]
        clock = open_min
        prev_hall, prev_pos = "", ENTRY
        for record in records:
            walk = distance.segment(prev_hall, prev_pos, record["hall"], record["pos"])
            arrive = max(clock + distance.walk_minutes(walk), open_min,
                         preferred_start(record["priority"]))
            end = arrive + DWELL_MIN
            if end > close_min:
                skipped.append({"ref": record["ref"], "name": record["name"],
                                "hall": record["hall"], "code": "D_BOOKING_FULL",
                                "note": f"第 {day} 天该对接人的可约时段已排到闭馆"})
                continue
            appointments.append({
                "appointment_id": appointment_id(record["ref"]),
                "ref": record["ref"], "name": record["name"],
                "hall": record["hall"], "booth_code": record["booth_code"],
                "day": day, "slot_start": arrive, "slot_end": end,
                "dwell_min": DWELL_MIN, "pos": record["pos"],
                "priority": record["priority"],
                "owner_id": record["owner_id"],
                "owner_name": record["owner_name"] or "未指派",
                "owner_external": record["owner_external"],
                "row_ref": record["row_ref"],
                "name_key": normalize.normalize_name(record["name"]),
            })
            clock = end
            prev_hall, prev_pos = record["hall"], record["pos"]
    appointments.sort(key=lambda item: (item["day"], item["slot_start"],
                                        item["appointment_id"]))
    return appointments, skipped


def _hall_index(hall: str) -> int:
    return (config.HALL_ORDER.index(hall) if hall in config.HALL_ORDER
            else len(config.HALL_ORDER))


def preferred_start(priority: int) -> int:
    """优先级 1 约在上午首选时段开始，2 约在下午首选时段，其余不限。"""
    if priority <= 1:
        return config.PREFERRED_WINDOW[1][0]
    if priority == 2:
        return 13 * 60
    return 0


def position_of(record: dict) -> tuple | None:
    if record.get("row_no") and record.get("col_no"):
        return geometry.position(record["hall"], record["row_no"], record["col_no"])
    return None


# ---------- 完成记录对位 ----------
def index_records(records: list[dict],
                  appointments: list[dict]) -> tuple[dict, list[dict]]:
    """完成记录 → {appointment_id: record}；对不上的记录逐条留证。"""
    by_booth = {item["booth_code"]: item for item in appointments if item["booth_code"]}
    by_name: dict[str, list[dict]] = {}
    for item in appointments:
        by_name.setdefault(item["name_key"], []).append(item)
    for items in by_name.values():
        items.sort(key=lambda item: (item["day"], item["slot_start"]))

    matched: dict[str, dict] = {}
    issues: list[dict] = []
    for record in records:
        if record.get("status") == "duplicate":
            continue
        target = None
        if record.get("booth_code"):
            target = by_booth.get(record["booth_code"])
        if target is None and record.get("exhibitor_name"):
            key = normalize.normalize_name(record["exhibitor_name"])
            candidates = by_name.get(key) or []
            if candidates:
                target = candidates[0]
        if target is None:
            record["status"] = "unknown"
            record["code"] = "D_UNKNOWN_REF"
            issues.append({
                "code": "D_UNKNOWN_REF", "stage": "duty", "level": "error",
                "subject": record["record_id"], "field": "completions",
                "raw_value": record.get("booth_raw") or record.get("exhibitor_name"),
                "detail": "完成记录指向的展商不在本批次预约里",
                "source_url": record.get("source"), "row_ref": record.get("row_ref"),
            })
            continue
        if target["appointment_id"] in matched:
            issues.append({
                "code": "D_DUPLICATE_RECORD", "stage": "duty", "level": "warn",
                "subject": record["record_id"], "field": "completions",
                "raw_value": target["appointment_id"],
                "detail": "同一预约出现多条完成记录，取最早一条",
                "source_url": record.get("source"), "row_ref": record.get("row_ref"),
            })
            continue
        matched[target["appointment_id"]] = record
    return matched, issues


# ---------- 分工 ----------
def assign(appointments: list[dict], people: list[dict],
           completed: dict[str, dict] | None = None,
           carried: dict[str, dict] | None = None) -> dict:
    """把每个预约落到一个人身上。

    completed：本次完成记录按预约号索引；carried：上一版已完成、本次记录里
    没有再出现的预约（续跑时沿用）。两者都已成事实，不再重排。
    """
    completed = completed or {}
    carried = carried or {}
    team = [person for person in people if not person.get("external")]
    team_by_id = {person["id"]: person for person in team}
    team_by_name = {person["name"]: person for person in team}
    outsiders: dict[str, dict] = {}
    plans: dict[tuple, list[dict]] = {}

    factual: dict[str, dict] = {}
    for appointment in appointments:
        record = completed.get(appointment["appointment_id"])
        if record is not None:
            factual[appointment["appointment_id"]] = {
                "record": record, "source": "record", "carried": False}
            continue
        previous = carried.get(appointment["appointment_id"])
        if previous is not None:
            factual[appointment["appointment_id"]] = {
                "record": previous, "source": "carried", "carried": True}

    resolved: list[dict] = []
    takeovers: list[dict] = []
    public = []   # 需要出现在个人行程里的行

    for appointment in appointments:
        item = factual.get(appointment["appointment_id"])
        if item is None:
            continue
        record = item["record"]
        person_name = (record.get("person_name") or record.get("person") or "").strip()
        carry = bool(item["carried"])
        if carry:
            record = {**record, "status": "carried",
                      "code": record.get("code") or "D_RESUMED"}
        person = team_by_name.get(person_name)
        if person is None:
            person = outsiders.setdefault(
                person_name or "外组", roster.external_person(person_name or "外组"))
        arrival = _completed_minute(appointment, record)
        row = _row(appointment, person, DONE, arrival, arrival,
                   arrival + appointment["dwell_min"], record_id=record.get("record_id"))
        row["note"] = "沿用上一版已完成" if item["carried"] else "现场完成记录"
        public.append(row)
        bucket = plans.setdefault((person["id"], appointment["day"]), [])
        bucket.append(row)
        bucket.sort(key=lambda item: (item["arrival_min"], item["appointment_id"]))
        resolved.append(_appointment_row(
            appointment, DONE, person, record_id=record.get("record_id"),
            code="D_RESUMED" if item["carried"] else None, note=row["note"]))
        if person_name and person_name != appointment["owner_name"]:
            takeovers.append(_takeover_row(
                appointment, person,
                source="carried" if item["carried"] else "record",
                reason_code="D_LATE_TAKEOVER",
                reason=(f"原对接人 {appointment['owner_name']} 未完成，"
                        f"现场已由 {person_name} 完成"),
                record_id=record.get("record_id")))

    takeover_count = {person["id"]: 0 for person in team}
    # 先排原对接人，再排接管：同一时段几个预约抢同一个人时，谁的场次只有自己接得住，
    # 谁先落人才排得出更连续的路线（同权重内按到站最早、工号确定，与输入行序无关）。
    for appointment in appointments:
        if appointment["appointment_id"] in factual:
            continue
        owner = team_by_id.get(appointment["owner_id"])
        arrival = _fits(owner, appointment, plans) if owner is not None else None
        if arrival is not None:
            row = _row(appointment, owner, OWNER, appointment["slot_start"],
                       arrival, arrival + appointment["dwell_min"])
            public.append(row)
            resolved.append(_appointment_row(
                appointment, OWNER, owner, code="D_OWNER_KEPT",
                note="原对接人按约到站"))
            _insert(plans, owner, row)
            factual[appointment["appointment_id"]] = {"record": None,
                                                      "source": "owner"}

    for appointment in appointments:
        if appointment["appointment_id"] in factual:
            continue
        owner = team_by_id.get(appointment["owner_id"])
        reason_code = _owner_miss_reason(owner, appointment)
        candidates = []
        for person in team:
            if owner is not None and person["id"] == owner["id"]:
                continue
            arrival = _fits(person, appointment, plans)
            if arrival is None:
                continue
            candidates.append((takeover_count[person["id"]], arrival,
                               person["id"], person))
        if not candidates:
            resolved.append(_appointment_row(
                appointment, UNCOVERED, None, code="D_UNCOVERED",
                note="没有同事的在场窗口与行程能覆盖该时段，预约保留待人工安排"))
            continue
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        takeover_count[candidates[0][3]["id"]] += 1
        chosen, arrival = candidates[0][3], candidates[0][1]
        row = _row(appointment, chosen, TAKEOVER, appointment["slot_start"],
                   arrival, arrival + appointment["dwell_min"])
        public.append(row)
        resolved.append(_appointment_row(
            appointment, TAKEOVER, chosen, code=reason_code,
            note=_miss_text(reason_code, appointment, chosen)))
        takeovers.append(_takeover_row(
            appointment, chosen, source="attendance", reason_code=reason_code,
            reason=_miss_text(reason_code, appointment, chosen)))
        _insert(plans, chosen, row)
        factual[appointment["appointment_id"]] = {"record": None,
                                                  "source": "takeover"}

    itinerary = _itinerary(public)
    resolved.sort(key=lambda row: (row["day"], row["slot_start"], row["appointment_id"]))
    takeovers.sort(key=lambda row: (row["day"], row["slot_start"], row["appointment_id"]))
    return {"appointments": resolved, "itinerary": itinerary, "takeovers": takeovers}


def _completed_minute(appointment: dict, record: dict) -> int:
    """完成时刻折算成当天分钟；写坏了就退回预约时段，不让分工中断。"""
    value = record.get("completed_utc") or record.get("completed_at")
    local = windows.to_local(value) if value else None
    open_min, close_min = config.SHOW_HOURS[appointment["day"]]
    fallback = appointment["slot_start"]
    if local is None or windows.day_of(value) != appointment["day"]:
        return fallback
    minute = local.hour * 60 + local.minute
    latest = close_min - appointment["dwell_min"]
    if minute < open_min or minute > latest:
        return fallback
    return minute


def _itinerary(rows: list[dict]) -> list[dict]:
    """按人按天排序、补步行段，输出可落库的个人行程。

    已完成的行是事实，不改时刻；其余行按「已约开始」与「上一场结束 + 步行」取晚者到站。
    """
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["person_id"], row["day"]), []).append(row)
    out: list[dict] = []
    for (_person_id, day), items in grouped.items():
        items.sort(key=lambda item: (item["arrival_min"], item["appointment_id"]))
        open_min = config.SHOW_HOURS[day][0]
        prev_hall, prev_pos, prev_depart = "", ENTRY, open_min
        for index, row in enumerate(items, start=1):
            walk = distance.segment(prev_hall, prev_pos, row["hall"], row["pos"])
            walk_min = distance.walk_minutes(walk)
            if row["role"] != DONE:
                arrive = max(row["slot_start"], prev_depart + walk_min)
                row["arrival_min"] = arrive
                row["arrival_utc"] = _utc(day, arrive)
                row["depart_min"] = arrive + row["dwell_min"]
                row["depart_utc"] = _utc(day, row["depart_min"])
            row["walk_m"] = walk
            row["walk_min"] = walk_min
            row["seq"] = index
            prev_hall, prev_pos = row["hall"], row["pos"]
            prev_depart = row["depart_min"]
            out.append(row)
    out.sort(key=lambda row: (row["person_id"], row["day"], row["seq"]))
    return out


def _fits(person: dict, appointment: dict, plans: dict) -> int | None:
    """人在该时段是否接得下；接得下返回到站分钟，接不下返回 None。"""
    if person is None:
        return None
    day = appointment["day"]
    start = appointment["slot_start"]
    end = appointment["slot_end"]
    if not roster.covers(person, day, start, appointment["dwell_min"]):
        return None
    rows = plans.get((person["id"], day), [])
    # 已完成的行可能就是这段的真实占用，先按实际时段判重叠（左闭右开）
    for row in rows:
        if row["arrival_min"] < end and row["depart_min"] > start:
            return None
    before = [row for row in rows if row["depart_min"] <= start + GRACE_MIN]
    after = [row for row in rows if row["arrival_min"] >= end - GRACE_MIN]
    if before:
        prev = max(before, key=lambda row: (row["depart_min"], row["appointment_id"]))
        walk = distance.segment(prev["hall"], prev["pos"],
                                appointment["hall"], appointment["pos"])
        arrive = prev["depart_min"] + distance.walk_minutes(walk)
    else:
        open_min = config.SHOW_HOURS[day][0]
        walk = distance.segment("", ENTRY, appointment["hall"], appointment["pos"])
        arrive = open_min + distance.walk_minutes(walk)
    arrive = max(arrive, start)
    if arrive > start + GRACE_MIN:
        return None
    depart = arrive + appointment["dwell_min"]
    if depart > end + GRACE_MIN:
        return None
    if after:
        nxt = min(after, key=lambda row: (row["arrival_min"], row["appointment_id"]))
        walk = distance.segment(appointment["hall"], appointment["pos"],
                                nxt["hall"], nxt["pos"])
        if depart + distance.walk_minutes(walk) > nxt["arrival_min"] + GRACE_MIN:
            return None
    return arrive


def _insert(plans: dict, person: dict, row: dict) -> None:
    rows = plans.setdefault((person["id"], row["day"]), [])
    rows.append(row)
    rows.sort(key=lambda item: (item["arrival_min"], item["appointment_id"]))


def _owner_miss_reason(owner: dict | None, appointment: dict) -> str:
    if owner is None:
        return "D_OWNER_EXTERNAL"
    window_start, window_end = roster.window_of(owner, appointment["day"])
    if window_start >= window_end:
        return "D_OWNER_ABSENT"
    if window_start > appointment["slot_start"]:
        return "D_OWNER_LATE"
    if appointment["slot_end"] > window_end:
        return "D_OWNER_LEFT"
    return "D_OWNER_TRAVEL"


def _miss_text(code: str, appointment: dict, chosen: dict) -> str:
    slot = (f"第 {appointment['day']} 天 {roster.hhmm(appointment['slot_start'])}"
            f"–{roster.hhmm(appointment['slot_end'])}")
    owner = appointment["owner_name"]
    if code == "D_OWNER_EXTERNAL":
        head = f"原对接人 {owner} 不在五人名单"
    elif code == "D_OWNER_ABSENT":
        head = f"原对接人 {owner} 当天不在馆"
    elif code == "D_OWNER_LATE":
        head = (f"原对接人 {owner} 晚于 "
                f"{roster.hhmm(appointment['slot_start'])} 才到馆")
    elif code == "D_OWNER_LEFT":
        head = (f"原对接人 {owner} 早于 "
                f"{roster.hhmm(appointment['slot_end'])} 已离馆")
    else:
        head = f"原对接人 {owner} 行程赶不上 {roster.hhmm(appointment['slot_start'])}"
    return f"{head}，{slot} 由 {chosen['name']} 接管"


def _row(appointment: dict, person: dict, role: str, slot_start: int,
         arrival: int, depart: int, record_id: str | None = None) -> dict:
    return {
        "person_id": person["id"], "person_name": person["name"],
        "day": appointment["day"], "seq": 0,
        "appointment_id": appointment["appointment_id"],
        "ref": appointment["ref"], "name": appointment["name"],
        "hall": appointment["hall"], "booth_code": appointment["booth_code"],
        "pos": appointment["pos"],
        "slot_start": slot_start, "slot_end": appointment["slot_end"],
        "arrival_min": arrival, "depart_min": depart,
        "arrival_utc": _utc(appointment["day"], arrival),
        "depart_utc": _utc(appointment["day"], depart),
        "dwell_min": appointment["dwell_min"],
        "walk_m": Decimal("0.0"), "walk_min": 0,
        "role": role, "state": role,
        "owner_name": appointment["owner_name"],
        "record_id": record_id, "note": "",
    }


def _appointment_row(appointment: dict, role: str, person: dict | None, *,
                     code: str | None, note: str,
                     record_id: str | None = None) -> dict:
    return {
        "appointment_id": appointment["appointment_id"],
        "ref": appointment["ref"], "name": appointment["name"],
        "hall": appointment["hall"], "booth_code": appointment["booth_code"],
        "day": appointment["day"],
        "slot_start": appointment["slot_start"], "slot_end": appointment["slot_end"],
        "slot_start_utc": _utc(appointment["day"], appointment["slot_start"]),
        "slot_end_utc": _utc(appointment["day"], appointment["slot_end"]),
        "dwell_min": appointment["dwell_min"],
        "owner_id": appointment["owner_id"], "owner_name": appointment["owner_name"],
        "assignee_id": person["id"] if person else "",
        "assignee_name": person["name"] if person else "",
        "role": role, "state": role,
        "code": code, "record_id": record_id, "note": note,
    }


def _takeover_row(appointment: dict, person: dict, *, source: str,
                  reason_code: str, reason: str,
                  record_id: str | None = None) -> dict:
    return {
        "takeover_id": takeover_id(appointment["appointment_id"]),
        "appointment_id": appointment["appointment_id"],
        "ref": appointment["ref"], "name": appointment["name"],
        "day": appointment["day"],
        "from_person": appointment["owner_name"],
        "to_person": person["name"],
        "reason_code": reason_code, "reason": reason,
        "slot_start": appointment["slot_start"], "slot_end": appointment["slot_end"],
        "slot_start_utc": _utc(appointment["day"], appointment["slot_start"]),
        "slot_end_utc": _utc(appointment["day"], appointment["slot_end"]),
        "source": source, "record_id": record_id, "note": "",
    }


def _utc(day: int, minutes: int) -> str:
    return windows.at_local(day, minutes).isoformat()


# ---------- 对账 ----------
def reconcile(appointments: list[dict], itinerary: list[dict],
              takeovers: list[dict], records: list[dict],
              skipped_booking: list[dict]) -> dict:
    """四本账：预约、个人行程、接管、完成记录。每本左右都能逐条对上。"""
    roles = {OWNER: 0, TAKEOVER: 0, DONE: 0, UNCOVERED: 0}
    done_by_owner = 0
    done_by_other = 0
    for row in appointments:
        roles[row["role"]] = roles.get(row["role"], 0) + 1
        if row["role"] == DONE:
            if row["assignee_name"] == row["owner_name"]:
                done_by_owner += 1
            else:
                done_by_other += 1
    placed = roles[OWNER] + roles[TAKEOVER] + roles[DONE]
    per_person: dict[str, int] = {}
    for row in itinerary:
        per_person[row["person_name"]] = per_person.get(row["person_name"], 0) + 1
    valid = len([r for r in records if r.get("status") == "done"])
    duplicate = len([r for r in records if r.get("status") == "duplicate"])
    unknown = len([r for r in records if r.get("status") == "unknown"])
    carried_records = len([r for r in records if r.get("status") == "carried"])
    record_right = (f"{valid} 有效 + {duplicate} 重复 + {unknown} 未知引用")
    if carried_records:
        record_right += f" + {carried_records} 续跑沿用"
    accounts = [
        {"account": "预约账", "left": len(appointments), "left_label": "预约场次",
         "right": (f"{roles[OWNER]} 原对接 + {roles[TAKEOVER]} 待接管 + "
                   f"{roles[DONE]} 已完成 + {roles[UNCOVERED]} 无人接管"),
         "right_total": placed + roles[UNCOVERED],
         "balanced": len(appointments) == placed + roles[UNCOVERED]},
        {"account": "行程账", "left": placed, "left_label": "已落人预约",
         "right": f"{len(itinerary)} 条个人行程（{len(per_person)} 人）",
         "right_total": len(itinerary),
         "balanced": len(itinerary) == placed},
        {"account": "接管账", "left": len(takeovers), "left_label": "接管记录",
         "right": f"{roles[TAKEOVER]} 待接管 + {done_by_other} 已由他人完成",
         "right_total": roles[TAKEOVER] + done_by_other,
         "balanced": len(takeovers) == roles[TAKEOVER] + done_by_other},
        {"account": "完成记录账", "left": len(records), "left_label": "现场记录",
         "right": record_right,
         "right_total": valid + duplicate + unknown + carried_records,
         "balanced": len(records) == valid + duplicate + unknown + carried_records},
    ]
    if skipped_booking:
        accounts.append({
            "account": "可约展商账", "left": len(appointments) + len(skipped_booking),
            "left_label": "可约展商",
            "right": f"{len(appointments)} 已约 + {len(skipped_booking)} 未约",
            "right_total": len(appointments) + len(skipped_booking), "balanced": True})
    return {
        "accounts": accounts,
        "all_balanced": all(item["balanced"] for item in accounts),
        "per_person": per_person,
        "overlaps": _overlaps(itinerary),
        "done_by_owner": done_by_owner,
        "done_by_other": done_by_other,
        "note": ("预约 = 原对接 + 待接管 + 已完成 + 无人接管；"
                 "行程 = 已落人预约；接管 = 待接管 + 已由他人完成"),
    }


def _overlaps(itinerary: list[dict]) -> list[dict]:
    rows: list[dict] = []
    grouped: dict[tuple, list[dict]] = {}
    for row in itinerary:
        grouped.setdefault((row["person_name"], row["day"]), []).append(row)
    for (person, day), items in sorted(grouped.items()):
        items = sorted(items, key=lambda item: (item["arrival_min"], item["depart_min"]))
        for left, right in zip(items, items[1:]):
            if right["arrival_min"] < left["depart_min"]:
                rows.append({
                    "person": person, "day": day,
                    "left": left["appointment_id"], "right": right["appointment_id"],
                    "detail": (f"{left['ref']} 到 {roster.hhmm(left['depart_min'])}，"
                               f"{right['ref']} 已于 {roster.hhmm(right['arrival_min'])} 到站")})
    return rows


def person_summary(people: list[dict], itinerary: list[dict],
                   takeovers: list[dict]) -> list[dict]:
    rows = []
    by_person: dict[str, list[dict]] = {}
    for row in itinerary:
        by_person.setdefault(row["person_name"], []).append(row)
    incoming: dict[str, int] = {}
    outgoing: dict[str, int] = {}
    for row in takeovers:
        incoming[row["to_person"]] = incoming.get(row["to_person"], 0) + 1
        outgoing[row["from_person"]] = outgoing.get(row["from_person"], 0) + 1
    for person in people:
        items = by_person.get(person["name"], [])
        walk = sum((row["walk_m"] for row in items), Decimal("0"))
        rows.append({
            "id": person["id"], "name": person["name"], "stops": len(items),
            "days": len({row["day"] for row in items}),
            "walk_m": str(geometry._q(walk)),
            "takeovers_in": incoming.get(person["name"], 0),
            "takeovers_out": outgoing.get(person["name"], 0),
            "first": roster.hhmm(min((row["arrival_min"] for row in items),
                                     default=None)),
            "last": roster.hhmm(max((row["depart_min"] for row in items),
                                    default=None)),
            "note": person.get("note", ""),
        })
    return rows
