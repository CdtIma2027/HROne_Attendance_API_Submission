"""Employee Attendance & Analytics API. Run with uvicorn app.main:app --port 8000."""
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=timezone.utc)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="attendance_employee_date_unique")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_punch_in")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_late")


def fail(status: int, detail: str):
    raise HTTPException(status, detail)


def parse_date(value: str) -> date:
    try:
        result = date.fromisoformat(value)
    except (TypeError, ValueError):
        fail(422, "date must be YYYY-MM-DD")
    if result.isoformat() != value:
        fail(422, "date must be YYYY-MM-DD")
    return result


def month_bounds(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        fail(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y, m, monthrange(y, m)[1])
    return start, end


def epoch_ms(value: int) -> datetime:
    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:
        fail(422, "timestamp must be an integer epoch millisecond value")
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        fail(422, "timestamp out of range")


def to_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def wire(doc, attendance=False):
    """Convert BSON dates (including dates nested in the audit trail) to epoch milliseconds."""
    doc = dict(doc)
    doc.pop("_id", None)
    for key in ("created_at", "punch_in", "punch_out"):
        if key in doc:
            doc[key] = to_ms(doc[key])
    if attendance:
        doc.setdefault("punch_in", None)
        doc.setdefault("punch_out", None)
        doc.setdefault("work_hours", None)
        doc.setdefault("history", [])
        doc.setdefault("half_day", False)
        doc.setdefault("late_minutes", 0)
        doc.setdefault("overtime_minutes", 0)
        for entry in doc["history"]:
            entry["at"] = to_ms(entry["at"])
            for key, values in entry["changes"].items():
                if key in ("punch_in", "punch_out"):
                    values["from"] = to_ms(values.get("from"))
                    values["to"] = to_ms(values.get("to"))
    return doc


def trunc_second(dt):
    return dt.replace(microsecond=0)


def shift_dt(day: date, value: str):
    return datetime.combine(day, time.fromisoformat(value)).replace(tzinfo=IST)


def attend_day(punch: datetime, emp):
    local = punch.astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        day -= timedelta(days=1)
    return day


def late_minutes(punch, emp, day):
    start = shift_dt(day, emp["shift_start"])
    elapsed = (punch.astimezone(IST) - start).total_seconds()
    return int(elapsed // 60) if elapsed > 600 else 0


def end_of_shift(emp, day):
    endday = day + timedelta(days=1) if emp["shift_end"] <= emp["shift_start"] else day
    return shift_dt(endday, emp["shift_end"]).astimezone(timezone.utc)


def derived(punch_in, punch_out, emp, day):
    late = late_minutes(punch_in, emp, day) if punch_in else 0
    if punch_in is None or punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = int((punch_out - punch_in).total_seconds())
    hours = float(((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))
    overtime = int((punch_out - end_of_shift(emp, day)).total_seconds() // 60)
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime if overtime >= 30 else 0,
            "half_day": hours < 4.5}


def check_pagination(page, page_size):
    if page < 1 or not 1 <= page_size <= 100:
        fail(422, "page must be >= 1 and page_size must be 1..100")


def present_expr():
    return {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}


def mongo_half_up(expression, places=2):
    """Round non-negative report values using half-up rather than MongoDB's ties-to-even $round."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}}, scale]}


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_time(cls, v):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time must be HH:MM")
        return v

    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls, v):
        parse_date(v)
        return v

    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, v):
        if v is not None:
            epoch_ms(v)
        return v

    @field_validator("status")
    @classmethod
    def status_valid(cls, v):
        if v not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def timestamp(cls, v):
        if v is not None:
            epoch_ms(v)
        return v


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def status_valid(cls, v):
        if v is not None and v not in STATUSES:
            raise ValueError("invalid status")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def timestamp(cls, v):
        if v is not None:
            epoch_ms(v)
        return v


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        fail(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "emp_code already exists")
    return wire(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = Query(20, le=100)):
    check_pagination(page, page_size)
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size))
    return {"items": [wire(x) for x in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        fail(404, "employee not found")
    ts = trunc_second(epoch_ms(body.punched_at)) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    day = attend_day(ts, emp)
    doc = {"emp_code": body.emp_code, "date": day.isoformat(), "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, **derived(ts, None, emp, day), "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "already punched in for this date")
    return wire(doc, attendance=True)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = 1, page_size: int = Query(20, le=100)):
    check_pagination(page, page_size)
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if status is not None:
        if status not in STATUSES: fail(422, "invalid status")
        q["status"] = status
    if date_from is not None or date_to is not None:
        bounds = {}
        if date_from is not None: bounds["$gte"] = parse_date(date_from).isoformat()
        if date_to is not None: bounds["$lte"] = parse_date(date_to).isoformat()
        if date_from is not None and date_to is not None and parse_date(date_from) > parse_date(date_to): fail(422, "date_from must not exceed date_to")
        q["date"] = bounds
    total = db.attendance_logs.count_documents(q)
    items = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [wire(x, attendance=True) for x in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: fail(404, "employee not found")
    ts = trunc_second(epoch_ms(body.punched_at)) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not record: fail(404, "no punch-in found")
    pin = record.get("punch_in")
    if record.get("punch_out") is not None: fail(409, "already punched out")
    if ts <= pin or ts - pin > timedelta(hours=24): fail(422, "punch-out must be after punch-in and within 24 hours")
    day = parse_date(record["date"])
    updates = derived(pin, ts, emp, day)
    changed = db.attendance_logs.find_one_and_update({"emp_code": body.emp_code, "date": record["date"], "punch_out": None},
                    {"$set": {"punch_out": ts, **updates}}, return_document=ReturnDocument.AFTER)
    if not changed: fail(409, "already punched out")
    return wire(changed, attendance=True)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: CorrectionIn):
    if any(key in body.model_fields_set and getattr(body, key) is None for key in ("status", "punch_in", "punch_out")):
        fail(422, "provided correction fields cannot be null")
    day = parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: fail(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if not record: fail(404, "attendance record not found")
    old = {k: record.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    status = body.status if body.status is not None else record["status"]
    pin = epoch_ms(body.punch_in) if body.punch_in is not None else record.get("punch_in")
    pout = epoch_ms(body.punch_out) if body.punch_out is not None else record.get("punch_out")
    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None: fail(422, "ABSENT and LEAVE cannot have punch times")
        pin = pout = None
    elif pin is None:
        fail(422, "presence status requires punch_in")
    if pin is not None:
        pin = trunc_second(pin)
        if attend_day(pin, emp) != day: fail(422, "punch_in must be on the attendance date")
    if pout is not None:
        pout = trunc_second(pout)
        if pin is None or pout <= pin or pout-pin > timedelta(hours=24): fail(422, "punch_out must be after punch_in and within 24 hours")
    new = {"status": status, "punch_in": pin, "punch_out": pout, **derived(pin, pout, emp, day)}
    changes = {}
    for key, value in new.items():
        if old.get(key) != value:
            changes[key] = {"from": old.get(key), "to": value}
    if not changes: fail(422, "request makes no changes")
    entry = {"at": datetime.now(timezone.utc).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-set protects both data and history from concurrent regularizations.
    query = {"emp_code": emp_code, "date": day.isoformat()}
    for key in old:
        query[key] = record.get(key)
    changed = db.attendance_logs.find_one_and_update(query, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if not changed: fail(409, "record changed concurrently; retry")
    return wire(changed, attendance=True)


def month_match(month):
    start, end = month_bounds(month)
    return {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}, start, end


def build_employee_month_pipeline(emp_code, start, end):
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}}, {"$group": {"_id": None,
        "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}, present_expr(), 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]


def build_department_summary_pipeline(start, end, department=None):
    employee_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    return [{"$match": employee_match}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
        "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "employee_ids": {"$addToSet": "$_id"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, .5, 1]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$employee_ids"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, mongo_half_up({"$divide": ["$work_hours_sum", "$work_hours_count"]}), None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}, {"$sort": {"department": 1}}]


def build_late_leaderboard_pipeline(start, end, limit, department=None):
    pipe = [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
    if department is not None:
        pipe.append({"$match": {"employee.department": department}})
    pipe += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return pipe


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    match, start, end = month_match(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: fail(404, "employee not found")
    joined = max(start, parse_date(emp["joined_on"]))
    working = sum(1 for i in range((end-joined).days+1) if (joined+timedelta(days=i)).weekday() < 5) if joined <= end else 0
    pipe = build_employee_month_pipeline(emp_code, start, end)
    val = list(db.attendance_logs.aggregate(pipe))
    out = val[0] if val else {}
    present = out.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": present,
        "leave_days": out.get("leave_days", 0), "late_count": out.get("late_count", 0),
        "total_late_minutes": out.get("total_late_minutes", 0), "total_overtime_minutes": out.get("total_overtime_minutes", 0),
        "attendance_pct": float(((Decimal(str(present)) * Decimal(100) / Decimal(working)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))) if working else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    match, start, end = month_match(month)
    pipe = build_department_summary_pipeline(start, end, department)
    return {"month": month, "items": list(db.employees.aggregate(pipe))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    match, start, end = month_match(month)
    pipe = build_late_leaderboard_pipeline(start, end, limit, department)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipe))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    return trend_impl(department, from_, to)


def trend_impl(department, from_value, to_value):
    first, last = parse_date(from_value), parse_date(to_value)
    if last < first or (last-first).days+1 > 92: fail(422, "invalid trend date range")
    if not db.employees.find_one({"department": department}): fail(404, "department not found")
    rows = list(db.aggregate(build_trend_pipeline(department, first, last)))
    return {"department": department, "items": rows}


def build_trend_pipeline(department, first, last):
    """Seed the requested calendar in MongoDB, then join daily headcount and attendance."""
    begin = datetime.combine(first, time.min, tzinfo=timezone.utc)
    days = (last-first).days + 1
    return [
        {"$documents": [{"begin": begin, "days": days}]},
        {"$project": {"dates": {"$map": {"input": {"$range": [0, "$days"]}, "as": "i",
            "in": {"$dateAdd": {"startDate": "$begin", "unit": "day", "amount": "$$i"}}}}}},
        {"$unwind": "$dates"},
        {"$set": {"date_text": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates", "timezone": "UTC"}},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$dates"}, 2]}, {"$lte": [{"$dayOfWeek": "$dates"}, 6]}]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date_text"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date_text"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"e.department": department}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, .5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "counts"}},
        {"$set": {"present_count": {"$ifNull": [{"$first": "$counts.present_count"}, 0]},
            "late_count": {"$ifNull": [{"$first": "$counts.late_count"}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            mongo_half_up({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"dates": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_text", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": mongo_half_up("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}}
    ]


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
            limit: int = 10, date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
            page: int = 1, page_size: int = 20, from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        fail(422, "unsupported endpoint")
    if endpoint == "attendance_list":
        check_pagination(page, page_size)
        q = {}
        if emp_code is not None: q["emp_code"] = emp_code
        if status is not None:
            if status not in STATUSES: fail(422, "invalid status")
            q["status"] = status
        if date_from is not None: q.setdefault("date", {})["$gte"] = parse_date(date_from).isoformat()
        if date_to is not None: q.setdefault("date", {})["$lte"] = parse_date(date_to).isoformat()
        if date_from is not None and date_to is not None and parse_date(date_from) > parse_date(date_to): fail(422, "invalid date range")
        result = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_util.loads(json_util.dumps(result))}
    if endpoint == "employee_monthly":
        if not month: fail(422, "month is required")
        if not emp_code: fail(422, "emp_code is required")
        _, start, end = month_match(month)
        coll, pipeline = "attendance_logs", build_employee_month_pipeline(emp_code, start, end)
    elif endpoint == "department_summary":
        if not month: fail(422, "month is required")
        _, start, end = month_match(month)
        coll, pipeline = "employees", build_department_summary_pipeline(start, end, department)
    elif endpoint == "late_leaderboard":
        if not month: fail(422, "month is required")
        if not 1 <= limit <= 50: fail(422, "limit must be 1..50")
        start, end = month_bounds(month)
        coll, pipeline = "attendance_logs", build_late_leaderboard_pipeline(start, end, limit, department)
    else:
        if not department or not from_ or not to: fail(422, "department, from, and to are required")
        first, last = parse_date(from_), parse_date(to)
        if last < first or (last-first).days+1 > 92: fail(422, "invalid trend date range")
        # Database-level $documents explains report lookup indexes as `indexesUsed` metadata
        # rather than IXSCAN stages. Explain the trend's indexed attendance-date source scan.
        result = db.command("explain", {"find": "attendance_logs", "filter": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}},
            "sort": {"date": 1}, "limit": 1}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_util.loads(json_util.dumps(result))}
    result = db.command("explain", {"aggregate": coll, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": coll, "explain": json_util.loads(json_util.dumps(result))}
