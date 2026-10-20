"""Email tools: search, read, draft and send mail through Gmail, Microsoft 365 (Outlook via Graph) or any
IMAP/SMTP mailbox, plus model-assisted thread summaries, action items, deadlines and follow-ups.

Adapter choice: the first connected of gmail > microsoft365 > imap_smtp, unless `account` names a connector.
"""
from __future__ import annotations

import asyncio
import base64
import email.policy
import html as htmllib
import imaplib
import re
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from typing import Any, Literal
from urllib.parse import quote

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin import _http as H
from app.tools.builtin._oauth import google_client, graph_client, graph_user_base, microsoft_token
from app.tools.connectors import ConnectorField, ConnectorKindSpec, register_connector_kind

MAIL_KINDS = ("gmail", "microsoft365", "imap_smtp")
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.modify",)
MAX_ATTACH_TOTAL = 25 * 1024 * 1024
GRAPH_INLINE_ATTACH_LIMIT = 3 * 1024 * 1024
_ADDR_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_RE_PREFIX = re.compile(r"^\s*((re|aw|sv|antw|fwd?|fw|wg|tr)\s*(\[\d+\])?\s*:\s*)+", re.I)

BodyFormat = Literal["text", "markdown", "html"]


# =========================================================================== message building helpers
@dataclass
class Outgoing:
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    fmt: str = "text"
    attachments: list[tuple[str, bytes, str]] = field(default_factory=list)  # (filename, data, mime)
    in_reply_to: str | None = None
    references: str | None = None
    thread_id: str | None = None


def norm_addresses(values: Any, field_name: str = "recipients") -> list[str]:
    """Validate/normalise addresses ("Name <a@b.c>" or "a@b.c"); accepts a list or a comma/semicolon string."""
    if not values:
        return []
    items = [values.replace(";", ",")] if isinstance(values, str) else [str(v).replace(";", ",") for v in values]
    out: list[str] = []
    for item in items:
        if not item.strip():
            continue
        pairs = [p for p in getaddresses([item]) if p != ("", "")]
        if not pairs:
            raise ToolError(f"Invalid email address in {field_name}: '{item.strip()}'.")
        for name, addr in pairs:
            if not addr or not _ADDR_RE.match(addr):
                raise ToolError(f"Invalid email address in {field_name}: '{name or addr or item.strip()}'.")
            out.append(formataddr((name, addr)) if name else addr)
    return out


def _addr_only(values: list[str]) -> set[str]:
    return {a.lower() for _, a in getaddresses(values) if a}


def dedupe_addresses(values: list[str], exclude: list[str] | None = None, me: str = "") -> list[str]:
    seen = _addr_only(exclude or [])
    if me:
        seen.add(me.lower())
    out = []
    for v in values:
        addrs = [a.lower() for _, a in getaddresses([v]) if a]
        if not addrs or addrs[0] in seen:
            continue
        seen.add(addrs[0])
        out.append(v)
    return out


def render_bodies(body: str, fmt: str) -> tuple[str, str | None]:
    """(plain text, html or None) for a body written as text, markdown or html."""
    body = body or ""
    if fmt == "html":
        return H.html_to_text(body), body
    if fmt == "markdown":
        from markdown_it import MarkdownIt

        return body, MarkdownIt("commonmark", {"html": False}).enable("table").render(body)
    return body, None


def text_to_html(text: str) -> str:
    return "<div>" + htmllib.escape(text or "").replace("\n", "<br>") + "</div>"


def build_mime(m: Outgoing, sender: str | None) -> EmailMessage:
    msg = EmailMessage()
    try:
        if sender:
            msg["From"] = sender
        if m.to:
            msg["To"] = ", ".join(m.to)
        if m.cc:
            msg["Cc"] = ", ".join(m.cc)
        if m.bcc:
            msg["Bcc"] = ", ".join(m.bcc)
        msg["Subject"] = m.subject or ""
        msg["Date"] = formatdate(localtime=False, usegmt=True)
        domain = sender.rsplit("@", 1)[-1].strip(" >") if sender and "@" in sender else "ren.mail"
        msg["Message-ID"] = make_msgid(domain=domain)
        if m.in_reply_to:
            msg["In-Reply-To"] = m.in_reply_to
        if m.references:
            msg["References"] = m.references
    except (ValueError, TypeError) as e:
        raise ToolError(f"Invalid email header: {e}") from e
    plain, html = render_bodies(m.body, m.fmt)
    msg.set_content(plain)
    if html:
        msg.add_alternative(html, subtype="html")
    for name, data, mime in m.attachments:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=name)
    return msg


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode((data or "") + "=" * (-len(data or "") % 4))


def reply_subject(subject: str | None) -> str:
    s = (subject or "").strip()
    return s if re.match(r"(?i)^re\s*:", s) else f"Re: {s}"


def forward_subject(subject: str | None) -> str:
    s = (subject or "").strip()
    return s if re.match(r"(?i)^(fwd?|fw)\s*:", s) else f"Fwd: {s}"


def norm_subject(subject: str | None) -> str:
    return _RE_PREFIX.sub("", subject or "").strip()


def reply_recipients(h: dict[str, Any], me: str, reply_all: bool) -> tuple[list[str], list[str]]:
    primary = h.get("reply-to") or h.get("from") or ""
    pairs = [p for p in getaddresses([primary]) if p[1]]
    if me and pairs and all(a.lower() == me for _, a in pairs):  # replying to my own message → original recipients
        pairs = [p for p in getaddresses([h.get("to") or ""]) if p[1]]
    to = [formataddr(p) if p[0] else p[1] for p in pairs]
    cc: list[str] = []
    if reply_all:
        others = [formataddr(p) if p[0] else p[1] for p in getaddresses([h.get("to") or "", h.get("cc") or ""]) if p[1]]
        cc = dedupe_addresses(others, exclude=to, me=me)
    return to, cc


def forward_body(comment: str, full: dict[str, Any], fmt: str) -> str:
    head = ["---------- Forwarded message ---------", f"From: {full.get('from', '')}", f"Date: {full.get('date', '')}",
            f"Subject: {full.get('subject', '')}", f"To: {full.get('to', '')}"]
    if full.get("cc"):
        head.append(f"Cc: {full['cc']}")
    original = full.get("body") or ""
    comment = comment or ""
    if fmt == "html":
        block = "<br>".join(htmllib.escape(x) for x in head) + "<br><br>" + htmllib.escape(original).replace("\n", "<br>")
        return f"{comment}<br><br><div>{block}</div>"
    if fmt == "markdown":
        return f"{comment}\n\n" + "\n".join("> " + x for x in head + [""] + original.splitlines())
    return f"{comment}\n\n" + "\n".join(head) + "\n\n" + original


def _match_attachment(atts: list[dict], ref: str) -> dict | None:
    r = (ref or "").strip()
    if not r:
        return None
    for a in atts:
        if str(a.get("id")) == r:
            return a
    if r.isdigit() and 1 <= int(r) <= len(atts):
        return atts[int(r) - 1]
    for a in atts:
        if str(a.get("filename") or "").lower() == r.lower():
            return a
    return None


def _date_iso(value: Any) -> str | None:
    return H.iso_or_raw(value)


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().lower()
    m = re.fullmatch(r"(\d+)\s*d(ays?)?", v)
    if m:
        return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
    d = H.parse_datetime(value)
    if d is None:
        raise ToolError(f"Invalid date '{value}' — use YYYY-MM-DD (or e.g. '7d').")
    return d


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(n or lo)))


async def _load_attachments(ctx: ToolContext, paths: list[str] | None) -> list[tuple[str, bytes, str]]:
    out: list[tuple[str, bytes, str]] = []
    total = 0
    for p in paths or []:
        data, name, mime = await H.read_workspace_file(ctx, p, max_bytes=MAX_ATTACH_TOTAL)
        total += len(data)
        if total > MAX_ATTACH_TOTAL:
            raise ToolError("Attachments exceed 25 MB in total — share large files via a drive link instead.")
        out.append((name, data, mime))
    return out


# =========================================================================== adapters
class MailAdapter:
    kind = ""
    label = ""

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        self.ctx = ctx
        self.cfg = cfg
        self._me: str | None = None

    # --- abstract surface ---------------------------------------------------------------
    async def me(self) -> str:
        raise NotImplementedError

    async def search(self, *, query: str = "", from_address: str | None = None, unread_only: bool = False,
                     since: datetime | None = None, folder: str | None = None, limit: int = 20) -> list[dict]:
        raise NotImplementedError

    async def get(self, message_id: str, *, max_chars: int = 8000) -> dict:
        raise NotImplementedError

    async def thread(self, thread_id: str, *, max_chars: int = 3000, limit: int = 25) -> dict:
        raise NotImplementedError

    async def attachment(self, message_id: str, ref: str) -> tuple[bytes, str, str]:
        raise NotImplementedError

    async def send(self, out: Outgoing) -> dict:
        raise NotImplementedError

    async def create_draft(self, out: Outgoing) -> dict:
        raise NotImplementedError

    async def update_draft(self, draft_id: str, *, to: list[str] | None, cc: list[str] | None,
                           subject: str | None, body: str | None, fmt: str) -> dict:
        raise NotImplementedError

    async def delete_draft(self, draft_id: str) -> dict:
        raise NotImplementedError

    async def modify(self, ids: list[str], *, add: list[str] = (), remove: list[str] = (), archive: bool = False,
                     read: bool | None = None) -> dict:
        raise NotImplementedError

    async def reply_context(self, message_id: str) -> dict[str, Any]:
        """Lower-cased headers of the original (from, to, cc, reply-to, subject, message-id, references) +
        '_thread_id'."""
        raise NotImplementedError

    async def forward_source(self, message_id: str, include_attachments: bool) -> tuple[dict, list[tuple[str, bytes, str]]]:
        raise NotImplementedError

    # --- shared behaviour -----------------------------------------------------------------
    async def reply_outgoing(self, message_id: str, body: str, *, reply_all: bool, cc: list[str] | None,
                             subject: str | None, fmt: str, attachments: list[tuple[str, bytes, str]]) -> Outgoing:
        h = await self.reply_context(message_id)
        me = (await self.me() or "").lower()
        to, cc_auto = reply_recipients(h, me, reply_all)
        if not to:
            raise ToolError("Could not determine who to reply to (the original has no sender address).")
        refs = " ".join(x for x in (h.get("references"), h.get("message-id")) if x)
        return Outgoing(to=to, cc=dedupe_addresses(cc_auto + (cc or []), exclude=to, me=me),
                        subject=subject or reply_subject(h.get("subject")), body=body, fmt=fmt,
                        attachments=attachments, in_reply_to=h.get("message-id"), references=refs or None,
                        thread_id=h.get("_thread_id"))

    async def reply(self, message_id: str, body: str, *, reply_all: bool = False, cc: list[str] | None = None,
                    subject: str | None = None, fmt: str = "text",
                    attachments: list[tuple[str, bytes, str]] | None = None) -> dict:
        out = await self.reply_outgoing(message_id, body, reply_all=reply_all, cc=cc, subject=subject, fmt=fmt,
                                        attachments=attachments or [])
        res = await self.send(out)
        return {**res, "to": out.to, "cc": out.cc, "subject": out.subject}

    async def create_reply_draft(self, message_id: str, body: str, *, reply_all: bool, cc: list[str] | None,
                                 subject: str | None, fmt: str, attachments: list[tuple[str, bytes, str]]) -> dict:
        out = await self.reply_outgoing(message_id, body, reply_all=reply_all, cc=cc, subject=subject, fmt=fmt,
                                        attachments=attachments)
        res = await self.create_draft(out)
        return {**res, "to": out.to, "cc": out.cc, "subject": out.subject}

    async def forward(self, message_id: str, to: list[str], body: str, *, cc: list[str] | None = None,
                      subject: str | None = None, fmt: str = "text", include_attachments: bool = True) -> dict:
        full, atts = await self.forward_source(message_id, include_attachments)
        out = Outgoing(to=to, cc=cc or [], subject=subject or forward_subject(full.get("subject")),
                       body=forward_body(body, full, fmt), fmt=fmt, attachments=atts)
        res = await self.send(out)
        return {**res, "to": out.to, "cc": out.cc, "subject": out.subject, "attachments": [a[0] for a in atts]}


# --------------------------------------------------------------------------- Gmail
def _gmail_folder(folder: str) -> str:
    f = folder.strip().lower()
    mapping = {"inbox": "in:inbox", "sent": "in:sent", "drafts": "in:drafts", "draft": "in:drafts", "spam": "in:spam",
               "junk": "in:spam", "trash": "in:trash", "starred": "is:starred", "important": "is:important",
               "archive": "-in:inbox -in:sent -in:drafts -in:spam -in:trash", "all": "", "all mail": ""}
    if f in mapping:
        return mapping[f]
    return "label:" + re.sub(r"\s+", "-", folder.strip())


def _gheaders(payload: dict) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in (payload or {}).get("headers", [])}


def _gparts(part: dict):
    yield part
    for p in part.get("parts") or []:
        yield from _gparts(p)


def _gcharset(part: dict) -> str:
    ctype = _gheaders(part).get("content-type", "")
    m = re.search(r'charset="?([\w.-]+)"?', ctype, re.I)
    return m.group(1) if m else "utf-8"


def _decode_part(data: bytes, charset: str) -> str:
    try:
        return data.decode(charset or "utf-8", "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


class GmailAdapter(MailAdapter):
    kind = "gmail"
    label = "Gmail"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        self.api = google_client(cfg, GMAIL_BASE, service="Gmail", scopes=GMAIL_SCOPES)
        self._labels: dict[str, str] | None = None

    async def me(self) -> str:
        if self._me is None:
            prof = await self.api.get("/profile") or {}
            self._me = prof.get("emailAddress") or ""
        return self._me

    def _summary(self, m: dict) -> dict:
        h = _gheaders(m.get("payload") or {})
        labels = m.get("labelIds") or []
        shown = [lb for lb in labels if not lb.startswith("CATEGORY_") and lb != "UNREAD"]
        return H.compact({
            "id": m.get("id"), "thread_id": m.get("threadId"), "from": h.get("from"), "to": H.clip(h.get("to"), 300),
            "date": _date_iso(m.get("internalDate") or h.get("date")), "subject": h.get("subject") or "(no subject)",
            "snippet": H.clip(htmllib.unescape(m.get("snippet") or ""), 220), "unread": "UNREAD" in labels,
            "labels": shown,
        })

    def _full(self, m: dict, max_chars: int) -> dict:
        payload = m.get("payload") or {}
        h = _gheaders(payload)
        plain: list[str] = []
        html: list[str] = []
        atts: list[dict] = []
        for part in _gparts(payload):
            mime = (part.get("mimeType") or "").lower()
            body = part.get("body") or {}
            fname = part.get("filename") or ""
            if fname and (body.get("attachmentId") or body.get("data")):
                atts.append({"id": body.get("attachmentId") or "", "filename": fname, "mime": mime,
                             "size": body.get("size", 0)})
            elif mime == "text/plain" and body.get("data"):
                plain.append(_decode_part(_b64d(body["data"]), _gcharset(part)))
            elif mime == "text/html" and body.get("data"):
                html.append(_decode_part(_b64d(body["data"]), _gcharset(part)))
        text = "\n".join(plain).strip() or H.html_to_text("\n".join(html))
        out = self._summary(m)
        out.pop("snippet", None)
        out.update({"cc": h.get("cc"), "reply_to": h.get("reply-to"), "body": H.clip(text, max_chars),
                    "body_truncated": len(text) > max_chars or None,
                    "attachments": [{"index": i + 1, **a} for i, a in enumerate(atts)]})
        return H.compact(out)

    async def _meta(self, mid: str) -> dict:
        return await self.api.get(f"/messages/{quote(mid, safe='')}", params={
            "format": "metadata", "metadataHeaders": ["From", "To", "Cc", "Subject", "Date"]})

    async def search(self, *, query="", from_address=None, unread_only=False, since=None, folder=None, limit=20):
        q = [query.strip()] if query and query.strip() else []
        if from_address:
            q.append(f"from:{from_address}")
        if unread_only:
            q.append("is:unread")
        if since:
            q.append(f"after:{since.strftime('%Y/%m/%d')}")
        if folder:
            q.append(_gmail_folder(folder))
        data = await self.api.get("/messages", params={"q": " ".join(x for x in q if x) or None, "maxResults": limit})
        ids = [m["id"] for m in (data or {}).get("messages", [])][:limit]
        metas = await H.gather_limited([self._meta(i) for i in ids], 6)
        return [self._summary(m) for m in metas if m]

    async def get(self, message_id, *, max_chars=8000):
        m = await self.api.get(f"/messages/{quote(message_id, safe='')}", params={"format": "full"})
        return self._full(m, max_chars)

    async def thread(self, thread_id, *, max_chars=3000, limit=25):
        try:
            t = await self.api.get(f"/threads/{quote(thread_id, safe='')}", params={"format": "full"})
        except H.ApiError as e:
            if e.status not in (400, 404):
                raise
            meta = await self._meta(thread_id)  # a message id was passed → use its thread
            t = await self.api.get(f"/threads/{quote(meta['threadId'], safe='')}", params={"format": "full"})
        msgs = t.get("messages") or []
        full = [self._full(m, max_chars) for m in msgs[-limit:]]
        return {"thread_id": t.get("id") or thread_id, "subject": full[0].get("subject") if full else None,
                "message_count": len(msgs), "messages": full}

    async def attachment(self, message_id, ref):
        full = await self.get(message_id, max_chars=100)
        atts = full.get("attachments") or []
        att = _match_attachment(atts, ref)
        if att is None and len(ref) < 40:
            names = ", ".join(f"{a['index']}: {a.get('filename')}" for a in atts) or "none"
            raise ToolError(f"No attachment '{ref}' on this message. Attachments: {names}.")
        aid = att["id"] if att else ref
        data = await self.api.get(f"/messages/{quote(message_id, safe='')}/attachments/{quote(aid, safe='')}")
        return (_b64d((data or {}).get("data", "")), (att or {}).get("filename") or "attachment",
                (att or {}).get("mime") or "application/octet-stream")

    def _raw(self, out: Outgoing) -> str:
        return b64url(build_mime(out, self.cfg.get("from_address") or None).as_bytes(policy=email.policy.SMTP))

    async def send(self, out):
        body: dict[str, Any] = {"raw": self._raw(out)}
        if out.thread_id:
            body["threadId"] = out.thread_id
        res = await self.api.post("/messages/send", json=body) or {}
        return {"sent": True, "id": res.get("id"), "thread_id": res.get("threadId")}

    async def reply_context(self, message_id):
        m = await self.api.get(f"/messages/{quote(message_id, safe='')}", params={
            "format": "metadata",
            "metadataHeaders": ["From", "To", "Cc", "Reply-To", "Subject", "Message-ID", "References"]})
        h = _gheaders(m.get("payload") or {})
        h["_thread_id"] = m.get("threadId")
        return h

    async def forward_source(self, message_id, include_attachments):
        m = await self.api.get(f"/messages/{quote(message_id, safe='')}", params={"format": "full"})
        full = self._full(m, 200_000)
        atts: list[tuple[str, bytes, str]] = []
        total = 0
        if include_attachments:
            for a in full.get("attachments") or []:
                if not a.get("id"):
                    continue
                data = await self.api.get(f"/messages/{quote(message_id, safe='')}/attachments/{quote(a['id'], safe='')}")
                raw = _b64d((data or {}).get("data", ""))
                total += len(raw)
                if total > MAX_ATTACH_TOTAL:
                    raise ToolError("The original's attachments exceed 25 MB — forward without attachments instead.")
                atts.append((a.get("filename") or "attachment", raw, a.get("mime") or "application/octet-stream"))
        return full, atts

    async def create_draft(self, out):
        msg: dict[str, Any] = {"raw": self._raw(out)}
        if out.thread_id:
            msg["threadId"] = out.thread_id
        res = await self.api.post("/drafts", json={"message": msg}) or {}
        return {"draft_id": res.get("id"), "message_id": (res.get("message") or {}).get("id"),
                "thread_id": (res.get("message") or {}).get("threadId")}

    async def _draft_id(self, ref: str) -> str:
        try:
            await self.api.get(f"/drafts/{quote(ref, safe='')}", params={"format": "minimal"})
            return ref
        except H.ApiError as e:
            if e.status not in (400, 404):
                raise
        data = await self.api.get("/drafts", params={"maxResults": 500}) or {}
        for d in data.get("drafts", []):
            if (d.get("message") or {}).get("id") == ref:
                return d["id"]
        raise ToolError(f"No draft with id '{ref}'.")

    async def update_draft(self, draft_id, *, to, cc, subject, body, fmt):
        did = await self._draft_id(draft_id)
        d = await self.api.get(f"/drafts/{quote(did, safe='')}", params={"format": "full"})
        msg = d.get("message") or {}
        h = _gheaders(msg.get("payload") or {})
        full = self._full(msg, 1_000_000)
        atts: list[tuple[str, bytes, str]] = []
        for a in full.get("attachments") or []:  # keep existing attachments
            if a.get("id"):
                data = await self.api.get(f"/messages/{quote(msg['id'], safe='')}/attachments/{quote(a['id'], safe='')}")
                atts.append((a.get("filename") or "attachment", _b64d((data or {}).get("data", "")),
                             a.get("mime") or "application/octet-stream"))
        out = Outgoing(
            to=to if to is not None else norm_addresses(h.get("to")),
            cc=cc if cc is not None else norm_addresses(h.get("cc")),
            bcc=norm_addresses(h.get("bcc")),
            subject=subject if subject is not None else h.get("subject", ""),
            body=body if body is not None else full.get("body", ""), fmt=fmt if body is not None else "text",
            attachments=atts, in_reply_to=h.get("in-reply-to"), references=h.get("references"),
            thread_id=msg.get("threadId"))
        payload: dict[str, Any] = {"raw": self._raw(out)}
        if out.thread_id:
            payload["threadId"] = out.thread_id
        await self.api.put(f"/drafts/{quote(did, safe='')}", json={"id": did, "message": payload})
        return {"draft_id": did, "updated": True}

    async def delete_draft(self, draft_id):
        did = await self._draft_id(draft_id)
        await self.api.delete(f"/drafts/{quote(did, safe='')}")
        return {"draft_id": did, "deleted": True}

    async def _label_map(self) -> dict[str, str]:
        if self._labels is None:
            data = await self.api.get("/labels") or {}
            self._labels = {}
            for lb in data.get("labels", []):
                self._labels[lb["name"].lower()] = lb["id"]
                self._labels[lb["id"].lower()] = lb["id"]
        return self._labels

    async def modify(self, ids, *, add=(), remove=(), archive=False, read=None):
        add_ids: list[str] = []
        remove_ids: list[str] = []
        if add or remove:
            labels = await self._label_map()
            for name in add:
                lid = labels.get(name.lower())
                if lid is None:
                    created = await self.api.post("/labels", json={
                        "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})
                    lid = created["id"]
                    labels[name.lower()] = lid
                add_ids.append(lid)
            for name in remove:
                lid = labels.get(name.lower())
                if lid is None:
                    raise ToolError(f"Label '{name}' doesn't exist.")
                remove_ids.append(lid)
        if archive:
            remove_ids.append("INBOX")
        if read is True:
            remove_ids.append("UNREAD")
        elif read is False:
            add_ids.append("UNREAD")
        body: dict[str, Any] = {"ids": ids}
        if add_ids:
            body["addLabelIds"] = add_ids
        if remove_ids:
            body["removeLabelIds"] = remove_ids
        await self.api.post("/messages/batchModify", json=body)
        return {"updated": len(ids), "added_labels": add_ids, "removed_labels": remove_ids}


# --------------------------------------------------------------------------- Microsoft Graph (Outlook)
GRAPH_MSG_SELECT = ("id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,isRead,"
                    "hasAttachments,categories,webLink")
GRAPH_TEXT_BODY = {"Prefer": 'outlook.body-content-type="text"'}
GRAPH_FOLDERS = {"inbox": "inbox", "sent": "sentitems", "sentitems": "sentitems", "drafts": "drafts",
                 "draft": "drafts", "trash": "deleteditems", "deleted": "deleteditems", "deleteditems": "deleteditems",
                 "spam": "junkemail", "junk": "junkemail", "junkemail": "junkemail", "archive": "archive",
                 "outbox": "outbox"}


def _q(value: str) -> str:
    return quote(str(value), safe="")


def _odata(value: str) -> str:
    return str(value).replace("'", "''")


def _gaddr(r: dict | None) -> str:
    ea = (r or {}).get("emailAddress") or {}
    name, addr = ea.get("name") or "", ea.get("address") or ""
    return formataddr((name, addr)) if name and addr and name != addr else (addr or name)


def _grecips(addrs: list[str]) -> list[dict]:
    out = []
    for name, addr in getaddresses(addrs):
        if addr:
            ea: dict[str, str] = {"address": addr}
            if name:
                ea["name"] = name
            out.append({"emailAddress": ea})
    return out


class GraphMailAdapter(MailAdapter):
    kind = "microsoft365"
    label = "Microsoft 365"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        self.api = graph_client(cfg, service="Microsoft 365 Mail", reconnect="Microsoft 365")

    async def me(self) -> str:
        if self._me is None:
            if self.cfg.get("user") and not self.cfg.get("refresh_token"):
                self._me = str(self.cfg["user"])
            else:
                data = await self.api.get("", params={"$select": "mail,userPrincipalName"}) or {}
                self._me = data.get("mail") or data.get("userPrincipalName") or ""
        return self._me

    def _summary(self, m: dict) -> dict:
        return H.compact({
            "id": m.get("id"), "thread_id": m.get("conversationId"), "from": _gaddr(m.get("from")),
            "to": H.clip(", ".join(_gaddr(r) for r in m.get("toRecipients") or []), 300),
            "date": _date_iso(m.get("receivedDateTime")), "subject": m.get("subject") or "(no subject)",
            "snippet": H.clip(m.get("bodyPreview"), 220), "unread": not m.get("isRead", True),
            "has_attachments": m.get("hasAttachments") or None, "labels": m.get("categories") or None,
        })

    async def _attachments(self, mid: str) -> list[dict]:
        data = await self.api.get(f"/messages/{_q(mid)}/attachments", params={
            "$select": "id,name,contentType,size,isInline"}) or {}
        return [{"index": i + 1, "id": a.get("id"), "filename": a.get("name"), "mime": a.get("contentType"),
                 "size": a.get("size")} for i, a in enumerate(data.get("value", []))]

    def _full(self, m: dict, max_chars: int, atts: list[dict] | None = None) -> dict:
        body = (m.get("body") or {}).get("content") or ""
        if (m.get("body") or {}).get("contentType", "").lower() == "html":
            body = H.html_to_text(body)
        out = self._summary(m)
        out.pop("snippet", None)
        out.update({"cc": ", ".join(_gaddr(r) for r in m.get("ccRecipients") or []) or None,
                    "body": H.clip(body.strip(), max_chars), "body_truncated": len(body) > max_chars or None,
                    "attachments": atts or None})
        return H.compact(out)

    async def _folder(self, name: str) -> str:
        key = name.strip().lower()
        if key in GRAPH_FOLDERS:
            return GRAPH_FOLDERS[key]
        data = await self.api.get("/mailFolders", params={"$filter": f"displayName eq '{_odata(name.strip())}'",
                                                          "$select": "id,displayName"}) or {}
        vals = data.get("value") or []
        if not vals:
            raise ToolError(f"Mail folder '{name}' not found.")
        return vals[0]["id"]

    async def search(self, *, query="", from_address=None, unread_only=False, since=None, folder=None, limit=20):
        base = f"/mailFolders/{_q(await self._folder(folder))}/messages" if folder else "/messages"
        if query and query.strip():
            kql = query.strip()
            if from_address:
                kql += f" from:{from_address}"
            if since:
                kql += f" received>={since.strftime('%Y-%m-%d')}"
            params = {"$search": '"' + kql.replace('"', '\\"') + '"', "$top": limit if not unread_only else 50,
                      "$select": GRAPH_MSG_SELECT}
        else:
            if not folder:
                base = "/mailFolders/inbox/messages"
            since_s = H.iso(since) if since else "1900-01-01T00:00:00Z"
            filters = [f"receivedDateTime ge {since_s}"]  # must lead the filter to allow $orderby on it
            if unread_only:
                filters.append("isRead eq false")
            if from_address:
                filters.append(f"from/emailAddress/address eq '{_odata(from_address)}'")
            params = {"$filter": " and ".join(filters), "$orderby": "receivedDateTime desc", "$top": limit,
                      "$select": GRAPH_MSG_SELECT}
        data = await self.api.get(base, params=params) or {}
        msgs = data.get("value") or []
        if unread_only:
            msgs = [m for m in msgs if not m.get("isRead")]
        return [self._summary(m) for m in msgs[:limit]]

    async def get(self, message_id, *, max_chars=8000):
        m = await self.api.get(f"/messages/{_q(message_id)}", params={"$select": GRAPH_MSG_SELECT + ",body,replyTo"},
                               headers=GRAPH_TEXT_BODY)
        atts = await self._attachments(message_id) if m.get("hasAttachments") else None
        return self._full(m, max_chars, atts)

    async def thread(self, thread_id, *, max_chars=3000, limit=25):
        async def by_conversation(cid: str) -> list[dict]:
            data = await self.api.get("/messages", params={
                "$filter": f"conversationId eq '{_odata(cid)}'", "$select": GRAPH_MSG_SELECT + ",body",
                "$top": 50}, headers=GRAPH_TEXT_BODY) or {}
            return data.get("value") or []

        msgs = await by_conversation(thread_id)
        if not msgs:  # maybe a message id was passed
            try:
                m = await self.api.get(f"/messages/{_q(thread_id)}", params={"$select": "conversationId"})
                thread_id = m.get("conversationId") or thread_id
                msgs = await by_conversation(thread_id)
            except H.ApiError:
                msgs = []
        if not msgs:
            raise ToolError(f"No conversation found for '{thread_id}'.")
        msgs.sort(key=lambda m: m.get("receivedDateTime") or "")
        full = [self._full(m, max_chars) for m in msgs[-limit:]]
        return {"thread_id": thread_id, "subject": full[0].get("subject"), "message_count": len(msgs), "messages": full}

    async def attachment(self, message_id, ref):
        atts = await self._attachments(message_id)
        att = _match_attachment(atts, ref)
        if att is None:
            names = ", ".join(f"{a['index']}: {a.get('filename')}" for a in atts) or "none"
            raise ToolError(f"No attachment '{ref}' on this message. Attachments: {names}.")
        data, _ = await self.api.fetch(f"/messages/{_q(message_id)}/attachments/{_q(att['id'])}/$value")
        return data, att.get("filename") or "attachment", att.get("mime") or "application/octet-stream"

    def _message(self, out: Outgoing) -> dict:
        plain, html = render_bodies(out.body, out.fmt)
        msg: dict[str, Any] = {"subject": out.subject, "body": {"contentType": "HTML" if html else "Text",
                                                                 "content": html or plain},
                               "toRecipients": _grecips(out.to)}
        if out.cc:
            msg["ccRecipients"] = _grecips(out.cc)
        if out.bcc:
            msg["bccRecipients"] = _grecips(out.bcc)
        if out.attachments:
            msg["attachments"] = [{"@odata.type": "#microsoft.graph.fileAttachment", "name": n, "contentType": mt,
                                   "contentBytes": base64.b64encode(d).decode("ascii")} for n, d, mt in out.attachments]
        return msg

    async def send(self, out):
        if sum(len(a[1]) for a in out.attachments) > GRAPH_INLINE_ATTACH_LIMIT:
            draft = await self.api.post("/messages", json={k: v for k, v in self._message(out).items()
                                                           if k != "attachments"})
            await self._add_attachments(draft["id"], out.attachments)
            await self.api.post(f"/messages/{_q(draft['id'])}/send", content=b"")
            return {"sent": True, "id": draft.get("id")}
        await self.api.post("/sendMail", json={"message": self._message(out), "saveToSentItems": True})
        return {"sent": True}

    async def _add_attachments(self, mid: str, atts: list[tuple[str, bytes, str]]) -> None:
        for name, data, mime in atts:
            if len(data) <= GRAPH_INLINE_ATTACH_LIMIT:
                await self.api.post(f"/messages/{_q(mid)}/attachments", json={
                    "@odata.type": "#microsoft.graph.fileAttachment", "name": name, "contentType": mime,
                    "contentBytes": base64.b64encode(data).decode("ascii")})
                continue
            session = await self.api.post(f"/messages/{_q(mid)}/attachments/createUploadSession", json={
                "AttachmentItem": {"attachmentType": "file", "name": name, "size": len(data)}})
            url = session["uploadUrl"]
            chunk = 3 * 1024 * 1024
            for start in range(0, len(data), chunk):
                part = data[start:start + chunk]
                await H.api("PUT", url, service="Microsoft 365 Mail", content=part, headers={
                    "Content-Range": f"bytes {start}-{start + len(part) - 1}/{len(data)}",
                    "Content-Type": "application/octet-stream"})

    async def reply_context(self, message_id):  # used only by the generic draft path
        m = await self.api.get(f"/messages/{_q(message_id)}", params={
            "$select": "subject,from,toRecipients,ccRecipients,replyTo,internetMessageId,conversationId"})
        return {"from": _gaddr(m.get("from")), "to": ", ".join(_gaddr(r) for r in m.get("toRecipients") or []),
                "cc": ", ".join(_gaddr(r) for r in m.get("ccRecipients") or []),
                "reply-to": ", ".join(_gaddr(r) for r in m.get("replyTo") or []), "subject": m.get("subject"),
                "message-id": m.get("internetMessageId"), "_thread_id": m.get("conversationId")}

    async def _reply_draft(self, message_id: str, body: str, *, reply_all: bool, cc: list[str] | None,
                           subject: str | None, fmt: str, attachments: list[tuple[str, bytes, str]]) -> dict:
        endpoint = "createReplyAll" if reply_all else "createReply"
        draft = await self.api.post(f"/messages/{_q(message_id)}/{endpoint}", json={})
        plain, html = render_bodies(body, fmt)
        quoted = (draft.get("body") or {}).get("content") or ""
        patch: dict[str, Any] = {"body": {"contentType": "HTML", "content": (html or text_to_html(plain)) + "<br>" + quoted}}
        if cc:
            patch["ccRecipients"] = (draft.get("ccRecipients") or []) + _grecips(cc)
        if subject:
            patch["subject"] = subject
        await self.api.patch(f"/messages/{_q(draft['id'])}", json=patch)
        if attachments:
            await self._add_attachments(draft["id"], attachments)
        return draft

    async def reply(self, message_id, body, *, reply_all=False, cc=None, subject=None, fmt="text", attachments=None):
        draft = await self._reply_draft(message_id, body, reply_all=reply_all, cc=cc, subject=subject, fmt=fmt,
                                        attachments=attachments or [])
        await self.api.post(f"/messages/{_q(draft['id'])}/send", content=b"")
        return {"sent": True, "to": [_gaddr(r) for r in draft.get("toRecipients") or []],
                "cc": [_gaddr(r) for r in draft.get("ccRecipients") or []] + list(cc or []),
                "subject": subject or draft.get("subject")}

    async def create_reply_draft(self, message_id, body, *, reply_all, cc, subject, fmt, attachments):
        draft = await self._reply_draft(message_id, body, reply_all=reply_all, cc=cc, subject=subject, fmt=fmt,
                                        attachments=attachments)
        return {"draft_id": draft.get("id"), "to": [_gaddr(r) for r in draft.get("toRecipients") or []],
                "subject": subject or draft.get("subject")}

    async def forward(self, message_id, to, body, *, cc=None, subject=None, fmt="text", include_attachments=True):
        plain, html = render_bodies(body, fmt)
        draft = await self.api.post(f"/messages/{_q(message_id)}/createForward", json={})
        quoted = (draft.get("body") or {}).get("content") or ""
        patch: dict[str, Any] = {"toRecipients": _grecips(to),
                                 "body": {"contentType": "HTML", "content": (html or text_to_html(plain)) + "<br>" + quoted}}
        if cc:
            patch["ccRecipients"] = _grecips(cc)
        if subject:
            patch["subject"] = subject
        await self.api.patch(f"/messages/{_q(draft['id'])}", json=patch)
        names: list[str] = []
        if not include_attachments:
            for a in await self._attachments(draft["id"]):
                await self.api.delete(f"/messages/{_q(draft['id'])}/attachments/{_q(a['id'])}")
        else:
            names = [a.get("filename") for a in await self._attachments(draft["id"])]
        await self.api.post(f"/messages/{_q(draft['id'])}/send", content=b"")
        return {"sent": True, "to": to, "cc": cc or [], "subject": subject or draft.get("subject"),
                "attachments": names}

    async def create_draft(self, out):
        draft = await self.api.post("/messages", json={k: v for k, v in self._message(out).items() if k != "attachments"})
        if out.attachments:
            await self._add_attachments(draft["id"], out.attachments)
        return {"draft_id": draft.get("id"), "web_link": draft.get("webLink")}

    async def update_draft(self, draft_id, *, to, cc, subject, body, fmt):
        patch: dict[str, Any] = {}
        if to is not None:
            patch["toRecipients"] = _grecips(to)
        if cc is not None:
            patch["ccRecipients"] = _grecips(cc)
        if subject is not None:
            patch["subject"] = subject
        if body is not None:
            plain, html = render_bodies(body, fmt)
            patch["body"] = {"contentType": "HTML" if html else "Text", "content": html or plain}
        if not patch:
            raise ToolError("Nothing to update — pass to, cc, subject or body.")
        await self.api.patch(f"/messages/{_q(draft_id)}", json=patch)
        return {"draft_id": draft_id, "updated": True}

    async def delete_draft(self, draft_id):
        await self.api.delete(f"/messages/{_q(draft_id)}")
        return {"draft_id": draft_id, "deleted": True}

    async def modify(self, ids, *, add=(), remove=(), archive=False, read=None):
        for mid in ids:
            patch: dict[str, Any] = {}
            if read is not None:
                patch["isRead"] = read
            if add or remove:
                cur = await self.api.get(f"/messages/{_q(mid)}", params={"$select": "categories"}) or {}
                cats = [c for c in cur.get("categories") or [] if c.lower() not in {r.lower() for r in remove}]
                for a in add:
                    if a.lower() not in {c.lower() for c in cats}:
                        cats.append(a)
                patch["categories"] = cats
            if patch:
                await self.api.patch(f"/messages/{_q(mid)}", json=patch)
            if archive:
                await self.api.post(f"/messages/{_q(mid)}/move", json={"destinationId": "archive"})
        return {"updated": len(ids), "added_categories": list(add), "removed_categories": list(remove)}


# --------------------------------------------------------------------------- IMAP / SMTP
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$')
_SPECIAL_USE = {"sent": "\\sent", "drafts": "\\drafts", "archive": "\\archive", "trash": "\\trash",
                "spam": "\\junk", "all": "\\all"}
_FOLDER_FALLBACK = {
    "sent": ["Sent", "Sent Items", "Sent Messages", "Sent Mail", "[Gmail]/Sent Mail", "INBOX.Sent"],
    "drafts": ["Drafts", "Draft", "[Gmail]/Drafts", "INBOX.Drafts"],
    "archive": ["Archive", "Archives", "[Gmail]/All Mail", "INBOX.Archive"],
    "trash": ["Trash", "Deleted Items", "Deleted Messages", "[Gmail]/Trash", "INBOX.Trash"],
    "spam": ["Junk", "Spam", "Junk E-mail", "Junk Email", "[Gmail]/Spam", "INBOX.Junk", "INBOX.Spam"],
}


def utf7_encode(s: str) -> str:
    """IMAP modified UTF-7 (RFC 3501 §5.1.3) for mailbox names."""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            b = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(b).decode("ascii").rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def utf7_decode(s: str) -> str:
    def repl(m: re.Match) -> str:
        chunk = m.group(1)
        if not chunk:
            return "&"
        b64 = chunk.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            return base64.b64decode(b64).decode("utf-16-be", "replace")
        except Exception:  # noqa: BLE001
            return m.group(0)

    return re.sub(r"&([A-Za-z0-9+,]*)-", repl, s)


def _iq(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _split_id(mid: str) -> tuple[str, str]:
    s = str(mid or "").strip()
    if s.isdigit():
        return "INBOX", s
    box, sep, uid = s.rpartition(":")
    if sep and uid.isdigit() and box:
        return box, uid
    raise ToolError(f"Invalid IMAP message id '{mid}' (expected 'FOLDER:UID', e.g. 'INBOX:1234').")


def _fetch_items(data: list) -> list[list[bytes]]:
    """imaplib FETCH response → [[meta, payload]] (meta includes trailing bytes like ' UID 5)')."""
    items: list[list[bytes]] = []
    for el in data or []:
        if isinstance(el, tuple):
            items.append([el[0] or b"", el[1] or b""])
        elif isinstance(el, bytes) and items:
            items[-1][0] = items[-1][0] + b" " + el
    return items


def _imap_err(e: Exception) -> str:
    s = str(e)
    if s.startswith("b'") or s.startswith('b"'):
        s = s[2:-1]
    return s[:200]


def _email_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:  # noqa: BLE001
        content = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    return H.html_to_text(content) if part.get_content_type() == "text/html" else str(content)


def _email_attachments(msg: EmailMessage) -> list[tuple[dict, Any]]:
    out = []
    for i, part in enumerate(msg.iter_attachments()):
        payload = part.get_payload(decode=True) or b""
        out.append(({"index": i + 1, "id": str(i + 1), "filename": part.get_filename() or f"attachment-{i + 1}",
                     "mime": part.get_content_type(), "size": len(payload)}, part))
    return out


class ImapSmtpAdapter(MailAdapter):
    kind = "imap_smtp"
    label = "IMAP/SMTP"

    # --- connections ------------------------------------------------------------------------
    def _mode(self, port: int | None, default_port_ssl: int, default_port_plain: int) -> tuple[str, int]:
        raw = str(self.cfg.get("use_ssl", "ssl")).strip().lower()
        if raw in ("none", "plain", "insecure"):
            mode = "none"
        elif raw in ("ssl", "tls", "true", "1", "yes", "on", "implicit", ""):
            mode = "ssl"
        else:
            mode = "starttls"
        if port in (465, 993):
            mode = "ssl"
        elif port in (25, 143, 587) and mode == "ssl":
            mode = "starttls"
        return mode, int(port or (default_port_ssl if mode == "ssl" else default_port_plain))

    def _port(self, key: str) -> int | None:
        v = str(self.cfg.get(key) or "").strip()
        return int(v) if v.isdigit() else None

    def _imap(self) -> imaplib.IMAP4:
        host = str(self.cfg.get("imap_host") or "").strip()
        if not host:
            raise ToolError("The IMAP/SMTP connector has no IMAP host.")
        mode, port = self._mode(self._port("imap_port"), 993, 143)
        tls = ssl.create_default_context()
        try:
            if mode == "ssl":
                conn = imaplib.IMAP4_SSL(host, port, ssl_context=tls, timeout=30)
            else:
                conn = imaplib.IMAP4(host, port, timeout=30)
                if mode == "starttls":
                    conn.starttls(ssl_context=tls)
        except (OSError, ssl.SSLError, imaplib.IMAP4.error) as e:
            raise ToolError(f"Could not connect to IMAP server {host}:{port} ({type(e).__name__}: {_imap_err(e)}).") from e
        try:
            conn.login(str(self.cfg.get("username") or ""), str(self.cfg.get("password") or ""))
        except imaplib.IMAP4.error as e:
            raise ToolError(f"IMAP login failed ({_imap_err(e)}) — check the username/password (many providers "
                            "require an app password) in Connectors.") from e
        return conn

    def _with_imap(self, fn):
        conn = self._imap()
        try:
            return fn(conn)
        except (imaplib.IMAP4.error, OSError) as e:
            raise ToolError(f"IMAP error: {_imap_err(e)}") from e
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def _sender(self) -> str:
        sender = str(self.cfg.get("from_address") or "").strip()
        if not sender:
            user = str(self.cfg.get("username") or "").strip()
            if "@" not in user:
                raise ToolError("Set 'From address' in the IMAP/SMTP connector (the username isn't an email address).")
            sender = user
        return sender

    def _smtp_send(self, msg: EmailMessage) -> dict:
        host = str(self.cfg.get("smtp_host") or "").strip()
        if not host:
            raise ToolError("The IMAP/SMTP connector has no SMTP host.")
        mode, port = self._mode(self._port("smtp_port"), 465, 587)
        tls = ssl.create_default_context()
        try:
            if mode == "ssl":
                server = smtplib.SMTP_SSL(host, port, context=tls, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.ehlo()
                if mode == "starttls":
                    server.starttls(context=tls)
                    server.ehlo()
            try:
                if self.cfg.get("username"):
                    server.login(str(self.cfg["username"]), str(self.cfg.get("password") or ""))
                refused = server.send_message(msg) or {}
            finally:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001
                    pass
        except smtplib.SMTPAuthenticationError as e:
            raise ToolError("SMTP login failed — check the username/password (many providers require an app "
                            "password).") from e
        except smtplib.SMTPRecipientsRefused as e:
            raise ToolError(f"The SMTP server refused the recipients: {', '.join(e.recipients)}.") from e
        except smtplib.SMTPException as e:
            raise ToolError(f"SMTP error: {e}") from e
        except (OSError, ssl.SSLError) as e:
            raise ToolError(f"Could not connect to SMTP server {host}:{port} ({type(e).__name__}).") from e
        return {"refused": list(refused)} if refused else {}

    # --- mailbox helpers (run inside threads) ------------------------------------------------
    def _boxes(self, conn: imaplib.IMAP4) -> list[tuple[set[str], str]]:
        typ, data = conn.list()
        out: list[tuple[set[str], str]] = []
        for item in data or []:
            if item is None:
                continue
            if isinstance(item, tuple):
                m = _LIST_RE.match(item[0].strip())
                raw_name = item[1]
            else:
                m = _LIST_RE.match(item.strip())
                raw_name = m.group("name") if m else None
            if not m or raw_name is None:
                continue
            name = raw_name.decode("utf-8", "replace") if isinstance(raw_name, bytes) else str(raw_name)
            name = name.strip()
            if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            flags = {f.lower() for f in m.group("flags").decode("utf-8", "replace").split()}
            out.append((flags, name))
        return out

    def _folder(self, conn: imaplib.IMAP4, name: str | None, *, create: bool = False) -> str:
        n = (name or "INBOX").strip()
        if n.lower() == "inbox":
            return "INBOX"
        boxes = self._boxes(conn)
        key = "spam" if n.lower() == "junk" else n.lower()
        if key in _SPECIAL_USE:
            for flags, raw in boxes:
                if _SPECIAL_USE[key] in flags:
                    return raw
            for cand in _FOLDER_FALLBACK.get(key, []):
                for _, raw in boxes:
                    if utf7_decode(raw).lower() == cand.lower():
                        return raw
        for _, raw in boxes:
            if raw.lower() == n.lower() or utf7_decode(raw).lower() == n.lower():
                return raw
        if create:
            raw = utf7_encode(n)
            conn.create(_iq(raw))
            return raw
        avail = ", ".join(utf7_decode(r) for _, r in boxes[:40]) or "none"
        raise ToolError(f"Mail folder '{name}' not found. Folders: {avail}.")

    @staticmethod
    def _select(conn: imaplib.IMAP4, box: str, readonly: bool = True) -> None:
        typ, data = conn.select(_iq(box), readonly=readonly)
        if typ != "OK":
            raise ToolError(f"Could not open mail folder '{utf7_decode(box)}'.")

    @staticmethod
    def _search(conn: imaplib.IMAP4, crit: list[str], text: str | None = None, key: str = "TEXT") -> list[str]:
        if text:
            if text.isascii():
                typ, data = conn.uid("SEARCH", *crit, key, _iq(text))
            else:
                conn.literal = text.encode("utf-8")
                typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *crit, key)
        else:
            typ, data = conn.uid("SEARCH", *(crit or ["ALL"]))
        if typ != "OK":
            raise ToolError("IMAP search failed.")
        return [u.decode() for u in ((data or [b""])[0] or b"").split()]

    @staticmethod
    def _fetch_message(conn: imaplib.IMAP4, uid: str) -> tuple[EmailMessage, set[str]]:
        typ, data = conn.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
        items = _fetch_items(data)
        if typ != "OK" or not items:
            raise ToolError(f"Message {uid} not found.")
        meta, raw = items[0]
        flags = re.search(rb"FLAGS \(([^)]*)\)", meta)
        msg = BytesParser(policy=email.policy.default).parsebytes(raw)
        return msg, set((flags.group(1).decode() if flags else "").split())

    def _summary(self, box: str, uid: str, msg: Any, flags: set[str]) -> dict:
        return H.compact({
            "id": f"{box}:{uid}", "thread_id": f"{box}:{uid}", "from": str(msg.get("from") or ""),
            "to": H.clip(str(msg.get("to") or ""), 300), "date": _date_iso(str(msg.get("date") or "")),
            "subject": str(msg.get("subject") or "(no subject)"), "unread": "\\Seen" not in flags,
            "folder": utf7_decode(box),
        })

    def _full(self, box: str, uid: str, msg: EmailMessage, flags: set[str], max_chars: int) -> dict:
        text = _email_text(msg)
        out = self._summary(box, uid, msg, flags)
        out.update({"cc": str(msg.get("cc") or "") or None, "reply_to": str(msg.get("reply-to") or "") or None,
                    "body": H.clip(text.strip(), max_chars), "body_truncated": len(text) > max_chars or None,
                    "attachments": [a for a, _ in _email_attachments(msg)] or None})
        return H.compact(out)

    def _delete_uid(self, conn: imaplib.IMAP4, box: str, uid: str) -> None:
        self._select(conn, box, readonly=False)
        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        caps = {c.upper() for c in (conn.capabilities or ())}
        if "UIDPLUS" in caps:
            conn.uid("EXPUNGE", uid)
        else:
            conn.expunge()

    # --- sync operations -----------------------------------------------------------------------
    def _search_sync(self, query, from_address, unread_only, since, folder, limit) -> list[dict]:
        def op(conn: imaplib.IMAP4) -> list[dict]:
            box = self._folder(conn, folder or "INBOX")
            self._select(conn, box)
            crit: list[str] = []
            if unread_only:
                crit.append("UNSEEN")
            if from_address:
                crit += ["FROM", _iq(from_address)]
            if since:
                crit += ["SINCE", since.strftime("%d-%b-%Y")]
            uids = self._search(conn, crit, (query or "").strip() or None)
            uids = sorted(uids, key=int)[-limit:][::-1]
            if not uids:
                return []
            typ, data = conn.uid("FETCH", ",".join(uids),
                                 "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])")
            rows = []
            for meta, payload in _fetch_items(data):
                um = re.search(rb"UID (\d+)", meta)
                if not um:
                    continue
                fm = re.search(rb"FLAGS \(([^)]*)\)", meta)
                hdr = BytesHeaderParser(policy=email.policy.default).parsebytes(payload)
                rows.append(self._summary(box, um.group(1).decode(), hdr, set((fm.group(1).decode() if fm else "").split())))
            rows.sort(key=lambda r: int(r["id"].rpartition(":")[2]), reverse=True)
            return rows

        return self._with_imap(op)

    def _get_sync(self, message_id: str, max_chars: int) -> dict:
        box, uid = _split_id(message_id)

        def op(conn):
            self._select(conn, box)
            msg, flags = self._fetch_message(conn, uid)
            return self._full(box, uid, msg, flags, max_chars)

        return self._with_imap(op)

    def _raw_sync(self, message_id: str) -> EmailMessage:
        box, uid = _split_id(message_id)

        def op(conn):
            self._select(conn, box)
            return self._fetch_message(conn, uid)[0]

        return self._with_imap(op)

    def _thread_sync(self, message_id: str, max_chars: int, limit: int) -> dict:
        box, uid = _split_id(message_id)

        def op(conn):
            self._select(conn, box)
            root, _ = self._fetch_message(conn, uid)
            subject = norm_subject(str(root.get("subject") or ""))
            boxes = [box]
            try:
                sent = self._folder(conn, "sent")
                if sent != box:
                    boxes.append(sent)
            except ToolError:
                pass
            found: dict[str, tuple[datetime, dict]] = {}
            for b in boxes:
                self._select(conn, b)
                uids = self._search(conn, [], subject, key="SUBJECT") if subject else ([uid] if b == box else [])
                for u in sorted(uids, key=int)[-limit:]:
                    msg, flags = self._fetch_message(conn, u)
                    if subject and norm_subject(str(msg.get("subject") or "")).lower() != subject.lower():
                        continue
                    key = str(msg.get("message-id") or f"{b}:{u}")
                    when = H.parse_datetime(str(msg.get("date") or "")) or datetime.min.replace(tzinfo=timezone.utc)
                    found.setdefault(key, (when, self._full(b, u, msg, flags, max_chars)))
            msgs = [v for _, v in sorted(found.values(), key=lambda x: x[0])][-limit:]
            return {"thread_id": message_id, "subject": subject, "message_count": len(found), "messages": msgs,
                    "note": "IMAP has no native threads; messages were grouped by subject (incl. the Sent folder)."}

        return self._with_imap(op)

    def _append_sync(self, folder_key: str, msg: EmailMessage, flags: str) -> str:
        def op(conn):
            box = self._folder(conn, folder_key, create=True)
            typ, data = conn.append(_iq(box), flags, imaplib.Time2Internaldate(time.time()),
                                    msg.as_bytes(policy=email.policy.SMTP))
            if typ != "OK":
                raise ToolError(f"Could not save the message to '{utf7_decode(box)}'.")
            m = re.search(rb"APPENDUID \d+ (\d+)", (data or [b""])[0] or b"")
            if m:
                return f"{box}:{m.group(1).decode()}"
            self._select(conn, box)
            uids = self._search(conn, ["HEADER", "Message-ID", _iq(str(msg["Message-ID"]))])
            return f"{box}:{uids[-1]}" if uids else box

        return self._with_imap(op)

    def _delete_sync(self, message_id: str) -> None:
        box, uid = _split_id(message_id)
        self._with_imap(lambda conn: self._delete_uid(conn, box, uid))

    def _modify_sync(self, ids: list[str], add: list[str], remove: list[str], archive: bool, read: bool | None) -> dict:
        by_box: dict[str, list[str]] = {}
        for mid in ids:
            box, uid = _split_id(mid)
            by_box.setdefault(box, []).append(uid)
        notes: list[str] = []

        def op(conn):
            caps = {c.upper() for c in (conn.capabilities or ())}
            for box, uids in by_box.items():
                self._select(conn, box, readonly=False)
                uset = ",".join(uids)
                if read is True:
                    conn.uid("STORE", uset, "+FLAGS", "(\\Seen)")
                elif read is False:
                    conn.uid("STORE", uset, "-FLAGS", "(\\Seen)")
                for name in add:
                    target = self._folder(conn, name, create=True)
                    self._select(conn, box, readonly=False)
                    conn.uid("COPY", uset, _iq(target))
                if remove:
                    notes.append("IMAP folders work like labels: removing a label isn't supported (move the "
                                 "message instead).")
                if archive:
                    target = self._folder(conn, "archive", create=True)
                    self._select(conn, box, readonly=False)
                    if "MOVE" in caps:
                        conn.uid("MOVE", uset, _iq(target))
                    else:
                        conn.uid("COPY", uset, _iq(target))
                        conn.uid("STORE", uset, "+FLAGS", "(\\Deleted)")
                        if "UIDPLUS" in caps:
                            conn.uid("EXPUNGE", uset)
                        else:
                            conn.expunge()
            return {"updated": len(ids), "notes": notes or None}

        return H.compact(self._with_imap(op))

    # --- async surface -------------------------------------------------------------------------
    async def me(self) -> str:
        if self._me is None:
            _, addr = getaddresses([self._sender()])[0]
            self._me = addr
        return self._me

    async def search(self, *, query="", from_address=None, unread_only=False, since=None, folder=None, limit=20):
        return await asyncio.to_thread(self._search_sync, query, from_address, unread_only, since, folder, limit)

    async def get(self, message_id, *, max_chars=8000):
        return await asyncio.to_thread(self._get_sync, message_id, max_chars)

    async def thread(self, thread_id, *, max_chars=3000, limit=25):
        return await asyncio.to_thread(self._thread_sync, thread_id, max_chars, limit)

    async def attachment(self, message_id, ref):
        msg = await asyncio.to_thread(self._raw_sync, message_id)
        pairs = _email_attachments(msg)
        att = _match_attachment([a for a, _ in pairs], ref)
        if att is None:
            names = ", ".join(f"{a['index']}: {a['filename']}" for a, _ in pairs) or "none"
            raise ToolError(f"No attachment '{ref}' on this message. Attachments: {names}.")
        part = pairs[att["index"] - 1][1]
        return part.get_payload(decode=True) or b"", att["filename"], att["mime"]

    async def send(self, out):
        msg = build_mime(out, self._sender())
        res = await asyncio.to_thread(self._smtp_send, msg)
        if H.truthy(self.cfg.get("save_to_sent")):
            try:
                await asyncio.to_thread(self._append_sync, "sent", msg, "(\\Seen)")
            except ToolError as e:
                res["note"] = f"Sent, but saving a copy to the Sent folder failed: {e}"
        return {"sent": True, "message_id_header": str(msg["Message-ID"]), **res}

    async def reply_context(self, message_id):
        msg = await asyncio.to_thread(self._raw_sync, message_id)
        h = {k: str(msg.get(k) or "") for k in ("from", "to", "cc", "reply-to", "subject", "message-id", "references")}
        return {k: v for k, v in h.items() if v}

    async def forward_source(self, message_id, include_attachments):
        msg = await asyncio.to_thread(self._raw_sync, message_id)
        box, uid = _split_id(message_id)
        full = self._full(box, uid, msg, {"\\Seen"}, 200_000)
        atts = []
        if include_attachments:
            for meta, part in _email_attachments(msg):
                atts.append((meta["filename"], part.get_payload(decode=True) or b"", meta["mime"]))
        if sum(len(a[1]) for a in atts) > MAX_ATTACH_TOTAL:
            raise ToolError("The original's attachments exceed 25 MB — forward without attachments instead.")
        return full, atts

    async def create_draft(self, out):
        msg = build_mime(out, self._sender())
        mid = await asyncio.to_thread(self._append_sync, "drafts", msg, "(\\Draft \\Seen)")
        return {"draft_id": mid}

    async def update_draft(self, draft_id, *, to, cc, subject, body, fmt):
        old = await asyncio.to_thread(self._raw_sync, draft_id)
        atts = [(m["filename"], p.get_payload(decode=True) or b"", m["mime"]) for m, p in _email_attachments(old)]
        out = Outgoing(
            to=to if to is not None else norm_addresses(str(old.get("to") or "")),
            cc=cc if cc is not None else norm_addresses(str(old.get("cc") or "")),
            bcc=norm_addresses(str(old.get("bcc") or "")),
            subject=subject if subject is not None else str(old.get("subject") or ""),
            body=body if body is not None else _email_text(old), fmt=fmt if body is not None else "text",
            attachments=atts, in_reply_to=str(old.get("in-reply-to") or "") or None,
            references=str(old.get("references") or "") or None)
        new_id = (await self.create_draft(out))["draft_id"]
        await asyncio.to_thread(self._delete_sync, draft_id)
        return {"draft_id": new_id, "replaced": draft_id, "updated": True}

    async def delete_draft(self, draft_id):
        await asyncio.to_thread(self._delete_sync, draft_id)
        return {"draft_id": draft_id, "deleted": True}

    async def modify(self, ids, *, add=(), remove=(), archive=False, read=None):
        return await asyncio.to_thread(self._modify_sync, list(ids), list(add), list(remove), archive, read)


ADAPTERS: dict[str, type[MailAdapter]] = {"gmail": GmailAdapter, "microsoft365": GraphMailAdapter,
                                          "imap_smtp": ImapSmtpAdapter}


async def mail_adapter(ctx: ToolContext, account: str | None = None) -> MailAdapter:
    """The mailbox adapter serving this call (also used by the calendar and notify groups)."""
    kind, cfg = await H.pick_connector(ctx, MAIL_KINDS, account, what="Email")
    return ADAPTERS[kind](ctx, cfg)


async def optional_mail_adapter(ctx: ToolContext, account: str | None = None) -> MailAdapter | None:
    try:
        return await mail_adapter(ctx, account)
    except ToolError:
        return None


# =========================================================================== tool group
group = ToolGroup(
    "email", "Email",
    "Search, read, draft and send email through Gmail, Microsoft 365 (Outlook) or any IMAP/SMTP mailbox; summarize "
    "threads and extract action items, deadlines and follow-ups.",
    prefix="email", icon="mail", category="productivity", connector_kinds=MAIL_KINDS,
)


def _acct(args: dict) -> str:
    return args.get("account") or "default mailbox"


def _send_preview(a: dict) -> str:
    return H.preview("Send email", [
        ("Account", _acct(a)), ("To", H.as_list(a.get("to"))), ("Cc", H.as_list(a.get("cc"))),
        ("Bcc", H.as_list(a.get("bcc"))), ("Subject", a.get("subject")), ("Attachments", H.as_list(a.get("attachments"))),
        ("Format", a.get("body_format") if a.get("body_format") not in (None, "text") else None),
    ], a.get("body", ""), body_label="Body")


def _reply_preview(a: dict) -> str:
    return H.preview("Reply to email", [
        ("Account", _acct(a)), ("In reply to message", a.get("message_id")),
        ("Reply all", bool(a.get("reply_all"))), ("Additional Cc", H.as_list(a.get("cc"))),
        ("Subject", a.get("subject") or "Re: <original subject>"), ("Attachments", H.as_list(a.get("attachments"))),
    ], a.get("body", ""), body_label="Body",
        note="Recipients come from the original message: its sender" + (" plus all its recipients."
                                                                         if a.get("reply_all") else "."))


def _forward_preview(a: dict) -> str:
    return H.preview("Forward email", [
        ("Account", _acct(a)), ("Message", a.get("message_id")), ("To", H.as_list(a.get("to"))),
        ("Cc", H.as_list(a.get("cc"))), ("Subject", a.get("subject") or "Fwd: <original subject>"),
        ("Include original attachments", a.get("include_attachments", True)),
    ], a.get("body", ""), body_label="Comment above the forwarded message",
        note="The full original message (and its attachments, if included) will be sent to the recipients.")


def _draft_preview(a: dict) -> str:
    rows = [("Account", _acct(a)), ("To", H.as_list(a.get("to"))), ("Cc", H.as_list(a.get("cc"))),
            ("Subject", a.get("subject")), ("Reply to message", a.get("reply_to_message_id"))]
    if a.get("draft_id"):
        rows.insert(1, ("Draft", a.get("draft_id")))
    return H.preview("Save email draft (not sent)", rows, a.get("body") if a.get("body") is not None else None,
                     body_label="Body")


@group.tool(risk=Risk.LOW, scope="read")
async def search(ctx: ToolContext, query: str = "", from_address: str | None = None, unread_only: bool = False,
                 since: str | None = None, folder: str | None = None, limit: int = 20,
                 account: str | None = None) -> dict:
    """Search the mailbox and list matching messages (newest first) with sender, subject, date and a snippet.

    Args:
        query: Search text in the provider's syntax (Gmail: `from:anna has:attachment invoice`; Outlook: KQL such as `subject:budget`; IMAP: plain words matched anywhere). Empty = latest messages.
        from_address: Only messages from this sender address.
        unread_only: Only unread messages.
        since: Only messages received on/after this date (YYYY-MM-DD, or e.g. "7d" for the last 7 days).
        folder: inbox, sent, drafts, archive, spam, trash, or a label/folder name. Default: all mail (Gmail/Outlook search) or INBOX (IMAP).
        limit: Maximum messages to return (1-50).
        account: Connector name or kind (gmail, microsoft365, imap_smtp) when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    rows = await ad.search(query=query, from_address=from_address, unread_only=unread_only,
                           since=_parse_since(since), folder=folder, limit=_clamp(limit, 1, 50))
    return {"account": ad.label, "count": len(rows), "messages": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def get(ctx: ToolContext, message_id: str, max_chars: int = 8000, account: str | None = None) -> dict:
    """Read one email: headers, plain-text body (HTML converted) and its attachment list.

    Args:
        message_id: Message id from email_search / email_thread.
        max_chars: Maximum body characters to return.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    return await ad.get(message_id, max_chars=_clamp(max_chars, 200, 100_000))


@group.tool(risk=Risk.LOW, scope="read")
async def thread(ctx: ToolContext, thread_id: str, max_chars_per_message: int = 3000,
                 account: str | None = None) -> dict:
    """Read a whole conversation (all messages of a thread, oldest first).

    Args:
        thread_id: Thread/conversation id from email_search (a message id also works).
        max_chars_per_message: Body characters to keep per message.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    return await ad.thread(thread_id, max_chars=_clamp(max_chars_per_message, 200, 20_000))


@group.tool(risk=Risk.LOW, scope="read")
async def list_attachments(ctx: ToolContext, message_id: str, account: str | None = None) -> dict:
    """List the attachments of an email (index, id, file name, type, size).

    Args:
        message_id: Message id.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    msg = await ad.get(message_id, max_chars=200)
    return {"message_id": message_id, "subject": msg.get("subject"), "attachments": msg.get("attachments") or []}


@group.tool(risk=Risk.LOW, scope="write")
async def download_attachment(ctx: ToolContext, message_id: str, attachment: str, account: str | None = None) -> dict:
    """Download an email attachment into the workspace `downloads/` folder (never overwrites existing files).

    Args:
        message_id: Message id.
        attachment: Attachment index (1, 2, ...), id or exact file name from email_list_attachments.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    data, name, mime = await ad.attachment(message_id, attachment)
    saved = await H.save_bytes(ctx, data, name or "attachment", "downloads")
    return {**saved, "source": f"email {message_id}"}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("subject", "body"), preview=_send_preview)
async def send(ctx: ToolContext, to: list[str], subject: str, body: str, cc: list[str] | None = None,
               bcc: list[str] | None = None, body_format: BodyFormat = "text", attachments: list[str] | None = None,
               account: str | None = None) -> dict:
    """Send a new email.

    Args:
        to: Recipient addresses ("Name <a@b.com>" or "a@b.com").
        subject: Subject line.
        body: Message body.
        cc: Cc addresses.
        bcc: Bcc addresses.
        body_format: "text" (default), "markdown" (rendered to HTML with a plain-text alternative) or "html".
        attachments: Workspace file paths to attach, e.g. ["output/report.pdf"] (25 MB total).
        account: Connector name or kind when several mailboxes are connected.
    """
    out = Outgoing(to=norm_addresses(to, "to"), cc=norm_addresses(cc, "cc"), bcc=norm_addresses(bcc, "bcc"),
                   subject=subject, body=body, fmt=body_format, attachments=await _load_attachments(ctx, attachments))
    if not out.to:
        raise ToolError("At least one recipient is required.")
    ad = await mail_adapter(ctx, account)
    res = await ad.send(out)
    return {**res, "account": ad.label, "to": out.to, "cc": out.cc or None, "subject": subject}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("subject", "body"), preview=_reply_preview)
async def reply(ctx: ToolContext, message_id: str, body: str, reply_all: bool = False, cc: list[str] | None = None,
                subject: str | None = None, body_format: BodyFormat = "text", attachments: list[str] | None = None,
                account: str | None = None) -> dict:
    """Reply to an email in its thread (to the sender, or to everyone with reply_all).

    Args:
        message_id: The message being replied to.
        body: Reply text.
        reply_all: Also reply to all original To/Cc recipients.
        cc: Extra Cc addresses.
        subject: Override the subject (default "Re: <original subject>").
        body_format: "text", "markdown" or "html".
        attachments: Workspace file paths to attach.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    res = await ad.reply(message_id, body, reply_all=reply_all, cc=norm_addresses(cc, "cc"), subject=subject,
                         fmt=body_format, attachments=await _load_attachments(ctx, attachments))
    return {**res, "account": ad.label, "in_reply_to": message_id}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("subject", "body"), preview=_forward_preview)
async def forward(ctx: ToolContext, message_id: str, to: list[str], body: str = "", cc: list[str] | None = None,
                  include_attachments: bool = True, subject: str | None = None, body_format: BodyFormat = "text",
                  account: str | None = None) -> dict:
    """Forward an email (with its attachments) to new recipients, with an optional comment on top.

    Args:
        message_id: The message to forward.
        to: Recipient addresses.
        body: Comment placed above the forwarded message.
        cc: Cc addresses.
        include_attachments: Forward the original attachments too.
        subject: Override the subject (default "Fwd: <original subject>").
        body_format: "text", "markdown" or "html" (format of the comment).
        account: Connector name or kind when several mailboxes are connected.
    """
    to_n = norm_addresses(to, "to")
    if not to_n:
        raise ToolError("At least one recipient is required.")
    ad = await mail_adapter(ctx, account)
    res = await ad.forward(message_id, to_n, body, cc=norm_addresses(cc, "cc"), subject=subject, fmt=body_format,
                           include_attachments=include_attachments)
    return {**res, "account": ad.label, "forwarded": message_id}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("subject", "body"), preview=_draft_preview)
async def create_draft(ctx: ToolContext, to: list[str] | None = None, subject: str = "", body: str = "",
                       cc: list[str] | None = None, bcc: list[str] | None = None, body_format: BodyFormat = "text",
                       attachments: list[str] | None = None, reply_to_message_id: str | None = None,
                       reply_all: bool = False, account: str | None = None) -> dict:
    """Save an email draft (not sent) — a new message, or a reply when reply_to_message_id is given.

    Args:
        to: Recipients (ignored for reply drafts, which address the original sender).
        subject: Subject line (reply drafts default to "Re: <original subject>").
        body: Draft body.
        cc: Cc addresses.
        bcc: Bcc addresses.
        body_format: "text", "markdown" or "html".
        attachments: Workspace file paths to attach.
        reply_to_message_id: Make this a reply draft in that message's thread.
        reply_all: For reply drafts, address all original recipients.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    atts = await _load_attachments(ctx, attachments)
    if reply_to_message_id:
        res = await ad.create_reply_draft(reply_to_message_id, body, reply_all=reply_all,
                                          cc=norm_addresses(cc, "cc"), subject=subject or None, fmt=body_format,
                                          attachments=atts)
    else:
        res = await ad.create_draft(Outgoing(to=norm_addresses(to, "to"), cc=norm_addresses(cc, "cc"),
                                             bcc=norm_addresses(bcc, "bcc"), subject=subject, body=body,
                                             fmt=body_format, attachments=atts))
    return H.compact({**res, "account": ad.label, "saved": True,
                      "note": "Draft saved — nothing was sent. Use email_send/email_reply to send."})


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("subject", "body"), preview=_draft_preview)
async def update_draft(ctx: ToolContext, draft_id: str, to: list[str] | None = None, subject: str | None = None,
                       body: str | None = None, cc: list[str] | None = None, body_format: BodyFormat = "text",
                       account: str | None = None) -> dict:
    """Change an existing draft's recipients, subject or body (unspecified fields stay as they are).

    Args:
        draft_id: Draft id from email_create_draft (Gmail also accepts the draft's message id).
        to: New recipient list.
        subject: New subject.
        body: New body (replaces the old body).
        cc: New Cc list.
        body_format: Format of the new body: "text", "markdown" or "html".
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    res = await ad.update_draft(draft_id, to=norm_addresses(to, "to") if to is not None else None,
                                cc=norm_addresses(cc, "cc") if cc is not None else None, subject=subject, body=body,
                                fmt=body_format)
    return {**res, "account": ad.label}


@group.tool(risk=Risk.MEDIUM, scope="write",
            preview=lambda a: H.preview("Delete email draft", [("Account", _acct(a)), ("Draft", a.get("draft_id"))]))
async def delete_draft(ctx: ToolContext, draft_id: str, account: str | None = None) -> dict:
    """Delete an unsent draft.

    Args:
        draft_id: Draft id (Gmail also accepts the draft's message id).
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    return {**await ad.delete_draft(draft_id), "account": ad.label}


def _ids(message_ids: list[str]) -> list[str]:
    ids = [i for i in H.as_list(message_ids)]
    if not ids:
        raise ToolError("Pass at least one message id.")
    if len(ids) > 500:
        raise ToolError("At most 500 messages per call.")
    return ids


@group.tool(risk=Risk.MEDIUM, scope="write")
async def label(ctx: ToolContext, message_ids: list[str], add: list[str] | None = None,
                remove: list[str] | None = None, account: str | None = None) -> dict:
    """Add or remove labels (Gmail labels, Outlook categories, IMAP folders) on messages.

    Args:
        message_ids: Messages to change.
        add: Label names to add (created if missing).
        remove: Label names to remove.
        account: Connector name or kind when several mailboxes are connected.
    """
    if not add and not remove:
        raise ToolError("Pass labels to add or remove.")
    ad = await mail_adapter(ctx, account)
    return {**await ad.modify(_ids(message_ids), add=H.as_list(add), remove=H.as_list(remove)), "account": ad.label}


@group.tool(risk=Risk.MEDIUM, scope="write")
async def archive(ctx: ToolContext, message_ids: list[str], account: str | None = None) -> dict:
    """Archive messages (remove them from the inbox without deleting).

    Args:
        message_ids: Messages to archive.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    return {**await ad.modify(_ids(message_ids), archive=True), "account": ad.label}


@group.tool(risk=Risk.MEDIUM, scope="write")
async def mark_read(ctx: ToolContext, message_ids: list[str], account: str | None = None) -> dict:
    """Mark messages as read.

    Args:
        message_ids: Messages to mark.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    return {**await ad.modify(_ids(message_ids), read=True), "account": ad.label}


@group.tool(risk=Risk.MEDIUM, scope="write")
async def mark_unread(ctx: ToolContext, message_ids: list[str], account: str | None = None) -> dict:
    """Mark messages as unread.

    Args:
        message_ids: Messages to mark.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    return {**await ad.modify(_ids(message_ids), read=False), "account": ad.label}


# --------------------------------------------------------------------------- model-assisted tools
def transcript(messages: list[dict], budget: int = 14000) -> str:
    if not messages:
        return ""
    per = max(600, budget // len(messages))
    parts = []
    for i, m in enumerate(messages, 1):
        head = f"[{i}] id={m.get('id')} | From: {m.get('from', '')} | To: {m.get('to', '')} | Date: {m.get('date', '')}"
        parts.append(f"{head}\nSubject: {m.get('subject', '')}\n{H.clip(m.get('body') or m.get('snippet') or '', per)}")
    return H.clip("\n\n".join(parts), budget + 2000)


async def _collect(ad: MailAdapter, thread_id: str | None, message_ids: list[str] | None, query: str | None,
                   limit: int) -> list[dict]:
    if thread_id:
        return (await ad.thread(thread_id, max_chars=4000, limit=limit))["messages"]
    ids = H.as_list(message_ids)[:limit]
    if not ids and query:
        ids = [m["id"] for m in await ad.search(query=query, limit=limit)]
    if not ids:
        raise ToolError("Pass a thread_id, message_ids or a search query.")
    return await H.gather_limited([ad.get(i, max_chars=4000) for i in ids], 4)


@group.tool(risk=Risk.LOW, scope="read")
async def summarize_thread(ctx: ToolContext, thread_id: str, focus: str | None = None,
                           account: str | None = None) -> dict:
    """Summarize an email conversation: what it's about, positions, decisions, open questions and next steps.

    Args:
        thread_id: Thread id (or a message id) to summarize.
        focus: Optional angle, e.g. "pricing concerns" or "what does the customer want from us".
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    th = await ad.thread(thread_id, max_chars=4000, limit=40)
    if not th["messages"]:
        raise ToolError("That thread has no messages.")
    system = ("You summarize email threads for a busy professional. Be factual and concise; use short markdown "
              "sections: Summary, Key points, Decisions, Open questions, Next steps (with owners when stated). "
              "Never invent facts not in the emails.")
    prompt = (f"Focus: {focus}\n\n" if focus else "") + f"Thread '{th.get('subject')}':\n\n" + transcript(th["messages"])
    summary = await llm_summary(ctx, system, prompt)
    return {"thread_id": th["thread_id"], "subject": th.get("subject"), "messages": th["message_count"],
            "participants": sorted({m.get("from", "") for m in th["messages"] if m.get("from")})[:20],
            "summary": summary}


async def llm_summary(ctx: ToolContext, system: str, prompt: str) -> str:
    return await H.llm_complete(ctx, system, prompt, max_tokens=900)


ACTION_SCHEMA = {
    "type": "object",
    "properties": {"action_items": {"type": "array", "items": {"type": "object", "properties": {
        "task": {"type": "string"}, "owner": {"type": "string"}, "due": {"type": "string"},
        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "source_message_id": {"type": "string"}}, "required": ["task"]}}},
    "required": ["action_items"],
}
DEADLINE_SCHEMA = {
    "type": "object",
    "properties": {"deadlines": {"type": "array", "items": {"type": "object", "properties": {
        "what": {"type": "string"}, "due_date": {"type": "string", "description": "YYYY-MM-DD when determinable"},
        "due_text": {"type": "string"}, "owner": {"type": "string"}, "source_message_id": {"type": "string"}},
        "required": ["what"]}}},
    "required": ["deadlines"],
}


@group.tool(risk=Risk.LOW, scope="read")
async def extract_action_items(ctx: ToolContext, thread_id: str | None = None, message_ids: list[str] | None = None,
                               query: str | None = None, limit: int = 10, account: str | None = None) -> dict:
    """Extract action items (task, owner, due date, priority) from a thread, specific messages or search results.

    Args:
        thread_id: Thread to analyse.
        message_ids: Specific messages to analyse (alternative to thread_id).
        query: Search query whose results to analyse (alternative), e.g. "is:unread newer_than:3d".
        limit: Maximum messages to read.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    msgs = await _collect(ad, thread_id, message_ids, query, _clamp(limit, 1, 30))
    me = await ad.me()
    data = await H.llm_structured(ctx, (
        "Extract concrete action items from these emails. Each item: task (imperative), owner (name/email, or 'me' "
        f"for the mailbox owner {me}), due (as stated), priority, source_message_id. Only real requests or "
        "commitments; no duplicates."), transcript(msgs), ACTION_SCHEMA)
    items = (data or {}).get("action_items") if isinstance(data, dict) else None
    return {"messages_analyzed": len(msgs), "action_items": items or []}


@group.tool(risk=Risk.LOW, scope="read")
async def extract_deadlines(ctx: ToolContext, thread_id: str | None = None, message_ids: list[str] | None = None,
                            query: str | None = None, limit: int = 10, account: str | None = None) -> dict:
    """Find deadlines and due dates mentioned in a thread, specific messages or search results.

    Args:
        thread_id: Thread to analyse.
        message_ids: Specific messages to analyse (alternative to thread_id).
        query: Search query whose results to analyse (alternative).
        limit: Maximum messages to read.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    msgs = await _collect(ad, thread_id, message_ids, query, _clamp(limit, 1, 30))
    today = datetime.now(timezone.utc).date().isoformat()
    data = await H.llm_structured(ctx, (
        f"Today is {today}. List every deadline or due date in these emails: what is due, due_date as YYYY-MM-DD "
        "when it can be resolved (resolve relative dates against each email's date), the original wording in "
        "due_text, owner if stated, and source_message_id."), transcript(msgs), DEADLINE_SCHEMA)
    items = (data or {}).get("deadlines") if isinstance(data, dict) else None
    items = sorted(items or [], key=lambda d: d.get("due_date") or "9999")
    return {"messages_analyzed": len(msgs), "deadlines": items}


FOLLOWUP_SCHEMA = {
    "type": "object",
    "properties": {"needs_reply": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["id"]}}},
    "required": ["needs_reply"],
}


@group.tool(risk=Risk.LOW, scope="read")
async def find_followups(ctx: ToolContext, days: int = 7, min_age_hours: int = 48, limit: int = 30,
                         account: str | None = None) -> dict:
    """Find conversations needing follow-up: emails you sent that got no reply, and recent emails that still
    need your answer.

    Args:
        days: How far back to look.
        min_age_hours: Only flag your sent emails older than this as "awaiting reply".
        limit: Maximum messages to scan per folder.
        account: Connector name or kind when several mailboxes are connected.
    """
    ad = await mail_adapter(ctx, account)
    since = datetime.now(timezone.utc) - timedelta(days=_clamp(days, 1, 60))
    lim = _clamp(limit, 5, 50)
    sent = await ad.search(folder="sent", since=since, limit=lim)
    inbox = await ad.search(folder="inbox", since=since, limit=lim * 2)
    me = (await ad.me() or "").lower()
    now = datetime.now(timezone.utc)

    def key(m: dict) -> str:
        return (m.get("thread_id") if ad.kind != "imap_smtp" else None) or norm_subject(m.get("subject")).lower()

    latest_in: dict[str, datetime] = {}
    for m in inbox:
        if me and me in (m.get("from") or "").lower():
            continue
        d = H.parse_datetime(m.get("date"))
        if d and (key(m) not in latest_in or d > latest_in[key(m)]):
            latest_in[key(m)] = d
    awaiting, seen = [], set()
    for m in sent:
        k, d = key(m), H.parse_datetime(m.get("date"))
        if not d or k in seen:
            continue
        seen.add(k)
        if (now - d) < timedelta(hours=min_age_hours):
            continue
        if k in latest_in and latest_in[k] > d:
            continue
        awaiting.append({"id": m.get("id"), "thread_id": m.get("thread_id"), "to": m.get("to"),
                         "subject": m.get("subject"), "sent": m.get("date"), "days_waiting": (now - d).days})
    unread = [m for m in inbox if m.get("unread")][:25]
    needs: list[dict] = []
    if unread:
        listing = "\n".join(f"id={m['id']} | From: {m.get('from')} | Subject: {m.get('subject')} | "
                            f"{H.clip(m.get('snippet'), 200)}" for m in unread)
        data = await H.llm_structured(ctx, (
            "Which of these unread emails need a personal reply from the mailbox owner (questions, requests, "
            "approvals)? Ignore newsletters, notifications, receipts and FYIs. Return their ids with a short "
            "reason."), listing, FOLLOWUP_SCHEMA)
        valid = {m["id"]: m for m in unread}
        for item in (data or {}).get("needs_reply") or [] if isinstance(data, dict) else []:
            m = valid.get(str(item.get("id")))
            if m:
                needs.append({"id": m["id"], "from": m.get("from"), "subject": m.get("subject"),
                              "received": m.get("date"), "reason": item.get("reason")})
    return {"account": ad.label, "window_days": days, "awaiting_reply": awaiting[:lim], "needs_your_reply": needs}


# =========================================================================== connector kinds
def google_oauth_fields(scopes_hint: str) -> list[ConnectorField]:
    return [
        ConnectorField("client_id", "OAuth client ID", placeholder="1234567890-abc.apps.googleusercontent.com",
                       help="Google Cloud Console → APIs & Services → Credentials → OAuth client ID. Required for the "
                            "refresh-token method."),
        ConnectorField("client_secret", "OAuth client secret", type="password", secret=True),
        ConnectorField("refresh_token", "Refresh token", type="password", secret=True,
                       help=f"From the OAuth consent flow with offline access and scope {scopes_hint} (e.g. via the "
                            "OAuth 2.0 Playground using your own client ID)."),
        ConnectorField("service_account_json", "Service account key JSON (Workspace alternative)", type="textarea",
                       secret=True, help="Instead of a refresh token: a service account with domain-wide delegation "
                                         "for the scope above. Also set 'Act as user'."),
        ConnectorField("delegated_user", "Act as user (domain-wide delegation)", placeholder="you@company.com"),
    ]


def microsoft_fields() -> list[ConnectorField]:
    return [
        ConnectorField("tenant_id", "Directory (tenant) ID", placeholder="contoso.onmicrosoft.com or GUID",
                       help="Use 'common' (or leave empty) for delegated access with personal or multi-tenant accounts."),
        ConnectorField("client_id", "Application (client) ID", required=True,
                       placeholder="00000000-0000-0000-0000-000000000000"),
        ConnectorField("client_secret", "Client secret", type="password", secret=True,
                       help="Certificates & secrets → New client secret (value)."),
        ConnectorField("refresh_token", "Refresh token (delegated access)", type="password", secret=True,
                       help="From an authorization-code sign-in with scopes: offline_access User.Read Mail.ReadWrite "
                            "Mail.Send Calendars.ReadWrite Files.ReadWrite.All. Leave empty for app-only access."),
        ConnectorField("user", "Mailbox / user (app-only access)", placeholder="jane@contoso.com",
                       help="With application permissions (admin consent) instead of a refresh token: the user whose "
                            "mail, calendar and OneDrive the tools act on."),
        ConnectorField("drive_id", "SharePoint drive ID (optional)",
                       help="Use a SharePoint document library instead of the user's OneDrive."),
        ConnectorField("timezone", "Default time zone (optional)", placeholder="Europe/Berlin"),
    ]


@H.connector_test
async def _test_gmail(cfg: dict) -> tuple[bool, str]:
    api = google_client(cfg, GMAIL_BASE, service="Gmail", scopes=GMAIL_SCOPES)
    prof = await api.get("/profile") or {}
    return True, f"Connected to Gmail as {prof.get('emailAddress', 'unknown')} ({prof.get('messagesTotal', '?')} messages)."


@H.connector_test
async def _test_microsoft365(cfg: dict) -> tuple[bool, str]:
    await microsoft_token(cfg)
    api = graph_client(cfg)
    try:
        me = await api.get("", params={"$select": "displayName,mail,userPrincipalName"}) or {}
        who = me.get("mail") or me.get("userPrincipalName") or me.get("displayName") or "unknown"
        return True, f"Connected to Microsoft 365 as {who}."
    except H.ApiError as e:
        if e.status != 403:
            raise
        await api.get("/mailFolders/inbox", params={"$select": "displayName"})
        return True, f"Connected to Microsoft 365 ({graph_user_base(cfg).rsplit('/', 1)[-1]}); mailbox access OK."


@H.connector_test
async def _test_imap_smtp(cfg: dict) -> tuple[bool, str]:
    ad = ImapSmtpAdapter(ToolContext(org_id="_test", user_id=None), cfg)

    def check() -> str:
        def op(conn):
            typ, data = conn.select("INBOX", readonly=True)
            return (data or [b"?"])[0].decode(errors="replace")

        count = ad._with_imap(op)
        host = str(cfg.get("smtp_host") or "").strip()
        if host:
            mode, port = ad._mode(ad._port("smtp_port"), 465, 587)
            tls = ssl.create_default_context()
            server = (smtplib.SMTP_SSL(host, port, context=tls, timeout=20) if mode == "ssl"
                      else smtplib.SMTP(host, port, timeout=20))
            try:
                if mode == "starttls":
                    server.ehlo()
                    server.starttls(context=tls)
                server.ehlo()
                if cfg.get("username"):
                    server.login(str(cfg["username"]), str(cfg.get("password") or ""))
            finally:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001
                    pass
        return count

    try:
        count = await asyncio.to_thread(check)
    except smtplib.SMTPAuthenticationError:
        return False, "IMAP login OK, but SMTP login failed — check the password (app password?)."
    except (smtplib.SMTPException, OSError) as e:
        return False, f"IMAP login OK, but the SMTP server is unreachable ({type(e).__name__})."
    return True, f"IMAP and SMTP login OK ({count} messages in INBOX)."


register_connector_kind(ConnectorKindSpec(
    kind="gmail", name="Gmail", description="Read, search, label and send Gmail / Google Workspace mail.",
    icon="mail", category="productivity", tool_groups=["email", "notify"],
    fields=google_oauth_fields("https://www.googleapis.com/auth/gmail.modify") + [
        ConnectorField("from_address", "Send as (optional)", placeholder="Jane Doe <jane@company.com>",
                       help="A verified send-as alias; defaults to the account address."),
    ],
    docs_url="https://developers.google.com/gmail/api/auth/scopes", test=_test_gmail,
))

register_connector_kind(ConnectorKindSpec(
    kind="microsoft365", name="Microsoft 365",
    description="Outlook mail, calendar and OneDrive / SharePoint files via Microsoft Graph.",
    icon="mail", category="productivity", tool_groups=["email", "calendar", "drive", "notify"],
    fields=microsoft_fields(),
    docs_url="https://learn.microsoft.com/graph/auth-v2-user", test=_test_microsoft365,
))

register_connector_kind(ConnectorKindSpec(
    kind="imap_smtp", name="IMAP / SMTP email",
    description="Any mailbox that supports IMAP (reading) and SMTP (sending), over TLS.",
    icon="mail", category="productivity", tool_groups=["email", "notify"],
    fields=[
        ConnectorField("imap_host", "IMAP host", required=True, placeholder="imap.example.com"),
        ConnectorField("imap_port", "IMAP port", type="number", placeholder="993"),
        ConnectorField("smtp_host", "SMTP host", required=True, placeholder="smtp.example.com"),
        ConnectorField("smtp_port", "SMTP port", type="number", placeholder="465 (SSL) or 587 (STARTTLS)"),
        ConnectorField("username", "Username", required=True, placeholder="jane@example.com"),
        ConnectorField("password", "Password", type="password", required=True, secret=True,
                       help="Gmail, Outlook.com, iCloud and Yahoo require an app password."),
        ConnectorField("from_address", "From address", placeholder="Jane Doe <jane@example.com>",
                       help="Defaults to the username when it's an email address."),
        ConnectorField("use_ssl", "Encryption", type="select", options=["ssl", "starttls"],
                       help="ssl = implicit TLS (ports 993/465); starttls = upgrade a plain connection (143/587)."),
        ConnectorField("save_to_sent", "Save sent mail to the Sent folder", type="select", options=["no", "yes"],
                       help="Enable if your provider doesn't store SMTP-sent mail automatically."),
    ],
    docs_url="https://datatracker.ietf.org/doc/html/rfc9051", test=_test_imap_smtp,
))
