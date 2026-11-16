"""SQLite 存储：批次、页面、展商、论坛、隔离、行程点、阶段计数、合计、运行清单。

约定：
- 距离与面积列一律 TEXT（Decimal 字符串），不落 float；
- 一次运行的所有行在同一个事务里「先删后插」，失败整体回滚，不留半批数据；
- 读数按 batch_id 过滤，页面永远只展示一个批次。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input_digest TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    resume_of TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_batches_key ON batches(batch_key, status);
CREATE TABLE IF NOT EXISTS pages (
    batch_id TEXT NOT NULL, url TEXT NOT NULL, source TEXT NOT NULL,
    kind TEXT NOT NULL, page_no INTEGER NOT NULL, status TEXT NOT NULL,
    declared_charset TEXT, encoding TEXT, bytes INTEGER, sha256 TEXT,
    row_count INTEGER NOT NULL DEFAULT 0, code TEXT, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS exhibitors (
    batch_id TEXT NOT NULL, ex_id TEXT NOT NULL, name TEXT NOT NULL,
    hall TEXT NOT NULL, booth_raw TEXT NOT NULL, booth_code TEXT NOT NULL,
    row_no TEXT, col_no TEXT, x_m TEXT, y_m TEXT, area_sqm TEXT,
    priority INTEGER NOT NULL DEFAULT 5, owner TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, code TEXT, source_url TEXT NOT NULL,
    row_ref TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS forums (
    batch_id TEXT NOT NULL, session_id TEXT NOT NULL, title TEXT NOT NULL,
    hall TEXT NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
    day INTEGER NOT NULL, tz_raw TEXT NOT NULL DEFAULT '',
    code TEXT, detail TEXT,
    source_url TEXT NOT NULL, row_ref TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS issues (
    batch_id TEXT NOT NULL, code TEXT NOT NULL, stage TEXT NOT NULL,
    level TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
    field TEXT, raw_value TEXT, detail TEXT,
    source_url TEXT, row_ref TEXT
);
CREATE TABLE IF NOT EXISTS stops (
    batch_id TEXT NOT NULL, plan_day INTEGER NOT NULL, seq INTEGER NOT NULL,
    kind TEXT NOT NULL, ref TEXT NOT NULL, name TEXT NOT NULL, hall TEXT NOT NULL,
    arrival_utc TEXT NOT NULL, depart_utc TEXT NOT NULL,
    dwell_min INTEGER NOT NULL, walk_m TEXT NOT NULL,
    status TEXT NOT NULL, code TEXT, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stages (
    batch_id TEXT NOT NULL, stage TEXT NOT NULL, label TEXT NOT NULL,
    in_count INTEGER NOT NULL, out_count INTEGER NOT NULL,
    quarantine_count INTEGER NOT NULL, status TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS totals (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
    batch_id TEXT PRIMARY KEY, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS duty_plans (
    duty_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, duty_key TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL,
    created_at TEXT NOT NULL, finished_at TEXT, resume_of TEXT,
    params_json TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_duty_key ON duty_plans(duty_key, version);
CREATE TABLE IF NOT EXISTS duty_appointments (
    duty_id TEXT NOT NULL, appointment_id TEXT NOT NULL, ref TEXT NOT NULL,
    name TEXT NOT NULL, hall TEXT NOT NULL, booth_code TEXT,
    day INTEGER NOT NULL, slot_start_utc TEXT NOT NULL, slot_end_utc TEXT NOT NULL,
    dwell_min INTEGER NOT NULL, owner_id TEXT NOT NULL DEFAULT '',
    owner_name TEXT NOT NULL, assignee_id TEXT NOT NULL DEFAULT '',
    assignee_name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL,
    state TEXT NOT NULL, code TEXT, record_id TEXT,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (duty_id, appointment_id)
);
CREATE TABLE IF NOT EXISTS duty_itinerary (
    duty_id TEXT NOT NULL, person_id TEXT NOT NULL, person_name TEXT NOT NULL,
    day INTEGER NOT NULL, seq INTEGER NOT NULL, appointment_id TEXT NOT NULL,
    ref TEXT NOT NULL, name TEXT NOT NULL, hall TEXT NOT NULL,
    booth_code TEXT, arrival_utc TEXT NOT NULL, depart_utc TEXT NOT NULL,
    dwell_min INTEGER NOT NULL, walk_m TEXT NOT NULL, walk_min INTEGER NOT NULL,
    role TEXT NOT NULL, owner_name TEXT NOT NULL, state TEXT NOT NULL,
    record_id TEXT, note TEXT NOT NULL DEFAULT '',
    UNIQUE (duty_id, appointment_id)
);
CREATE TABLE IF NOT EXISTS duty_takeovers (
    duty_id TEXT NOT NULL, takeover_id TEXT NOT NULL, appointment_id TEXT NOT NULL,
    ref TEXT NOT NULL, name TEXT NOT NULL, day INTEGER NOT NULL,
    from_person TEXT NOT NULL, to_person TEXT NOT NULL,
    reason_code TEXT NOT NULL, reason TEXT NOT NULL,
    slot_start_utc TEXT NOT NULL, slot_end_utc TEXT NOT NULL,
    source TEXT NOT NULL, record_id TEXT, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS duty_records (
    duty_id TEXT NOT NULL, record_id TEXT NOT NULL, ref TEXT NOT NULL,
    person_name TEXT NOT NULL, completed_at TEXT NOT NULL,
    appointment_id TEXT, status TEXT NOT NULL, code TEXT,
    note TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 批次 ----------
    def upsert_batch(self, batch: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO batches (batch_id, batch_key, run_id, source, status,
                        created_at, finished_at, input_digest, version, resume_of,
                        params_json, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     note=excluded.note, params_json=excluded.params_json""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["source"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch.get("version", 1), batch.get("resume_of"),
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 batch.get("note", "")),
            )

    def find_done_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? AND status='done' "
                "ORDER BY created_at LIMIT 1", (batch_key,)).fetchone()
        return dict(row) if row else None

    def get_batch(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return dict(row) if row else None

    def latest_batch(self, status: str = "done") -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE status=? ORDER BY created_at DESC, rowid DESC "
                "LIMIT 1", (status,)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
        return dict(row) if row else None

    def list_batches(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def next_version(self, batch_key: str) -> int:
        """同一批次键的下一版本号；用于陈旧提交拒绝。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM batches WHERE batch_key=?",
                (batch_key,)).fetchone()
        return int(row["v"] or 0) + 1

    # ---------- 运行结果落库（单事务替换） ----------
    def save_run(self, *, batch: dict, pages: list[dict], exhibitors: list[dict],
                 forums: list[dict], issues: list[dict], stops: list[dict],
                 stages: list[dict], totals: dict, manifest: dict) -> None:
        bid = batch["batch_id"]
        with self.connect() as conn:
            for table in ("pages", "exhibitors", "forums", "issues", "stops",
                          "stages", "totals"):
                conn.execute(f"DELETE FROM {table} WHERE batch_id=?", (bid,))
            for p in pages:
                conn.execute(
                    """INSERT INTO pages (batch_id,url,source,kind,page_no,status,
                        declared_charset,encoding,bytes,sha256,row_count,code,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bid, p["url"], p["source"], p["kind"], p["page_no"], p["status"],
                     p.get("declared_charset"), p.get("encoding"), p.get("bytes"),
                     p.get("sha256"), p.get("row_count", 0), p.get("code"),
                     p.get("note", "")))
            for e in exhibitors:
                conn.execute(
                    """INSERT INTO exhibitors (batch_id,ex_id,name,hall,booth_raw,
                        booth_code,row_no,col_no,x_m,y_m,area_sqm,priority,owner,status,
                        code,source_url,row_ref,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bid, e["ex_id"], e["name"], e["hall"], e["booth_raw"],
                     e["booth_code"], e.get("row_no"), e.get("col_no"),
                     _s(e.get("x_m")), _s(e.get("y_m")), _s(e.get("area_sqm")),
                     e.get("priority", 5), e.get("owner", ""), e["status"],
                     e.get("code"), e["source_url"], e["row_ref"], e.get("note", "")))
            for f in forums:
                conn.execute(
                    """INSERT INTO forums (batch_id,session_id,title,hall,start_utc,
                        end_utc,day,tz_raw,code,detail,source_url,row_ref,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bid, f["session_id"], f["title"], f["hall"], f["start_utc"],
                     f["end_utc"], f["day"], f.get("tz_raw", ""), f.get("code"),
                     f.get("detail"), f["source_url"], f["row_ref"],
                     f.get("note", "")))
            for q in issues:
                conn.execute(
                    """INSERT INTO issues (batch_id,code,stage,level,subject,field,
                        raw_value,detail,source_url,row_ref) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (bid, q["code"], q["stage"], q["level"], q.get("subject", ""),
                     q.get("field"), q.get("raw_value"), q.get("detail"),
                     q.get("source_url"), q.get("row_ref")))
            for s in stops:
                conn.execute(
                    """INSERT INTO stops (batch_id,plan_day,seq,kind,ref,name,hall,
                        arrival_utc,depart_utc,dwell_min,walk_m,status,code,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bid, s["plan_day"], s["seq"], s["kind"], s["ref"], s["name"],
                     s["hall"], s["arrival_utc"], s["depart_utc"],
                     s.get("dwell_min", s.get("dwell", 0)),
                     str(s["walk_m"]), s["status"], s.get("code"), s.get("note", "")))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,label,in_count,out_count,
                        quarantine_count,status,elapsed_ms,note)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (bid, s["stage"], s["label"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"],
                     s.get("note", "")))
            for key, value in totals.items():
                conn.execute("INSERT INTO totals (batch_id,key,value) VALUES (?,?,?)",
                             (bid, key, str(value)))
            conn.execute(
                "INSERT INTO manifests (batch_id,json) VALUES (?,?) "
                "ON CONFLICT(batch_id) DO UPDATE SET json=excluded.json",
                (bid, json.dumps(manifest, ensure_ascii=False)))
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,source,status,
                        created_at,finished_at,input_digest,version,resume_of,
                        params_json,note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     note=excluded.note, input_digest=excluded.input_digest""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["source"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch.get("version", 1), batch.get("resume_of"),
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 batch.get("note", "")))

    def save_attempt(self, *, batch: dict, pages: list[dict], stages: list[dict],
                     note: str = "") -> None:
        """失败尝试：只写批次、页面与阶段，不清空其他批次的任何数据。"""
        bid = batch["batch_id"]
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,source,status,
                        created_at,finished_at,input_digest,version,resume_of,
                        params_json,note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     note=excluded.note""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["source"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch.get("version", 1), batch.get("resume_of"),
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 note or batch.get("note", "")))
            conn.execute("DELETE FROM pages WHERE batch_id=?", (bid,))
            conn.execute("DELETE FROM stages WHERE batch_id=?", (bid,))
            for p in pages:
                conn.execute(
                    """INSERT INTO pages (batch_id,url,source,kind,page_no,status,
                        declared_charset,encoding,bytes,sha256,row_count,code,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bid, p["url"], p["source"], p["kind"], p["page_no"], p["status"],
                     p.get("declared_charset"), p.get("encoding"), p.get("bytes"),
                     p.get("sha256"), p.get("row_count", 0), p.get("code"),
                     p.get("note", "")))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,label,in_count,out_count,
                        quarantine_count,status,elapsed_ms,note)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (bid, s["stage"], s["label"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"],
                     s.get("note", "")))

    # ---------- 读取 ----------
    def load_pages(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM pages WHERE batch_id=? ORDER BY source, page_no, url",
            (batch_id,))

    def load_exhibitors(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM exhibitors WHERE batch_id=? ORDER BY hall, row_no, col_no, ex_id",
            (batch_id,))
        for r in rows:
            for key in ("x_m", "y_m", "area_sqm"):
                r[key] = None if r[key] is None else Decimal(r[key])
        return rows

    def load_forums(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM forums WHERE batch_id=? ORDER BY start_utc, session_id",
            (batch_id,))

    def load_issues(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM issues WHERE batch_id=? ORDER BY stage, code, row_ref",
            (batch_id,))

    def load_stops(self, batch_id: str, plan_day: int | None = None) -> list[dict]:
        if plan_day is None:
            rows = self._all(
                "SELECT * FROM stops WHERE batch_id=? ORDER BY plan_day, seq", (batch_id,))
        else:
            rows = self._all(
                "SELECT * FROM stops WHERE batch_id=? AND plan_day=? ORDER BY seq",
                (batch_id, plan_day))
        for r in rows:
            r["walk_m"] = Decimal(r["walk_m"])
        return rows

    def load_stages(self, batch_id: str) -> list[dict]:
        return self._all("SELECT * FROM stages WHERE batch_id=? ORDER BY rowid", (batch_id,))

    def load_totals(self, batch_id: str) -> dict:
        rows = self._all("SELECT key, value FROM totals WHERE batch_id=?", (batch_id,))
        return {r["key"]: r["value"] for r in rows}

    def load_manifest(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT json FROM manifests WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    def count_rows(self, batch_id: str) -> dict:
        with self.connect() as conn:
            out = {}
            for table in ("pages", "exhibitors", "forums", "issues", "stops"):
                out[table] = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE batch_id=?", (batch_id,)
                ).fetchone()["n"]
        return out

    def _all(self, sql: str, args: tuple) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # ---------- M8 现场分工 ----------
    def save_duty(self, *, duty: dict, appointments: list[dict],
                  itinerary: list[dict], takeovers: list[dict],
                  records: list[dict]) -> None:
        """整批写入一份分工：同一事务内先删后插，只有本 duty_id 的行会被替换。

        新版本用新的 duty_id，所以任一环节抛错回滚后，旧版本的预约、行程、
        接管记录原样保留，页面继续看到上一版安排。
        """
        did = duty["duty_id"]
        with self.connect() as conn:
            for table in ("duty_appointments", "duty_itinerary",
                          "duty_takeovers", "duty_records"):
                conn.execute(f"DELETE FROM {table} WHERE duty_id=?", (did,))
            conn.execute("DELETE FROM duty_plans WHERE duty_id=?", (did,))
            for item in appointments:
                conn.execute(
                    """INSERT INTO duty_appointments (duty_id, appointment_id, ref,
                        name, hall, booth_code, day, slot_start_utc, slot_end_utc,
                        dwell_min, owner_id, owner_name, assignee_id, assignee_name,
                        role, state, code, record_id, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (did, item["appointment_id"], item["ref"], item["name"],
                     item["hall"], item.get("booth_code"), item["day"],
                     item["slot_start_utc"], item["slot_end_utc"],
                     item["dwell_min"], item.get("owner_id", ""),
                     item["owner_name"], item.get("assignee_id", ""),
                     item.get("assignee_name", ""), item["role"], item["state"],
                     item.get("code"), item.get("record_id"),
                     item.get("note", "")))
            for item in itinerary:
                conn.execute(
                    """INSERT INTO duty_itinerary (duty_id, person_id, person_name,
                        day, seq, appointment_id, ref, name, hall, booth_code,
                        arrival_utc, depart_utc, dwell_min, walk_m, walk_min,
                        role, owner_name, state, record_id, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (did, item["person_id"], item["person_name"], item["day"],
                     item["seq"], item["appointment_id"], item["ref"],
                     item["name"], item["hall"], item.get("booth_code"),
                     item["arrival_utc"], item["depart_utc"], item["dwell_min"],
                     str(item["walk_m"]), item["walk_min"], item["role"],
                     item["owner_name"], item["state"], item.get("record_id"),
                     item.get("note", "")))
            for item in takeovers:
                conn.execute(
                    """INSERT INTO duty_takeovers (duty_id, takeover_id,
                        appointment_id, ref, name, day, from_person, to_person,
                        reason_code, reason, slot_start_utc, slot_end_utc, source,
                        record_id, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (did, item["takeover_id"], item["appointment_id"], item["ref"],
                     item["name"], item["day"], item["from_person"],
                     item["to_person"], item["reason_code"], item["reason"],
                     item["slot_start_utc"], item["slot_end_utc"],
                     item["source"], item.get("record_id"),
                     item.get("note", "")))
            for item in records:
                conn.execute(
                    """INSERT INTO duty_records (duty_id, record_id, ref,
                        person_name, completed_at, appointment_id, status, code,
                        note)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (did, item["record_id"], item["ref"], item["person_name"],
                     item["completed_at"], item.get("appointment_id"),
                     item["status"], item.get("code"), item.get("note", "")))
            conn.execute(
                """INSERT INTO duty_plans (duty_id, batch_id, duty_key, version,
                        status, created_at, finished_at, resume_of, params_json,
                        note)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (did, duty["batch_id"], duty["duty_key"], duty.get("version", 1),
                 duty["status"], duty["created_at"], duty.get("finished_at"),
                 duty.get("resume_of"),
                 json.dumps(duty.get("params", {}), ensure_ascii=False),
                 duty.get("note", "")))

    def get_duty(self, duty_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM duty_plans WHERE duty_id=?",
                               (duty_id,)).fetchone()
        return dict(row) if row else None

    def latest_duty(self, status: str = "done") -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM duty_plans WHERE status=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (status,)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM duty_plans ORDER BY created_at DESC, rowid DESC "
                    "LIMIT 1").fetchone()
        return dict(row) if row else None

    def list_duties(self, limit: int = 30) -> list[dict]:
        return self._all(
            "SELECT * FROM duty_plans ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,))

    def find_done_duty_by_key(self, duty_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM duty_plans WHERE duty_key=? AND status='done' "
                "ORDER BY version LIMIT 1", (duty_key,)).fetchone()
        return dict(row) if row else None

    def next_duty_version(self, duty_key: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM duty_plans WHERE duty_key=?",
                (duty_key,)).fetchone()
        return int(row["v"] or 0) + 1

    def duty_versions(self, duty_key: str) -> list[dict]:
        return self._all(
            "SELECT duty_id, duty_key, version, status, created_at, finished_at, "
            "resume_of, note FROM duty_plans WHERE duty_key=? ORDER BY version",
            (duty_key,))

    def load_duty_appointments(self, duty_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM duty_appointments WHERE duty_id=? "
            "ORDER BY day, slot_start_utc, appointment_id", (duty_id,))
        for row in rows:
            row["slot_start"] = _local_minutes(row["slot_start_utc"])
            row["slot_end"] = _local_minutes(row["slot_end_utc"])
        return rows

    def load_duty_itinerary(self, duty_id: str, person_id: str | None = None) -> list[dict]:
        if person_id:
            rows = self._all(
                "SELECT * FROM duty_itinerary WHERE duty_id=? AND person_id=? "
                "ORDER BY day, seq", (duty_id, person_id))
        else:
            rows = self._all(
                "SELECT * FROM duty_itinerary WHERE duty_id=? ORDER BY person_id, day, seq",
                (duty_id,))
        for row in rows:
            row["walk_m"] = Decimal(row["walk_m"])
            row["arrival_min"] = _local_minutes(row["arrival_utc"])
            row["depart_min"] = _local_minutes(row["depart_utc"])
        return rows

    def load_duty_takeovers(self, duty_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM duty_takeovers WHERE duty_id=? "
            "ORDER BY day, slot_start_utc, appointment_id", (duty_id,))
        for row in rows:
            row["slot_start"] = _local_minutes(row["slot_start_utc"])
            row["slot_end"] = _local_minutes(row["slot_end_utc"])
        return rows

    def load_duty_records(self, duty_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM duty_records WHERE duty_id=? ORDER BY completed_at, record_id",
            (duty_id,))


def _s(value):
    return None if value is None else str(value)


def _local_minutes(value: str | None) -> int | None:
    """UTC ISO 时刻 → 当天本地分钟；坏值返回 None，不抛。"""
    import datetime as dt

    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    local = parsed.astimezone(dt.timezone(dt.timedelta(hours=8)))
    return local.hour * 60 + local.minute
