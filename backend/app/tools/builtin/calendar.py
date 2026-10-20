"""Calendar tools: Google Calendar, Microsoft 365 (Outlook calendar via Graph), or — when no calendar is connected
— a local JSON calendar in the user's workspace (`workspace/.calendar/events.json`) so personal mode works out of
the box. Meeting prep / recap tools combine calendar and email context into an LLM briefing.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin import _http as H
from app.tools.builtin._oauth import google_client, graph_client
from app.tools.builtin.email import google_oauth_fields, norm_addresses, optional_mail_adapter, transcript
from app.tools.connectors import ConnectorField, ConnectorKindSpec, register_connector_kind

CAL_KINDS = ("google_calendar", "microsoft365")
GCAL_BASE = "https://www.googleapis.com/calendar/v3"
GCAL_SCOPES = ("https://www.googleapis.com/auth/calendar",)
UTC = dt.timezone.utc
LOCAL_CALENDAR_PATH = "workspace/.calendar/events.json"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GENERIC_WORDS = {"meeting", "sync", "call", "weekly", "daily", "monthly", "with", "and", "the", "for", "of", "re",
                  "catch", "up", "catchup", "1:1", "1-1", "chat", "review", "standup", "check", "in", "a", "to"}


def _q(v: str) -> str:
    return quote(str(v), safe="")


# =========================================================================== time helpers
def get_tz(name: str | None) -> dt.tzinfo:
    if not name or not str(name).strip():
        return UTC
    try:
        return ZoneInfo(str(name).strip())
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ToolError(f"Unknown time zone '{name}' — use an IANA name such as 'Europe/Berlin' or 'UTC'.") from e


def tz_name(tz: dt.tzinfo) -> str:
    return getattr(tz, "key", None) or "UTC"


def parse_when(value: str, tz: dt.tzinfo) -> tuple[dt.datetime, bool]:
    """ISO date/datetime (or now/today/tomorrow/yesterday) → (aware datetime, is_date_only)."""
    v = (value or "").strip()
    low = v.lower()
    now = dt.datetime.now(tz)
    if low == "now":
        return now, False
    if low in ("today", "tomorrow", "yesterday"):
        d = now.date() + dt.timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[low])
        return dt.datetime(d.year, d.month, d.day, tzinfo=tz), True
    if _DATE_RE.match(v):
        d = dt.date.fromisoformat(v)
        return dt.datetime(d.year, d.month, d.day, tzinfo=tz), True
    try:
        parsed = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError(f"Invalid date/time '{value}' — use ISO 8601 like 2026-10-06T15:00 or 2026-10-06.") from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed, False


def rfc3339(d: dt.datetime) -> str:
    return d.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc_naive(d: dt.datetime) -> str:
    return d.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def _sort_key(e: dict) -> dt.datetime:
    return H.parse_datetime(e.get("start")) or dt.datetime.max.replace(tzinfo=UTC)


def _range(start: str | None, end: str | None, days: int, tz: dt.tzinfo, *, past_days: int = 0) -> tuple[dt.datetime, dt.datetime]:
    if start:
        s, _ = parse_when(start, tz)
    else:
        s = dt.datetime.now(tz) - dt.timedelta(days=past_days)
    if end:
        e, date_only = parse_when(end, tz)
        if date_only:
            e += dt.timedelta(days=1)  # inclusive end date
    else:
        e = s + dt.timedelta(days=max(1, min(int(days or 7), 366)))
    if e <= s:
        raise ToolError("The end of the range must be after its start.")
    return s, e


# =========================================================================== event model
@dataclass
class EventInput:
    title: str | None = None
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    all_day: bool = False
    description: str | None = None
    location: str | None = None
    attendees: list[str] | None = None  # replaces the list
    add_attendees: list[str] | None = None  # appended to the existing list
    meeting_link: bool = False
    tz: dt.tzinfo = UTC


def view(e: dict, *, full: bool = False) -> dict:
    out: dict[str, Any] = {
        "id": e.get("id"), "title": e.get("title"), "start": e.get("start"), "end": e.get("end"),
        "all_day": True if e.get("all_day") else None, "location": e.get("location"),
        "meeting_link": e.get("meeting_link"), "organizer": e.get("organizer"),
        "status": e.get("status") if e.get("status") not in (None, "confirmed") else None,
        "recurring": True if e.get("recurring") else None, "url": e.get("url"),
    }
    atts = e.get("attendees") or []
    if full:
        out["attendees"] = [H.compact(dict(a)) for a in atts]
        out["description"] = H.clip(e.get("description"), 3000)
    elif atts:
        out["attendee_count"] = len(atts)
    return H.compact(out)


def other_attendees(e: dict, me: str = "") -> list[dict]:
    me = (me or "").lower()
    return [a for a in e.get("attendees") or []
            if not a.get("self") and (a.get("email") or "").lower() not in ("", me)]


# =========================================================================== adapters
class CalendarAdapter:
    kind = ""
    label = ""
    remote = True

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any] | None = None):
        self.ctx = ctx
        self.cfg = cfg or {}
        self._me: str | None = None

    def default_tz(self) -> str | None:
        return self.cfg.get("timezone") or None

    async def me(self) -> str:
        return ""

    async def list(self, start: dt.datetime, end: dt.datetime, limit: int, calendar_id: str | None = None,
                   query: str | None = None) -> list[dict]:
        raise NotImplementedError

    async def get(self, event_id: str) -> dict:
        raise NotImplementedError

    async def search(self, query: str, start: dt.datetime, end: dt.datetime, limit: int) -> list[dict]:
        events = await self.list(start, end, 250)
        return _filter(events, query)[:limit]

    async def create(self, ev: EventInput, *, notify: bool, calendar_id: str | None = None) -> dict:
        raise NotImplementedError

    async def update(self, event_id: str, ev: EventInput, *, notify: bool) -> dict:
        raise NotImplementedError

    async def cancel(self, event_id: str, *, message: str | None, notify: bool) -> dict:
        raise NotImplementedError

    async def busy(self, emails: list[str], start: dt.datetime, end: dt.datetime,
                   tz: dt.tzinfo) -> dict[str, Any]:
        raise NotImplementedError

    async def add_link(self, event_id: str, link: str | None, *, notify: bool) -> dict:
        if not link:
            raise ToolError("Pass a meeting link to add.")
        e = await self.get(event_id)
        desc = (e.get("description") or "").rstrip()
        ev = EventInput(description=(desc + "\n\n" if desc else "") + f"Join: {link}")
        if not e.get("location"):
            ev.location = link
        return await self.update(event_id, ev, notify=notify)


def _filter(events: list[dict], query: str) -> list[dict]:
    terms = [t for t in re.split(r"\s+", (query or "").lower()) if t]
    if not terms:
        return events
    out = []
    for e in events:
        hay = " ".join(str(x or "") for x in (
            e.get("title"), e.get("description"), e.get("location"), e.get("organizer"),
            " ".join(f"{a.get('name') or ''} {a.get('email') or ''}" for a in e.get("attendees") or []))).lower()
        if all(t in hay for t in terms):
            out.append(e)
    return out


# --------------------------------------------------------------------------- Google Calendar
class GoogleCalendarAdapter(CalendarAdapter):
    kind = "google_calendar"
    label = "Google Calendar"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        self.api = google_client(cfg, GCAL_BASE, service="Google Calendar", scopes=GCAL_SCOPES)
        self.cal = str(cfg.get("calendar_id") or "primary")

    async def me(self) -> str:
        if self._me is None:
            data = await self.api.get(f"/calendars/{_q(self.cal)}") or {}
            self._me = data.get("id") if "@" in str(data.get("id", "")) else ""
        return self._me or ""

    @staticmethod
    def norm(e: dict) -> dict:
        s, en = e.get("start") or {}, e.get("end") or {}
        link = e.get("hangoutLink") or next(
            (p.get("uri") for p in ((e.get("conferenceData") or {}).get("entryPoints") or [])
             if p.get("entryPointType") == "video"), None)
        return {
            "id": e.get("id"), "title": e.get("summary") or "(no title)", "start": s.get("dateTime") or s.get("date"),
            "end": en.get("dateTime") or en.get("date"), "all_day": "date" in s and "dateTime" not in s,
            "location": e.get("location"), "meeting_link": link,
            "organizer": (e.get("organizer") or {}).get("email"), "status": e.get("status"),
            "url": e.get("htmlLink"), "description": e.get("description"),
            "recurring": bool(e.get("recurringEventId") or e.get("recurrence")),
            "attendees": [H.compact({"email": a.get("email"), "name": a.get("displayName"),
                                     "response": a.get("responseStatus"), "organizer": a.get("organizer") or None,
                                     "self": a.get("self") or None, "optional": a.get("optional") or None})
                          for a in e.get("attendees") or []],
        }

    async def list(self, start, end, limit, calendar_id=None, query=None):
        data = await self.api.get(f"/calendars/{_q(calendar_id or self.cal)}/events", params={
            "timeMin": rfc3339(start), "timeMax": rfc3339(end), "singleEvents": True, "orderBy": "startTime",
            "maxResults": max(1, min(limit, 250)), "q": query or None}) or {}
        return [self.norm(e) for e in data.get("items", []) if e.get("status") != "cancelled"]

    async def search(self, query, start, end, limit):
        return await self.list(start, end, limit, query=query)

    async def get(self, event_id):
        return self.norm(await self.api.get(f"/calendars/{_q(self.cal)}/events/{_q(event_id)}"))

    @staticmethod
    def _time(d: dt.datetime, all_day: bool, tz: dt.tzinfo) -> dict:
        if all_day:
            return {"date": d.date().isoformat()}
        return {"dateTime": d.isoformat(), "timeZone": tz_name(tz)}

    async def _body(self, ev: EventInput, event_id: str | None = None) -> dict:
        body: dict[str, Any] = {}
        if ev.title is not None:
            body["summary"] = ev.title
        if ev.description is not None:
            body["description"] = ev.description
        if ev.location is not None:
            body["location"] = ev.location
        if ev.start is not None:
            body["start"] = self._time(ev.start, ev.all_day, ev.tz)
        if ev.end is not None:
            body["end"] = self._time(ev.end, ev.all_day, ev.tz)
        if ev.attendees is not None or ev.add_attendees:
            current: list[dict] = []
            if ev.attendees is None and event_id:
                raw = await self.api.get(f"/calendars/{_q(self.cal)}/events/{_q(event_id)}")
                current = list(raw.get("attendees") or [])
            emails = {(a.get("email") or "").lower() for a in current}
            for addr in (ev.attendees or []) + (ev.add_attendees or []):
                if addr.lower() not in emails:
                    current.append({"email": addr})
                    emails.add(addr.lower())
            body["attendees"] = current
        if ev.meeting_link:
            body["conferenceData"] = {"createRequest": {"requestId": uuid.uuid4().hex,
                                                        "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        return body

    async def create(self, ev, *, notify, calendar_id=None):
        body = await self._body(ev)
        e = await self.api.post(f"/calendars/{_q(calendar_id or self.cal)}/events", json=body, params={
            "sendUpdates": "all" if notify else "none", "conferenceDataVersion": 1 if ev.meeting_link else None})
        return self.norm(e)

    async def update(self, event_id, ev, *, notify):
        body = await self._body(ev, event_id)
        if not body:
            raise ToolError("Nothing to update.")
        e = await self.api.patch(f"/calendars/{_q(self.cal)}/events/{_q(event_id)}", json=body, params={
            "sendUpdates": "all" if notify else "none", "conferenceDataVersion": 1 if ev.meeting_link else None})
        return self.norm(e)

    async def cancel(self, event_id, *, message, notify):
        await self.api.delete(f"/calendars/{_q(self.cal)}/events/{_q(event_id)}",
                              params={"sendUpdates": "all" if notify else "none"}, allow_status=(410,))
        return {"cancelled": True, "id": event_id, "attendees_notified": notify}

    async def busy(self, emails, start, end, tz):
        items = [{"id": self.cal}] + [{"id": e} for e in emails]
        data = await self.api.post("/freeBusy", json={"timeMin": rfc3339(start), "timeMax": rfc3339(end),
                                                       "timeZone": tz_name(tz), "items": items}) or {}
        out: dict[str, Any] = {}
        for cid, info in (data.get("calendars") or {}).items():
            name = "me" if cid == self.cal else cid
            if info.get("errors"):
                out[name] = {"error": (info["errors"][0] or {}).get("reason", "unavailable")}
            else:
                out[name] = [(H.parse_datetime(b["start"]), H.parse_datetime(b["end"])) for b in info.get("busy", [])]
        return out

    async def add_link(self, event_id, link, *, notify):
        if link:
            return await super().add_link(event_id, link, notify=notify)
        return await self.update(event_id, EventInput(meeting_link=True), notify=notify)


# --------------------------------------------------------------------------- Microsoft 365 (Graph)
GRAPH_EVENT_SELECT = ("id,subject,start,end,isAllDay,location,organizer,attendees,onlineMeeting,onlineMeetingUrl,"
                      "isOnlineMeeting,webLink,isCancelled,bodyPreview,body,showAs,type,seriesMasterId,isOrganizer")
UTC_PREF = {"Prefer": 'outlook.timezone="UTC", outlook.body-content-type="text"'}


class GraphCalendarAdapter(CalendarAdapter):
    kind = "microsoft365"
    label = "Microsoft 365 Calendar"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        self.api = graph_client(cfg, service="Microsoft 365 Calendar", reconnect="Microsoft 365")

    async def me(self) -> str:
        if self._me is None:
            if self.cfg.get("user") and not self.cfg.get("refresh_token"):
                self._me = str(self.cfg["user"])
            else:
                data = await self.api.get("", params={"$select": "mail,userPrincipalName"}) or {}
                self._me = data.get("mail") or data.get("userPrincipalName") or ""
        return self._me

    @staticmethod
    def norm(e: dict) -> dict:
        all_day = bool(e.get("isAllDay"))

        def when(x: dict | None) -> str | None:
            raw = (x or {}).get("dateTime")
            if not raw:
                return None
            if all_day:
                return raw[:10]
            d = H.parse_datetime(raw)
            return H.iso(d) if d else raw

        body = (e.get("body") or {}).get("content") or e.get("bodyPreview") or ""
        if (e.get("body") or {}).get("contentType", "").lower() == "html":
            body = H.html_to_text(body)
        organizer = ((e.get("organizer") or {}).get("emailAddress") or {}).get("address")
        return {
            "id": e.get("id"), "title": e.get("subject") or "(no title)", "start": when(e.get("start")),
            "end": when(e.get("end")), "all_day": all_day,
            "location": (e.get("location") or {}).get("displayName") or None,
            "meeting_link": (e.get("onlineMeeting") or {}).get("joinUrl") or e.get("onlineMeetingUrl"),
            "organizer": organizer, "status": "cancelled" if e.get("isCancelled") else "confirmed",
            "url": e.get("webLink"), "description": body.strip() or None,
            "recurring": bool(e.get("seriesMasterId") or e.get("type") in ("occurrence", "seriesMaster")),
            "is_organizer": e.get("isOrganizer"),
            "attendees": [H.compact({"email": (a.get("emailAddress") or {}).get("address"),
                                     "name": (a.get("emailAddress") or {}).get("name"),
                                     "response": (a.get("status") or {}).get("response"),
                                     "optional": a.get("type") == "optional" or None})
                          for a in e.get("attendees") or []],
        }

    async def list(self, start, end, limit, calendar_id=None, query=None):
        path = f"/calendars/{_q(calendar_id)}/calendarView" if calendar_id else "/calendarView"
        params = {"startDateTime": rfc3339(start), "endDateTime": rfc3339(end), "$top": max(1, min(limit, 250)),
                  "$orderby": "start/dateTime", "$select": GRAPH_EVENT_SELECT}
        data = await self.api.get(path, params=params, headers=UTC_PREF) or {}
        events = [self.norm(e) for e in data.get("value", []) if not e.get("isCancelled")]
        return _filter(events, query) if query else events

    async def get(self, event_id):
        return self.norm(await self.api.get(f"/events/{_q(event_id)}", params={"$select": GRAPH_EVENT_SELECT},
                                            headers=UTC_PREF))

    @staticmethod
    def _time(d: dt.datetime, all_day: bool) -> dict:
        if all_day:
            return {"dateTime": d.date().isoformat() + "T00:00:00", "timeZone": "UTC"}
        return {"dateTime": _utc_naive(d), "timeZone": "UTC"}

    async def _body(self, ev: EventInput, event_id: str | None = None) -> dict:
        body: dict[str, Any] = {}
        if ev.title is not None:
            body["subject"] = ev.title
        if ev.description is not None:
            body["body"] = {"contentType": "text", "content": ev.description}
        if ev.location is not None:
            body["location"] = {"displayName": ev.location}
        if ev.start is not None:
            body["start"] = self._time(ev.start, ev.all_day)
            body["isAllDay"] = ev.all_day
        if ev.end is not None:
            body["end"] = self._time(ev.end, ev.all_day)
        if ev.attendees is not None or ev.add_attendees:
            current: list[dict] = []
            if ev.attendees is None and event_id:
                raw = await self.api.get(f"/events/{_q(event_id)}", params={"$select": "attendees"})
                current = list(raw.get("attendees") or [])
            emails = {((a.get("emailAddress") or {}).get("address") or "").lower() for a in current}
            for addr in (ev.attendees or []) + (ev.add_attendees or []):
                if addr.lower() not in emails:
                    current.append({"emailAddress": {"address": addr}, "type": "required"})
                    emails.add(addr.lower())
            body["attendees"] = current
        if ev.meeting_link:
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"
        return body

    async def create(self, ev, *, notify, calendar_id=None):
        path = f"/calendars/{_q(calendar_id)}/events" if calendar_id else "/events"
        return self.norm(await self.api.post(path, json=await self._body(ev), headers=UTC_PREF))

    async def update(self, event_id, ev, *, notify):
        body = await self._body(ev, event_id)
        if not body:
            raise ToolError("Nothing to update.")
        return self.norm(await self.api.patch(f"/events/{_q(event_id)}", json=body, headers=UTC_PREF))

    async def cancel(self, event_id, *, message, notify):
        e = await self.get(event_id)
        if notify and e.get("is_organizer") and e.get("attendees"):
            await self.api.post(f"/events/{_q(event_id)}/cancel", json={"comment": message or ""})
            return {"cancelled": True, "id": event_id, "attendees_notified": True}
        await self.api.delete(f"/events/{_q(event_id)}")
        return {"cancelled": True, "id": event_id, "attendees_notified": False}

    async def busy(self, emails, start, end, tz):
        me = await self.me()
        schedules = ([me] if me else []) + [e for e in emails if e.lower() != (me or "").lower()]
        data = await self.api.post("/calendar/getSchedule", json={
            "schedules": schedules, "startTime": {"dateTime": _utc_naive(start), "timeZone": "UTC"},
            "endTime": {"dateTime": _utc_naive(end), "timeZone": "UTC"}, "availabilityViewInterval": 30}) or {}
        out: dict[str, Any] = {}
        for sch in data.get("value", []):
            sid = sch.get("scheduleId") or "?"
            name = "me" if me and sid.lower() == me.lower() else sid
            if sch.get("error"):
                out[name] = {"error": (sch["error"] or {}).get("message", "unavailable")}
                continue
            blocks = []
            for item in sch.get("scheduleItems") or []:
                if (item.get("status") or "busy").lower() == "free":
                    continue
                s = H.parse_datetime((item.get("start") or {}).get("dateTime"))
                en = H.parse_datetime((item.get("end") or {}).get("dateTime"))
                if s and en:
                    blocks.append((s, en))
            out[name] = blocks
        return out

    async def add_link(self, event_id, link, *, notify):
        if link:
            return await super().add_link(event_id, link, notify=notify)
        return await self.update(event_id, EventInput(meeting_link=True), notify=notify)


# --------------------------------------------------------------------------- local JSON calendar
_LOCAL_LOCK = threading.Lock()


class LocalCalendarAdapter(CalendarAdapter):
    kind = "local"
    label = "Local calendar (workspace)"
    remote = False

    def __init__(self, ctx: ToolContext):
        super().__init__(ctx, {})
        self.path = H.ws_path(ctx, LOCAL_CALENDAR_PATH, for_write=True)

    def default_tz(self) -> str | None:
        return None

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text("utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as e:
            raise ToolError(f"The local calendar file ({LOCAL_CALENDAR_PATH}) is unreadable or corrupted.") from e
        events = data.get("events") if isinstance(data, dict) else data
        return [e for e in events or [] if isinstance(e, dict)]

    def _write(self, events: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "events": events}, indent=1, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)

    def _mutate(self, fn):
        if not self.ctx.can_write:
            raise ToolError("Your role is read-only, so the calendar can't be changed.")
        with _LOCAL_LOCK:
            events = self._read()
            result = fn(events)
            self._write(events)
            return result

    @staticmethod
    def _bounds(e: dict) -> tuple[dt.datetime, dt.datetime]:
        s = H.parse_datetime(e.get("start")) or dt.datetime.min.replace(tzinfo=UTC)
        en = H.parse_datetime(e.get("end")) or s
        if e.get("all_day") and _DATE_RE.match(str(e.get("start") or "")):
            s = dt.datetime.fromisoformat(str(e["start"])).replace(tzinfo=UTC)
            en = dt.datetime.fromisoformat(str(e.get("end") or e["start"])).replace(tzinfo=UTC)
        return s, en

    async def list(self, start, end, limit, calendar_id=None, query=None):
        with _LOCAL_LOCK:
            events = self._read()
        out = []
        for e in events:
            if e.get("status") == "cancelled":
                continue
            s, en = self._bounds(e)
            if s < end and en > start:
                out.append(e)
        out.sort(key=_sort_key)
        return (_filter(out, query) if query else out)[:limit]

    async def get(self, event_id):
        with _LOCAL_LOCK:
            events = self._read()
        for e in events:
            if e.get("id") == event_id:
                return e
        raise ToolError(f"No event with id '{event_id}' in the local calendar.")

    @staticmethod
    def _stamp(d: dt.datetime, all_day: bool) -> str:
        return d.date().isoformat() if all_day else d.isoformat()

    async def create(self, ev, *, notify, calendar_id=None):
        now = H.iso(dt.datetime.now(UTC))
        event = {
            "id": uuid.uuid4().hex[:16], "title": ev.title or "(no title)",
            "start": self._stamp(ev.start, ev.all_day), "end": self._stamp(ev.end, ev.all_day), "all_day": ev.all_day,
            "location": ev.location, "description": ev.description, "meeting_link": None, "status": "confirmed",
            "attendees": [{"email": a, "response": "needsAction"} for a in (ev.attendees or [])],
            "created_at": now, "updated_at": now,
        }
        self._mutate(lambda events: events.append(event))
        return event

    async def update(self, event_id, ev, *, notify):
        def apply(events: list[dict]) -> dict:
            for e in events:
                if e.get("id") != event_id:
                    continue
                if ev.title is not None:
                    e["title"] = ev.title
                if ev.description is not None:
                    e["description"] = ev.description
                if ev.location is not None:
                    e["location"] = ev.location
                if ev.start is not None:
                    e["start"] = self._stamp(ev.start, ev.all_day)
                    e["all_day"] = ev.all_day
                if ev.end is not None:
                    e["end"] = self._stamp(ev.end, ev.all_day)
                if ev.attendees is not None:
                    e["attendees"] = [{"email": a, "response": "needsAction"} for a in ev.attendees]
                for a in ev.add_attendees or []:
                    if a.lower() not in {(x.get("email") or "").lower() for x in e.get("attendees") or []}:
                        e.setdefault("attendees", []).append({"email": a, "response": "needsAction"})
                if ev.meeting_link and not e.get("meeting_link"):
                    raise ToolError("The local calendar can't generate meeting links — pass `link` with a Zoom/Meet/"
                                    "Teams URL.")
                e["updated_at"] = H.iso(dt.datetime.now(UTC))
                return dict(e)
            raise ToolError(f"No event with id '{event_id}' in the local calendar.")

        return self._mutate(apply)

    async def cancel(self, event_id, *, message, notify):
        def apply(events: list[dict]) -> dict:
            for i, e in enumerate(events):
                if e.get("id") == event_id:
                    events.pop(i)
                    return {"cancelled": True, "id": event_id, "title": e.get("title"), "attendees_notified": False}
            raise ToolError(f"No event with id '{event_id}' in the local calendar.")

        return self._mutate(apply)

    async def busy(self, emails, start, end, tz):
        events = await self.list(start, end, 1000)
        out: dict[str, Any] = {"me": [self._bounds(e) for e in events if not e.get("all_day")]}
        for addr in emails:
            out[addr] = {"error": "unknown — no calendar service is connected"}
        return out

    async def add_link(self, event_id, link, *, notify):
        if not link:
            raise ToolError("The local calendar can't generate meeting links — pass `link` with a Zoom/Meet/Teams URL.")

        def apply(events: list[dict]) -> dict:
            for e in events:
                if e.get("id") == event_id:
                    e["meeting_link"] = link
                    e["updated_at"] = H.iso(dt.datetime.now(UTC))
                    return dict(e)
            raise ToolError(f"No event with id '{event_id}' in the local calendar.")

        return self._mutate(apply)


ADAPTERS: dict[str, type[CalendarAdapter]] = {"google_calendar": GoogleCalendarAdapter,
                                              "microsoft365": GraphCalendarAdapter}


async def calendar_adapter(ctx: ToolContext, account: str | None = None) -> CalendarAdapter:
    if account and account.strip().lower() in ("local", "workspace"):
        return LocalCalendarAdapter(ctx)
    if not await H.connected(ctx, CAL_KINDS):
        if account:
            raise ToolError(f"No calendar account named '{account}' is connected (only the local calendar is "
                            "available — use account='local').")
        return LocalCalendarAdapter(ctx)
    kind, cfg = await H.pick_connector(ctx, CAL_KINDS, account, what="Calendar")
    return ADAPTERS[kind](ctx, cfg)


def _zone(ad: CalendarAdapter, timezone: str | None) -> dt.tzinfo:
    return get_tz(timezone or ad.default_tz())


def _times(start: str, end: str | None, duration_minutes: int | None, all_day: bool,
           tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime, bool]:
    s, date_only = parse_when(start, tz)
    all_day = all_day or date_only
    if end:
        e, _ = parse_when(end, tz)
    elif all_day:
        e = s + dt.timedelta(days=1)
    else:
        e = s + dt.timedelta(minutes=max(5, int(duration_minutes or 30)))
    if all_day:
        s = dt.datetime(s.year, s.month, s.day, tzinfo=tz)
        e = dt.datetime(e.year, e.month, e.day, tzinfo=tz)
        if e <= s:
            e = s + dt.timedelta(days=1)
    if e <= s:
        raise ToolError("The event must end after it starts.")
    return s, e, all_day


# =========================================================================== tool group
group = ToolGroup(
    "calendar", "Calendar",
    "View, schedule and update meetings in Google Calendar or Microsoft 365 (or a built-in local calendar when "
    "none is connected); check availability and prepare meeting briefings.",
    prefix="calendar", icon="calendar", category="productivity", connector_kinds=CAL_KINDS, connector_optional=True,
)


def _invite_risk(args: dict) -> Risk:
    return Risk.HIGH if H.as_list(args.get("attendees")) else Risk.MEDIUM


def _update_risk(args: dict) -> Risk:
    if args.get("notify_attendees") or H.as_list(args.get("attendees")) or H.as_list(args.get("add_attendees")):
        return Risk.HIGH
    return Risk.MEDIUM


def _create_preview(a: dict) -> str:
    atts = H.as_list(a.get("attendees"))
    return H.preview("Create calendar event" + (" and send invitations" if atts else ""), [
        ("Calendar", a.get("account") or "default calendar"), ("Title", a.get("title")), ("Start", a.get("start")),
        ("End", a.get("end") or (f"{a.get('duration_minutes') or 30} minutes after start" if not a.get("all_day") else None)),
        ("All day", a.get("all_day") or None), ("Time zone", a.get("timezone")), ("Location", a.get("location")),
        ("Invite (emails will be sent)", atts), ("Add video meeting link", a.get("add_meeting_link") or None),
    ], a.get("description") if a.get("description") else None, body_label="Description")


def _update_preview(a: dict) -> str:
    rows = [("Calendar", a.get("account") or "default calendar"), ("Event", a.get("event_id")),
            ("New title", a.get("title")), ("New start", a.get("start")), ("New end", a.get("end")),
            ("New location", a.get("location")), ("Replace attendees with", H.as_list(a.get("attendees"))),
            ("Add attendees", H.as_list(a.get("add_attendees"))),
            ("Notify attendees", bool(a.get("notify_attendees")))]
    return H.preview("Update calendar event", rows, a.get("description"), body_label="New description")


@group.tool("list", risk=Risk.LOW, scope="read")
async def list_events(ctx: ToolContext, start: str | None = None, end: str | None = None, days: int = 7,
                      limit: int = 50, timezone: str | None = None, calendar_id: str | None = None,
                      account: str | None = None) -> dict:
    """List calendar events in a time range (default: from now for the next 7 days), soonest first.

    Args:
        start: Range start (ISO date/datetime, or now/today/tomorrow). Default: now.
        end: Range end (an end date is inclusive). Default: start + `days`.
        days: Range length in days when `end` is omitted.
        limit: Maximum events to return (1-250).
        timezone: IANA time zone for interpreting times without an offset, e.g. "Europe/Berlin".
        calendar_id: A specific calendar (default: the primary calendar).
        account: Calendar connector name or kind (google_calendar, microsoft365, or "local").
    """
    ad = await calendar_adapter(ctx, account)
    tz = _zone(ad, timezone)
    s, e = _range(start, end, days, tz)
    events = await ad.list(s, e, max(1, min(int(limit or 50), 250)), calendar_id)
    return {"calendar": ad.label, "range": {"start": rfc3339(s), "end": rfc3339(e)}, "count": len(events),
            "events": [view(x) for x in events]}


@group.tool(risk=Risk.LOW, scope="read")
async def get_event(ctx: ToolContext, event_id: str, account: str | None = None) -> dict:
    """Get one event with its full details: description, attendees and their responses, meeting link.

    Args:
        event_id: Event id from calendar_list / calendar_search.
        account: Calendar connector name or kind (or "local").
    """
    ad = await calendar_adapter(ctx, account)
    return {"calendar": ad.label, **view(await ad.get(event_id), full=True)}


@group.tool(risk=Risk.LOW, scope="read")
async def search(ctx: ToolContext, query: str, start: str | None = None, end: str | None = None, limit: int = 20,
                 timezone: str | None = None, account: str | None = None) -> dict:
    """Search events by words in the title, description, location or attendees (default window: 30 days back to
    180 days ahead).

    Args:
        query: Words to match, e.g. "board meeting" or an attendee's name/email.
        start: Window start (ISO date/datetime).
        end: Window end.
        limit: Maximum events to return.
        timezone: IANA time zone for times without an offset.
        account: Calendar connector name or kind (or "local").
    """
    ad = await calendar_adapter(ctx, account)
    tz = _zone(ad, timezone)
    s, e = _range(start, end, 210, tz, past_days=0 if start else 30)
    events = await ad.search(query, s, e, max(1, min(int(limit or 20), 100)))
    return {"calendar": ad.label, "query": query, "count": len(events), "events": [view(x) for x in events]}


@group.tool(risk=Risk.MEDIUM, scope="write", risk_fn=_invite_risk, preview=_create_preview,
            content_fields=("title", "description"))
async def create(ctx: ToolContext, title: str, start: str, end: str | None = None, duration_minutes: int = 30,
                 all_day: bool = False, description: str | None = None, location: str | None = None,
                 attendees: list[str] | None = None, add_meeting_link: bool = False, timezone: str | None = None,
                 calendar_id: str | None = None, account: str | None = None) -> dict:
    """Create a calendar event. With attendees, invitations are emailed to them.

    Args:
        title: Event title.
        start: Start, ISO 8601 (e.g. "2026-10-06T15:00", or "2026-10-06" for an all-day event). Times without an offset use `timezone`.
        end: End time. For all-day events the end date is exclusive (the day after the last day). Default: start + duration.
        duration_minutes: Length when `end` is omitted.
        all_day: Create an all-day event.
        description: Agenda / notes.
        location: Room or address.
        attendees: Email addresses to invite (invitations are sent).
        add_meeting_link: Attach a Google Meet / Teams video link (connected calendars only).
        timezone: IANA time zone, e.g. "America/New_York" (default: connector setting or UTC).
        calendar_id: A specific calendar (default: primary).
        account: Calendar connector name or kind (or "local").
    """
    ad = await calendar_adapter(ctx, account)
    tz = _zone(ad, timezone)
    s, e, all_day = _times(start, end, duration_minutes, all_day, tz)
    invitees = [a.split("<")[-1].strip(" >") for a in norm_addresses(attendees, "attendees")]
    if add_meeting_link and not ad.remote:
        raise ToolError("The local calendar can't create video links — add one later with calendar_add_meeting_link "
                        "and a `link`.")
    ev = EventInput(title=title, start=s, end=e, all_day=all_day, description=description, location=location,
                    attendees=invitees or None, meeting_link=add_meeting_link, tz=tz)
    created = await ad.create(ev, notify=bool(invitees), calendar_id=calendar_id)
    out = {"calendar": ad.label, "created": True, **view(created, full=True)}
    if invitees and not ad.remote:
        out["note"] = "Local calendar: attendees were recorded but not invited (no calendar service is connected)."
    return out


@group.tool(risk=Risk.MEDIUM, scope="write", risk_fn=_update_risk, preview=_update_preview,
            content_fields=("title", "description"))
async def update(ctx: ToolContext, event_id: str, title: str | None = None, start: str | None = None,
                 end: str | None = None, description: str | None = None, location: str | None = None,
                 attendees: list[str] | None = None, add_attendees: list[str] | None = None,
                 notify_attendees: bool = False, timezone: str | None = None, account: str | None = None) -> dict:
    """Change an event's title, time, description, location or attendees.

    Events with other attendees can only be changed with notify_attendees=true (they will see the change).

    Args:
        event_id: Event id.
        title: New title.
        start: New start (ISO 8601). If only start is given, the original duration is kept.
        end: New end.
        description: New description (replaces the old one).
        location: New location.
        attendees: Replace the attendee list with these emails.
        add_attendees: Invite these additional emails.
        notify_attendees: Send update notifications to attendees (required when the event has other attendees).
        timezone: IANA time zone for times without an offset.
        account: Calendar connector name or kind (or "local").
    """
    ad = await calendar_adapter(ctx, account)
    tz = _zone(ad, timezone)
    current = await ad.get(event_id)
    others = other_attendees(current, await ad.me()) if ad.remote else []
    if others and not notify_attendees:
        raise ToolError(f"This event has {len(others)} other attendee(s) who will see any change. Re-run with "
                        "notify_attendees=true to update it and notify them.")
    ev = EventInput(title=title, description=description, location=location, tz=tz,
                    attendees=[a.split("<")[-1].strip(" >") for a in norm_addresses(attendees, "attendees")]
                    if attendees is not None else None,
                    add_attendees=[a.split("<")[-1].strip(" >") for a in norm_addresses(add_attendees, "add_attendees")]
                    or None)
    if start or end:
        cur_s = H.parse_datetime(current.get("start"))
        cur_e = H.parse_datetime(current.get("end"))
        duration = (cur_e - cur_s) if cur_s and cur_e else dt.timedelta(minutes=30)
        if start:
            s, ev.all_day = parse_when(start, tz)
            ev.start = s
            ev.end = parse_when(end, tz)[0] if end else s + duration
        else:
            ev.start = cur_s
            ev.end = parse_when(end, tz)[0]
            ev.all_day = bool(current.get("all_day"))
        if ev.start is None or ev.end is None:
            raise ToolError("Could not determine the event's new time range — pass both start and end.")
        if ev.all_day:
            ev.start = dt.datetime(ev.start.year, ev.start.month, ev.start.day, tzinfo=tz)
            ev.end = dt.datetime(ev.end.year, ev.end.month, ev.end.day, tzinfo=tz)
            if ev.end <= ev.start:
                ev.end = ev.start + dt.timedelta(days=1)
        if ev.end <= ev.start:
            raise ToolError("The event must end after it starts.")
    updated = await ad.update(event_id, ev, notify=bool(notify_attendees or ev.attendees or ev.add_attendees))
    return {"calendar": ad.label, "updated": True, **view(updated, full=True)}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("message",),
            preview=lambda a: H.preview("Cancel calendar event", [
                ("Calendar", a.get("account") or "default calendar"), ("Event", a.get("event_id")),
                ("Notify attendees", a.get("notify_attendees", True))],
                a.get("message") if a.get("message") else None, body_label="Cancellation message"))
async def cancel(ctx: ToolContext, event_id: str, message: str | None = None, notify_attendees: bool = True,
                 account: str | None = None) -> dict:
    """Cancel (delete) an event; as organizer, attendees get a cancellation notice.

    Args:
        event_id: Event id.
        message: Optional note included in the cancellation (Microsoft 365).
        notify_attendees: Send cancellation notices to attendees.
        account: Calendar connector name or kind (or "local").
    """
    ad = await calendar_adapter(ctx, account)
    return {"calendar": ad.label, **await ad.cancel(event_id, message=message, notify=notify_attendees)}


@group.tool(risk=Risk.LOW, scope="read")
async def attendees(ctx: ToolContext, event_id: str, account: str | None = None) -> dict:
    """List an event's attendees with their response status (accepted / declined / tentative / no response).

    Args:
        event_id: Event id.
        account: Calendar connector name or kind (or "local").
    """
    ad = await calendar_adapter(ctx, account)
    e = await ad.get(event_id)
    atts = e.get("attendees") or []
    summary: dict[str, int] = {}
    for a in atts:
        r = a.get("response") or "unknown"
        summary[r] = summary.get(r, 0) + 1
    return {"event": e.get("title"), "start": e.get("start"), "organizer": e.get("organizer"),
            "attendees": [H.compact(dict(a)) for a in atts], "responses": summary}


def _merge(blocks: list[tuple[dt.datetime, dt.datetime]]) -> list[tuple[dt.datetime, dt.datetime]]:
    out: list[tuple[dt.datetime, dt.datetime]] = []
    for s, e in sorted(b for b in blocks if b[0] and b[1]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def free_slots(busy: list[tuple[dt.datetime, dt.datetime]], start: dt.datetime, end: dt.datetime,
               duration: dt.timedelta, work: tuple[dt.time, dt.time], tz: dt.tzinfo, *, weekends: bool = False,
               max_slots: int = 12) -> list[dict]:
    merged = _merge(busy)
    slots: list[dict] = []
    day = start.astimezone(tz).date()
    last = end.astimezone(tz).date()
    while day <= last and len(slots) < max_slots:
        if weekends or day.weekday() < 5:
            ws = max(dt.datetime.combine(day, work[0], tzinfo=tz), start)
            we = min(dt.datetime.combine(day, work[1], tzinfo=tz), end)
            cur = ws
            for bs, be in merged:
                if be <= cur or bs >= we:
                    continue
                if bs - cur >= duration:
                    slots.append({"start": cur.isoformat(), "end": bs.isoformat(),
                                  "minutes": int((bs - cur).total_seconds() // 60)})
                cur = max(cur, be)
            if we - cur >= duration:
                slots.append({"start": cur.isoformat(), "end": we.isoformat(),
                              "minutes": int((we - cur).total_seconds() // 60)})
        day += dt.timedelta(days=1)
    return slots[:max_slots]


@group.tool(risk=Risk.LOW, scope="read")
async def availability(ctx: ToolContext, start: str | None = None, end: str | None = None, days: int = 5,
                       attendees: list[str] | None = None, duration_minutes: int = 30,
                       working_hours: str = "09:00-17:00", include_weekends: bool = False,
                       timezone: str | None = None, account: str | None = None) -> dict:
    """Check free/busy for you (and optionally colleagues) and suggest open slots for a meeting.

    Args:
        start: Window start (default: now).
        end: Window end (default: start + `days`).
        days: Window length in days when `end` is omitted.
        attendees: Colleagues' emails to include (needs a connected calendar with permission to see their free/busy).
        duration_minutes: Meeting length the suggested slots must fit.
        working_hours: Daily window for suggestions, "HH:MM-HH:MM".
        include_weekends: Also suggest weekend slots.
        timezone: IANA time zone for the window and the suggestions.
        account: Calendar connector name or kind (or "local").
    """
    ad = await calendar_adapter(ctx, account)
    tz = _zone(ad, timezone)
    s, e = _range(start, end, days, tz)
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", working_hours or "")
    if not m:
        raise ToolError("working_hours must look like '09:00-17:00'.")
    work = (dt.time(int(m.group(1)), int(m.group(2))), dt.time(int(m.group(3)), int(m.group(4))))
    emails = [a.strip() for a in H.as_list(attendees)]
    info = await ad.busy(emails, s, e, tz)
    busy_all: list[tuple[dt.datetime, dt.datetime]] = []
    people: dict[str, Any] = {}
    for who, blocks in info.items():
        if isinstance(blocks, dict):
            people[who] = blocks
            continue
        busy_all += blocks
        people[who] = {"busy": [{"start": b[0].astimezone(tz).isoformat(), "end": b[1].astimezone(tz).isoformat()}
                                for b in _merge(blocks)][:40]}
    return {"calendar": ad.label, "timezone": tz_name(tz), "range": {"start": s.isoformat(), "end": e.isoformat()},
            "people": people,
            "suggested_slots": free_slots(busy_all, s, e, dt.timedelta(minutes=max(5, int(duration_minutes or 30))),
                                          work, tz, weekends=include_weekends)}


@group.tool(risk=Risk.MEDIUM, scope="write",
            risk_fn=lambda a: Risk.HIGH if a.get("notify_attendees") else Risk.MEDIUM,
            preview=lambda a: H.preview("Add meeting link to event", [
                ("Event", a.get("event_id")), ("Link", a.get("link") or "generate Google Meet / Teams link"),
                ("Notify attendees", bool(a.get("notify_attendees")))]))
async def add_meeting_link(ctx: ToolContext, event_id: str, link: str | None = None, notify_attendees: bool = False,
                           account: str | None = None) -> dict:
    """Add a video meeting link to an event: generate Google Meet / Teams, or attach a given URL (Zoom etc.).

    Args:
        event_id: Event id.
        link: An existing meeting URL to attach; omit to generate one with the calendar's provider.
        notify_attendees: Notify attendees of the change (required when the event has other attendees).
        account: Calendar connector name or kind (or "local").
    """
    ad = await calendar_adapter(ctx, account)
    if link and not re.match(r"^https?://", link.strip()):
        raise ToolError("The link must be an http(s) URL.")
    if ad.remote:
        others = other_attendees(await ad.get(event_id), await ad.me())
        if others and not notify_attendees:
            raise ToolError(f"This event has {len(others)} other attendee(s) who will see the change. Re-run with "
                            "notify_attendees=true.")
    updated = await ad.add_link(event_id, link.strip() if link else None, notify=notify_attendees)
    return {"calendar": ad.label, "updated": True, **view(updated)}


# --------------------------------------------------------------------------- meeting intelligence
def _keywords(title: str, limit: int = 4) -> str:
    words = [w for w in re.findall(r"[\w'&-]+", title or "") if w.lower() not in _GENERIC_WORDS and len(w) > 1]
    return " ".join(words[:limit])


async def _find_event(ad: CalendarAdapter, event_id: str | None, query: str | None, *, upcoming: bool) -> dict:
    if event_id:
        return await ad.get(event_id)
    now = dt.datetime.now(UTC)
    if upcoming:
        events = await (ad.search(query, now, now + dt.timedelta(days=60), 20) if query
                        else ad.list(now, now + dt.timedelta(days=14), 20))
        events = [e for e in events if (H.parse_datetime(e.get("end")) or now) >= now]
        if not events:
            raise ToolError("No upcoming event found" + (f" matching '{query}'." if query else " in the next 2 weeks."))
        return await ad.get(sorted(events, key=_sort_key)[0]["id"])
    events = await ad.search(query or "", now - dt.timedelta(days=180), now, 100)
    events = [e for e in events if (H.parse_datetime(e.get("start")) or now) < now]
    if not events:
        raise ToolError("No past event found" + (f" matching '{query}'." if query else "."))
    return await ad.get(sorted(events, key=_sort_key)[-1]["id"])


async def _related_emails(ctx: ToolContext, event: dict, email_account: str | None, since: dt.datetime,
                          until: dt.datetime | None = None, limit: int = 8) -> list[dict]:
    mail = await optional_mail_adapter(ctx, email_account)
    if mail is None:
        return []
    me = (await mail.me() or "").lower()
    found: dict[str, dict] = {}
    kw = _keywords(event.get("title") or "")
    try:
        if kw:
            for m in await mail.search(query=kw, since=since, limit=limit):
                found.setdefault(m["id"], m)
        for a in other_attendees(event, me)[:4]:
            for m in await mail.search(from_address=a.get("email"), since=since, limit=3):
                found.setdefault(m["id"], m)
    except ToolError:
        return list(found.values())
    rows = list(found.values())
    if until:
        rows = [m for m in rows if (H.parse_datetime(m.get("date")) or until) <= until]
    rows.sort(key=lambda m: m.get("date") or "", reverse=True)
    rows = rows[:limit]
    detailed = []
    for m in rows[:4]:
        try:
            detailed.append(await mail.get(m["id"], max_chars=2500))
        except ToolError:
            detailed.append(m)
    return detailed + rows[4:]


def _event_text(e: dict) -> str:
    atts = ", ".join(f"{a.get('name') or ''} <{a.get('email')}> ({a.get('response') or '?'})".strip()
                     for a in e.get("attendees") or [])
    return (f"Title: {e.get('title')}\nWhen: {e.get('start')} → {e.get('end')}\nLocation: {e.get('location') or '-'}\n"
            f"Organizer: {e.get('organizer') or '-'}\nAttendees: {atts or '-'}\n"
            f"Description:\n{H.clip(e.get('description') or '-', 4000)}")


@group.tool(risk=Risk.LOW, scope="read")
async def prepare_meeting(ctx: ToolContext, event_id: str | None = None, query: str | None = None,
                          account: str | None = None, email_account: str | None = None) -> dict:
    """Prepare a briefing for a meeting: purpose, attendees, related recent emails and past meetings, suggested
    agenda, open questions and a prep checklist. Defaults to your next upcoming meeting.

    Args:
        event_id: The meeting to prepare for.
        query: Or find the next upcoming meeting matching these words, e.g. "board meeting".
        account: Calendar connector name or kind (or "local").
        email_account: Mailbox to search for related emails (default: the first connected mailbox, if any).
    """
    ad = await calendar_adapter(ctx, account)
    event = await _find_event(ad, event_id, query, upcoming=True)
    now = dt.datetime.now(UTC)
    emails = await _related_emails(ctx, event, email_account, now - dt.timedelta(days=30))
    past: list[dict] = []
    kw = _keywords(event.get("title") or "")
    if kw:
        try:
            past = [e for e in await ad.search(kw, now - dt.timedelta(days=120), now, 10)
                    if e.get("id") != event.get("id")][-3:]
        except ToolError:
            past = []
    prompt = ("## Meeting\n" + _event_text(event)
              + ("\n\n## Previous related meetings\n" + "\n".join(
                  f"- {p.get('start')}: {p.get('title')} — {H.clip(p.get('description') or '', 300)}" for p in past)
                 if past else "")
              + ("\n\n## Related emails (most recent first)\n" + transcript(emails, 9000) if emails else
                 "\n\n(No related emails available.)"))
    system = ("You are an executive assistant preparing someone for a meeting. Using ONLY the context provided, "
              "write a concise markdown briefing with sections: Purpose, Who's attending (role/context if known), "
              "Background & recent threads, Suggested agenda, Open questions / decisions needed, Prep checklist. "
              "Mention what information is missing rather than inventing it.")
    briefing = await H.llm_complete(ctx, system, prompt, max_tokens=1200)
    return {"event": view(event, full=False), "briefing": briefing,
            "context": {"emails": [H.compact({"id": m.get("id"), "subject": m.get("subject"), "from": m.get("from"),
                                              "date": m.get("date")}) for m in emails],
                        "previous_meetings": [H.compact({"id": p.get("id"), "title": p.get("title"),
                                                         "start": p.get("start")}) for p in past]}}


@group.tool(risk=Risk.LOW, scope="read")
async def summarize_previous_meeting(ctx: ToolContext, query: str | None = None, event_id: str | None = None,
                                     account: str | None = None, email_account: str | None = None) -> dict:
    """Recap the most recent past meeting matching `query` (or a given event): what it covered, decisions,
    action items and open questions, from its calendar notes and the emails around it.

    Args:
        query: Words identifying the meeting series, e.g. "weekly sales sync".
        event_id: A specific past event instead.
        account: Calendar connector name or kind (or "local").
        email_account: Mailbox to search for related emails (default: first connected mailbox, if any).
    """
    if not query and not event_id:
        raise ToolError("Pass a query (e.g. the meeting's name) or an event_id.")
    ad = await calendar_adapter(ctx, account)
    event = await _find_event(ad, event_id, query, upcoming=False)
    start = H.parse_datetime(event.get("start")) or dt.datetime.now(UTC)
    emails = await _related_emails(ctx, event, email_account, start - dt.timedelta(days=3),
                                   until=start + dt.timedelta(days=7), limit=10)
    prompt = ("## Meeting\n" + _event_text(event)
              + ("\n\n## Emails around the meeting\n" + transcript(emails, 10000) if emails else
                 "\n\n(No related emails found.)"))
    system = ("Summarize this past meeting for someone who missed it, using ONLY the calendar details and emails "
              "provided (meeting notes may be in the description or in follow-up emails). Markdown sections: "
              "Overview, Discussed, Decisions, Action items (owner, due), Open questions. If the context doesn't "
              "reveal what was discussed, say so plainly.")
    summary = await H.llm_complete(ctx, system, prompt, max_tokens=1000)
    return {"event": view(event), "summary": summary, "sources": {"emails": len(emails),
                                                                  "has_notes": bool(event.get("description"))}}


@group.tool(risk=Risk.MEDIUM, scope="write",
            risk_fn=lambda a: Risk.HIGH if a.get("invite_attendees") else Risk.MEDIUM,
            content_fields=("title", "notes"),
            preview=lambda a: H.preview("Create follow-up meeting", [
                ("Original event", a.get("event_id")), ("Title", a.get("title") or "Follow-up: <original title>"),
                ("Start", a.get("start") or "same time one week after the original"),
                ("Duration (min)", a.get("duration_minutes") or 30),
                ("Invite the original attendees (emails will be sent)", bool(a.get("invite_attendees")))],
                a.get("notes") if a.get("notes") else None, body_label="Notes"))
async def create_followup(ctx: ToolContext, event_id: str, start: str | None = None, duration_minutes: int = 30,
                          title: str | None = None, notes: str | None = None, invite_attendees: bool = False,
                          timezone: str | None = None, account: str | None = None) -> dict:
    """Schedule a follow-up meeting for an existing event (by default one week later at the same time).

    Args:
        event_id: The original meeting.
        start: Follow-up start (ISO 8601); default one week after the original.
        duration_minutes: Follow-up length.
        title: Title (default "Follow-up: <original title>").
        notes: Agenda / notes for the follow-up.
        invite_attendees: Invite the original attendees (sends invitations).
        timezone: IANA time zone for times without an offset.
        account: Calendar connector name or kind (or "local").
    """
    ad = await calendar_adapter(ctx, account)
    tz = _zone(ad, timezone)
    original = await ad.get(event_id)
    if start:
        s, _ = parse_when(start, tz)
    else:
        orig = H.parse_datetime(original.get("start"))
        if orig is None:
            raise ToolError("The original event has no start time — pass `start`.")
        s = orig.astimezone(tz) + dt.timedelta(days=7)
    e = s + dt.timedelta(minutes=max(5, int(duration_minutes or 30)))
    invitees = [a["email"] for a in other_attendees(original, await ad.me())] if invite_attendees else []
    desc = ((notes.strip() + "\n\n") if notes else "") + f"Follow-up to '{original.get('title')}' ({original.get('start')})."
    created = await ad.create(EventInput(title=title or f"Follow-up: {original.get('title')}", start=s, end=e,
                                         description=desc, location=original.get("location"),
                                         attendees=invitees or None, tz=tz), notify=bool(invitees))
    return {"calendar": ad.label, "created": True, "invited": invitees, **view(created)}


# =========================================================================== connector kind
@H.connector_test
async def _test_google_calendar(cfg: dict) -> tuple[bool, str]:
    cal = str(cfg.get("calendar_id") or "primary")
    api = google_client(cfg, GCAL_BASE, service="Google Calendar", scopes=GCAL_SCOPES)
    data = await api.get(f"/calendars/{_q(cal)}") or {}
    return True, f"Connected to Google Calendar '{data.get('summary', cal)}' ({data.get('timeZone', 'UTC')})."


register_connector_kind(ConnectorKindSpec(
    kind="google_calendar", name="Google Calendar", description="Events, invitations and free/busy in Google Calendar.",
    icon="calendar", category="productivity", tool_groups=["calendar"],
    fields=google_oauth_fields("https://www.googleapis.com/auth/calendar") + [
        ConnectorField("calendar_id", "Calendar ID (optional)", placeholder="primary",
                       help="Defaults to the account's primary calendar."),
        ConnectorField("timezone", "Default time zone (optional)", placeholder="America/New_York"),
    ],
    docs_url="https://developers.google.com/calendar/api/guides/overview", test=_test_google_calendar,
))
