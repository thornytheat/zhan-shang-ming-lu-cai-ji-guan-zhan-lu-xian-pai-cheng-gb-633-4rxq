"""M8 现场分工：在场窗口、预约生成、原对接人连续、接管、续跑、整批原子、三账。"""
from __future__ import annotations

import csv
import io
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app import config, errors, reasons
from app.modules.m8_duty import planner, records, roster, service
from app.store import Store

TEAM = ("张岚", "王覃", "李彤", "赵丰", "周蕙")
ROOT = pathlib.Path(__file__).resolve().parent.parent


def member(name, days):
    return {"id": "P" + str(TEAM.index(name) + 1), "name": name,
            "days": days, "note": "", "external": False}


def full_days():
    return {1: (9 * 60, 17 * 60 + 30), 2: (9 * 60, 16 * 60)}


def exhibitor(ref, name, hall, row, col, owner, *, priority=3):
    return {"ex_id": ref, "name": name, "hall": hall, "row_no": row, "col_no": col,
            "booth_code": f"{hall}-{row}-{col}", "priority": priority,
            "owner": owner, "status": "ok", "code": None, "row_ref": ref}


@pytest.fixture()
def duty_store(tmp_path_factory):
    """本文件专用的库与批次，避免与其他用例共用版本链。"""
    from app.modules.m5_batches import service as batch_service

    base = tmp_path_factory.mktemp("duty")
    settings = config.Settings(db_path=str(base / "duty.sqlite"),
                               eval_date=config.Settings().eval_date,
                               sample_site_dir=str(ROOT / "sample_site"),
                               sample_site_base="http://sample-site")
    store = Store(settings.db_path)
    result = batch_service.run(settings=settings, store=store, source="all")
    return {"settings": settings, "store": store, "batch": result["batch"]}


@pytest.fixture()
def first_duty(duty_store):
    return service.run(duty_store["settings"], duty_store["store"],
                       duty_store["batch"]["batch_id"])


# ---------- 在场窗口 ----------
def test_roster_windows_and_labels():
    people = roster.base_roster()
    assert [person["name"] for person in people] == list(TEAM)
    zhang = people[0]
    assert roster.window_of(zhang, 1) == (14 * 60 + 30, 17 * 60 + 30)
    # 左闭右开：14:30 起可接，17:30 结束不可接
    assert roster.covers(zhang, 1, 14 * 60 + 30, 20) is True
    assert roster.covers(zhang, 1, 14 * 60 + 10, 20) is False
    rows = roster.window_rows(people)
    assert rows[0]["day1"] == "14:30–17:30"
    assert rows[3]["day2"] == "09:00–13:00"


def test_roster_attendance_adjustments():
    people = roster.build_roster([
        {"name": "王覃", "day": 1, "end": "12:00"},
        {"name": "赵丰", "day": 2, "absent": True},
        {"name": "周蕙", "day": 1, "start": "11:00"},
    ])
    by_name = {person["name"]: person for person in people}
    assert roster.window_of(by_name["王覃"], 1) == (9 * 60, 12 * 60)
    assert roster.window_of(by_name["赵丰"], 2) == (0, 0)
    assert roster.covers(by_name["赵丰"], 2, 9 * 60, 20) is False
    assert roster.window_of(by_name["周蕙"], 1) == (11 * 60, 16 * 60)
    assert roster.window_of(by_name["王覃"], 2) == full_days()[2]


def test_roster_rejects_bad_attendance():
    with pytest.raises(ValueError):
        roster.build_roster([{"name": "不存在", "day": 1}])
    with pytest.raises(ValueError):
        roster.build_roster([{"name": "张岚", "day": 3}])
    with pytest.raises(ValueError):
        roster.build_roster([{"name": "张岚", "day": 1, "start": "18:00"}])
    with pytest.raises(ValueError):
        roster.build_roster([{"name": "张岚", "day": 1, "start": "9点"}])
    assert roster.parse_hhmm("09:05") == 545
    assert roster.hhmm(545) == "09:05"


# ---------- 预约 ----------
def test_appointments_booked_per_owner_without_gaps_in_time():
    people = roster.base_roster()
    rows = [exhibitor("E1", "甲", "A1", "01", "01", "张岚", priority=1),
            exhibitor("E2", "乙", "A1", "01", "02", "张岚", priority=1),
            exhibitor("E3", "丙", "A1", "01", "03", "张岚", priority=1)]
    appointments, skipped = planner.build_appointments(rows, people)
    assert not skipped
    starts = [item["slot_start"] for item in appointments]
    assert starts == sorted(starts)
    # 同一对接人连续约：后一场不早于前一场结束 + 步行分钟
    for left, right in zip(appointments, appointments[1:]):
        assert right["slot_start"] >= left["slot_end"]
        assert left["dwell_min"] == config.DWELL_EXHIBITOR_MIN
    # 优先级 1 的预约落在首选时段内
    assert appointments[0]["slot_start"] >= config.PREFERRED_WINDOW[1][0]
    assert appointments[0]["slot_start"] < config.PREFERRED_WINDOW[1][1]


def test_appointment_booked_on_hall_day_and_priority_order():
    people = roster.base_roster()
    rows = [exhibitor("E1", "甲", "B2", "01", "01", "孙溯", priority=5),
            exhibitor("E2", "乙", "B2", "02", "02", "孙溯", priority=1),
            exhibitor("E3", "丙", "A1", "01", "01", "张岚", priority=2)]
    appointments, _skipped = planner.build_appointments(rows, people)
    by_ref = {item["ref"]: item for item in appointments}
    assert by_ref["E2"]["day"] == 2 and by_ref["E1"]["day"] == 2
    assert by_ref["E3"]["day"] == 1
    assert [item["ref"] for item in appointments if item["day"] == 2] == ["E2", "E1"]


def test_unplaced_exhibitor_not_booked():
    people = roster.base_roster()
    rows = [{"ex_id": "E9", "name": "光地", "hall": "A1", "row_no": None,
             "col_no": None, "status": "unplaced", "priority": 1, "owner": "张岚"}]
    appointments, skipped = planner.build_appointments(rows, people)
    assert appointments == []
    assert skipped[0]["code"] == "P_NO_POSITION"


# ---------- 原对接 / 接管 ----------
def test_owner_keeps_slot_when_present():
    people = [member(name, full_days()) for name in TEAM]
    rows = [exhibitor("E1", "甲", "A1", "01", "01", "张岚"),
            exhibitor("E2", "乙", "A1", "02", "02", "张岚")]
    appointments, _ = planner.build_appointments(rows, people)
    result = planner.assign(appointments, people)
    assert all(row["role"] == planner.OWNER for row in result["appointments"])
    assert result["takeovers"] == []
    # 同一对接人自己连续完成，不需要换人
    assert {row["person_name"] for row in result["itinerary"]} == {"张岚"}


def test_late_owner_hands_over_slot_to_someone_who_can_arrive():
    # TRAP-31：晚到的原对接人只交出这一个时段，预约不作废也不改时段
    people = [member("张岚", {1: (14 * 60 + 30, 17 * 60 + 30), 2: full_days()[2]}),
              member("王覃", full_days()), member("李彤", full_days()),
              member("赵丰", full_days()), member("周蕙", full_days())]
    rows = [exhibitor("E1", "甲", "A1", "01", "01", "张岚", priority=1)]
    appointments, _ = planner.build_appointments(rows, people)
    assert appointments[0]["slot_start"] < 14 * 60 + 30
    result = planner.assign(appointments, people)
    row = result["appointments"][0]
    assert row["role"] == planner.TAKEOVER
    assert row["code"] == "D_OWNER_LATE"
    assert row["assignee_name"] != "张岚"
    # 预约时段没有因为换人而改动，也没有作废
    assert row["slot_start"] == appointments[0]["slot_start"]
    assert len(result["takeovers"]) == 1
    takeover = result["takeovers"][0]
    assert takeover["from_person"] == "张岚"
    assert takeover["to_person"] == row["assignee_name"]
    assert "晚于" in takeover["reason"]


def test_absent_owner_and_external_owner_are_taken_over():
    people = [member(name, full_days()) for name in TEAM]
    people[0]["days"][1] = (0, 0)          # 张岚第 1 天不在
    rows = [exhibitor("E1", "甲", "A1", "01", "01", "张岚"),
            exhibitor("E2", "乙", "A1", "02", "02", "外组同事")]
    appointments, _ = planner.build_appointments(rows, people)
    result = planner.assign(appointments, people)
    by_ref = {row["ref"]: row for row in result["appointments"]}
    assert by_ref["E1"]["role"] == planner.TAKEOVER
    assert by_ref["E1"]["code"] == "D_OWNER_ABSENT"
    assert by_ref["E2"]["role"] == planner.TAKEOVER
    assert by_ref["E2"]["code"] == "D_OWNER_EXTERNAL"


def test_uncovered_when_nobody_on_site():
    people = [member(name, full_days()) for name in TEAM]
    for person in people:
        person["days"][1] = (0, 0)
    rows = [exhibitor("E1", "甲", "A1", "01", "01", "张岚")]
    appointments, _ = planner.build_appointments(rows, people)
    result = planner.assign(appointments, people)
    row = result["appointments"][0]
    assert row["role"] == planner.UNCOVERED
    assert row["code"] == "D_UNCOVERED"
    assert row["assignee_id"] == ""
    # 预约还在，不随人离场作废
    assert row["slot_start"] == appointments[0]["slot_start"]
    assert result["itinerary"] == []


def test_completed_record_is_factual_even_if_owner_late():
    people = [member(name, full_days()) for name in TEAM]
    people[0]["days"][1] = (15 * 60, 17 * 60 + 30)
    rows = [exhibitor("E1", "甲", "A1", "01", "01", "张岚")]
    appointments, _ = planner.build_appointments(rows, people)
    text = ("记录号,展位号,展商名称,完成人,完成时间\n"
            "R1,A1-01-01,甲,张岚,2026-11-16 15:10\n")
    record_set = records.read(text)
    matched, _issues = planner.index_records(record_set.rows, appointments)
    result = planner.assign(appointments, people, matched)
    row = result["appointments"][0]
    assert row["role"] == planner.DONE
    assert row["assignee_name"] == "张岚"
    assert row["record_id"] == "R1"
    itinerary = result["itinerary"][0]
    assert itinerary["role"] == planner.DONE
    assert itinerary["arrival_min"] == 15 * 60 + 10


def test_duplicate_and_unknown_records_are_isolated():
    # TRAP-34：重复上报取最早一条，后报只留证
    people = [member(name, full_days()) for name in TEAM]
    rows = [exhibitor("E1", "甲", "A1", "01", "01", "张岚")]
    appointments, _ = planner.build_appointments(rows, people)
    text = ("记录号,展位号,展商名称,完成人,完成时间\n"
            "R1,A1-01-01,甲,张岚,2026-11-16 10:00\n"
            "R2,A1-01-01,甲,王覃,2026-11-16 10:05\n"
            "R3,Z9-99-99,无名,赵丰,2026-11-16 10:00\n")
    record_set = records.read(text)
    assert [row["status"] for row in record_set.rows] == ["done", "duplicate", "done"]
    # 重复上报在读取层就留证，且不会顶掉最早一条
    assert [item["code"] for item in record_set.issues] == ["D_DUPLICATE_RECORD"]
    matched, issues = planner.index_records(record_set.rows, appointments)
    codes = [item["code"] for item in issues]
    assert codes.count("D_UNKNOWN_REF") == 1
    assert matched["AP-E1"]["record_id"] == "R1"
    assert record_set.rows[2]["status"] == "unknown"


def test_no_two_appointments_overlap_for_same_person(duty_store):
    # TRAP-32：同一时段多单抢同一个人时按固定顺序定人，不给同一个人排两处
    result = service.run(duty_store["settings"], duty_store["store"],
                         duty_store["batch"]["batch_id"])
    grouped = {}
    for row in result["itinerary"]:
        grouped.setdefault((row["person_name"], row["day"]), []).append(row)
    for items in grouped.values():
        items = sorted(items, key=lambda row: row["arrival_min"])
        for left, right in zip(items, items[1:]):
            assert right["arrival_min"] >= left["depart_min"]
    assert result["reconciliation"]["overlaps"] == []


def test_itinerary_stays_inside_attendance_window(duty_store):
    result = service.run(duty_store["settings"], duty_store["store"],
                         duty_store["batch"]["batch_id"])
    people = {person["name"]: person for person in result["people"]}
    for row in result["itinerary"]:
        if row["role"] == planner.DONE:
            continue
        person = people[row["person_name"]]
        start, end = roster.window_of(person, row["day"])
        assert start <= row["arrival_min"]
        assert row["depart_min"] <= end


# ---------- 对账 ----------
def test_three_accounts_balance(duty_store):
    result = service.run(duty_store["settings"], duty_store["store"],
                         duty_store["batch"]["batch_id"])
    accounts = {row["account"]: row for row in result["reconciliation"]["accounts"]}
    assert {"预约账", "行程账", "接管账", "完成记录账"} <= set(accounts)
    assert all(row["balanced"] for row in accounts.values())
    roles = {planner.OWNER: 0, planner.TAKEOVER: 0, planner.DONE: 0,
             planner.UNCOVERED: 0}
    for row in result["appointments"]:
        roles[row["role"]] = roles.get(row["role"], 0) + 1
    assert accounts["预约账"]["left"] == (
        roles[planner.OWNER] + roles[planner.TAKEOVER] + roles[planner.DONE]
        + roles[planner.UNCOVERED])
    assert accounts["行程账"]["left"] == len(result["itinerary"])
    assert accounts["接管账"]["left"] == len(result["takeovers"])


def test_every_takeover_has_an_appointment(duty_store):
    result = service.run(duty_store["settings"], duty_store["store"],
                         duty_store["batch"]["batch_id"])
    by_id = {row["appointment_id"]: row for row in result["appointments"]}
    for row in result["takeovers"]:
        target = by_id[row["appointment_id"]]
        assert row["to_person"] in (target["assignee_name"], target["owner_name"])
        assert row["from_person"] == target["owner_name"]
        assert target["role"] in (planner.TAKEOVER, planner.DONE)


# ---------- 续跑 / 版本 / 整批原子 ----------
def test_resume_keeps_completed_and_bumps_version(duty_store):
    settings, store = duty_store["settings"], duty_store["store"]
    batch_id = duty_store["batch"]["batch_id"]
    first = service.run(settings, store, batch_id)
    done_before = {(row["appointment_id"], row["person_name"])
                   for row in first["itinerary"] if row["role"] == planner.DONE}
    assert done_before
    second = service.run(settings, store, batch_id, mode="resume",
                         resume_of=first["duty"]["duty_id"])
    assert second["duty"]["version"] == first["duty"]["version"] + 1
    assert second["duty"]["resume_of"] == first["duty"]["duty_id"]
    assert second["duty"]["duty_key"] == first["duty"]["duty_key"]
    done_after = {(row["appointment_id"], row["person_name"])
                  for row in second["itinerary"] if row["role"] == planner.DONE}
    assert done_before <= done_after
    assert second["reconciliation"]["all_balanced"] is True
    chain = [row["version"] for row in store.duty_versions(first["duty"]["duty_key"])]
    assert chain == [1, 2]
    # 旧版的行一条没动
    old = store.load_duty_appointments(first["duty"]["duty_id"])
    assert len(old) == len(first["appointments"])


def test_resume_continues_after_processed_sessions(duty_store):
    """中断后从已处理场次续跑（TRAP-35）：上一版完成的场次不再重排。"""
    settings, store = duty_store["settings"], duty_store["store"]
    batch_id = duty_store["batch"]["batch_id"]
    first = service.run(settings, store, batch_id)
    you = [row for row in store.load_duty_itinerary(first["duty"]["duty_id"])
           if row["role"] == planner.DONE]
    assert you
    second = service.run(settings, store, batch_id, mode="resume",
                         resume_of=first["duty"]["duty_id"])
    carried = [row for row in second["itinerary"] if row["role"] == planner.DONE]
    for row in you:
        match = [item for item in carried
                 if item["appointment_id"] == row["appointment_id"]]
        assert match and match[0]["person_name"] == row["person_name"]
        assert match[0]["record_id"] == (row["record_id"] or match[0]["record_id"])


def test_stale_submit_rejected(duty_store):
    settings, store = duty_store["settings"], duty_store["store"]
    batch_id = duty_store["batch"]["batch_id"]
    first = service.run(settings, store, batch_id)
    service.run(settings, store, batch_id, mode="resume",
                resume_of=first["duty"]["duty_id"])
    with pytest.raises(errors.Conflict) as exc:
        service.run(settings, store, batch_id, mode="resume",
                    resume_of=first["duty"]["duty_id"], submitted_version=1)
    assert exc.value.code == "D_STALE_REJECTED"
    assert exc.value.status_code == 409


def test_failed_write_keeps_previous_version(duty_store, monkeypatch):
    """整批原子（TRAP-33）：写入失败时旧版预约 / 行程 / 接管原样保留。"""
    settings, store = duty_store["settings"], duty_store["store"]
    batch_id = duty_store["batch"]["batch_id"]
    first = service.run(settings, store, batch_id)
    before_appointments = store.load_duty_appointments(first["duty"]["duty_id"])
    before_itinerary = store.load_duty_itinerary(first["duty"]["duty_id"])
    before_takeovers = store.load_duty_takeovers(first["duty"]["duty_id"])
    calls = {"n": 0}
    original = store.save_duty

    def exploding(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("模拟写入失败")

    monkeypatch.setattr(store, "save_duty", exploding)
    with pytest.raises(RuntimeError):
        service.run(settings, store, batch_id, mode="resume",
                    resume_of=first["duty"]["duty_id"])
    assert calls["n"] == 1
    monkeypatch.setattr(store, "save_duty", original)
    assert store.load_duty_appointments(first["duty"]["duty_id"]) == before_appointments
    assert store.load_duty_itinerary(first["duty"]["duty_id"]) == before_itinerary
    assert store.load_duty_takeovers(first["duty"]["duty_id"]) == before_takeovers
    assert store.get_duty(first["duty"]["duty_id"])["status"] == "done"
    latest = store.latest_duty()
    assert latest["duty_id"] == first["duty"]["duty_id"]


def test_explicit_empty_records_override_sample(duty_store):
    """显式给空记录时不回退样例，续跑只靠上一版已完成场次。"""
    settings, store = duty_store["settings"], duty_store["store"]
    batch_id = duty_store["batch"]["batch_id"]
    first = service.run(settings, store, batch_id)
    second = service.run(settings, store, batch_id, records_text="",
                         mode="resume", resume_of=first["duty"]["duty_id"])
    assert second["records"]
    assert all(row["status"] == "carried" for row in second["records"])
    assert second["reconciliation"]["all_balanced"] is True


def test_atomic_write_rolls_back_within_transaction(duty_store):
    """事务内失败（TRAP-33）：同一 duty_id 的半批数据不落库，旧版不受影响。"""
    settings, store = duty_store["settings"], duty_store["store"]
    batch_id = duty_store["batch"]["batch_id"]
    first = service.run(settings, store, batch_id)
    before = len(store.load_duty_appointments(first["duty"]["duty_id"]))
    broken = {
        "duty_id": "DBROKEN", "batch_id": batch_id, "duty_key": "k",
        "version": 9, "status": "done", "created_at": "2026-11-16T00:00:00+00:00",
        "params": {},
    }
    good = {"appointment_id": "AP-X", "ref": "X", "name": "X", "hall": "A1",
            "day": 1, "slot_start_utc": "2026-11-16T02:00:00+00:00",
            "slot_end_utc": "2026-11-16T02:20:00+00:00", "dwell_min": 20,
            "owner_name": "某", "role": "OWNER", "state": "OWNER"}
    # 第二行缺必填字段：INSERT 抛错，前面的删除与插入必须一起回滚
    with pytest.raises(KeyError):
        store.save_duty(duty=broken, appointments=[good, {"ref": "坏行"}],
                        itinerary=[], takeovers=[], records=[])
    assert store.get_duty("DBROKEN") is None
    assert store.load_duty_appointments("DBROKEN") == []
    assert len(store.load_duty_appointments(first["duty"]["duty_id"])) == before
    assert store.get_duty(first["duty"]["duty_id"])["status"] == "done"


# ---------- 接口 / 页面 ----------
def test_duty_api_and_page(client: TestClient):
    created = client.post("/api/duty", json={})
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "done"
    assert body["summary"]["appointments"] == body["summary"]["owner"] \
        + body["summary"]["takeover_pending"] + body["summary"]["done"] \
        + body["summary"]["uncovered"]
    assert body["reconciliation"]["all_balanced"] is True
    latest = client.get("/api/duty").json()
    assert latest["duty_id"] == body["duty_id"]
    assert len(latest["appointments"]) == len(body["appointments"])
    page = client.get("/duty")
    assert page.status_code == 200
    for grid in ("grid-roster", "grid-duty-appointments", "grid-duty-takeovers",
                 "grid-duty-records", "grid-duty-reconcile"):
        assert f'id="{grid}"' in page.text
    csv_response = client.get("/api/duty/itinerary.csv")
    assert csv_response.status_code == 200
    rows = list(csv.reader(io.StringIO(csv_response.text.lstrip("﻿"))))
    assert rows[0][0] == "同事"
    assert len(rows) - 1 == len(body["itinerary"])
    records_response = client.get("/api/duty/records")
    assert records_response.status_code == 200
    assert records_response.json()["count"] == body["summary"]["records"]


def test_duty_api_validation(client: TestClient):
    assert client.post("/api/duty", json={"mode": "delta"}).status_code == 400
    assert client.post("/api/duty", json={"mode": "resume"}).status_code == 400
    bad_attendance = client.post("/api/duty", json={
        "attendance": [{"name": "查无此人", "day": 1}]})
    assert bad_attendance.status_code == 400
    assert bad_attendance.json()["error"] == "E_BAD_ATTENDANCE"
    bad_version = client.post("/api/duty", json={"version": "x"})
    assert bad_version.status_code == 400


def test_duty_without_batch(empty_client: TestClient):
    assert empty_client.get("/duty").status_code == 200
    assert empty_client.get("/api/duty").status_code == 404
    assert empty_client.post("/api/duty", json={}).status_code == 404
    assert empty_client.get("/api/duty/roster").status_code == 200


def test_duty_codes_registered():
    for code in reasons.REASONS:
        if code.startswith("D_"):
            assert reasons.stage_of(code) == "duty"
    for code in ("D_OWNER_KEPT", "D_LATE_TAKEOVER", "D_UNCOVERED",
                 "D_DUPLICATE_RECORD", "D_UNKNOWN_REF", "D_RESUMED",
                 "D_STALE_REJECTED"):
        assert code in reasons.REASONS
    assert reasons.level_of("D_UNCOVERED") == reasons.ERROR
    assert reasons.level_of("D_LATE_TAKEOVER") == reasons.WARN


def test_sample_records_parse():
    text = records.sample_text(config.Settings.from_env())
    record_set = records.read(text)
    assert record_set.count >= 5
    codes = [item["code"] for item in record_set.issues]
    assert "D_DUPLICATE_RECORD" in codes
    assert any(row["status"] == "done" for row in record_set.rows)


def test_record_rows_present_time_and_person():
    text = ("记录号,展位号,展商名称,完成人,完成时间\n"
            "R1,Ａ１-01-01,甲,张岚,2026-11-16T02:10:00Z\n")
    record_set = records.read(text)
    assert record_set.rows[0]["booth_code"] == "A1-01-01"
    rows = records.record_rows(record_set.rows)
    assert rows[0]["at"] == "11-16 10:10"
    assert rows[0]["person"] == "张岚"


def test_duty_page_person_filter(client: TestClient):
    created = client.post("/api/duty", json={}).json()
    name = created["itinerary"][0]["person_name"]
    page = client.get(f"/duty?person={created['itinerary'][0]['person_id']}")
    assert page.status_code == 200
    assert f"{name} 的个人行程" in page.text
    other = [row for row in created["person_summary"] if row["name"] != name][0]
    assert f"{other['name']} 的个人行程" not in page.text
