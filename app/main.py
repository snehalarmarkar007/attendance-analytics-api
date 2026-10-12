"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:   uvicorn app.main:app --port 8000
Env:   MONGO_URI, MONGO_DB   (a local .env file is read, real environment variables win)

Layout of this file (everything is in one file on purpose):
  1. Config + database connection
  2. Small helpers  (time, rounding, business rules R1-R5)
  3. Indexes        (created at startup)
  4. Request models (validation)
  5. Employee + attendance endpoints
  6. Analytics endpoints  (MongoDB aggregation pipelines)
  7. Explain endpoint
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time as Time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Config + database connection
# --------------------------------------------------------------------------- #
load_dotenv()  # reads .env if present; it never overrides real environment variables

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True -> datetimes coming back from MongoDB carry UTC tzinfo (no naive datetimes).
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
WEEKDAYS = [2, 3, 4, 5, 6]  # MongoDB $dayOfWeek: 1=Sunday ... 7=Saturday -> Mon..Fri = 2..6
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_PUNCH_GAP_SECONDS = 24 * 3600

# Index key patterns (also used as hints so the planner always picks the intended index)
K_EMP_DATE = {"emp_code": 1, "date": 1}
K_DATE_EMP = {"date": -1, "emp_code": 1}
K_DEPT_JOINED = {"department": 1, "joined_on": 1}

# Types used by the request models
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


# --------------------------------------------------------------------------- #
# 2. Helpers
# --------------------------------------------------------------------------- #
def parse_date_str(value: str, field: str = "date") -> Date:
    """'YYYY-MM-DD' -> date. Anything else (or an impossible date like 2026-02-30) -> 422."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value or ""):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{field} is not a real calendar date")


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC datetime -> epoch milliseconds (exact integer arithmetic, no float)."""
    if dt is None:
        return None
    if dt.tzinfo is None:  # safety net: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def round_half_up(value: Decimal, places: int) -> Decimal:
    """Ordinary school rounding (0.125 -> 0.13). Python's round() and Mongo's $round do NOT do this."""
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def hhmm(value: str) -> Time:
    h, m = value.split(":")
    return Time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; for overnight shifts, a punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_at(day: str, shift_start: str) -> datetime:
    return datetime.combine(Date.fromisoformat(day), hhmm(shift_start), tzinfo=IST)


def shift_end_at(day: str, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(Date.fromisoformat(day), hhmm(shift_end), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)  # overnight shift ends on the next calendar day
    return end


def compute_late_minutes(punch_in: datetime, start: datetime) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, end: datetime) -> int:
    """R3: whole minutes after shift end, counted only when that is >= 30."""
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600, rounded to 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def derive(emp: dict, status: str, day: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All derived fields for a record (see the table in DATA_MODEL.md)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, shift_start_at(day, emp["shift_start"]))
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = float(hours)
        out["overtime_minutes"] = compute_overtime_minutes(
            punch_out, shift_end_at(day, emp["shift_start"], emp["shift_end"])
        )
        out["half_day"] = hours < HALF_DAY_LIMIT
    return out


def to_api_history(entry: dict) -> dict:
    changes = {}
    for field, pair in (entry.get("changes") or {}).items():
        convert = dt_to_ms if field in ("punch_in", "punch_out") else (lambda v: v)
        changes[field] = {"from": convert(pair.get("from")), "to": convert(pair.get("to"))}
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def to_api_record(doc: dict) -> dict:
    """MongoDB document -> API shape. Missing legacy fields get their defaults; _id is never exposed."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [to_api_history(h) for h in (doc.get("history") or [])],
    }


def to_api_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def month_bounds(month: str) -> tuple[Date, Date]:
    """'2026-07' -> (first day of the month, first day of the next month)."""
    year, mon = int(month[:4]), int(month[5:7])
    try:
        return Date(year, mon, 1), Date(year + (mon == 12), mon % 12 + 1, 1)
    except ValueError:
        raise HTTPException(422, "month out of range")


def count_working_days(first: Date, last: Date, joined_on: str) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last, inclusive."""
    start = max(first, Date.fromisoformat(joined_on))
    if start > last:
        return 0
    return sum(1 for i in range((last - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# 3. Indexes (idempotent: creating an identical index again is a no-op)
# --------------------------------------------------------------------------- #
INDEXES = [
    # (collection, keys, options)
    ("employees", [("emp_code", ASCENDING)], {"unique": True, "name": "uniq_emp_code"}),
    ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp_code"}),
    ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined_on"}),
    ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "uniq_emp_date"}),
    ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_emp"}),
    ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
    ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
]
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    ok = True
    for collection, keys, options in INDEXES:
        try:
            db[collection].create_index(keys, **options)
        except PyMongoError as exc:  # do not crash the app; /health will retry later
            ok = False
            log.error("could not create index %s: %s", options["name"], exc)
    _indexes_ready = ok


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.error("MongoDB error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, v: str) -> str:
        Date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def no_explicit_null(cls, data):
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must not be null")
        return data


# --------------------------------------------------------------------------- #
# 5. Employees + attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the UNIQUE index decides, so parallel duplicates -> one 201, rest 409
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return to_api_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [to_api_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start_at(day, emp["shift_start"])),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return to_api_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent record whose punch_in is at or before the punch-out (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    gap = (ts - rec["punch_in"]).total_seconds()
    if gap <= 0 or gap > MAX_PUNCH_GAP_SECONDS:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")

    derived = derive(emp, rec["status"], rec["date"], rec["punch_in"], ts)
    # Only succeeds while punch_out is still empty -> two simultaneous punch-outs: exactly one wins.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": rec["date"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return to_api_record(updated)


def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    if date_from is not None:
        parse_date_str(date_from, "date_from")
    if date_to is not None:
        parse_date_str(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    date_range = {}
    if date_from is not None:
        date_range["$gte"] = date_from
    if date_to is not None:
        date_range["$lte"] = date_to
    if date_range:
        q["date"] = date_range
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [to_api_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


TRACKED_FIELDS = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date_str(date, "date")
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day") or False),
    }

    new_status = body.status or old["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT / LEAVE cannot have punch times")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise HTTPException(422, "a presence status needs a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (body.punch_in is not None or body.punch_out is not None):
            gap = (new_out - new_in).total_seconds()
            if gap <= 0 or gap > MAX_PUNCH_GAP_SECONDS:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new.update(derive(emp, new_status, date, new_in, new_out))

    changes = {f: {"from": old[f], "to": new[f]} for f in TRACKED_FIELDS if old[f] != new[f]}
    if not changes:
        raise HTTPException(422, "nothing to change")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic locking: the update only applies if the record is still exactly as we read it
    # (same status, punches and same number of history entries). Otherwise -> 409, nothing is lost.
    guard: dict[str, Any] = {
        "emp_code": emp_code,
        "date": date,
        "status": old["status"],
        "punch_in": old["punch_in"],
        "punch_out": old["punch_out"],
    }
    n_history = len(rec.get("history") or [])
    if n_history:
        guard["history"] = {"$size": n_history}
    else:  # legacy records may have no history field at all (or null)
        guard["$or"] = [{"history": None}, {"history": {"$size": 0}}]

    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was changed by someone else, please retry")
    return to_api_record(updated)


# --------------------------------------------------------------------------- #
# 6. Analytics (MongoDB aggregation pipelines)
# --------------------------------------------------------------------------- #
# Small reusable pieces of aggregation expressions. They run on attendance_logs documents.
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}
IS_PRESENT_STATUS = {"$in": ["$status", PRESENCE]}
IS_WEEKDAY = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, WEEKDAYS]}
# Present value in "halves": full day = 2, half day = 1, not present = 0 (keeps the maths exact).
HALVES_ANY_DAY = {"$cond": [IS_PRESENT_STATUS, {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]}, 0]}
HALVES_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT_STATUS, IS_WEEKDAY]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]}, 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT_STATUS, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}


def monthly_pipeline(emp_code: str, first: Date, nxt: Date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_halves": {"$sum": HALVES_WEEKDAY},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE_MINUTES},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_PATTERN)):
    first, nxt = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, nxt), hint=K_EMP_DATE))
    r = rows[0] if rows else {}
    halves = int(r.get("present_halves", 0))
    working_days = count_working_days(first, nxt - timedelta(days=1), emp["joined_on"])
    pct = None
    if working_days > 0:
        pct = float(round_half_up(Decimal(halves) * 100 / Decimal(2 * working_days), 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


def summary_pipeline(first: Date, nxt: Date, department: Optional[str]) -> list:
    last = nxt - timedelta(days=1)
    emp_match: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}  # R9: joined on/before month end
    if department is not None:
        emp_match["department"] = department
    return [
        # Start from EMPLOYEES so people with zero logs are still counted.
        {"$match": emp_match},
        {"$project": {"_id": 0, "emp_code": 1, "department": 1}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_halves": {"$sum": HALVES_WEEKDAY},
                            # work hours as integer hundredths, so the average can be rounded exactly later
                            "wh_cents": {
                                "$sum": {
                                    "$cond": [
                                        HAS_WORK_HOURS,
                                        {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}},
                                        0,
                                    ]
                                }
                            },
                            "wh_count": {"$sum": {"$cond": [HAS_WORK_HOURS, 1, 0]}},
                            "late_count": {"$sum": IS_LATE},
                            "total_late_minutes": {"$sum": LATE_MINUTES},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "m",
            }
        },
        {"$unwind": {"path": "$m", "preserveNullAndEmptyArrays": True}},  # keeps employees with no logs
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{
                    name: {"$sum": {"$ifNull": [f"$m.{name}", 0]}}
                    for name in (
                        "present_halves", "wh_cents", "wh_count", "late_count",
                        "total_late_minutes", "leave_count", "on_duty_count",
                    )
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, nxt = month_bounds(month)
    items = []
    for r in db.employees.aggregate(summary_pipeline(first, nxt, department), hint=K_DEPT_JOINED):
        avg = None
        if r["wh_count"]:
            avg = float(round_half_up(Decimal(int(r["wh_cents"])) / Decimal(int(r["wh_count"]) * 100), 2))
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": int(r["present_halves"]) / 2,
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


def leaderboard_pipeline(first: Date, nxt: Date, limit: int, department: Optional[str]) -> list:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}],
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4 (ties share a rank, next rank is skipped)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, nxt = month_bounds(month)
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(first, nxt, limit, department), hint=K_DATE_EMP)
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


def validate_trend_range(frm: str, to: str) -> tuple[Date, int]:
    start, end = parse_date_str(frm, "from"), parse_date_str(to, "to")
    if end < start:
        raise HTTPException(422, "'to' must not be before 'from'")
    n_days = (end - start).days + 1
    if n_days > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return start, n_days


def trend_inputs(department: str) -> tuple[list, list]:
    """Employee codes of the department, and how many of them joined on each date (for headcount)."""
    groups = list(
        db.employees.aggregate(
            [
                {"$match": {"department": department}},
                {"$group": {"_id": "$joined_on", "n": {"$sum": 1}, "codes": {"$push": "$emp_code"}}},
                {"$sort": {"_id": 1}},
            ]
        )
    )
    codes = [c for g in groups for c in g["codes"]]
    joins = [{"d": g["_id"], "n": g["n"]} for g in groups]
    return codes, joins


def trend_pipeline(department: str, start: Date, n_days: int, codes: list, joins: list) -> list:
    window = {"documents": [-6, "current"]}  # the current row + up to 6 rows before it (inside the range)
    return [
        # 1) Build one row per calendar day with $range (needs one seed document: any employee of the department).
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "offset": {"$range": [0, n_days]}}},
        {"$unwind": "$offset"},
        {
            "$project": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": {
                            "$dateAdd": {
                                "startDate": {"$dateFromString": {"dateString": start.isoformat()}},
                                "unit": "day",
                                "amount": "$offset",
                            }
                        },
                    }
                }
            }
        },
        # 2) Attach that day's numbers (days without any record simply get an empty result).
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "date",
                "foreignField": "date",
                "pipeline": [
                    {"$match": {"emp_code": {"$in": codes}}},
                    {"$group": {"_id": None, "halves": {"$sum": HALVES_ANY_DAY}, "late_count": {"$sum": IS_LATE}}},
                ],
                "as": "agg",
            }
        },
        {"$unwind": {"path": "$agg", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "halves": {"$ifNull": ["$agg.halves", 0]},
                "late_count": {"$ifNull": ["$agg.late_count", 0]},
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                # R9: employees of the department that joined on or before this day
                "headcount": {
                    "$reduce": {
                        "input": {"$literal": joins},
                        "initialValue": 0,
                        "in": {"$cond": [{"$lte": ["$$this.d", "$date"]}, {"$add": ["$$value", "$$this.n"]}, "$$value"]},
                    }
                },
            }
        },
        {"$addFields": {"is_working_day": {"$in": ["$dow", WEEKDAYS]}}},
        # attendance_rate in units of 0.0001, rounded half-up with integer maths:
        #   rate = halves / (2 * headcount)  ->  units = floor((10000*halves + headcount) / (2*headcount))
        {
            "$addFields": {
                "rate_units": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": ["$halves", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {"$addFields": {"has_rate": {"$cond": [{"$eq": ["$rate_units", None]}, 0, 1]}}},
        {"$sort": {"date": 1}},
        # 3) 7-day moving average over the rounded daily rates (nulls are ignored by $sum / has_rate).
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "win_sum": {"$sum": "$rate_units", "window": window},
                    "win_cnt": {"$sum": "$has_rate", "window": window},
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$divide": ["$halves", 2]},
                "late_count": 1,
                "attendance_rate": {"$cond": [{"$eq": ["$rate_units", None]}, None, {"$divide": ["$rate_units", 10000]}]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$win_cnt", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {"$add": [{"$multiply": ["$win_sum", 2]}, "$win_cnt"]},
                                            {"$multiply": [2, "$win_cnt"]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()):
    start, n_days = validate_trend_range(from_, to)
    codes, joins = trend_inputs(department)
    if not codes:
        raise HTTPException(404, "unknown department")
    rows = db.employees.aggregate(trend_pipeline(department, start, n_days, codes, joins), hint=K_DEPT_JOINED)
    return {"department": department, "items": list(rows)}


# --------------------------------------------------------------------------- #
# 7. Explain endpoint
# --------------------------------------------------------------------------- #
def run_explain(inner_command: dict) -> dict:
    raw = db.command({"explain": inner_command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(raw))  # turns BSON types (timestamps, ids ...) into plain JSON


def need(value, name: str):
    if value is None:
        raise HTTPException(422, f"query parameter '{name}' is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = build_attendance_filter(emp_code, date_from, date_to, status)
        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        first, nxt = month_bounds(need(month, "month"))
        pipeline = monthly_pipeline(need(emp_code, "emp_code"), first, nxt)
        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}, "hint": K_EMP_DATE}
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        first, nxt = month_bounds(need(month, "month"))
        command = {"aggregate": "employees", "pipeline": summary_pipeline(first, nxt, department), "cursor": {}, "hint": K_DEPT_JOINED}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        first, nxt = month_bounds(need(month, "month"))
        command = {
            "aggregate": "attendance_logs",
            "pipeline": leaderboard_pipeline(first, nxt, limit, department),
            "cursor": {},
            "hint": K_DATE_EMP,
        }
        collection = "attendance_logs"
    else:  # department_trend
        dept = need(department, "department")
        start, n_days = validate_trend_range(need(from_, "from"), need(to, "to"))
        codes, joins = trend_inputs(dept)
        command = {
            "aggregate": "employees",
            "pipeline": trend_pipeline(dept, start, n_days, codes, joins),
            "cursor": {},
            "hint": K_DEPT_JOINED,
        }
        collection = "employees"
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}
