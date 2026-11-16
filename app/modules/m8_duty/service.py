"""现场分工编排：批次预约 → 完成记录 → 分工 → 整批原子落库 → 版本与续跑。

三条边界写死在这里：

- **整批生效**：新版用新的 duty_id，一次事务写入预约、个人行程、接管、完成记录
  与版本行；任一步失败整体回滚，旧版的行一条不动，页面继续看到上一版安排。
- **续跑**：上一版已完成的场次是事实，本次记录里没有再出现的也照原人原时刻沿用，
  只重排剩余场次；版本号 +1，记 resume_of。
- **陈旧提交拒绝**：提交版本小于同一 duty_key 的最新版本时 409，旧安排不被覆盖。
"""
from __future__ import annotations

import hashlib
import json

from ... import errors
from . import planner, records as records_mod, roster


def duty_key_for(batch_id: str, records_digest: str, people: list[dict]) -> str:
    """分工键 = 批次 + 记录摘要 + 在场窗口快照；任一变化就是新键。"""
    window = json.dumps(roster.stores_days(people), sort_keys=True,
                        ensure_ascii=False)
    digest = hashlib.sha256(f"{batch_id}\n{records_digest}\n{window}"
                            .encode("utf-8")).hexdigest()
    return f"{batch_id}:{digest[:16]}"


def run(settings, store, batch_id: str | None, *, records_text: str | None = None,
        attendance: list[dict] | None = None, mode: str = "full",
        resume_of: str | None = None,
        submitted_version: int | None = None) -> dict:
    if mode not in ("full", "resume"):
        raise errors.BadRequest(
            f"mode 只支持 full / resume，收到 {mode!r}", code="E_BAD_DUTY_MODE")
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch()
    if batch is None:
        raise errors.NotFound("没有可用的批次，先调用 POST /api/crawl",
                              code="E_NO_BATCH")
    if batch["status"] != "done":
        raise errors.BadRequest(
            f"批次 {batch['batch_id']} 还没完成，不能排分工", code="E_BATCH_NOT_DONE")
    exhibitors = store.load_exhibitors(batch["batch_id"])
    if not exhibitors:
        raise errors.BadRequest(
            f"批次 {batch['batch_id']} 里没有展商，无法生成预约", code="E_NO_APPOINTMENT")

    previous = None
    if resume_of:
        previous = store.get_duty(resume_of)
        if previous is None:
            raise errors.NotFound(f"分工 {resume_of} 不存在", code="E_NOT_FOUND")
        if previous["batch_id"] != batch["batch_id"]:
            raise errors.BadRequest(
                f"分工 {resume_of} 属于批次 {previous['batch_id']}，与本次批次不一致",
                code="E_DUTY_BATCH_MISMATCH")
        _guard(store, previous, submitted_version)
        mode = "resume"

    people = roster.build_roster(attendance)
    # None = 没给记录，用样例站那份；"" = 明确给了一份空记录（续跑时常见）
    text = records_text if records_text is not None else records_mod.sample_text(settings)
    record_set = records_mod.read(text, source="inline" if records_text is not None
                                  else "sample")
    appointments, skipped_booking = planner.build_appointments(exhibitors, people)
    matched, record_issues = planner.index_records(record_set.rows, appointments)
    carried = _carried_appointments(store, previous) if previous else {}

    assigned = planner.assign(appointments, people, matched, carried)
    all_records = list(record_set.rows) + _carried_records(
        carried, matched, appointments)
    issues = list(record_set.issues) + record_issues

    if previous is not None:
        # 续跑落在同一条版本链上：键沿用上一版，版本号 +1
        duty_key = previous["duty_key"]
        version = int(previous.get("version", 1)) + 1
    else:
        duty_key = duty_key_for(batch["batch_id"],
                                records_mod.record_digest(all_records), people)
        version = store.next_duty_version(duty_key)
    duty_id = _new_duty_id()
    created = manifest_run_iso()
    params = {"mode": mode, "attendance": attendance or [],
              "records_source": record_set.source,
              "records_digest": records_mod.record_digest(all_records)}
    reconciliation = planner.reconcile(
        assigned["appointments"], assigned["itinerary"], assigned["takeovers"],
        all_records, skipped_booking)
    params["reconciliation"] = reconciliation
    params["people"] = [{"id": person["id"], "name": person["name"],
                         "days": {str(day): list(span)
                                  for day, span in sorted(person["days"].items())},
                         "note": person.get("note", ""),
                         "external": person.get("external", False)}
                        for person in people]
    params["issues"] = issues
    params["skipped_booking"] = skipped_booking
    duty = {
        "duty_id": duty_id, "batch_id": batch["batch_id"], "duty_key": duty_key,
        "version": version, "status": "done", "created_at": created,
        "finished_at": manifest_run_iso(), "resume_of": resume_of,
        "params": params, "note": "续跑" if resume_of else "",
    }
    store.save_duty(duty=duty, appointments=assigned["appointments"],
                    itinerary=assigned["itinerary"],
                    takeovers=assigned["takeovers"], records=all_records)
    return {
        "status": "done",
        "duty": duty,
        "batch": batch,
        "people": people,
        "appointments": assigned["appointments"],
        "itinerary": assigned["itinerary"],
        "takeovers": assigned["takeovers"],
        "records": all_records,
        "issues": issues,
        "reconciliation": reconciliation,
        "skipped_booking": skipped_booking,
        "person_summary": planner.person_summary(
            people, assigned["itinerary"], assigned["takeovers"]),
        "resume_of": resume_of,
    }


def _guard(store, previous: dict, submitted: int | None) -> None:
    latest = store.next_duty_version(previous["duty_key"]) - 1
    if submitted is not None and submitted < latest:
        raise errors.Conflict(
            f"提交版本 v{submitted} 早于当前 v{latest}，已拒绝",
            code="D_STALE_REJECTED",
            detail={"submitted": submitted, "latest": latest})


def _carried_appointments(store, previous: dict | None) -> dict[str, dict]:
    """上一版已完成的预约 → 本次沿用；记录里重新出现的以我为准。"""
    if previous is None:
        return {}
    rows = store.load_duty_itinerary(previous["duty_id"])
    carried: dict[str, dict] = {}
    for row in rows:
        if row["role"] != planner.DONE and row["state"] != planner.DONE:
            continue
        carried[row["appointment_id"]] = {
            "record_id": row.get("record_id") or f"v{previous['version']}-{row['appointment_id']}",
            "person_name": row["person_name"],
            "completed_at": row["arrival_utc"],
            "completed_utc": row["arrival_utc"],
            "status": "done",
        }
    return carried


def _carried_records(carried: dict[str, dict], matched: dict[str, dict],
                     appointments: list[dict]) -> list[dict]:
    """续跑时把沿用场次补成记录行，让完成记录账与版本链对得上。"""
    by_id = {item["appointment_id"]: item for item in appointments}
    rows = []
    for appointment_id, record in sorted(carried.items()):
        if appointment_id in matched or appointment_id not in by_id:
            continue
        appointment = by_id[appointment_id]
        rows.append({
            "record_id": record["record_id"], "ref": appointment["ref"],
            "booth_code": appointment.get("booth_code") or "",
            "exhibitor_name": appointment["name"],
            "booth_raw": appointment.get("booth_code") or "",
            "person_name": record["person_name"],
            "completed_at": record["completed_at"],
            "completed_utc": record.get("completed_utc") or record["completed_at"],
            "note": "续跑沿用的上一版完成记录", "appointment_key": appointment_id,
            "status": "carried", "code": "D_RESUMED",
            "row_ref": "", "source": "state",
        })
    return rows


def _new_duty_id() -> str:
    from ..m5_batches import history

    return "D" + history.new_batch_id()[1:]


def manifest_run_iso() -> str:
    from ..m5_batches import history

    return history.now_iso()


# ---------- 读取与视图 ----------
def detail(store, duty_id: str) -> dict | None:
    duty = store.get_duty(duty_id)
    if duty is None:
        return None
    params = json.loads(duty.get("params_json") or "{}")
    appointments = store.load_duty_appointments(duty_id)
    itinerary = store.load_duty_itinerary(duty_id)
    takeovers = store.load_duty_takeovers(duty_id)
    records = store.load_duty_records(duty_id)
    reconciliation = params.get("reconciliation") or {"accounts": [],
                                                      "all_balanced": False}
    return {
        "duty": duty,
        "params": params,
        "appointments": appointments,
        "itinerary": itinerary,
        "takeovers": takeovers,
        "records": records,
        "reconciliation": reconciliation,
        "versions": store.duty_versions(duty["duty_key"]),
    }


def appointments_by_day(appointments: list[dict]) -> list[dict]:
    groups: list[dict] = []
    for row in appointments:
        if not groups or groups[-1]["day"] != row["day"]:
            groups.append({"day": row["day"], "rows": []})
        groups[-1]["rows"].append(row)
    return groups


def itinerary_groups(itinerary: list[dict]) -> list[dict]:
    """页面用：先按人分块，再按天分块。"""
    people: list[dict] = []
    for row in itinerary:
        item = next((p for p in people if p["person_id"] == row["person_id"]), None)
        if item is None:
            item = {"person_id": row["person_id"],
                    "person_name": row["person_name"], "days": []}
            people.append(item)
        day_item = next((d for d in item["days"] if d["day"] == row["day"]), None)
        if day_item is None:
            day_item = {"day": row["day"], "rows": []}
            item["days"].append(day_item)
        day_item["rows"].append(row)
    return people


def appointment_rows(appointments: list[dict]) -> list[dict]:
    rows = []
    for row in appointments:
        rows.append({
            **row,
            "slot": f"{roster.hhmm(row['slot_start'])}–{roster.hhmm(row['slot_end'])}",
            "role_label": planner.ROLE_LABELS.get(row["role"], row["role"]),
            "assignee": row["assignee_name"] or "—",
        })
    return rows


def itinerary_rows(itinerary: list[dict]) -> list[dict]:
    rows = []
    for row in itinerary:
        rows.append({
            **row,
            "slot": f"{roster.hhmm(row['arrival_min'])}–{roster.hhmm(row['depart_min'])}",
            "role_label": planner.ROLE_LABELS.get(row["role"], row["role"]),
        })
    return rows


def takeover_rows(takeovers: list[dict]) -> list[dict]:
    from ... import reasons

    rows = []
    for row in takeovers:
        rows.append({
            **row,
            "slot": (f"{roster.hhmm(row['slot_start'])}–"
                     f"{roster.hhmm(row['slot_end'])}"),
            "reason_title": reasons.label(row["reason_code"]),
        })
    return rows


def summary(payload: dict) -> dict:
    appointments = payload["appointments"]
    counts = {planner.OWNER: 0, planner.TAKEOVER: 0, planner.DONE: 0,
              planner.UNCOVERED: 0}
    for row in appointments:
        counts[row["role"]] = counts.get(row["role"], 0) + 1
    return {
        "duty_id": payload["duty"]["duty_id"],
        "version": payload["duty"].get("version", 1),
        "resume_of": payload["duty"].get("resume_of"),
        "appointments": len(appointments),
        "owner": counts[planner.OWNER],
        "takeover_pending": counts[planner.TAKEOVER],
        "done": counts[planner.DONE],
        "uncovered": counts[planner.UNCOVERED],
        "takeovers": len(payload["takeovers"]),
        "records": len(payload["records"]),
        "people": len(payload["people"]),
        "balanced": payload["reconciliation"]["all_balanced"],
    }
