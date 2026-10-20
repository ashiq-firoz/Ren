"""Team chat tools: Slack (Web API, bot token + optional user token for search) and Microsoft Teams (Graph channel
messages, or an incoming-webhook URL for posting only). Summaries, decisions, action items and mentions are
extracted from channel history with the org's model.
"""
from __future__ import annotations

import base64
import html as htmllib
import json
import mimetypes
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin import _http as H
from app.tools.builtin._oauth import graph_client, microsoft_token
from app.tools.connectors import ConnectorField, ConnectorKindSpec, register_connector_kind

CHAT_KINDS = ("slack", "teams")
SLACK_API = "https://slack.com/api"
UTC = timezone.utc
_SLACK_ERRORS = {
    "not_authed": None, "invalid_auth": None, "token_revoked": None, "token_expired": None, "account_inactive": None,
    "channel_not_found": "Channel not found — check the name/ID; private channels need the bot invited.",
    "not_in_channel": "The bot isn't a member of that channel — invite it with /invite @YourBot.",
    "is_archived": "That channel is archived.",
    "msg_too_long": "The message is too long for Slack.",
    "no_text": "The message is empty.",
    "ratelimited": "Slack rate limit reached — try again in a minute.",
    "name_taken": "A channel with that name already exists.",
    "invalid_name": "Invalid channel name (lowercase letters, numbers, hyphens and underscores; max 80 chars).",
    "invalid_name_specials": "Invalid channel name (lowercase letters, numbers, hyphens and underscores only).",
    "not_allowed_token_type": "This Slack action needs a user token (xoxp-…) — add one in the Slack connector.",
    "users_not_found": "No Slack user found with that email.",
    "user_not_found": "Slack user not found.",
    "message_not_found": "Message not found.",
    "thread_not_found": "Thread not found.",
    "file_not_found": "File not found.",
    "cant_invite_self": "The bot can't invite itself.",
    "restricted_action": "The workspace's settings don't allow this action for the app.",
}
TEAMS_REACTIONS = {"like": "👍", "thumbsup": "👍", "+1": "👍", "heart": "❤️", "laugh": "😆", "joy": "😂",
                   "surprised": "😮", "sad": "😢", "angry": "😡", "white_check_mark": "✅", "check": "✅",
                   "eyes": "👀", "tada": "🎉", "pray": "🙏", "fire": "🔥", "rocket": "🚀"}


def _ts_iso(ts: Any) -> str | None:
    try:
        return H.iso(datetime.fromtimestamp(float(ts), tz=UTC))
    except (TypeError, ValueError):
        return None


def _md_to_html(text: str) -> str:
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark", {"html": False}).render(text or "")


class ChatAdapter:
    kind = ""
    label = ""

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        self.ctx = ctx
        self.cfg = cfg

    def default_channel(self) -> str | None:
        return str(self.cfg.get("default_channel") or "").strip() or None


# =========================================================================== Slack
class SlackAdapter(ChatAdapter):
    kind = "slack"
    label = "Slack"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        self.bot = str(cfg.get("bot_token") or "").strip()
        self.user_token = str(cfg.get("user_token") or "").strip()
        if not self.bot and not self.user_token:
            raise ToolError("The Slack connector has no token — add the bot token in Connectors.")
        self._users: dict[str, str] = {}
        self._channels: dict[str, str] | None = None
        self._channel_names: dict[str, str] = {}

    # --- transport ----------------------------------------------------------------------------
    async def call(self, method: str, *, params: dict | None = None, json_body: dict | None = None,
                   form: dict | None = None, user: bool = False) -> dict:
        token = self.user_token if (user and self.user_token) else (self.bot or self.user_token)
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{SLACK_API}/{method}"
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = await H.api("POST", url, service="Slack", headers=headers,
                               content=json.dumps(json_body).encode("utf-8"))
        elif form is not None:
            data = await H.api("POST", url, service="Slack", headers=headers, data=H.clean_params(form))
        else:
            data = await H.api("GET", url, service="Slack", headers=headers, params=params)
        if not isinstance(data, dict):
            raise ToolError("Unexpected response from Slack.")
        if not data.get("ok"):
            err = str(data.get("error") or "unknown_error")
            if err == "already_reacted":
                return data
            if err in _SLACK_ERRORS and _SLACK_ERRORS[err] is None:
                raise ToolError(f"Slack credentials are invalid or expired ({err}) — reconnect Slack in Connectors.")
            if err == "missing_scope":
                raise ToolError(f"The Slack app is missing a permission scope (needed: {data.get('needed', '?')}). "
                                "Add it in the Slack app settings, reinstall, and update the token.")
            raise ToolError(_SLACK_ERRORS.get(err) or f"Slack error: {err}")
        return data

    # --- lookups ------------------------------------------------------------------------------
    async def _load_channels(self) -> dict[str, str]:
        if self._channels is None:
            self._channels = {}
            cursor = None
            for _ in range(5):
                data = await self.call("conversations.list", params={
                    "types": "public_channel,private_channel", "exclude_archived": True, "limit": 1000,
                    "cursor": cursor})
                for c in data.get("channels", []):
                    self._channels[c["name"].lower()] = c["id"]
                    self._channel_names[c["id"]] = c["name"]
                cursor = (data.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        return self._channels

    async def channel_id(self, ref: str, *, for_history: bool = False) -> str:
        r = (ref or "").strip()
        if not r:
            raise ToolError("Pass a Slack channel (e.g. #general or C0123456).")
        m = re.fullmatch(r"<#([CGD][A-Z0-9]+)(?:\|[^>]*)?>", r)
        if m:
            return m.group(1)
        if re.fullmatch(r"[CGD][A-Z0-9]{6,}", r):
            return r
        if r.startswith("@") or "@" in r or re.fullmatch(r"[UW][A-Z0-9]{6,}", r):
            uid = await self.resolve_user(r.lstrip("@"))
            if not for_history:
                return uid  # posting to a user id lands in the app's DM with them
            data = await self.call("conversations.open", json_body={"users": uid})
            return data["channel"]["id"]
        name = r.lstrip("#").lower()
        channels = await self._load_channels()
        if name in channels:
            return channels[name]
        raise ToolError(f"Slack channel '#{name}' not found (the bot only sees public channels and private channels "
                        "it was invited to).")

    async def channel_name(self, cid: str) -> str:
        if cid not in self._channel_names:
            try:
                info = await self.call("conversations.info", params={"channel": cid})
                self._channel_names[cid] = (info.get("channel") or {}).get("name") or cid
            except ToolError:
                self._channel_names[cid] = cid
        return self._channel_names[cid]

    async def user_name(self, uid: str | None) -> str:
        if not uid:
            return ""
        if uid not in self._users:
            if len(self._users) > 60:
                return uid
            try:
                u = (await self.call("users.info", params={"user": uid})).get("user") or {}
                prof = u.get("profile") or {}
                self._users[uid] = prof.get("display_name") or prof.get("real_name") or u.get("name") or uid
            except ToolError:
                self._users[uid] = uid
        return self._users[uid]

    async def resolve_user(self, ref: str) -> str:
        r = ref.strip()
        if re.fullmatch(r"[UW][A-Z0-9]{6,}", r):
            return r
        if r.lower() == "me":
            return (await self.me())["id"]
        if "@" in r:
            data = await self.call("users.lookupByEmail", params={"email": r})
            return data["user"]["id"]
        cursor = None
        low = r.lower()
        for _ in range(5):
            data = await self.call("users.list", params={"limit": 500, "cursor": cursor})
            for u in data.get("members", []):
                prof = u.get("profile") or {}
                if low in {str(u.get("name", "")).lower(), str(prof.get("display_name", "")).lower(),
                           str(prof.get("real_name", "")).lower()}:
                    return u["id"]
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        raise ToolError(f"No Slack user matching '{ref}'.")

    async def me(self) -> dict:
        data = await self.call("auth.test", user=bool(self.user_token))
        return {"id": data.get("user_id"), "name": data.get("user"), "team": data.get("team")}

    async def fmt(self, text: str) -> str:
        uids = list(dict.fromkeys(re.findall(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", text or "")))[:20]
        names = {u: await self.user_name(u) for u in uids}
        text = re.sub(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>", lambda m: "@" + (m.group(2) or names.get(m.group(1), m.group(1))),
                      text or "")
        text = re.sub(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>", lambda m: "#" + (m.group(2) or m.group(1)), text)
        text = re.sub(r"<!(here|channel|everyone)[^>]*>", r"@\1", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
        text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
        return htmllib.unescape(text)

    async def norm(self, m: dict, channel: str | None = None, max_chars: int = 1500) -> dict:
        user = await self.user_name(m.get("user")) if m.get("user") else (
            m.get("username") or (m.get("bot_profile") or {}).get("name"))
        ch = channel or ((m.get("channel") or {}).get("id") if isinstance(m.get("channel"), dict) else m.get("channel"))
        return H.compact({
            "id": m.get("ts"), "channel": ch, "user": user, "text": H.clip(await self.fmt(m.get("text") or ""), max_chars),
            "time": _ts_iso(m.get("ts")),
            "thread_id": m.get("thread_ts") if m.get("thread_ts") and m.get("thread_ts") != m.get("ts") else None,
            "replies": m.get("reply_count") or None, "files": [f.get("name") for f in m.get("files") or []] or None,
            "permalink": m.get("permalink"),
        })

    # --- operations ---------------------------------------------------------------------------
    async def history(self, channel: str, since: datetime, limit: int) -> list[dict]:
        cid = await self.channel_id(channel, for_history=True)
        out: list[dict] = []
        cursor = None
        while len(out) < limit:
            data = await self.call("conversations.history", params={
                "channel": cid, "oldest": f"{since.timestamp():.6f}", "limit": min(200, limit - len(out)),
                "cursor": cursor})
            out += [m for m in data.get("messages", []) if m.get("type") == "message"]
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor or not data.get("has_more"):
                break
        out.sort(key=lambda m: float(m.get("ts") or 0))
        return [await self.norm(m, cid, 1200) for m in out[-limit:]]

    async def search(self, query: str, channel: str | None, limit: int) -> dict:
        if self.user_token:
            q = query + (f" in:#{channel.lstrip('#')}" if channel and not re.fullmatch(r"[CGD][A-Z0-9]{6,}", channel)
                         else "")
            data = await self.call("search.messages", params={"query": q, "count": limit, "sort": "timestamp"},
                                   user=True)
            matches = (data.get("messages") or {}).get("matches") or []
            rows = []
            for m in matches[:limit]:
                row = await self.norm(m)
                row["channel"] = "#" + ((m.get("channel") or {}).get("name") or row.get("channel") or "")
                rows.append(row)
            return {"messages": rows, "total": (data.get("messages") or {}).get("total")}
        target = channel or self.default_channel()
        if not target:
            raise ToolError("Slack full-text search needs a user token (xoxp-…, scope search:read) in the connector. "
                            "Without one, pass `channel` to scan that channel's recent history.")
        msgs = await self.history(target, datetime.now(UTC) - timedelta(days=90), 1000)
        terms = [t.lower() for t in query.split() if t]
        hits = [m for m in msgs if all(t in (m.get("text") or "").lower() for t in terms)]
        return {"messages": hits[-limit:][::-1], "note": f"Scanned the last 90 days of {target} (no user token for "
                                                          "workspace-wide search)."}

    async def thread(self, channel: str, thread_id: str, limit: int) -> dict:
        cid = await self.channel_id(channel, for_history=True)
        data = await self.call("conversations.replies", params={"channel": cid, "ts": thread_id, "limit": limit})
        msgs = [await self.norm(m, cid) for m in data.get("messages", [])[:limit]]
        return {"channel": cid, "thread_id": thread_id, "count": len(msgs), "messages": msgs}

    async def channels(self, query: str | None, limit: int) -> list[dict]:
        rows: list[dict] = []
        cursor = None
        for _ in range(5):
            data = await self.call("conversations.list", params={
                "types": "public_channel,private_channel", "exclude_archived": True, "limit": 1000, "cursor": cursor})
            for c in data.get("channels", []):
                rows.append(H.compact({"id": c["id"], "name": "#" + c["name"], "private": c.get("is_private") or None,
                                       "members": c.get("num_members"), "is_member": c.get("is_member"),
                                       "topic": H.clip((c.get("topic") or {}).get("value"), 160),
                                       "purpose": H.clip((c.get("purpose") or {}).get("value"), 160)}))
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        if query:
            q = query.lower().lstrip("#")
            rows = [r for r in rows if q in r["name"].lower() or q in (r.get("topic") or "").lower()
                    or q in (r.get("purpose") or "").lower()]
        rows.sort(key=lambda r: -(r.get("members") or 0))
        return rows[:limit]

    async def members(self, channel: str, limit: int) -> list[dict]:
        cid = await self.channel_id(channel, for_history=True)
        data = await self.call("conversations.members", params={"channel": cid, "limit": min(limit, 1000)})
        return [{"id": uid, "name": await self.user_name(uid)} for uid in data.get("members", [])[:limit]]

    async def send(self, channel: str, text: str, *, thread_id: str | None = None, broadcast: bool = False) -> dict:
        cid = await self.channel_id(channel)
        body: dict[str, Any] = {"channel": cid, "text": text, "mrkdwn": True, "unfurl_links": False}
        if thread_id:
            body["thread_ts"] = thread_id
            if broadcast:
                body["reply_broadcast"] = True
        data = await self.call("chat.postMessage", json_body=body)
        out = {"sent": True, "channel": data.get("channel"), "id": data.get("ts"), "thread_id": thread_id}
        try:
            link = await self.call("chat.getPermalink", params={"channel": data.get("channel"), "message_ts": data.get("ts")})
            out["permalink"] = link.get("permalink")
        except ToolError:
            pass
        return H.compact(out)

    async def react(self, channel: str, message_id: str, emoji: str) -> dict:
        cid = await self.channel_id(channel, for_history=True)
        name = emoji.strip().strip(":")
        data = await self.call("reactions.add", json_body={"channel": cid, "timestamp": message_id, "name": name})
        return {"reacted": True, "emoji": f":{name}:", "already": data.get("error") == "already_reacted" or None}

    async def create_channel(self, name: str, private: bool, members: list[str], description: str | None) -> dict:
        slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower().lstrip("#")).strip("-")[:80]
        if not slug:
            raise ToolError("Invalid channel name.")
        data = await self.call("conversations.create", json_body={"name": slug, "is_private": private})
        cid = data["channel"]["id"]
        if description:
            await self.call("conversations.setPurpose", json_body={"channel": cid, "purpose": description[:250]})
        invited = []
        if members:
            uids = [await self.resolve_user(m) for m in members]
            await self.call("conversations.invite", json_body={"channel": cid, "users": ",".join(uids)})
            invited = members
        return H.compact({"created": True, "id": cid, "name": "#" + slug, "private": private, "invited": invited})

    async def download(self, file_ref: str) -> tuple[bytes, str, str]:
        ref = file_ref.strip()
        if ref.startswith("http"):
            host = (urlparse(ref).hostname or "").lower()
            if not (host == "slack.com" or host.endswith(".slack.com")):
                raise ToolError("Only Slack file URLs (files.slack.com) can be downloaded with the Slack token.")
            url, name, mime = ref, ref.rstrip("/").rsplit("/", 1)[-1], None
        else:
            info = (await self.call("files.info", params={"file": ref})).get("file") or {}
            url = info.get("url_private_download") or info.get("url_private")
            name, mime = info.get("name") or ref, info.get("mimetype")
            if not url:
                raise ToolError("That Slack file has no downloadable content (it may be external or a post).")
        data, headers = await H.fetch_bytes(url, service="Slack", headers={"Authorization": f"Bearer {self.bot or self.user_token}"})
        if "text/html" in headers.get("content-type", "") and not (name or "").endswith((".html", ".htm")):
            raise ToolError("Slack returned a login page instead of the file — the bot needs the files:read scope.")
        return data, name, mime or mimetypes.guess_type(name)[0] or "application/octet-stream"

    async def upload(self, channel: str, data: bytes, name: str, mime: str, comment: str | None,
                     thread_id: str | None) -> dict:
        cid = await self.channel_id(channel, for_history=True)
        ticket = await self.call("files.getUploadURLExternal", form={"filename": name, "length": str(len(data))})
        await H.api("POST", ticket["upload_url"], service="Slack", content=data,
                    headers={"Content-Type": "application/octet-stream"})
        form = {"files": json.dumps([{"id": ticket["file_id"], "title": name}]), "channel_id": cid,
                "initial_comment": comment or None, "thread_ts": thread_id or None}
        done = await self.call("files.completeUploadExternal", form=form)
        files = done.get("files") or [{}]
        return H.compact({"uploaded": True, "file_id": ticket["file_id"], "channel": cid, "name": name,
                          "permalink": files[0].get("permalink")})

    async def mentions(self, user: str | None, since: datetime, channel: str | None, limit: int) -> dict:
        if user:
            uid = await self.resolve_user(user)
        elif self.user_token:
            uid = (await self.me())["id"]
        else:
            raise ToolError("Pass `user` (email, name or Slack user id) — with only a bot token, 'me' is the bot.")
        if channel:
            channels = [channel]
        else:
            data = await self.call("users.conversations", params={"types": "public_channel,private_channel",
                                                                  "exclude_archived": True, "limit": 200})
            channels = [c["id"] for c in data.get("channels", [])][:15]
        token = f"@{await self.user_name(uid)}"
        hits: list[dict] = []
        for ch in channels:
            try:
                msgs = await self.history(ch, since, 400)
            except ToolError:
                continue
            for m in msgs:
                if token.lower() in (m.get("text") or "").lower():
                    cid = m.get("channel") or ch
                    m["channel"] = "#" + await self.channel_name(cid) if cid[:1] in ("C", "G") else cid
                    hits.append(m)
        hits.sort(key=lambda m: m.get("time") or "", reverse=True)
        return {"user": token, "channels_scanned": len(channels), "mentions": hits[:limit]}


# =========================================================================== Microsoft Teams
def _teams_text(m: dict, max_chars: int = 1500) -> str:
    body = m.get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        content = H.html_to_text(content)
    return H.clip(content.strip(), max_chars)


class TeamsAdapter(ChatAdapter):
    kind = "teams"
    label = "Microsoft Teams"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any], graph_cfg: dict[str, Any] | None):
        super().__init__(ctx, cfg)
        self.webhook = str(cfg.get("webhook_url") or "").strip()
        self.graph_cfg = graph_cfg
        self.api = graph_client(graph_cfg, service="Microsoft Teams", reconnect="Microsoft Teams",
                                user_scoped=False) if graph_cfg else None
        self._teams: list[dict] | None = None

    def _graph(self) -> H.RestClient:
        if self.api is None:
            raise ToolError("Reading Teams needs Microsoft Graph credentials (tenant, client id/secret and a refresh "
                            "token) in the Teams connector — the webhook URL can only post messages.")
        return self.api

    async def _joined(self) -> list[dict]:
        if self._teams is None:
            data = await self._graph().get("/me/joinedTeams", params={"$select": "id,displayName"}) or {}
            self._teams = data.get("value") or []
        return self._teams

    async def resolve(self, ref: str | None) -> tuple[str, str]:
        r = (ref or self.default_channel() or "").strip()
        if not r:
            raise ToolError("Pass a Teams channel as 'Team name / Channel name' (or 'teamId/channelId').")
        team_part, sep, chan_part = r.partition("/") if "/" in r else r.partition(">")
        if not sep:
            team_part, chan_part = str(self.cfg.get("default_team") or ""), r
        team_part, chan_part = team_part.strip(), chan_part.strip()
        if not team_part:
            raise ToolError("Which team? Use 'Team name / Channel name' or set a default team in the Teams connector.")
        team_id = team_part
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", team_part):
            match = [t for t in await self._joined() if (t.get("displayName") or "").lower() == team_part.lower()]
            if not match:
                raise ToolError(f"Team '{team_part}' not found among your joined teams.")
            team_id = match[0]["id"]
        if chan_part.startswith("19:"):
            return team_id, chan_part
        data = await self._graph().get(f"/teams/{quote(team_id, safe='')}/channels",
                                       params={"$select": "id,displayName"}) or {}
        for c in data.get("value") or []:
            if (c.get("displayName") or "").lower() == chan_part.lstrip("#").lower():
                return team_id, c["id"]
        raise ToolError(f"Channel '{chan_part}' not found in that team.")

    @staticmethod
    def _path(team_id: str, channel_id: str) -> str:
        return f"/teams/{quote(team_id, safe='')}/channels/{quote(channel_id, safe='')}"

    @staticmethod
    def norm(m: dict, channel: str | None = None) -> dict:
        frm = m.get("from") or {}
        who = (frm.get("user") or {}).get("displayName") or (frm.get("application") or {}).get("displayName")
        return H.compact({"id": m.get("id"), "channel": channel, "user": who, "subject": m.get("subject"),
                          "text": _teams_text(m), "time": m.get("createdDateTime"),
                          "thread_id": m.get("replyToId") or None, "url": m.get("webUrl"),
                          "files": [a.get("name") for a in m.get("attachments") or [] if a.get("contentType") == "reference"] or None})

    async def history(self, channel: str, since: datetime, limit: int) -> list[dict]:
        tid, cid = await self.resolve(channel)
        out: list[dict] = []
        url: str | None = f"{self._path(tid, cid)}/messages"
        params: dict | None = {"$top": 50}
        while url and len(out) < limit:
            data = await self._graph().get(url, params=params) or {}
            params = None
            stop = False
            for m in data.get("value") or []:
                when = H.parse_datetime(m.get("createdDateTime"))
                if when and when < since:
                    stop = True
                    continue
                if m.get("messageType", "message") == "message" and not m.get("deletedDateTime"):
                    out.append(m)
            url = None if stop else data.get("@odata.nextLink")
        out.sort(key=lambda m: m.get("createdDateTime") or "")
        return [self.norm(m, channel) for m in out[-limit:]]

    async def search(self, query: str, channel: str | None, limit: int) -> dict:
        data = await self._graph().post("/search/query", json={"requests": [{
            "entityTypes": ["chatMessage"], "query": {"queryString": query}, "from": 0, "size": min(limit, 25)}]}) or {}
        rows = []
        for container in ((data.get("value") or [{}])[0].get("hitsContainers") or []):
            for hit in container.get("hits") or []:
                res = hit.get("resource") or {}
                rows.append(H.compact({"id": res.get("id"), "user": ((res.get("from") or {}).get("emailAddress") or {}).get("name"),
                                       "text": H.clip(H.html_to_text(hit.get("summary") or "") or _teams_text(res), 800),
                                       "time": res.get("createdDateTime"),
                                       "channel": (res.get("channelIdentity") or {}).get("channelId")}))
        return {"messages": rows[:limit]}

    async def thread(self, channel: str, thread_id: str, limit: int) -> dict:
        tid, cid = await self.resolve(channel)
        root = await self._graph().get(f"{self._path(tid, cid)}/messages/{quote(thread_id, safe='')}")
        replies = await self._graph().get(f"{self._path(tid, cid)}/messages/{quote(thread_id, safe='')}/replies",
                                          params={"$top": min(limit, 50)}) or {}
        msgs = [self.norm(root, channel)] + [self.norm(m, channel) for m in sorted(
            replies.get("value") or [], key=lambda m: m.get("createdDateTime") or "")]
        return {"channel": channel, "thread_id": thread_id, "count": len(msgs), "messages": msgs[:limit]}

    async def channels(self, query: str | None, limit: int) -> list[dict]:
        rows = []
        for t in (await self._joined())[:25]:
            data = await self._graph().get(f"/teams/{quote(t['id'], safe='')}/channels",
                                           params={"$select": "id,displayName,description,membershipType"}) or {}
            for c in data.get("value") or []:
                rows.append(H.compact({"id": f"{t['id']}/{c['id']}", "name": f"{t.get('displayName')} / {c.get('displayName')}",
                                       "private": c.get("membershipType") == "private" or None,
                                       "description": H.clip(c.get("description"), 160)}))
        if query:
            rows = [r for r in rows if query.lower() in r["name"].lower()]
        return rows[:limit]

    async def members(self, channel: str, limit: int) -> list[dict]:
        tid, cid = await self.resolve(channel)
        data = await self._graph().get(f"{self._path(tid, cid)}/members") or {}
        return [H.compact({"id": m.get("userId"), "name": m.get("displayName"), "email": m.get("email"),
                           "roles": m.get("roles") or None}) for m in (data.get("value") or [])[:limit]]

    async def send(self, channel: str | None, text: str, *, thread_id: str | None = None, broadcast: bool = False) -> dict:
        if self.api is None or (not channel and not self.default_channel()):
            if not self.webhook:
                raise ToolError("Teams has neither Graph credentials nor a webhook URL configured.")
            if thread_id:
                raise ToolError("Replying in a thread needs Graph credentials (the webhook can only post new messages).")
            card = {"type": "message", "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
                "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                            "version": "1.4", "body": [{"type": "TextBlock", "text": text, "wrap": True}]}}]}
            await H.api("POST", self.webhook, service="Microsoft Teams webhook", json=card)
            return {"sent": True, "via": "webhook"}
        tid, cid = await self.resolve(channel)
        path = f"{self._path(tid, cid)}/messages" + (f"/{quote(thread_id, safe='')}/replies" if thread_id else "")
        m = await self._graph().post(path, json={"body": {"contentType": "html", "content": _md_to_html(text)}})
        return H.compact({"sent": True, "id": (m or {}).get("id"), "url": (m or {}).get("webUrl"), "thread_id": thread_id})

    async def react(self, channel: str, message_id: str, emoji: str) -> dict:
        tid, cid = await self.resolve(channel)
        name = emoji.strip().strip(":")
        reaction = TEAMS_REACTIONS.get(name.lower(), name)
        await self._graph().post(f"{self._path(tid, cid)}/messages/{quote(message_id, safe='')}/setReaction",
                                 json={"reactionType": reaction})
        return {"reacted": True, "emoji": reaction}

    async def create_channel(self, name: str, private: bool, members: list[str], description: str | None) -> dict:
        team_part, sep, chan = name.partition("/")
        if not sep:
            team_part, chan = str(self.cfg.get("default_team") or ""), name
        if not team_part.strip():
            raise ToolError("Name the team: 'Team name / New channel' (or set a default team in the connector).")
        tid = team_part.strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", tid):
            match = [t for t in await self._joined() if (t.get("displayName") or "").lower() == tid.lower()]
            if not match:
                raise ToolError(f"Team '{tid}' not found among your joined teams.")
            tid = match[0]["id"]
        body: dict[str, Any] = {"displayName": chan.strip()[:50], "description": description or "",
                                "membershipType": "private" if private else "standard"}
        c = await self._graph().post(f"/teams/{quote(tid, safe='')}/channels", json=body) or {}
        added = []
        for m in members:
            if "@" not in m:
                continue
            try:
                await self._graph().post(f"/teams/{quote(tid, safe='')}/channels/{quote(c['id'], safe='')}/members", json={
                    "@odata.type": "#microsoft.graph.aadUserConversationMember", "roles": [],
                    "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{m}')"})
                added.append(m)
            except ToolError:
                pass
        return H.compact({"created": True, "id": f"{tid}/{c.get('id')}", "name": c.get("displayName"),
                          "private": private, "invited": added, "url": c.get("webUrl")})

    async def download(self, file_ref: str) -> tuple[bytes, str, str]:
        ref = file_ref.strip()
        host = (urlparse(ref).hostname or "").lower()
        if not ref.startswith("https://") or not (host.endswith(".sharepoint.com") or host.endswith("onedrive.live.com")):
            raise ToolError("Pass the file's SharePoint/OneDrive URL (from the message attachment).")
        share = "u!" + base64.urlsafe_b64encode(ref.encode()).decode().rstrip("=")
        meta = await self._graph().get(f"/shares/{share}/driveItem", params={"$select": "name,file"}) or {}
        data, _ = await self._graph().fetch(f"/shares/{share}/driveItem/content")
        name = meta.get("name") or ref.rsplit("/", 1)[-1]
        return data, name, (meta.get("file") or {}).get("mimeType") or mimetypes.guess_type(name)[0] or "application/octet-stream"

    async def upload(self, channel: str, data: bytes, name: str, mime: str, comment: str | None,
                     thread_id: str | None) -> dict:
        tid, cid = await self.resolve(channel)
        folder = await self._graph().get(f"{self._path(tid, cid)}/filesFolder") or {}
        drive_id = (folder.get("parentReference") or {}).get("driveId")
        if not drive_id:
            raise ToolError("Couldn't find the channel's Files folder.")
        item = await self._graph().put(
            f"/drives/{quote(drive_id, safe='')}/items/{quote(folder['id'], safe='')}:/{quote(name)}:/content",
            content=data, headers={"Content-Type": mime}, params={"@microsoft.graph.conflictBehavior": "rename"}) or {}
        m = re.search(r"\{?([0-9A-Fa-f-]{36})\}?", item.get("eTag") or "")
        att_id = m.group(1) if m else uuid.uuid4().hex
        html = (_md_to_html(comment) if comment else "") + f'<attachment id="{att_id}"></attachment>'
        path = f"{self._path(tid, cid)}/messages" + (f"/{quote(thread_id, safe='')}/replies" if thread_id else "")
        msg = await self._graph().post(path, json={"body": {"contentType": "html", "content": html}, "attachments": [
            {"id": att_id, "contentType": "reference", "contentUrl": item.get("webUrl"), "name": item.get("name") or name}]})
        return H.compact({"uploaded": True, "name": item.get("name") or name, "url": item.get("webUrl"),
                          "message_id": (msg or {}).get("id")})

    async def me(self) -> dict:
        data = await self._graph().get("/me", params={"$select": "id,displayName,mail"}) or {}
        return {"id": data.get("id"), "name": data.get("displayName"), "email": data.get("mail")}

    async def mentions(self, user: str | None, since: datetime, channel: str | None, limit: int) -> dict:
        who = await self.me() if not user else {"name": user, "id": None, "email": user if "@" in user else None}
        channels = [channel] if channel else [c["id"] for c in await self.channels(None, 10)]
        hits: list[dict] = []
        for ch in channels:
            tid, cid = await self.resolve(ch)
            data = await self._graph().get(f"{self._path(tid, cid)}/messages", params={"$top": 50}) or {}
            for m in data.get("value") or []:
                when = H.parse_datetime(m.get("createdDateTime"))
                if when and when < since:
                    continue
                for men in m.get("mentions") or []:
                    u = (men.get("mentioned") or {}).get("user") or {}
                    if (who.get("id") and u.get("id") == who["id"]) or (
                            who.get("name") and (u.get("displayName") or "").lower() == str(who["name"]).lower()):
                        hits.append(self.norm(m, ch))
                        break
        hits.sort(key=lambda m: m.get("time") or "", reverse=True)
        return {"user": who.get("name"), "channels_scanned": len(channels), "mentions": hits[:limit]}


async def chat_adapter(ctx: ToolContext, account: str | None = None) -> ChatAdapter:
    """The chat adapter serving this call (also used by the notify group)."""
    kind, cfg = await H.pick_connector(ctx, CHAT_KINDS, account, what="Team chat")
    if kind == "slack":
        return SlackAdapter(ctx, cfg)
    graph_cfg = cfg if (cfg.get("refresh_token") or (cfg.get("client_id") and cfg.get("client_secret")
                                                      and cfg.get("tenant_id") and cfg.get("user"))) else None
    if graph_cfg is None:
        try:
            graph_cfg = await H.load_connector(ctx, "microsoft365") if ("microsoft365" in (ctx.extra.get("connectors") or {})
                                                                        or await ctx.has_connector("microsoft365")) else None
        except ToolError:
            graph_cfg = None
    return TeamsAdapter(ctx, cfg, graph_cfg)


# =========================================================================== tools
group = ToolGroup(
    "chat", "Team chat",
    "Read, search and post in Slack or Microsoft Teams channels; summarize channels and pull out decisions, action "
    "items and mentions.",
    prefix="chat", icon="message-square", category="productivity", connector_kinds=CHAT_KINDS,
)


def _lim(n: int, hi: int = 200) -> int:
    return max(1, min(int(n or 20), hi))


def _send_preview(a: dict) -> str:
    return H.preview("Post chat message", [("Workspace", a.get("account") or "default chat workspace"),
                                           ("Channel / person", a.get("channel"))], a.get("text", ""))


@group.tool(risk=Risk.LOW, scope="read")
async def search(ctx: ToolContext, query: str, channel: str | None = None, limit: int = 20,
                 account: str | None = None) -> dict:
    """Search chat messages (Slack workspace-wide search needs a user token; otherwise a channel's history is scanned).

    Args:
        query: Words to find (Slack search modifiers like `from:@anna` work with a user token).
        channel: Limit to one channel (e.g. "#sales").
        limit: Maximum messages.
        account: Chat connector name or kind (slack, teams).
    """
    ad = await chat_adapter(ctx, account)
    res = await ad.search(query, channel, _lim(limit, 100))
    return {"workspace": ad.label, "count": len(res.get("messages") or []), **res}


@group.tool(risk=Risk.LOW, scope="read")
async def get_thread(ctx: ToolContext, channel: str, thread_id: str, limit: int = 100,
                     account: str | None = None) -> dict:
    """Read a message thread (the parent message and its replies).

    Args:
        channel: Channel containing the thread ("#general", channel id, or "Team / Channel" for Teams).
        thread_id: Thread id — the parent message's id/ts.
        limit: Maximum messages.
        account: Chat connector name or kind.
    """
    ad = await chat_adapter(ctx, account)
    return {"workspace": ad.label, **await ad.thread(channel, thread_id, _lim(limit, 500))}


@group.tool(risk=Risk.LOW, scope="read")
async def list_channels(ctx: ToolContext, query: str | None = None, limit: int = 100,
                        account: str | None = None) -> dict:
    """List channels the app can see (name, id, member count, topic).

    Args:
        query: Filter by words in the name/topic.
        limit: Maximum channels.
        account: Chat connector name or kind.
    """
    ad = await chat_adapter(ctx, account)
    rows = await ad.channels(query, _lim(limit, 1000))
    return {"workspace": ad.label, "count": len(rows), "channels": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def members(ctx: ToolContext, channel: str, limit: int = 100, account: str | None = None) -> dict:
    """List the members of a channel.

    Args:
        channel: Channel name or id.
        limit: Maximum members.
        account: Chat connector name or kind.
    """
    ad = await chat_adapter(ctx, account)
    rows = await ad.members(channel, _lim(limit, 1000))
    return {"workspace": ad.label, "channel": channel, "count": len(rows), "members": rows}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("text",), preview=_send_preview)
async def send(ctx: ToolContext, channel: str, text: str, account: str | None = None) -> dict:
    """Post a message to a channel or person.

    Args:
        channel: Slack: "#channel", channel id, a user's email/@name for a DM. Teams: "Team name / Channel name".
        text: Message text (Slack mrkdwn / Markdown for Teams).
        account: Chat connector name or kind.
    """
    if not text.strip():
        raise ToolError("The message is empty.")
    ad = await chat_adapter(ctx, account)
    return {"workspace": ad.label, **await ad.send(channel, text)}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("text",),
            preview=lambda a: H.preview("Reply in chat thread", [
                ("Workspace", a.get("account") or "default chat workspace"), ("Channel", a.get("channel")),
                ("Thread", a.get("thread_id")), ("Also post to channel", bool(a.get("broadcast")))], a.get("text", "")))
async def reply(ctx: ToolContext, channel: str, thread_id: str, text: str, broadcast: bool = False,
                account: str | None = None) -> dict:
    """Reply in a thread.

    Args:
        channel: Channel containing the thread.
        thread_id: The parent message's id/ts.
        text: Reply text.
        broadcast: Slack: also show the reply in the channel.
        account: Chat connector name or kind.
    """
    if not text.strip():
        raise ToolError("The message is empty.")
    ad = await chat_adapter(ctx, account)
    return {"workspace": ad.label, **await ad.send(channel, text, thread_id=thread_id, broadcast=broadcast)}


@group.tool(risk=Risk.MEDIUM, scope="write")
async def react(ctx: ToolContext, channel: str, message_id: str, emoji: str = "white_check_mark",
                account: str | None = None) -> dict:
    """Add an emoji reaction to a message.

    Args:
        channel: Channel containing the message.
        message_id: Message id/ts.
        emoji: Emoji name without colons, e.g. "thumbsup", "eyes", "white_check_mark".
        account: Chat connector name or kind.
    """
    ad = await chat_adapter(ctx, account)
    return {"workspace": ad.label, **H.compact(await ad.react(channel, message_id, emoji))}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("name", "description"),
            preview=lambda a: H.preview("Create chat channel", [
                ("Workspace", a.get("account") or "default chat workspace"), ("Name", a.get("name")),
                ("Private", bool(a.get("private"))), ("Invite", H.as_list(a.get("members"))),
                ("Description", a.get("description"))]))
async def create_channel(ctx: ToolContext, name: str, private: bool = False, members: list[str] | None = None,
                         description: str | None = None, account: str | None = None) -> dict:
    """Create a channel and optionally invite people.

    Args:
        name: Channel name (Slack: lowercase-with-hyphens; Teams: "Team name / Channel name").
        private: Make it private.
        members: People to invite (emails, names or user ids).
        description: Channel purpose/description.
        account: Chat connector name or kind.
    """
    ad = await chat_adapter(ctx, account)
    return {"workspace": ad.label, **await ad.create_channel(name, private, H.as_list(members), description)}


@group.tool(risk=Risk.LOW, scope="write")
async def download_file(ctx: ToolContext, file_id: str, account: str | None = None) -> dict:
    """Download a file shared in chat into the workspace `downloads/` folder.

    Args:
        file_id: Slack file id (F0123...) or Slack file URL; Teams: the attachment's SharePoint URL.
        account: Chat connector name or kind.
    """
    ad = await chat_adapter(ctx, account)
    data, name, mime = await ad.download(file_id)
    return {**await H.save_bytes(ctx, data, name, "downloads"), "source": f"{ad.label} file"}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("comment",),
            preview=lambda a: H.preview("Share a file in chat", [
                ("Workspace", a.get("account") or "default chat workspace"), ("Channel", a.get("channel")),
                ("File", a.get("path")), ("Thread", a.get("thread_id"))],
                a.get("comment") if a.get("comment") else None, body_label="Comment"))
async def upload_file(ctx: ToolContext, channel: str, path: str, comment: str | None = None,
                      thread_id: str | None = None, account: str | None = None) -> dict:
    """Share a workspace file in a channel (optionally in a thread, with a comment).

    Args:
        channel: Target channel.
        path: Workspace file path, e.g. "output/q3-report.pdf".
        comment: Message posted with the file.
        thread_id: Post into this thread.
        account: Chat connector name or kind.
    """
    ad = await chat_adapter(ctx, account)
    data, name, mime = await H.read_workspace_file(ctx, path, max_bytes=250 * 1024 * 1024)
    return {"workspace": ad.label, **await ad.upload(channel, data, name, mime, comment, thread_id)}


# --------------------------------------------------------------------------- model-assisted
def _transcript(msgs: list[dict], budget: int = 16000) -> str:
    lines = [f"[{m.get('time', '')}] id={m.get('id')} {m.get('user') or '?'}: {m.get('text', '')}" for m in msgs]
    text = "\n".join(lines)
    return text[-budget:] if len(text) > budget else text


async def _history(ctx: ToolContext, channel: str, hours: float, limit: int, account: str | None) -> tuple[ChatAdapter, list[dict]]:
    ad = await chat_adapter(ctx, account)
    msgs = await ad.history(channel, datetime.now(UTC) - timedelta(hours=hours), limit)
    return ad, msgs


@group.tool(risk=Risk.LOW, scope="read")
async def summarize_channel(ctx: ToolContext, channel: str, hours: int = 24, limit: int = 300,
                            focus: str | None = None, account: str | None = None) -> dict:
    """Summarize recent activity in a channel: main topics, decisions, open questions, who's waiting on what.

    Args:
        channel: Channel to summarize.
        hours: Look-back window in hours.
        limit: Maximum messages to read.
        focus: Optional angle, e.g. "customer escalations".
        account: Chat connector name or kind.
    """
    ad, msgs = await _history(ctx, channel, max(1, min(hours, 24 * 30)), _lim(limit, 1000), account)
    if not msgs:
        return {"workspace": ad.label, "channel": channel, "messages": 0, "summary": "No messages in that window."}
    summary = await H.llm_complete(ctx, (
        "Summarize this team chat channel for someone catching up. Markdown sections: Highlights, Decisions, Open "
        "questions, Waiting on (person → item). Be concise and factual; attribute statements to people."),
        (f"Focus: {focus}\n\n" if focus else "") + f"Channel {channel}, last {hours}h:\n\n" + _transcript(msgs))
    return {"workspace": ad.label, "channel": channel, "messages": len(msgs), "summary": summary}


DECISION_SCHEMA = {"type": "object", "properties": {"decisions": {"type": "array", "items": {"type": "object", "properties": {
    "decision": {"type": "string"}, "decided_by": {"type": "string"}, "when": {"type": "string"},
    "message_id": {"type": "string"}, "context": {"type": "string"}}, "required": ["decision"]}}}, "required": ["decisions"]}
CHAT_ACTIONS_SCHEMA = {"type": "object", "properties": {"action_items": {"type": "array", "items": {"type": "object", "properties": {
    "task": {"type": "string"}, "owner": {"type": "string"}, "due": {"type": "string"},
    "message_id": {"type": "string"}}, "required": ["task"]}}}, "required": ["action_items"]}


@group.tool(risk=Risk.LOW, scope="read")
async def find_decisions(ctx: ToolContext, channel: str, days: int = 7, limit: int = 400,
                         account: str | None = None) -> dict:
    """List decisions made in a channel recently (what, who, when, link to the message).

    Args:
        channel: Channel to analyse.
        days: Look-back window in days.
        limit: Maximum messages to read.
        account: Chat connector name or kind.
    """
    ad, msgs = await _history(ctx, channel, 24 * max(1, min(days, 90)), _lim(limit, 1500), account)
    if not msgs:
        return {"workspace": ad.label, "channel": channel, "decisions": []}
    data = await H.llm_structured(ctx, (
        "Extract explicit decisions from this chat (agreements, approvals, chosen options). Skip proposals that "
        "weren't agreed. Include who decided, when, the message id and one line of context."), _transcript(msgs),
        DECISION_SCHEMA)
    return {"workspace": ad.label, "channel": channel, "messages": len(msgs),
            "decisions": (data or {}).get("decisions") or [] if isinstance(data, dict) else []}


@group.tool(risk=Risk.LOW, scope="read")
async def find_action_items(ctx: ToolContext, channel: str, days: int = 7, limit: int = 400,
                            account: str | None = None) -> dict:
    """List action items from a channel (task, owner, due date, source message).

    Args:
        channel: Channel to analyse.
        days: Look-back window in days.
        limit: Maximum messages to read.
        account: Chat connector name or kind.
    """
    ad, msgs = await _history(ctx, channel, 24 * max(1, min(days, 90)), _lim(limit, 1500), account)
    if not msgs:
        return {"workspace": ad.label, "channel": channel, "action_items": []}
    data = await H.llm_structured(ctx, (
        "Extract concrete action items from this chat: who committed to or was asked to do what, by when. "
        "Include the message id. No duplicates."), _transcript(msgs), CHAT_ACTIONS_SCHEMA)
    return {"workspace": ad.label, "channel": channel, "messages": len(msgs),
            "action_items": (data or {}).get("action_items") or [] if isinstance(data, dict) else []}


@group.tool(risk=Risk.LOW, scope="read")
async def find_mentions(ctx: ToolContext, user: str | None = None, days: int = 7, channel: str | None = None,
                        limit: int = 30, account: str | None = None) -> dict:
    """Find recent messages that @mention a person (default: you, when a user token is configured).

    Args:
        user: Person to look for (email, name or user id); default "me".
        days: Look-back window in days.
        channel: Only this channel (default: channels the app is in, up to 15).
        limit: Maximum mentions.
        account: Chat connector name or kind.
    """
    ad = await chat_adapter(ctx, account)
    res = await ad.mentions(user, datetime.now(UTC) - timedelta(days=max(1, min(days, 30))), channel, _lim(limit))
    return {"workspace": ad.label, **res}


# =========================================================================== connector kinds
@H.connector_test
async def _test_slack(cfg: dict) -> tuple[bool, str]:
    ad = SlackAdapter(ToolContext(org_id="_test", user_id=None), cfg)
    data = await ad.call("auth.test")
    msg = f"Connected to Slack workspace '{data.get('team')}' as {data.get('user')}."
    if ad.user_token:
        u = await ad.call("auth.test", user=True)
        msg += f" User token OK ({u.get('user')}) — search enabled."
    return True, msg


@H.connector_test
async def _test_teams(cfg: dict) -> tuple[bool, str]:
    has_graph = bool(cfg.get("refresh_token") or (cfg.get("client_id") and cfg.get("client_secret") and cfg.get("user")))
    if has_graph:
        await microsoft_token(cfg, service="Microsoft Teams")
        api = graph_client(cfg, service="Microsoft Teams", user_scoped=False)
        if cfg.get("refresh_token"):
            data = await api.get("/me/joinedTeams", params={"$select": "displayName"}) or {}
            names = ", ".join(t.get("displayName", "") for t in (data.get("value") or [])[:5])
            return True, f"Connected to Microsoft Teams ({len(data.get('value') or [])} teams: {names or 'none'})."
        return True, "Microsoft Teams app credentials accepted."
    hook = str(cfg.get("webhook_url") or "").strip()
    if hook:
        host = (urlparse(hook).hostname or "").lower()
        if not hook.startswith("https://") or not (host.endswith(".webhook.office.com") or host.endswith(".logic.azure.com")
                                                   or host.endswith(".powerplatform.com") or host.endswith(".environment.api.powerplatform.com")):
            return False, "That doesn't look like a Teams incoming-webhook or Workflows URL."
        return True, "Webhook URL saved (post-only). Not called during the test to avoid posting a message."
    return False, "Add Graph credentials (tenant, client id/secret, refresh token) or a webhook URL."


register_connector_kind(ConnectorKindSpec(
    kind="slack", name="Slack", description="Read, search and post in Slack channels and threads.",
    icon="message-square", category="productivity", tool_groups=["chat", "notify"],
    fields=[
        ConnectorField("bot_token", "Bot token", type="password", required=True, secret=True, placeholder="xoxb-…",
                       help="Slack app → OAuth & Permissions. Scopes: channels:read, channels:history, groups:read, "
                            "groups:history, chat:write, reactions:write, users:read, users:read.email, files:read, "
                            "files:write, channels:manage, groups:write, im:write."),
        ConnectorField("user_token", "User token (optional, for search)", type="password", secret=True,
                       placeholder="xoxp-…", help="Enables workspace-wide search (scope search:read)."),
        ConnectorField("default_channel", "Default channel", placeholder="#general",
                       help="Used by notifications when no channel is given."),
    ],
    docs_url="https://api.slack.com/authentication/basics", test=_test_slack,
))

register_connector_kind(ConnectorKindSpec(
    kind="teams", name="Microsoft Teams",
    description="Read and post Teams channel messages via Microsoft Graph, or post-only via a webhook.",
    icon="message-square", category="productivity", tool_groups=["chat", "notify"],
    fields=[
        ConnectorField("tenant_id", "Directory (tenant) ID", placeholder="contoso.onmicrosoft.com"),
        ConnectorField("client_id", "Application (client) ID"),
        ConnectorField("client_secret", "Client secret", type="password", secret=True),
        ConnectorField("refresh_token", "Refresh token", type="password", secret=True,
                       help="Delegated scopes: offline_access Team.ReadBasic.All Channel.ReadBasic.All "
                            "ChannelMessage.Read.All ChannelMessage.Send ChannelMember.Read.All Files.ReadWrite.All. "
                            "If empty, a connected Microsoft 365 account is used when available."),
        ConnectorField("webhook_url", "Incoming webhook / Workflows URL (post-only)", type="password", secret=True,
                       placeholder="https://….webhook.office.com/…"),
        ConnectorField("default_team", "Default team", placeholder="Sales"),
        ConnectorField("default_channel", "Default channel", placeholder="Sales / General"),
    ],
    docs_url="https://learn.microsoft.com/graph/teams-concept-overview", test=_test_teams,
))
