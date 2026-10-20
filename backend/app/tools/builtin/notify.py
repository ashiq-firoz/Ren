"""Notification tools: in-app notifications (bell), admin alerts, email, Slack/Teams, signed webhooks to admin-
configured endpoints (never agent-supplied URLs), SMS / WhatsApp via Twilio, and scheduled reminders.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin import _http as H
from app.tools.connectors import ConnectorField, ConnectorKindSpec, register_connector_kind

UTC = timezone.utc
TWILIO = "https://api.twilio.com/2010-04-01"
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")

group = ToolGroup(
    "notify", "Notifications",
    "Notify people: in-app notifications, admin alerts, email, Slack/Teams messages, webhooks, SMS/WhatsApp (Twilio) "
    "and scheduled reminders.",
    prefix="notify", icon="bell", category="productivity", connector_kinds=("webhook", "twilio"),
    connector_optional=True,
)


def _link(link: str | None) -> str | None:
    if not link:
        return None
    s = link.strip()
    if s.startswith("/") or re.match(r"^https?://", s):
        return s[:500]
    raise ToolError("link must be an app path like '/tasks/123' or an http(s) URL.")


async def _member_id(ctx: ToolContext, email: str) -> str:
    from sqlalchemy import func, select

    from app.models import Membership, User

    async with ctx.session() as db:
        row = (await db.execute(
            select(User.id).join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == ctx.org_id, func.lower(User.email) == email.strip().lower())
        )).first()
    if row is None:
        raise ToolError(f"'{email}' is not a member of this workspace.")
    return row[0]


# --------------------------------------------------------------------------- in-app
@group.tool(risk=Risk.LOW, scope="write", content_fields=("title", "message"))
async def send(ctx: ToolContext, message: str, title: str | None = None, user_email: str | None = None,
               link: str | None = None) -> dict:
    """Send an in-app notification (bell menu) to yourself or to a workspace member.

    Args:
        message: Notification text.
        title: Short title (default: first words of the message).
        user_email: A workspace member's email; omit to notify the current user.
        link: Optional app path (e.g. "/tasks/123") or URL to open.
    """
    from app.services.notifications import notify_user

    uid = await _member_id(ctx, user_email) if user_email else ctx.user_id
    if not uid:
        raise ToolError("No recipient — pass user_email.")
    note = await notify_user(ctx.org_id, uid, "agent", (title or H.clip(message, 80)).strip(), message, _link(link))
    return {"sent": True, "notification_id": note.get("id"), "to": user_email or "you"}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("title", "message"),
            preview=lambda a: H.preview("Notify all workspace admins", [("Title", a.get("title")), ("Link", a.get("link"))],
                                        a.get("message", "")))
async def notify_admins(ctx: ToolContext, title: str, message: str, link: str | None = None) -> dict:
    """Send an in-app notification to every admin/owner of the workspace (e.g. something needs attention).

    Args:
        title: Short title.
        message: Notification text.
        link: Optional app path or URL.
    """
    from app.services.notifications import notify_admins as _notify_admins

    n = await _notify_admins(ctx.org_id, "agent", title, message, _link(link))
    return {"sent": True, "admins_notified": n}


# --------------------------------------------------------------------------- email / chat
@group.tool(risk=Risk.HIGH, scope="write", content_fields=("subject", "body"),
            preview=lambda a: H.preview("Send notification email", [
                ("Account", a.get("account") or "default mailbox"), ("To", H.as_list(a.get("to"))),
                ("Cc", H.as_list(a.get("cc"))), ("Subject", a.get("subject"))], a.get("body", ""), body_label="Body"))
async def email(ctx: ToolContext, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                account: str | None = None) -> dict:
    """Send a notification email through the connected mailbox (Gmail, Microsoft 365 or IMAP/SMTP).

    Args:
        to: Recipient addresses.
        subject: Subject line.
        body: Plain-text body.
        cc: Cc addresses.
        account: Mailbox connector name or kind.
    """
    from app.tools.builtin.email import Outgoing, mail_adapter, norm_addresses

    out = Outgoing(to=norm_addresses(to, "to"), cc=norm_addresses(cc, "cc"), subject=subject, body=body)
    if not out.to:
        raise ToolError("At least one recipient is required.")
    ad = await mail_adapter(ctx, account)
    res = await ad.send(out)
    return {**res, "account": ad.label, "to": out.to}


async def _webhook_post(cfg: dict[str, Any], payload: dict[str, Any], service: str = "Webhook") -> dict:
    url = str(cfg.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        raise ToolError("The webhook connector has no valid http(s) URL.")
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "Ren-Webhook/1.0"}
    secret = str(cfg.get("signing_secret") or "").strip()
    if secret:
        ts = str(int(time.time()))
        sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        headers["X-Ren-Timestamp"] = ts
        headers["X-Ren-Signature"] = f"sha256={sig}"
    if cfg.get("header_name") and cfg.get("header_value"):
        name = str(cfg["header_name"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", name):
            raise ToolError("The webhook connector's custom header name is invalid.")
        headers[name] = str(cfg["header_value"])
    resp = await H.api("POST", url, service=service, content=body, headers=headers, raw=True, retries=2)
    return {"status": resp.status_code, "response": H.clip((resp.text or "").strip(), 300) or None}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("text",),
            preview=lambda a: H.preview("Post Slack / Teams notification", [
                ("Via", a.get("account") or "connected Slack/Teams, else the webhook connector"),
                ("Channel", a.get("channel") or "connector's default channel")], a.get("text", "")))
async def slack(ctx: ToolContext, text: str, channel: str | None = None, account: str | None = None) -> dict:
    """Post a notification to Slack or Teams (via the connected chat app, or a configured incoming webhook).

    Args:
        text: Message text.
        channel: Channel (e.g. "#alerts"); default: the connector's default channel.
        account: Chat or webhook connector name/kind to use.
    """
    if not text.strip():
        raise ToolError("The message is empty.")
    from app.tools.builtin.chat import CHAT_KINDS, chat_adapter

    chats = await H.connected(ctx, CHAT_KINDS)
    use_chat = bool(chats) and (not account or any(account.lower() in (k, n.lower(), H.label(k).lower())
                                                    for k, n in chats))
    if use_chat:
        ad = await chat_adapter(ctx, account)
        target = channel or ad.default_channel()
        if not target and ad.kind == "slack":
            raise ToolError("Pass a channel (or set a default channel in the Slack connector).")
        res = await ad.send(target, text)
        return {"via": ad.label, **res}
    hooks = await H.connected(ctx, ("webhook",))
    if not hooks:
        raise ToolError("Neither Slack/Teams nor a webhook is connected — ask an admin to connect one in Connectors.")
    name = account if account and any(account.lower() == n.lower() for _, n in hooks) else None
    cfg = await H.load_connector(ctx, "webhook", name)
    res = await _webhook_post(cfg, {"text": text})
    return {"via": "webhook", "sent": res["status"] < 300, **res}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("message", "data_json"),
            preview=lambda a: H.preview("POST webhook notification", [
                ("Webhook connector", a.get("webhook") or "default webhook"), ("Event", a.get("event") or "ren.notification")],
                json.dumps({"message": a.get("message", ""), "data": a.get("data_json")}, ensure_ascii=False, indent=1),
                body_label="Payload (summary)"))
async def webhook(ctx: ToolContext, message: str, data_json: str | None = None, event: str = "ren.notification",
                  webhook: str | None = None) -> dict:
    """POST a JSON notification to an admin-configured webhook (Zapier, Make, n8n, internal services...).

    Args:
        message: Human-readable message.
        data_json: Optional JSON object (as a string) with structured fields, e.g. '{"invoice": "INV-42", "amount": 120}'.
        event: Event name included in the payload.
        webhook: Name of the webhook connector to use (default: the first one).
    """
    data = None
    if data_json:
        try:
            data = json.loads(data_json)
        except json.JSONDecodeError as e:
            raise ToolError(f"data_json must be valid JSON: {e}") from e
    cfg = await H.load_connector(ctx, "webhook", webhook)
    payload = H.compact({"event": event or "ren.notification", "message": message, "data": data, "source": "ren",
                         "agent": ctx.agent_name, "task_id": ctx.task_id, "sent_at": H.iso(datetime.now(UTC))})
    res = await _webhook_post(cfg, payload)
    return {"sent": res["status"] < 300, "webhook": cfg.get("_connector_name") or webhook or "webhook", **res}


# --------------------------------------------------------------------------- Twilio
async def _twilio(ctx: ToolContext, to: str, body: str, whatsapp: bool) -> dict:
    cfg = await ctx.connector("twilio")
    H.need(cfg, ("account_sid", "auth_token"), "Twilio")
    number = re.sub(r"[\s()-]", "", to or "")
    if not _E164.match(number):
        raise ToolError("Phone numbers must be in E.164 format, e.g. +14155550123.")
    sid = str(cfg["account_sid"]).strip()
    form: dict[str, str] = {"To": f"whatsapp:{number}" if whatsapp else number, "Body": body}
    if whatsapp:
        sender = str(cfg.get("whatsapp_from") or cfg.get("from_number") or "").strip()
        if not sender:
            raise ToolError("Set a WhatsApp sender number in the Twilio connector.")
        form["From"] = sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}"
    elif cfg.get("messaging_service_sid"):
        form["MessagingServiceSid"] = str(cfg["messaging_service_sid"]).strip()
    elif cfg.get("from_number"):
        form["From"] = str(cfg["from_number"]).strip()
    else:
        raise ToolError("Set a From number or Messaging Service SID in the Twilio connector.")
    try:
        res = await H.api("POST", f"{TWILIO}/Accounts/{sid}/Messages.json", service="Twilio", data=form,
                          auth=(sid, str(cfg["auth_token"]).strip()), retries=1)
    except H.ApiError as e:
        code = (e.data or {}).get("code") if isinstance(e.data, dict) else None
        if code in (63016, 63032):
            raise ToolError("WhatsApp only allows free-form messages within 24 hours of the person's last message; "
                            "use an approved template otherwise.") from e
        raise
    return H.compact({"sent": True, "sid": res.get("sid"), "status": res.get("status"), "to": number,
                      "segments": res.get("num_segments")})


def _sms_preview(kind: str):
    return lambda a: H.preview(f"Send {kind}", [("To", a.get("to"))], a.get("body", ""), body_label="Message")


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("body",), preview=_sms_preview("SMS"))
async def sms(ctx: ToolContext, to: str, body: str) -> dict:
    """Send an SMS through Twilio.

    Args:
        to: Phone number in E.164 format, e.g. +14155550123.
        body: Message text (long messages are split into segments).
    """
    return await _twilio(ctx, to, body, whatsapp=False)


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("body",), preview=_sms_preview("WhatsApp message"))
async def whatsapp(ctx: ToolContext, to: str, body: str) -> dict:
    """Send a WhatsApp message through Twilio (free-form text only within 24h of the person's last message).

    Args:
        to: Phone number in E.164 format.
        body: Message text.
    """
    return await _twilio(ctx, to, body, whatsapp=True)


# --------------------------------------------------------------------------- scheduled reminders
def _parse_run_at(value: str, tz_name: str | None) -> datetime:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    v = value.strip()
    m = re.fullmatch(r"in\s+(\d+)\s*(m|min|minutes?|h|hours?|d|days?)", v, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = timedelta(minutes=n) if unit.startswith("m") else timedelta(hours=n) if unit.startswith("h") else timedelta(days=n)
        return datetime.now(UTC) + delta
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError("run_at must be ISO 8601 (e.g. 2026-10-06T09:00 or 2026-10-06T09:00+02:00) or 'in 2h'.") from e
    if d.tzinfo is None:
        try:
            tz = ZoneInfo(tz_name) if tz_name else UTC
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ToolError(f"Unknown time zone '{tz_name}'.") from e
        d = d.replace(tzinfo=tz)
    return d.astimezone(UTC)


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("title", "message"),
            preview=lambda a: H.preview("Schedule reminder", [
                ("When", a.get("run_at")), ("Repeat (cron)", a.get("cron")), ("Time zone", a.get("timezone")),
                ("Title", a.get("title"))], a.get("message", "")))
async def schedule(ctx: ToolContext, message: str, title: str | None = None, run_at: str | None = None,
                   cron: str | None = None, timezone: str | None = None, link: str | None = None) -> dict:
    """Schedule an in-app reminder for the current user — once (run_at) or recurring (cron).

    Args:
        message: Reminder text.
        title: Short title (default: "Reminder").
        run_at: When to fire: ISO 8601 datetime ("2026-10-06T09:00") or relative ("in 30m", "in 2h", "in 3d").
        cron: Recurring schedule in cron syntax, e.g. "0 9 * * MON" for Mondays 09:00 (read in `timezone`).
        timezone: IANA time zone for run_at values without an offset and for cron (default: the user's, else UTC).
        link: Optional app path or URL to open.
    """
    from app.services.scheduler import schedule_job

    if not ctx.user_id:
        raise ToolError("Reminders need a user to notify.")
    if not run_at and not cron:
        raise ToolError("Pass run_at (one-off) or cron (recurring).")
    from app.services.scheduler import valid_timezone
    from app.tools.builtin.governance import _user_timezone

    if timezone and not valid_timezone(timezone):
        raise ToolError(f"Unknown time zone '{timezone}'.")
    timezone = valid_timezone(timezone) or valid_timezone(await _user_timezone(ctx)) or "UTC"
    when = _parse_run_at(run_at, timezone) if run_at else None
    if when is not None and when < datetime.now(UTC) - timedelta(minutes=1):
        raise ToolError("run_at is in the past.")
    if cron:
        try:
            from croniter import croniter

            if not croniter.is_valid(cron):
                raise ValueError(cron)
        except ValueError as e:
            raise ToolError(f"Invalid cron expression '{cron}'.") from e
    job = await schedule_job(org_id=ctx.org_id, user_id=ctx.user_id, kind="notification",
                             title=(title or "Reminder")[:300],
                             payload=H.compact({"title": title or "Reminder", "body": message, "link": _link(link),
                                                "created_by_agent": ctx.agent_name}),
                             run_at=None if cron else when, cron=cron or None, timezone=timezone)
    return {"scheduled": True, "job_id": job["id"], "run_at": job["run_at"], "cron": job.get("cron"),
            "title": job["title"]}


@group.tool(risk=Risk.MEDIUM, scope="write")
async def cancel_schedule(ctx: ToolContext, job_id: str) -> dict:
    """Cancel a scheduled reminder (your own; admins can cancel any in the workspace).

    Args:
        job_id: Job id from notify_schedule / notify_list_schedules.
    """
    from app.models import ScheduledJob
    from app.services.scheduler import cancel_job

    async with ctx.session() as db:
        job = await db.get(ScheduledJob, job_id)
        if job is None or job.org_id != ctx.org_id:
            raise ToolError(f"No scheduled job '{job_id}'.")
        if job.user_id != ctx.user_id and not ctx.is_admin:
            raise ToolError("You can only cancel your own reminders.")
        if job.status != "scheduled":
            return {"cancelled": False, "job_id": job_id, "status": job.status}
    ok = await cancel_job(ctx.org_id, job_id)
    return {"cancelled": ok, "job_id": job_id}


@group.tool(risk=Risk.LOW, scope="read")
async def list_schedules(ctx: ToolContext, include_finished: bool = False, limit: int = 50) -> dict:
    """List your scheduled reminders and recurring jobs.

    Args:
        include_finished: Also show done/cancelled/failed jobs.
        limit: Maximum jobs.
    """
    from sqlalchemy import select

    from app.models import ScheduledJob
    from app.services.scheduler import job_out

    async with ctx.session() as db:
        q = select(ScheduledJob).where(ScheduledJob.org_id == ctx.org_id, ScheduledJob.user_id == ctx.user_id)
        if not include_finished:
            q = q.where(ScheduledJob.status == "scheduled")
        rows = (await db.execute(q.order_by(ScheduledJob.run_at).limit(max(1, min(int(limit or 50), 200))))).scalars().all()
    jobs = []
    for j in rows:
        d = job_out(j)
        d["payload"] = {k: v for k, v in d.get("payload", {}).items() if k in ("title", "body", "link")}
        jobs.append(H.compact(d))
    return {"count": len(jobs), "jobs": jobs}


# --------------------------------------------------------------------------- connector kinds
@H.connector_test
async def _test_webhook(cfg: dict) -> tuple[bool, str]:
    res = await _webhook_post(cfg, {"event": "ren.test", "text": "Ren connection test", "message": "Ren connection test",
                                    "test": True, "sent_at": H.iso(datetime.now(UTC))})
    ok = 200 <= res["status"] < 300
    return ok, f"Webhook responded HTTP {res['status']}." + ("" if ok else f" {res.get('response') or ''}")


@H.connector_test
async def _test_twilio(cfg: dict) -> tuple[bool, str]:
    H.need(cfg, ("account_sid", "auth_token"), "Twilio")
    sid = str(cfg["account_sid"]).strip()
    acct = await H.api("GET", f"{TWILIO}/Accounts/{sid}.json", service="Twilio", auth=(sid, str(cfg["auth_token"]).strip()))
    return True, f"Connected to Twilio account '{acct.get('friendly_name')}' ({acct.get('status')})."


register_connector_kind(ConnectorKindSpec(
    kind="webhook", name="Webhook", description="POST JSON notifications to Zapier, Make, n8n, Slack incoming webhooks "
                                                "or your own services.",
    icon="webhook", category="productivity", tool_groups=["notify"],
    fields=[
        ConnectorField("url", "Webhook URL", type="password", required=True, secret=True,
                       placeholder="https://hooks.example.com/…",
                       help="Stored encrypted (webhook URLs often embed a secret). The connection test POSTs a "
                            "{\"event\": \"ren.test\"} ping."),
        ConnectorField("signing_secret", "Signing secret (optional)", type="password", secret=True,
                       help="Adds X-Ren-Timestamp and X-Ren-Signature: sha256=HMAC(secret, timestamp + '.' + body)."),
        ConnectorField("header_name", "Custom header name (optional)", placeholder="Authorization"),
        ConnectorField("header_value", "Custom header value (optional)", type="password", secret=True,
                       placeholder="Bearer …"),
    ],
    docs_url="https://en.wikipedia.org/wiki/Webhook", test=_test_webhook,
))

register_connector_kind(ConnectorKindSpec(
    kind="twilio", name="Twilio", description="Send SMS and WhatsApp messages through Twilio.",
    icon="message-circle", category="productivity", tool_groups=["notify"],
    fields=[
        ConnectorField("account_sid", "Account SID", required=True, placeholder="AC…"),
        ConnectorField("auth_token", "Auth token", type="password", required=True, secret=True),
        ConnectorField("from_number", "From number", placeholder="+14155550123",
                       help="A Twilio phone number in E.164 format."),
        ConnectorField("messaging_service_sid", "Messaging Service SID (optional)", placeholder="MG…",
                       help="Used instead of the From number for SMS when set."),
        ConnectorField("whatsapp_from", "WhatsApp sender (optional)", placeholder="+14155238886",
                       help="Your approved WhatsApp sender (defaults to the From number)."),
    ],
    docs_url="https://www.twilio.com/docs/messaging/api/message-resource", test=_test_twilio,
))
