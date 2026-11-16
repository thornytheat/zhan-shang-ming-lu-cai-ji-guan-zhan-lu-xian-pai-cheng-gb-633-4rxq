"""现场完成记录：读样例 CSV，归一成「哪一家已经由谁在什么时候完成」。

记录是分工的输入之一，不是结果：已完成场次不再重排，只用来定人、定续跑起点。
脏点与名录同族：BOM、全角数字、中文时间、同一场次重复上报、引用了名录里
没有的展商。每条记录都留证据，坏记录不参与分工。

记录文件格式（逗号分隔，首行表头）：
    记录号,展位号,展商名称,完成人,完成时间,备注
展位号与名称任一能对上预约即可；两者都给时以展位号为准（名称只做交叉核对）。
完成时间接受 `2026-11-16 10:10`（按 +08:00）、`2026-11-16T10:10:00+08:00`、
`2026-11-16T02:10:00Z` 三种写法，由 M3 的时间解析统一归一。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from ... import config, reasons
from ..m2_booth import normalize, parse
from ..m3_windows import windows

COLUMNS = ("record_id", "booth_raw", "exhibitor_name", "person_name",
           "completed_at", "note")
COLUMN_ALIASES = {
    "记录号": "record_id", "记录": "record_id", "编号": "record_id",
    "展位号": "booth_raw", "展位": "booth_raw",
    "展商名称": "exhibitor_name", "展商": "exhibitor_name", "名称": "exhibitor_name",
    "完成人": "person_name", "完成者": "person_name", "对接人": "person_name",
    "完成时间": "completed_at", "时间": "completed_at",
    "备注": "note", "说明": "note",
}
RECORD_CODES = ("D_UNKNOWN_REF", "D_DUPLICATE_RECORD")
HEADER = "记录号,展位号,展商名称,完成人,完成时间,备注"


@dataclass
class Records:
    rows: list[dict]
    issues: list[dict]
    source: str = "inline"

    @property
    def count(self) -> int:
        return len(self.rows)


def sample_text(settings) -> str:
    """样例记录的默认内容；读不到时返回空表（只有表头）。"""
    from pathlib import Path

    path = Path(settings.sample_site_dir) / "field" / "completions.csv"
    try:
        raw = path.read_bytes()
    except OSError:
        return "﻿" + HEADER + "\r\n"
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return "﻿" + HEADER + "\r\n"


def read(text: str | None, source: str = "inline") -> Records:
    """解析记录文本；source 只用于证据里的来源标注。"""
    if text is None or not text.strip():
        return Records(rows=[], issues=[], source=source)
    buffer = io.StringIO(text.lstrip("﻿"))
    reader = csv.reader(buffer)
    raw_rows = list(reader)
    if not raw_rows:
        return Records(rows=[], issues=[], source=source)
    mapping = _map_header(raw_rows[0])
    rows: list[dict] = []
    issues: list[dict] = []
    seen: dict[str, dict] = {}
    for index, raw in enumerate(raw_rows[1:], start=2):
        if not any(str(cell).strip() for cell in raw):
            continue
        record = _one_row(raw, mapping, index, source)
        if record is None:
            issues.append(_issue("D_UNKNOWN_REF", subject=f"第 {index} 行",
                                 detail="列数不足，整行不参与分工", row_ref=f"#{index}"))
            continue
        identity = record["appointment_key"]
        first = seen.get(identity)
        if first is not None:
            record["status"] = "duplicate"
            record["code"] = "D_DUPLICATE_RECORD"
            record["duplicate_of"] = first["record_id"]
            issues.append(_issue(
                "D_DUPLICATE_RECORD", subject=record["record_id"],
                detail=f"与 {first['record_id']} 指向同一场次，取最早一条",
                raw_value=record.get("ref") or record.get("booth_raw") or "",
                row_ref=f"#{index}"))
            rows.append(record)
            continue
        seen[identity] = record
        rows.append(record)
    return Records(rows=rows, issues=issues, source=source)


def _map_header(header: list[str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for index, cell in enumerate(header):
        key = normalize.clean_text(str(cell)).lstrip("﻿")
        field = COLUMN_ALIASES.get(key)
        if field is None:
            field = COLUMN_ALIASES.get(key.replace(" ", ""))
        if field and field not in mapping.values():
            mapping[index] = field
    return mapping


def _one_row(raw: list[str], mapping: dict[int, str], index: int,
             source: str) -> dict | None:
    if len(raw) < 3:
        return None
    cells = {}
    for position, field in mapping.items():
        cells[field] = raw[position].strip() if position < len(raw) else ""
    for field in ("booth_raw", "exhibitor_name"):
        cells[field] = normalize.clean_text(cells.get(field) or "")
    person = _clean_person(cells.get("person_name"))
    completed = _parse_completed(cells.get("completed_at"))
    if not person or completed is None:
        return None
    booth = parse.parse_booth(cells.get("booth_raw"))
    booth_code = booth["booth_code"] or ""
    name_key = normalize.normalize_name(cells.get("exhibitor_name") or "")
    key = booth_code or ("name:" + name_key)
    return {
        "record_id": cells.get("record_id") or f"R{index:03d}",
        "ref": booth_code or (cells.get("exhibitor_name") or f"#{index}"),
        "booth_raw": cells.get("booth_raw") or "",
        "booth_code": booth_code,
        "exhibitor_name": cells.get("exhibitor_name") or "",
        "person_name": person,
        "completed_at": completed.isoformat(),
        "completed_utc": completed.astimezone(_utc()).isoformat(),
        "note": cells.get("note") or "",
        "appointment_key": key,
        "status": "done",
        "code": None,
        "row_ref": f"#{index}",
        "source": source,
    }


def _clean_person(value) -> str:
    text = normalize.clean_text(value or "")
    return "" if normalize.is_sentinel(text) else text


def _parse_completed(value) -> object | None:
    if not value:
        return None
    parsed = windows.parse_time(str(value))
    if not parsed["ok"] or parsed["value"] is None:
        return None
    return parsed["value"].astimezone(config.CHINA_TZ)


def _utc():
    return config.UTC


def _issue(code: str, *, subject: str, detail: str,
           raw_value: str | None = None, row_ref: str | None = None) -> dict:
    reason = reasons.get(code)
    return {"code": code, "stage": reason.stage, "level": reason.level,
            "subject": subject, "field": "completions", "raw_value": raw_value,
            "detail": detail, "source_url": config.DUTY_SAMPLE_RECORDS,
            "row_ref": row_ref}


def record_rows(records: list[dict]) -> list[dict]:
    """页面用：一条记录一行，时间换成本地时刻。"""
    rows = []
    for item in records:
        local = windows.to_local(item.get("completed_utc") or item["completed_at"])
        rows.append({
            "record_id": item["record_id"],
            "ref": item.get("booth_raw") or item.get("booth_code") or "—",
            "name": item.get("exhibitor_name") or "—",
            "person": item["person_name"],
            "at": local.strftime("%m-%d %H:%M") if local else "—",
            "status": item.get("status", "done"),
            "code": item.get("code") or "—",
            "note": item.get("note") or "",
        })
    return rows


def record_digest(records: list[dict]) -> str:
    """记录摘要：排序后拼关键字段，用于分工的幂等键。"""
    import hashlib

    parts = sorted(f"{r['record_id']}|{r.get('booth_code') or ''}|"
                   f"{r['person_name']}|{r.get('completed_utc') or r['completed_at']}|"
                   f"{r.get('status')}" for r in records)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

