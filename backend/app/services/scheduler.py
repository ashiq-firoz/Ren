"""Deferred and recurring jobs: reminders and scheduled agent tasks (e.g. a weekly report every Monday 9:00).

Cron expressions and wall-clock `run_at` values are read in the job's IANA time zone (`ScheduledJob.timezone`), so
"every day at 09:00" in Asia/Kolkata stays at 09:00 local time; stored `run_at` values are always UTC.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.core.db import as_utc, iso, session_scope, utcnow
from app.models import ScheduledJob

log = logging.getLogger("ren.scheduler")

ACTIVE_STATUSES = ("scheduled", "paused")
HISTORY_STATUSES = ("done", "cancelled", "failed")
_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_DAY_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


# ----------------------------------------------------------------------------------------- time zones & cron
# Browsers report CLDR's canonical IDs, which keep pre-rename names (Chrome: "Asia/Calcutta"). Debian's tz database
# ships those links only in `tzdata-legacy`, so map them to current IANA names before validating.
_LEGACY_ZONES = {
    "Asia/Calcutta": "Asia/Kolkata", "Asia/Katmandu": "Asia/Kathmandu", "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Rangoon": "Asia/Yangon", "Asia/Dacca": "Asia/Dhaka", "Asia/Thimbu": "Asia/Thimphu",
    "Asia/Ujung_Pandang": "Asia/Makassar", "Asia/Macao": "Asia/Macau", "Asia/Ulan_Bator": "Asia/Ulaanbaatar",
    "Asia/Chongqing": "Asia/Shanghai", "Asia/Chungking": "Asia/Shanghai", "Asia/Harbin": "Asia/Shanghai",
    "Asia/Kashgar": "Asia/Urumqi", "Asia/Tel_Aviv": "Asia/Jerusalem", "Asia/Istanbul": "Europe/Istanbul",
    "Europe/Kiev": "Europe/Kyiv", "Europe/Uzhgorod": "Europe/Kyiv", "Europe/Zaporozhye": "Europe/Kyiv",
    "Europe/Belfast": "Europe/London", "Atlantic/Faeroe": "Atlantic/Faroe", "Africa/Asmera": "Africa/Asmara",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires", "America/Catamarca": "America/Argentina/Catamarca",
    "America/Cordoba": "America/Argentina/Cordoba", "America/Jujuy": "America/Argentina/Jujuy",
    "America/Mendoza": "America/Argentina/Mendoza", "America/Indianapolis": "America/Indiana/Indianapolis",
    "America/Louisville": "America/Kentucky/Louisville", "America/Coral_Harbour": "America/Atikokan",
    "America/Godthab": "America/Nuuk", "America/Montreal": "America/Toronto", "America/Santa_Isabel": "America/Tijuana",
    "Australia/Currie": "Australia/Hobart", "Pacific/Truk": "Pacific/Chuuk", "Pacific/Ponape": "Pacific/Pohnpei",
    "Pacific/Enderbury": "Pacific/Kanton", "Pacific/Samoa": "Pacific/Pago_Pago",
}


def valid_timezone(tz: str | None) -> str | None:
    """The current IANA name if `tz` is a known time zone (legacy aliases are translated), else None."""
    name = (tz or "").strip()
    if not name or len(name) > 64:
        return None
    if name.upper() in ("UTC", "Z", "GMT", "ETC/UTC", "ETC/GMT"):
        return "UTC"
    name = _LEGACY_ZONES.get(name, name)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def zone(tz: str | None) -> ZoneInfo:
    return ZoneInfo(valid_timezone(tz) or "UTC")


def validate_cron(cron: str) -> str:
    from croniter import croniter

    expr = " ".join((cron or "").split())
    if len(expr.split(" ")) != 5 or not croniter.is_valid(expr):
        raise ValueError(f"'{cron}' is not a valid 5-field cron expression (minute hour day month weekday).")
    return expr


def next_cron_run(cron: str, after: datetime | None = None, tz: str | None = "UTC") -> datetime:
    """Next occurrence (UTC) of `cron` read in time zone `tz`, strictly after `after` (default: now)."""
    from croniter import croniter

    base = as_utc(after or utcnow()).astimezone(zone(tz))
    return as_utc(croniter(cron, base).get_next(datetime))


def upcoming_runs(cron: str | None, run_at: datetime | None, tz: str | None, count: int = 5) -> list[datetime]:
    if cron:
        out: list[datetime] = []
        after = utcnow()
        for _ in range(max(1, min(count, 20))):
            after = next_cron_run(cron, after, tz)
            out.append(after)
        return out
    return [as_utc(run_at)] if run_at else []


def parse_when(value: str, tz: str | None) -> datetime:
    """ISO-8601 datetime → UTC. Values without an offset are wall-clock time in `tz`."""
    text = (value or "").strip()
    if not text:
        raise ValueError("A date and time is required.")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"'{value}' is not an ISO-8601 date/time (e.g. 2026-10-06T09:00).") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone(tz))
    return as_utc(dt)


def _clock(hour: str, minute: str) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def _weekdays(field: str) -> list[int] | None:
    days: list[int] = []
    for part in field.lower().split(","):
        if "-" in part:
            a, _, b = part.partition("-")
            lo, hi = _DAY_NAMES.get(a, int(a) if a.isdigit() else -1), _DAY_NAMES.get(b, int(b) if b.isdigit() else -1)
            if lo < 0 or hi < 0 or lo > hi:
                return None
            days.extend(range(lo, hi + 1))
        elif part in _DAY_NAMES:
            days.append(_DAY_NAMES[part])
        elif part.isdigit() and int(part) <= 7:
            days.append(int(part) % 7)
        else:
            return None
    return sorted(set(d % 7 for d in days))


def describe_cron(cron: str) -> str:
    """Human summary for common 5-field patterns ("Every day at 09:00"); falls back to the expression."""
    parts = cron.split()
    if len(parts) != 5:
        return f"Cron {cron}"
    minute, hour, dom, month, dow = parts
    num = str.isdigit
    every_day = dom == "*" and month == "*"
    if minute.startswith("*/") and num(minute[2:]) and hour == "*" and every_day and dow == "*":
        return f"Every {int(minute[2:])} minutes"
    if num(minute) and hour.startswith("*/") and num(hour[2:]) and every_day and dow == "*":
        return f"Every {int(hour[2:])} hours"
    if num(minute) and hour == "*" and every_day and dow == "*":
        return f"Every hour at :{int(minute):02d}"
    if num(minute) and num(hour):
        at = _clock(hour, minute)
        if every_day:
            if dow == "*":
                return f"Every day at {at}"
            days = _weekdays(dow)
            if days == [1, 2, 3, 4, 5]:
                return f"Weekdays at {at}"
            if days == [0, 6]:
                return f"Weekends at {at}"
            if days:
                return f"Every {', '.join(_DAYS[d] for d in days)} at {at}"
        if num(dom) and month == "*" and dow == "*":
            return f"Monthly on day {int(dom)} at {at}"
    return f"Cron {cron}"


def describe_schedule(cron: str | None, tz: str | None, run_at: datetime | None) -> str:
    name = valid_timezone(tz) or "UTC"
    if cron:
        return f"{describe_cron(cron)} ({name})"
    if run_at:
        local = as_utc(run_at).astimezone(zone(name))
        return f"Once · {local:%a} {local.day} {local:%b %Y}, {local:%H:%M} ({name})"
    return "Not scheduled"


# ----------------------------------------------------------------------------------------- serialization
def job_out(j: ScheduledJob, *, agent_name: str | None = None, last_task: dict | None = None) -> dict:
    payload = dict(j.payload or {})
    tz = valid_timezone(j.timezone) or "UTC"
    instructions = payload.get("instructions") if j.kind == "task" else payload.get("body")
    return {
        "id": j.id, "kind": j.kind, "title": j.title, "status": j.status, "cron": j.cron, "timezone": tz,
        "run_at": iso(j.run_at), "description": describe_schedule(j.cron, tz, j.run_at),
        "last_run_at": iso(j.last_run_at), "run_count": j.run_count or 0, "created_at": iso(j.created_at),
        "user_id": j.user_id, "agent_id": payload.get("agent_id") if j.kind == "task" else None,
        "agent_name": agent_name or payload.get("agent_name"), "instructions": instructions,
        "conversation_id": payload.get("conversation_id"), "last_task": last_task, "payload": payload,
    }


# ----------------------------------------------------------------------------------------- create / cancel
async def schedule_job(
    *,
    org_id: str,
    user_id: str | None,
    kind: str,
    title: str,
    payload: dict,
    run_at: datetime | None = None,
    cron: str | None = None,
    timezone: str | None = None,  # noqa: A002 — mirrors the column / API field name
) -> dict:
    """kind='notification': payload {title, body, link?, channel?}; kind='task': payload {agent_id, title, instructions,
    conversation_id?}. Provide run_at (UTC) and/or a cron expression (read in `timezone`) for recurrence."""
    if run_at is None and not cron:
        raise ValueError("run_at or cron is required")
    tz = valid_timezone(timezone) or "UTC"
    if cron:
        cron = validate_cron(cron)
    if run_at is None:
        run_at = next_cron_run(cron, tz=tz)  # type: ignore[arg-type]
    async with session_scope() as db:
        job = ScheduledJob(org_id=org_id, user_id=user_id, kind=kind, title=title[:300], payload=payload,
                           run_at=as_utc(run_at), cron=cron, timezone=tz)
        db.add(job)
        await db.flush()
        return job_out(job)


async def cancel_job(org_id: str, job_id: str) -> bool:
    async with session_scope() as db:
        job = await db.get(ScheduledJob, job_id)
        if job is None or job.org_id != org_id:
            return False
        job.status = "cancelled"
        return True


# ----------------------------------------------------------------------------------------- running
async def _execute(job_id: str, kind: str, org_id: str, user_id: str | None, payload: dict) -> dict | None:
    """Fire the job's action and record the run. Returns the task snapshot for task jobs."""
    snap = None
    if kind == "notification" and user_id:
        from app.services.notifications import notify_user

        await notify_user(org_id, user_id, "reminder", payload.get("title") or "Reminder",
                          payload.get("body") or "", payload.get("link"))
    elif kind == "task":
        from app.agents.task_manager import task_manager

        snap = await task_manager.create_task(
            org_id=org_id, agent_id=payload["agent_id"], title=payload.get("title") or "Scheduled task",
            instructions=payload.get("instructions") or "", created_by=user_id,
            conversation_id=payload.get("conversation_id"), priority=payload.get("priority", "normal"),
            schedule_id=job_id,
        )
    async with session_scope() as db:
        job = await db.get(ScheduledJob, job_id)
        if job is not None:
            job.run_count = (job.run_count or 0) + 1
            if snap is not None:
                job.last_task_id = snap["id"]
    return snap


async def _run_job(job_id: str) -> None:
    async with session_scope() as db:
        job = await db.get(ScheduledJob, job_id)
        if job is None or job.status != "scheduled":
            return
        payload = dict(job.payload or {})
        kind, org_id, user_id = job.kind, job.org_id, job.user_id
        job.last_run_at = utcnow()
        if job.cron:
            job.run_at = next_cron_run(job.cron, utcnow(), job.timezone)
        else:
            job.status = "done"
    try:
        await _execute(job_id, kind, org_id, user_id, payload)
    except Exception:  # noqa: BLE001
        log.exception("scheduled job %s failed", job_id)
        async with session_scope() as db:
            job = await db.get(ScheduledJob, job_id)
            if job is not None and not job.cron:
                job.status = "failed"


async def run_job_now(job_id: str) -> dict | None:
    """Run a job once immediately without changing its schedule (counts as a run)."""
    async with session_scope() as db:
        job = await db.get(ScheduledJob, job_id)
        if job is None:
            raise ValueError("Scheduled job not found")
        payload = dict(job.payload or {})
        kind, org_id, user_id = job.kind, job.org_id, job.user_id
        job.last_run_at = utcnow()
    return await _execute(job_id, kind, org_id, user_id, payload)


def apply_changes(job: ScheduledJob, changes: dict) -> None:
    """Apply a partial edit to an active or paused job: title, instructions, cron (None → one-off, needs run_at),
    run_at (ISO; no offset = wall-clock in the time zone) and timezone. Raises ValueError for invalid input."""
    if job.status not in ACTIVE_STATUSES:
        raise ValueError("Only active or paused schedules can be changed.")
    payload = dict(job.payload or {})
    if changes.get("title") is not None:
        title = str(changes["title"]).strip()
        if not title:
            raise ValueError("Title can't be empty.")
        job.title = payload["title"] = title[:300]
    if changes.get("instructions") is not None:
        payload["instructions" if job.kind == "task" else "body"] = str(changes["instructions"]).strip()
    old_tz = valid_timezone(job.timezone) or "UTC"
    tz = old_tz
    if changes.get("timezone"):
        tz = valid_timezone(changes["timezone"]) or ""
        if not tz:
            raise ValueError(f"Unknown time zone '{changes['timezone']}'. Use an IANA name such as 'Asia/Kolkata'.")
    if "cron" in changes:
        job.cron = validate_cron(changes["cron"]) if changes["cron"] else None
        if job.cron is None and not changes.get("run_at"):
            raise ValueError("A one-off schedule needs run_at.")
    if job.cron:
        if "cron" in changes or tz != old_tz:
            job.run_at = next_cron_run(job.cron, utcnow(), tz)
    elif changes.get("run_at"):
        job.run_at = parse_when(str(changes["run_at"]), tz)
        if job.run_at < utcnow():
            raise ValueError("run_at is in the past.")
    elif tz != old_tz and job.run_at is not None:  # keep the same wall-clock time in the new zone
        local = as_utc(job.run_at).astimezone(zone(old_tz)).replace(tzinfo=None)
        job.run_at = parse_when(local.isoformat(timespec="minutes"), tz)
    job.timezone = tz
    job.payload = payload


def resume_run_at(job: ScheduledJob) -> datetime:
    """Next run when resuming: recurring jobs skip missed occurrences; overdue one-offs fire on the next tick."""
    if job.cron:
        return next_cron_run(job.cron, utcnow(), job.timezone)
    return as_utc(job.run_at) if job.run_at else utcnow() + timedelta(minutes=1)


async def scheduler_loop(interval: float = 20.0) -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            async with session_scope() as db:
                due = (await db.execute(
                    select(ScheduledJob.id).where(ScheduledJob.status == "scheduled", ScheduledJob.run_at <= now)
                    .limit(50)
                )).scalars().all()
            for jid in due:
                asyncio.create_task(_run_job(jid))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        await asyncio.sleep(interval)
