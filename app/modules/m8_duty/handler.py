"""M8 入口：排现场分工、按分工号查看、个人行程 / 预约 / 接管三张表与页面。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ... import errors
from ...web import Ctx, get_ctx, render
from . import planner, records as records_mod, roster, service

router = APIRouter(tags=["M8 现场分工"])


@router.post("/api/duty")
def api_duty(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    """排一版分工：new_version / resume 两种模式，整批原子生效。"""
    body = payload or {}
    mode = str(body.get("mode") or "full")
    if mode not in ("full", "new_version", "resume"):
        raise errors.BadRequest(
            f"mode 只支持 new_version / resume，收到 {mode!r}", code="E_BAD_DUTY_MODE")
    normalized = "full" if mode == "new_version" else mode
    resume_of = body.get("resume_of")
    if normalized == "resume" and not resume_of:
        raise errors.BadRequest("resume 模式必须给 resume_of", code="E_BAD_DUTY_MODE")
    records_text = body.get("records")
    if records_text is not None and not isinstance(records_text, str):
        raise errors.BadRequest("records 必须是 CSV 文本", code="E_BAD_RECORDS")
    attendance = body.get("attendance")
    try:
        result = service.run(
            ctx.settings, ctx.store, body.get("batch_id"),
            records_text=records_text, attendance=attendance,
            mode=normalized, resume_of=resume_of,
            submitted_version=_int_or_none(body.get("version"),
                                           "version", "E_BAD_VERSION"))
    except ValueError as exc:
        raise errors.BadRequest(str(exc), code="E_BAD_ATTENDANCE") from None
    return payload_of(result)


@router.get("/api/duty")
def api_duty_latest(duty_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    duty = ctx.store.get_duty(duty_id) if duty_id else ctx.store.latest_duty()
    if duty is None:
        raise errors.NotFound("还没有分工，先 POST /api/duty", code="E_NO_DUTY")
    payload = service.detail(ctx.store, duty["duty_id"])
    return payload_of(_payload_shim(ctx.store, payload))


@router.get("/api/duty/roster")
def api_roster(ctx: Ctx = Depends(get_ctx)) -> dict:
    people = roster.base_roster()
    return {"team": roster.window_rows(people),
            "grace_min": planner.GRACE_MIN,
            "dwell_min": planner.DWELL_MIN,
            "note": "窗口为本地时刻左闭右开；名单外对接人的预约由本组接管"}


@router.get("/api/duty/records")
def api_records(duty_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    duty = ctx.store.get_duty(duty_id) if duty_id else ctx.store.latest_duty()
    if duty is None:
        raise errors.NotFound("还没有分工，先 POST /api/duty", code="E_NO_DUTY")
    records = ctx.store.load_duty_records(duty["duty_id"])
    return {"duty_id": duty["duty_id"], "count": len(records),
            "records": records_mod.record_rows(records)}


@router.get("/api/duty/itinerary.csv")
def api_duty_itinerary_csv(duty_id: str | None = None,
                           ctx: Ctx = Depends(get_ctx)) -> Response:
    duty = ctx.store.get_duty(duty_id) if duty_id else ctx.store.latest_duty()
    if duty is None:
        raise errors.NotFound("还没有分工，先 POST /api/duty", code="E_NO_DUTY")
    text = _itinerary_csv(ctx.store.load_duty_itinerary(duty["duty_id"]))
    return Response(content=text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{duty["duty_id"]}-duty.csv"'})


@router.get("/duty")
def page_duty(request: Request, duty_id: str | None = None,
              person: str = "", ctx: Ctx = Depends(get_ctx)):
    stored = ctx.store.get_duty(duty_id) if duty_id else ctx.store.latest_duty()
    payload = None
    people_blocks: list[dict] = []
    appointment_rows: list[dict] = []
    takeover_rows: list[dict] = []
    record_rows: list[dict] = []
    person_options: list[tuple] = [("", "全部")]
    summary = {}
    reconciliation = None
    if stored is not None:
        detail = service.detail(ctx.store, stored["duty_id"])
        payload = _payload_shim(ctx.store, detail)
        summary = service.summary(payload)
        reconciliation = detail["reconciliation"]
        blocks = service.itinerary_groups(payload["itinerary"])
        seen: list[tuple] = []
        for block in blocks:
            if (block["person_id"], block["person_name"]) not in seen:
                seen.append((block["person_id"], block["person_name"]))
        person_options += seen
        if person:
            blocks = [block for block in blocks if block["person_id"] == person
                      or block["person_name"] == person]
        for block in blocks:
            for day in block["days"]:
                day["rows"] = service.itinerary_rows(day["rows"])
        people_blocks = blocks
        appointment_rows = service.appointment_rows(payload["appointments"])
        takeover_rows = service.takeover_rows(payload["takeovers"])
        record_rows = records_mod.record_rows(payload["records"])
    return render(request, "duty.html", batch=payload and payload["batch"],
                  page="duty", stored=stored, payload=payload, summary=summary,
                  reconciliation=reconciliation, people_blocks=people_blocks,
                  appointment_rows=appointment_rows, takeover_rows=takeover_rows,
                  record_rows=record_rows,
                  roster_rows=roster.window_rows(
                      payload["people"] if payload else roster.base_roster()),
                  person=person,
                  person_options=person_options,
                  duty_versions=stored and ctx.store.duty_versions(
                      stored["duty_key"]) or [],
                  page_title="现场分工")


def payload_of(result: dict) -> dict:
    """接口形状：结果 + 汇总，行明细保持可逐条核对。"""
    return {
        "status": result.get("status", result["duty"].get("status", "done")),
        "duty_id": result["duty"]["duty_id"],
        "duty_key": result["duty"]["duty_key"],
        "version": result["duty"].get("version", 1),
        "resume_of": result["duty"].get("resume_of"),
        "batch_id": result["batch"]["batch_id"],
        "summary": service.summary(result),
        "reconciliation": result["reconciliation"],
        "roster": roster.window_rows(result["people"]),
        "appointments": service.appointment_rows(result["appointments"]),
        "itinerary": service.itinerary_rows(result["itinerary"]),
        "takeovers": service.takeover_rows(result["takeovers"]),
        "records": records_mod.record_rows(result["records"]),
        "issues": result.get("issues", []),
        "skipped_booking": result.get("skipped_booking", []),
        "person_summary": result.get("person_summary", []),
    }


def _payload_shim(store, detail: dict) -> dict:
    """把落库明细拼成与 run() 结果同形的字典，页面与接口共用。"""
    duty = detail["duty"]
    batch = store.get_batch(duty["batch_id"]) or {"batch_id": duty["batch_id"]}
    params = detail["params"]
    return {
        "duty": duty, "batch": batch,
        "people": roster.from_snapshot(params.get("people")),
        "appointments": detail["appointments"],
        "itinerary": detail["itinerary"],
        "takeovers": detail["takeovers"],
        "records": detail["records"],
        "reconciliation": detail["reconciliation"],
        "issues": params.get("issues", []),
        "skipped_booking": params.get("skipped_booking", []),
        "person_summary": [],
    }


def _itinerary_csv(itinerary: list[dict]) -> str:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(("同事", "日期", "序号", "时段", "展商", "馆", "展位号",
                     "步行(米)", "角色", "原对接人", "记录号"))
    for row in itinerary:
        writer.writerow((
            row["person_name"], row["day"], row["seq"],
            f"{roster.hhmm(row['arrival_min'])}–{roster.hhmm(row['depart_min'])}",
            row["name"], row["hall"], row.get("booth_code") or "",
            f"{row['walk_m']:.1f}",
            planner.ROLE_LABELS.get(row["role"], row["role"]),
            row["owner_name"], row.get("record_id") or ""))
    return "﻿" + buffer.getvalue()


def _int_or_none(value, field: str, code: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise errors.BadRequest(f"{field} 必须是整数，收到 {value!r}",
                                code=code) from None
