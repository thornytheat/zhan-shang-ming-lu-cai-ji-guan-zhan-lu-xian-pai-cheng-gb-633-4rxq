"""在场窗口：五人名单、每人每天的到馆/离馆时间、可接管的判定。

规则只有一条来源：`config.TEAM`。接口可以按现场情况叠加「晚到 / 提前离馆 /
当天不在」的临时调整，调整只作用于本次分工，不写回配置（下一版重新开始时
仍以配置为准，除非再次传入）。

在场窗口是本地分钟、左闭右开 [start, end)：
- 窗口内才接得下预约；窗口外同一预约由别人接管，预约本身不作废；
- `absent` 把当天窗口压成空窗口，用于「临时不来」；
- 名单外的对接人（外组）一律按「原对接人不在场」处理，由本组接管。
"""
from __future__ import annotations

import re

from ... import config

HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
MINUTES_PER_DAY = 24 * 60


def parse_hhmm(text) -> int:
    """'09:00' → 540；只接受 HH:MM，不接受 24:00 之类的写法。"""
    match = HHMM_RE.match(str(text or "").strip())
    if not match:
        raise ValueError(f"时间必须是 HH:MM，收到 {text!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"时间超出范围：{text!r}")
    return hour * 60 + minute


def hhmm(minutes: int | None) -> str:
    if minutes is None:
        return "—"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def base_roster() -> list[dict]:
    """配置里的五人名单；每次重建，避免调整串到下一次分工。"""
    people = []
    for spec in config.TEAM:
        people.append({
            "id": spec["id"],
            "name": spec["name"],
            "days": {int(day): (int(span[0]), int(span[1]))
                     for day, span in spec["days"].items()},
            "note": spec.get("note", ""),
            "external": False,
        })
    return people


def build_roster(attendance: list[dict] | None = None) -> list[dict]:
    """配置 + 现场调整 → 本次分工的在场窗口。

    attendance 每项：{name, day, start?, end?, absent?}；未给的端点沿用配置。
    名单外的人、非展期日期、倒挂的窗口都直接拒绝，由 handler 映射成 400。
    """
    people = base_roster()
    if not attendance:
        return people
    by_name = {person["name"]: person for person in people}
    if not isinstance(attendance, list):
        raise ValueError("attendance 必须是数组")
    for item in attendance:
        if not isinstance(item, dict):
            raise ValueError("attendance 的每一项必须是对象")
        name = str(item.get("name") or "").strip()
        person = by_name.get(name)
        if person is None:
            raise ValueError(
                f"同事 {name!r} 不在五人名单内；可选 "
                + " / ".join(p["name"] for p in people))
        try:
            day = int(item.get("day"))
        except (TypeError, ValueError):
            raise ValueError(f"day 只支持 1 / 2，收到 {item.get('day')!r}") from None
        if day not in config.SHOW_DAYS:
            raise ValueError(f"day 只支持 1 / 2，收到 {day!r}")
        if item.get("absent"):
            person["days"][day] = (0, 0)
            continue
        start, end = person["days"].get(day, (0, 0))
        if item.get("start") not in (None, ""):
            start = parse_hhmm(item["start"])
        if item.get("end") not in (None, ""):
            end = parse_hhmm(item["end"])
        if start >= end:
            raise ValueError(
                f"{name} 第 {day} 天的在场窗口 {hhmm(start)}–{hhmm(end)} 不成立")
        person["days"][day] = (start, end)
    return people


def window_of(person: dict | None, day: int) -> tuple[int, int]:
    if person is None:
        return (0, 0)
    return person.get("days", {}).get(day, (0, 0))


def covers(person: dict | None, day: int, start: int, dwell: int) -> bool:
    """窗口是否整体盖住已约时段（预约不因晚到而顺延，只有 5 分钟宽限）。"""
    window = window_of(person, day)
    if not window or window[0] >= window[1]:
        return False
    return window[0] <= start and start + dwell <= window[1]


def person_indexes(people: list[dict]) -> tuple[dict, dict]:
    return ({person["id"]: person for person in people},
            {person["name"]: person for person in people})


def external_person(name: str) -> dict:
    """完成记录里出现、但不在五人名单里的人：只保留其已完成行程。"""
    return {"id": "X-" + name, "name": name, "days": {}, "note": "名单外",
            "external": True}


def window_rows(people: list[dict]) -> list[dict]:
    """页面用：一人一行，两天窗口 + 备注。"""
    rows = []
    for person in people:
        row = {"id": person["id"], "name": person["name"],
               "note": person.get("note", "")}
        for day in sorted(config.SHOW_DAYS):
            start, end = window_of(person, day)
            row[f"day{day}"] = "—" if start >= end else f"{hhmm(start)}–{hhmm(end)}"
        rows.append(row)
    return rows


def stores_days(people: list[dict]) -> dict:
    """落库用的窗口快照（JSON 键为字符串）。"""
    return {person["id"]: {str(day): list(span)
                           for day, span in sorted(person["days"].items())}
            for person in people}


def from_snapshot(items: list[dict] | None) -> list[dict]:
    """把落库的窗口快照还原成名单；JSON 键是字符串，这里转回 int 天号。"""
    people = []
    for item in items or []:
        people.append({
            "id": item["id"], "name": item["name"],
            "days": {int(day): (int(span[0]), int(span[1]))
                     for day, span in (item.get("days") or {}).items()},
            "note": item.get("note", ""),
            "external": bool(item.get("external")),
        })
    return people or base_roster()
