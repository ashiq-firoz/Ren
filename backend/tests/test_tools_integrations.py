"""Integration tool groups (SaaS connectors): registration, request shapes against mocked HTTP APIs, local
fallbacks, notifications/scheduling, credential-broker redaction, OAuth token caching and optional deps.

All HTTP is mocked with httpx.MockTransport (app.tools.net.http is monkeypatched); credentials are injected with
ctx.extra["connectors"]. Tests run against an isolated org so other suites' connectors can't interfere.
"""
from __future__ import annotations

import asyncio
import base64
import email as email_lib
import email.policy
import imaplib
import importlib.util
import json
import re
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.tools.builtin import load_builtin_tools

load_builtin_tools()

from app.tools.base import Risk, ToolContext, ToolError, registry  # noqa: E402
from app.tools.builtin import _http as H  # noqa: E402
from app.tools.builtin import _oauth  # noqa: E402
from app.tools.connectors import connector_kinds  # noqa: E402

GROUPS = {
    "email": ("email", "productivity", "mail"), "calendar": ("calendar", "productivity", "calendar"),
    "drive": ("cloud", "productivity", "hard-drive"), "chat": ("chat", "productivity", "message-square"),
    "github": ("github", "engineering", "github"), "pm": ("pm", "business", "kanban"),
    "knowledge_base": ("kb", "knowledge", "book-open"), "crm": ("crm", "business", "briefcase"),
    "aws": ("aws", "infrastructure", "cloud"), "azure": ("azure", "infrastructure", "cloud"),
    "gcp": ("gcp", "infrastructure", "cloud"), "secrets": ("secrets", "infrastructure", "key-round"),
    "notify": ("notify", "productivity", "bell"),
}
KINDS = ["gmail", "google_calendar", "google_drive", "microsoft365", "imap_smtp", "slack", "teams", "github", "jira",
         "linear", "asana", "notion", "confluence", "hubspot", "salesforce", "dropbox", "box", "aws", "azure", "gcp",
         "webhook", "twilio"]
EXPECTED_TOOLS = {
    "email": "search get thread list_attachments download_attachment send reply forward create_draft update_draft "
             "delete_draft label archive mark_read mark_unread summarize_thread extract_action_items "
             "extract_deadlines find_followups",
    "calendar": "list get_event search create update cancel attendees availability add_meeting_link prepare_meeting "
                "summarize_previous_meeting create_followup",
    "cloud": "search list get read download upload create_folder move copy rename delete share",
    "chat": "search get_thread list_channels members send reply react create_channel download_file upload_file "
            "summarize_channel find_decisions find_action_items find_mentions",
    "github": "search_code search_repositories get_repo list_issues get_issue create_issue update_issue comment "
              "list_prs get_pr create_pr update_pr review_pr merge_pr workflow_runs workflow_status workflow_logs "
              "list_releases list_tags create_release",
    "pm": "search get_project list_projects get_task create_task update_task complete_task assign_task create_project "
          "update_project comments add_comment attachments",
    "kb": "search get create update append database_query database_create database_update",
    "crm": "search_customer get_customer create_customer update_customer list_deals get_deal create_deal update_deal "
           "list_tasks create_task update_task pipeline_summary",
    "aws": "s3_list_buckets s3_list_objects s3_get_object s3_put_object ec2_list_instances ec2_start ec2_stop "
           "lambda_list lambda_invoke cloudwatch_logs_query ecs_services rds_instances cost_summary",
    "azure": "list_resource_groups list_resources vm_list storage_list_containers storage_list_blobs monitor_metrics",
    "gcp": "storage_list_buckets storage_list_objects compute_list_instances cloudrun_services logging_query",
    "secrets": "list_metadata check",
    "notify": "send notify_admins email slack webhook sms whatsapp schedule cancel_schedule",
}


# =========================================================================== shared test plumbing
class Router:
    """httpx.MockTransport handler: route by method + URL regex (latest registration wins), record calls."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern, Any]] = []
        self.calls: list[httpx.Request] = []

    def add(self, method: str, pattern: str, response: Any = None, *, json: Any = None, status: int = 200,
            text: str | None = None, content: bytes | None = None, headers: dict | None = None) -> Router:
        if response is None:
            def response(request, _j=json, _s=status, _t=text, _c=content, _h=headers):  # noqa: ANN001
                if _j is not None:
                    return httpx.Response(_s, json=_j, headers=_h)
                if _c is not None:
                    return httpx.Response(_s, content=_c, headers=_h)
                return httpx.Response(_s, text=_t or "", headers=_h)
        self.routes.append((method.upper(), re.compile(pattern), response))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        for method, pat, resp in reversed(self.routes):
            if method in (request.method, "*") and pat.search(url):
                return resp(request) if callable(resp) else resp
        return httpx.Response(599, text=f"unmocked {request.method} {url}")

    def find(self, method: str, pattern: str) -> list[httpx.Request]:
        rx = re.compile(pattern)
        return [r for r in self.calls if r.method == method.upper() and rx.search(str(r.url))]

    def one(self, method: str, pattern: str) -> httpx.Request:
        found = self.find(method, pattern)
        assert len(found) == 1, f"expected one {method} {pattern}, got {[str(r.url) for r in self.calls]}"
        return found[0]


def seq(*responses: httpx.Response):
    items = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return items.pop(0) if len(items) > 1 else items[0]

    return handler


def body_json(req: httpx.Request) -> Any:
    return json.loads(req.content.decode("utf-8"))


def form(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


def query(req: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlparse(str(req.url)).query)


async def call(name: str, ctx: ToolContext, **args: Any) -> Any:
    spec = registry.get(name)
    assert spec is not None, f"tool {name} not registered"
    return await spec.handler(ctx, **spec.validate(args))


@pytest.fixture
def http(monkeypatch):
    router = Router()
    client = httpx.AsyncClient(transport=httpx.MockTransport(router), follow_redirects=True)
    monkeypatch.setattr("app.tools.net.http", lambda: client)

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(H, "_sleep", no_sleep)
    _oauth.clear_cache()
    yield router
    _oauth.clear_cache()


_ORG: dict[str, str] = {}


@pytest.fixture(scope="session")
def integ_org(db_ready):
    """A dedicated org (owner + member + mock LLM provider) so connector rows from other suites can't leak in."""
    if _ORG:
        return _ORG
    from app.core.db import session_scope
    from app.models import Membership, Organization, Provider, User

    tag = uuid.uuid4().hex[:8]

    async def make() -> dict[str, str]:
        async with session_scope() as db:
            org = Organization(name="Integrations Org", slug=f"integ-{tag}", mode="business", settings={})
            owner = User(email=f"owner-{tag}@integ.dev", name="Integ Owner", password_hash="x")
            member = User(email=f"member-{tag}@integ.dev", name="Integ Member", password_hash="x")
            db.add_all([org, owner, member])
            await db.flush()
            db.add_all([Membership(org_id=org.id, user_id=owner.id, role="owner"),
                        Membership(org_id=org.id, user_id=member.id, role="member")])
            prov = Provider(org_id=org.id, name="Mock", kind="mock", base_url="mock://local", default_model="mock-1",
                            is_default=True)
            db.add(prov)
            await db.flush()
            org.settings = {"default_provider_id": prov.id}
            return {"org_id": org.id, "owner_id": owner.id, "member_id": member.id,
                    "member_email": member.email, "owner_email": owner.email}

    _ORG.update(asyncio.run(make()))
    return _ORG


@pytest.fixture
def ctx(integ_org) -> ToolContext:
    return ToolContext(org_id=integ_org["org_id"], user_id=integ_org["owner_id"], role="owner",
                       agent_name="Integ Agent", extra={"connectors": {}, "no_db": True})


GOOGLE = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "g-secret", "refresh_token": "g-refresh"}


def google_token_route(http: Router, token: str = "ya29.test") -> None:
    http.add("POST", r"oauth2\.googleapis\.com/token", json={"access_token": token, "expires_in": 3599,
                                                             "token_type": "Bearer"})


# =========================================================================== registration
def test_groups_tools_and_connector_kinds_registered():
    for gid, (prefix, category, icon) in GROUPS.items():
        g = registry.group(gid)
        assert g is not None, gid
        assert (g.prefix, g.category, g.icon) == (prefix, category, icon), gid
    for prefix, names in EXPECTED_TOOLS.items():
        for short in names.split():
            assert registry.get(f"{prefix}_{short}") is not None, f"{prefix}_{short}"
    assert registry.group("email").connector_kinds == ("gmail", "microsoft365", "imap_smtp")
    assert registry.group("calendar").connector_optional and registry.group("drive").connector_optional
    for gid in ("aws", "azure", "gcp"):
        assert registry.group(gid).default_enabled is False
    assert registry.group("aws").requires == ("boto3",)
    for kind in KINDS:
        spec = connector_kinds.get(kind)
        assert spec is not None, kind
        assert spec.test is not None and spec.fields, kind
        assert spec.docs_url, kind
        public = spec.public()
        assert "test" not in public and all("key" in f for f in public["fields"])
    must_be_secret = {"gmail": "client_secret refresh_token", "microsoft365": "client_secret refresh_token",
                      "imap_smtp": "password", "slack": "bot_token user_token", "teams": "client_secret webhook_url",
                      "github": "token", "jira": "api_token", "linear": "api_key", "asana": "token", "notion": "token",
                      "confluence": "api_token", "hubspot": "token", "salesforce": "client_secret refresh_token access_token",
                      "dropbox": "app_secret refresh_token access_token", "box": "client_secret developer_token",
                      "aws": "secret_access_key session_token", "azure": "client_secret", "gcp": "service_account_json",
                      "webhook": "url signing_secret header_value", "twilio": "auth_token"}
    for kind, keys in must_be_secret.items():
        assert set(keys.split()) <= connector_kinds[kind].secret_keys, kind


def test_risk_scope_content_fields_and_previews():
    expect = {
        "email_search": (Risk.LOW, "read"), "email_send": (Risk.HIGH, "write"), "email_reply": (Risk.HIGH, "write"),
        "email_forward": (Risk.HIGH, "write"), "email_create_draft": (Risk.MEDIUM, "write"),
        "email_mark_unread": (Risk.MEDIUM, "write"), "email_summarize_thread": (Risk.LOW, "read"),
        "calendar_create": (Risk.MEDIUM, "write"), "calendar_cancel": (Risk.HIGH, "write"),
        "cloud_upload": (Risk.MEDIUM, "write"), "cloud_delete": (Risk.CRITICAL, "write"),
        "cloud_share": (Risk.HIGH, "write"), "chat_send": (Risk.HIGH, "write"), "chat_react": (Risk.MEDIUM, "write"),
        "chat_create_channel": (Risk.HIGH, "write"), "chat_upload_file": (Risk.HIGH, "write"),
        "github_create_issue": (Risk.MEDIUM, "write"), "github_merge_pr": (Risk.CRITICAL, "write"),
        "github_create_release": (Risk.HIGH, "write"), "github_workflow_logs": (Risk.LOW, "read"),
        "pm_create_project": (Risk.HIGH, "write"), "pm_add_comment": (Risk.MEDIUM, "write"),
        "kb_database_create": (Risk.HIGH, "write"), "kb_create": (Risk.MEDIUM, "write"),
        "crm_create_deal": (Risk.MEDIUM, "write"), "crm_pipeline_summary": (Risk.LOW, "read"),
        "aws_ec2_stop": (Risk.CRITICAL, "admin"), "aws_ec2_start": (Risk.CRITICAL, "admin"),
        "aws_s3_put_object": (Risk.HIGH, "write"), "aws_lambda_invoke": (Risk.HIGH, "write"),
        "secrets_list_metadata": (Risk.LOW, "read"), "notify_send": (Risk.LOW, "write"),
        "notify_notify_admins": (Risk.MEDIUM, "write"), "notify_webhook": (Risk.HIGH, "write"),
        "notify_sms": (Risk.HIGH, "write"), "notify_schedule": (Risk.MEDIUM, "write"),
    }
    for name, (risk, scope) in expect.items():
        spec = registry.get(name)
        assert (spec.risk, spec.scope) == (risk, scope), name
    # dynamic risk: inviting attendees / approving a PR / notifying attendees escalates to HIGH
    assert registry.get("calendar_create").effective_risk({"attendees": ["a@b.co"]}) == Risk.HIGH
    assert registry.get("calendar_create").effective_risk({}) == Risk.MEDIUM
    assert registry.get("calendar_update").effective_risk({"notify_attendees": True}) == Risk.HIGH
    assert registry.get("github_review_pr").effective_risk({"event": "APPROVE"}) == Risk.HIGH
    assert registry.get("github_review_pr").effective_risk({"event": "COMMENT"}) == Risk.MEDIUM
    for name in ("email_send", "email_reply", "email_forward", "email_create_draft", "email_update_draft"):
        assert registry.get(name).content_fields == ("subject", "body"), name
    assert registry.get("chat_send").content_fields == ("text",)
    assert registry.get("notify_sms").content_fields == ("body",)
    mine = [s for g in GROUPS for s in registry.group(g).tools]
    for spec in mine:  # every outbound/destructive tool renders a readable preview
        if spec.risk in (Risk.HIGH, Risk.CRITICAL) or (spec.risk_fn and spec.effective_risk({"attendees": ["x@y.z"]}) == Risk.HIGH):
            assert spec.preview is not None, spec.name
        for field in spec.content_fields:  # declared content fields exist and are strings
            assert field in spec.parameters["properties"], (spec.name, field)
            assert spec.parameters["properties"][field].get("type") == "string", (spec.name, field)
    text = registry.get("email_send").render_preview({
        "to": ["Jane <jane@example.com>"], "cc": ["boss@example.com"], "subject": "Q3 numbers",
        "body": "Hi Jane,\n```\nrevenue: 1.2M\n```\nBest"})
    assert "**To:** Jane <jane@example.com>" in text and "**Subject:** Q3 numbers" in text
    assert "Hi Jane,\n```\nrevenue: 1.2M\n```\nBest" in text and "````" in text  # body verbatim, fence escaped
    assert "#general" in registry.get("chat_send").render_preview({"channel": "#general", "text": "Ship it"})


# =========================================================================== GitHub
async def test_github_list_issues_and_create_issue_request_shapes(ctx, http):
    ctx.extra["connectors"]["github"] = {"token": "ghp_test123"}
    http.add("GET", r"api\.github\.com/repos/acme/web/issues\?", json=[
        {"number": 7, "title": "Login broken", "state": "open", "labels": [{"name": "bug"}],
         "assignees": [{"login": "ana"}], "user": {"login": "bob"}, "comments": 2, "updated_at": "2026-10-01T10:00:00Z",
         "html_url": "https://github.com/acme/web/issues/7"},
        {"number": 8, "title": "A PR", "state": "open", "pull_request": {"url": "x"}, "user": {"login": "c"}},
    ])
    res = await call("github_list_issues", ctx, repo="acme/web", labels=["bug", "p1"], assignee="ana", limit=10)
    req = http.one("GET", r"/repos/acme/web/issues")
    assert req.headers["Authorization"] == "Bearer ghp_test123"
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["X-GitHub-Api-Version"] == "2022-11-28"
    q = query(req)
    assert q["state"] == ["open"] and q["labels"] == ["bug,p1"] and q["assignee"] == ["ana"]
    assert q["per_page"] == ["10"] and q["sort"] == ["updated"] and q["direction"] == ["desc"]
    assert [i["number"] for i in res["issues"]] == [7]  # PRs filtered out
    assert res["issues"][0] == {"number": 7, "title": "Login broken", "state": "open", "labels": ["bug"],
                                "assignees": ["ana"], "author": "bob", "comments": 2, "updated": "2026-10-01T10:00:00Z",
                                "url": "https://github.com/acme/web/issues/7"}

    http.add("POST", r"api\.github\.com/repos/acme/web/issues$", status=201,
             json={"number": 9, "title": "Add SSO", "html_url": "https://github.com/acme/web/issues/9"})
    created = await call("github_create_issue", ctx, repo="https://github.com/acme/web.git", title="Add SSO",
                         body="We need **SAML**.", labels=["feature"], assignees=["ana"])
    post = http.one("POST", r"/repos/acme/web/issues$")
    assert body_json(post) == {"title": "Add SSO", "body": "We need **SAML**.", "labels": ["feature"], "assignees": ["ana"]}
    assert created == {"created": True, "number": 9, "title": "Add SSO", "url": "https://github.com/acme/web/issues/9"}


async def test_github_enterprise_base_url_and_friendly_401(ctx, http):
    ctx.extra["connectors"]["github"] = {"token": "t", "base_url": "https://ghe.example.com", "default_owner": "acme"}
    http.add("GET", r"ghe\.example\.com/api/v3/repos/acme/api/pulls", status=401,
             json={"message": "Bad credentials", "documentation_url": "https://docs.github.com"})
    with pytest.raises(ToolError) as e:
        await call("github_list_prs", ctx, repo="api")
    assert "credentials are invalid or expired" in str(e.value) and "reconnect GitHub in Connectors" in str(e.value)
    assert "Bad credentials" in str(e.value)


async def test_http_retries_429_with_retry_after(ctx, http):
    ctx.extra["connectors"]["github"] = {"token": "t"}
    http.add("GET", r"/repos/o/r/tags", seq(httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow"}),
                                           httpx.Response(200, json=[{"name": "v1.0.0", "commit": {"sha": "abcdef123456"}}])))
    res = await call("github_list_tags", ctx, repo="o/r")
    assert res == {"count": 1, "tags": [{"name": "v1.0.0", "sha": "abcdef1"}]}
    assert len(http.find("GET", r"/repos/o/r/tags")) == 2


# =========================================================================== Slack
async def test_slack_send_payload(ctx, http):
    ctx.extra["connectors"]["slack"] = {"bot_token": "xoxb-1-abc"}
    http.add("GET", r"slack\.com/api/conversations\.list", json={
        "ok": True, "channels": [{"id": "C0GENERAL1", "name": "general"}, {"id": "C0RANDOM12", "name": "random"}],
        "response_metadata": {"next_cursor": ""}})
    http.add("POST", r"slack\.com/api/chat\.postMessage", json={"ok": True, "channel": "C0GENERAL1",
                                                                "ts": "1700000000.000100"})
    http.add("GET", r"slack\.com/api/chat\.getPermalink", json={"ok": True, "permalink": "https://acme.slack.com/p1"})
    res = await call("chat_send", ctx, channel="#general", text="Release *v2* is live :rocket:")
    post = http.one("POST", r"chat\.postMessage")
    assert post.headers["Authorization"] == "Bearer xoxb-1-abc"
    assert post.headers["Content-Type"].startswith("application/json") and "charset=utf-8" in post.headers["Content-Type"]
    assert body_json(post) == {"channel": "C0GENERAL1", "text": "Release *v2* is live :rocket:", "mrkdwn": True,
                               "unfurl_links": False}
    assert res == {"workspace": "Slack", "sent": True, "channel": "C0GENERAL1", "id": "1700000000.000100",
                   "permalink": "https://acme.slack.com/p1"}


async def test_slack_errors_are_actionable(ctx, http):
    ctx.extra["connectors"]["slack"] = {"bot_token": "xoxb-1"}
    http.add("POST", r"chat\.postMessage", json={"ok": False, "error": "not_in_channel"})
    with pytest.raises(ToolError, match="invite it"):
        await call("chat_send", ctx, channel="C0123456789", text="hi")
    http.add("POST", r"chat\.postMessage", json={"ok": False, "error": "invalid_auth"})
    with pytest.raises(ToolError, match="reconnect Slack"):
        await call("chat_send", ctx, channel="C0123456789", text="hi")
    http.add("POST", r"chat\.postMessage", json={"ok": False, "error": "missing_scope", "needed": "chat:write"})
    with pytest.raises(ToolError, match="chat:write"):
        await call("chat_send", ctx, channel="C0123456789", text="hi")


# =========================================================================== Gmail
async def test_gmail_send_builds_valid_rfc822_base64url_message(ctx, http):
    ctx.extra["connectors"]["gmail"] = dict(GOOGLE)
    google_token_route(http)
    http.add("POST", r"gmail\.googleapis\.com/gmail/v1/users/me/messages/send", json={"id": "m-1", "threadId": "t-1"})
    ctx.workspace.resolve("output/summary.txt", for_write=True).write_text("Q3 revenue: 1.2M", encoding="utf-8")
    res = await call("email_send", ctx, to=["Jane Doe <jane@example.com>", "ops@example.com"], cc=["boss@example.com"],
                     subject="Q3 report — final", body="Hi Jane,\n\nAttached is the summary.\n\nThanks",
                     attachments=["output/summary.txt"])
    assert res["sent"] and res["id"] == "m-1" and res["account"] == "Gmail"
    tok = http.one("POST", r"oauth2\.googleapis\.com/token")
    assert form(tok) == {"grant_type": "refresh_token", "client_id": GOOGLE["client_id"],
                         "client_secret": "g-secret", "refresh_token": "g-refresh"}
    req = http.one("POST", r"/messages/send")
    assert req.headers["Authorization"] == "Bearer ya29.test"
    raw = body_json(req)["raw"]
    assert re.fullmatch(r"[A-Za-z0-9_-]+=*", raw), "raw must be base64url"
    msg = email_lib.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
    assert msg["To"] == "Jane Doe <jane@example.com>, ops@example.com"
    assert msg["Cc"] == "boss@example.com" and msg["Subject"] == "Q3 report — final"
    assert msg["Message-ID"] and msg["Date"]
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert body.replace("\r\n", "\n").strip() == "Hi Jane,\n\nAttached is the summary.\n\nThanks"  # wire format is CRLF
    atts = list(msg.iter_attachments())
    assert [a.get_filename() for a in atts] == ["summary.txt"]
    content = atts[0].get_content()
    assert (content.decode() if isinstance(content, bytes) else content).strip() == "Q3 revenue: 1.2M"


async def test_gmail_search_and_reply_threading(ctx, http):
    ctx.extra["connectors"]["gmail"] = dict(GOOGLE)
    google_token_route(http)
    http.add("GET", r"/users/me/messages\?", json={"messages": [{"id": "m1", "threadId": "t1"}]})
    http.add("GET", r"/users/me/messages/m1\?format=metadata", json={
        "id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"], "snippet": "Can you send the deck &amp; notes?",
        "internalDate": "1759654800000", "payload": {"headers": [
            {"name": "From", "value": "Ana <ana@client.com>"}, {"name": "To", "value": "me@acme.com"},
            {"name": "Cc", "value": "Lee <lee@client.com>, me@acme.com"}, {"name": "Subject", "value": "Deck"},
            {"name": "Message-ID", "value": "<abc@client.com>"}, {"name": "References", "value": "<root@client.com>"}]}})
    res = await call("email_search", ctx, query="deck", from_address="ana@client.com", unread_only=True, since="2026-10-01")
    q = query(http.find("GET", r"/users/me/messages\?")[0])["q"][0]
    assert q == "deck from:ana@client.com is:unread after:2026/10/01"
    assert res["messages"][0]["snippet"] == "Can you send the deck & notes?" and res["messages"][0]["unread"] is True

    http.add("GET", r"/users/me/profile", json={"emailAddress": "me@acme.com"})
    http.add("POST", r"/messages/send", json={"id": "m2", "threadId": "t1"})
    out = await call("email_reply", ctx, message_id="m1", body="Sure — attached.", reply_all=True)
    raw = body_json(http.one("POST", r"/messages/send"))
    assert raw["threadId"] == "t1"
    msg = email_lib.message_from_bytes(base64.urlsafe_b64decode(raw["raw"]), policy=email.policy.default)
    assert msg["To"] == "Ana <ana@client.com>" and msg["Cc"] == "Lee <lee@client.com>"  # me excluded
    assert msg["Subject"] == "Re: Deck" and msg["In-Reply-To"] == "<abc@client.com>"
    assert msg["References"] == "<root@client.com> <abc@client.com>"
    assert out["to"] == ["Ana <ana@client.com>"] and out["cc"] == ["Lee <lee@client.com>"]


async def test_email_invalid_recipient_rejected(ctx, http):
    ctx.extra["connectors"]["gmail"] = dict(GOOGLE)
    with pytest.raises(ToolError, match="Invalid email address"):
        await call("email_send", ctx, to=["ok@example.com", "not an email"], subject="x", body="y")
    assert not http.calls  # nothing left the system


# =========================================================================== IMAP / SMTP
class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host, port, context=None, timeout=None, **kw):  # noqa: ANN001
        self.host, self.port, self.tls, self.logged_in, self.sent = host, port, False, None, []
        FakeSMTP.instances.append(self)

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, context=None):  # noqa: ANN001
        self.tls = True

    def login(self, user, password):  # noqa: ANN001
        self.logged_in = (user, password)

    def send_message(self, msg):  # noqa: ANN001
        self.sent.append(msg)
        return {}

    def quit(self):
        return 221, b"bye"


RAW_MAIL = (b"From: Ana <ana@client.com>\r\nTo: me@acme.com\r\nSubject: Invoice 42\r\n"
            b"Date: Mon, 05 Oct 2026 09:00:00 +0000\r\nMessage-ID: <inv42@client.com>\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Please pay invoice 42 by Friday.\r\n--XX\r\nContent-Type: application/pdf\r\n"
            b"Content-Disposition: attachment; filename=\"inv42.pdf\"\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            + base64.b64encode(b"%PDF-1.4 fake") + b"\r\n--XX--\r\n")


class FakeIMAP:
    searches: list[tuple] = []
    capabilities = ("IMAP4REV1", "UIDPLUS", "MOVE")

    def __init__(self, host, port, ssl_context=None, timeout=None):  # noqa: ANN001
        self.host, self.port = host, port

    def login(self, user, password):  # noqa: ANN001
        if password != "app-pass":
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"LOGIN completed"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "Sent Items"',
                      b'(\\HasNoChildren \\Drafts) "/" "Drafts"']

    def select(self, box, readonly=False):  # noqa: ANN001
        return "OK", [b"2"]

    def uid(self, command, *args):  # noqa: ANN001
        if command == "SEARCH":
            FakeIMAP.searches.append(args)
            return "OK", [b"41 42"]
        if command == "FETCH":
            uids, spec = args
            if "HEADER.FIELDS" in spec:
                out: list = []
                for u in uids.split(","):
                    hdr = RAW_MAIL.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                    if u == "41":
                        hdr = hdr.replace(b"Invoice 42", b"Invoice 41")
                    flags = b"\\Seen" if u == "41" else b""
                    out += [(f"{u} (UID {u} FLAGS ({flags.decode()}) BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)] {{{len(hdr)}}}".encode(), hdr), b")"]
                return "OK", out
            return "OK", [(f"1 (UID {uids} FLAGS () BODY[] {{{len(RAW_MAIL)}}}".encode(), RAW_MAIL), b")"]
        raise AssertionError(command)

    def logout(self):
        return "BYE", [b""]


IMAP_CFG = {"imap_host": "imap.example.com", "smtp_host": "smtp.example.com", "smtp_port": "465",
            "username": "me@acme.com", "password": "app-pass", "use_ssl": "ssl", "from_address": "Me <me@acme.com>"}


async def test_imap_smtp_send_with_monkeypatched_smtplib(ctx, monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    ctx.extra["connectors"]["imap_smtp"] = dict(IMAP_CFG)
    res = await call("email_send", ctx, to=["Client <client@example.org>"], subject="Proposal", body="Draft attached.",
                     body_format="markdown")
    smtp = FakeSMTP.instances[-1]
    assert (smtp.host, smtp.port, smtp.tls) == ("smtp.example.com", 465, False)  # implicit TLS
    assert smtp.logged_in == ("me@acme.com", "app-pass")
    msg = smtp.sent[0]
    assert msg["From"] == "Me <me@acme.com>" and msg["To"] == "Client <client@example.org>"
    assert msg.get_body(preferencelist=("html",)) is not None  # markdown → html alternative
    assert res["sent"] and res["account"] == "IMAP/SMTP"
    # port 587 → STARTTLS upgrade even if 'ssl' was selected
    ctx.extra["connectors"]["imap_smtp"] = {**IMAP_CFG, "smtp_port": "587"}
    await call("email_send", ctx, to=["a@example.org"], subject="x", body="y")
    assert FakeSMTP.instances[-1].port == 587 and FakeSMTP.instances[-1].tls is True


async def test_imap_search_get_and_attachment(ctx, monkeypatch):
    FakeIMAP.searches.clear()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    ctx.extra["connectors"]["imap_smtp"] = dict(IMAP_CFG)
    res = await call("email_search", ctx, query="invoice", unread_only=True, from_address="ana@client.com")
    assert FakeIMAP.searches[0] == ("UNSEEN", "FROM", '"ana@client.com"', "TEXT", '"invoice"')
    assert [m["id"] for m in res["messages"]] == ["INBOX:42", "INBOX:41"]
    assert res["messages"][0]["unread"] is True and res["messages"][1]["unread"] is False
    assert res["messages"][1]["subject"] == "Invoice 41"
    msg = await call("email_get", ctx, message_id="INBOX:42")
    assert msg["body"].strip() == "Please pay invoice 42 by Friday."
    assert msg["attachments"] == [{"index": 1, "id": "1", "filename": "inv42.pdf", "mime": "application/pdf", "size": 13}]
    saved = await call("email_download_attachment", ctx, message_id="INBOX:42", attachment="inv42.pdf")
    assert saved["path"].startswith("downloads/inv42") and ctx.workspace.resolve(saved["path"]).read_bytes() == b"%PDF-1.4 fake"
    ctx.extra["connectors"]["imap_smtp"] = {**IMAP_CFG, "password": "wrong"}
    with pytest.raises(ToolError, match="IMAP login failed"):
        await call("email_search", ctx)


# =========================================================================== Jira (ADF)
async def test_jira_create_task_sends_adf_description(ctx, http):
    ctx.extra["connectors"]["jira"] = {"site_url": "acme.atlassian.net", "email": "me@acme.com", "api_token": "jira-tok"}
    http.add("POST", r"acme\.atlassian\.net/rest/api/3/issue$", status=201, json={"id": "10001", "key": "ENG-42"})
    res = await call("pm_create_task", ctx, title="Fix login", project="eng",
                     description="Steps:\n- open the app\n- **click** login\n\n```python\nprint(1)\n```",
                     priority="High", labels=["auth bug"], due="2026-10-31")
    req = http.one("POST", r"/rest/api/3/issue$")
    assert req.headers["Authorization"] == "Basic " + base64.b64encode(b"me@acme.com:jira-tok").decode()
    fields = body_json(req)["fields"]
    assert fields["project"] == {"key": "ENG"} and fields["summary"] == "Fix login"
    assert fields["issuetype"] == {"name": "Task"} and fields["priority"] == {"name": "High"}
    assert fields["labels"] == ["auth-bug"] and fields["duedate"] == "2026-10-31"
    assert fields["description"] == {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Steps:"}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "open the app"}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "click", "marks": [{"type": "strong"}]}, {"type": "text", "text": " login"}]}]}]},
        {"type": "codeBlock", "content": [{"type": "text", "text": "print(1)"}], "attrs": {"language": "python"}},
    ]}
    assert res == {"tool": "Jira", "created": True, "id": "ENG-42", "url": "https://acme.atlassian.net/browse/ENG-42"}


async def test_jira_search_builds_jql_and_complete_uses_done_transition(ctx, http):
    ctx.extra["connectors"]["jira"] = {"site_url": "https://acme.atlassian.net/jira", "email": "e@x.co", "api_token": "t"}
    http.add("GET", r"/rest/api/3/search/jql", json={"issues": [{"key": "ENG-1", "fields": {
        "summary": "Crash", "status": {"name": "To Do"}, "project": {"key": "ENG"}, "priority": {"name": "High"}}}]})
    res = await call("pm_search", ctx, query='login "SSO"', project="ENG", status="To Do", assignee="me")
    jql = query(http.one("GET", r"/search/jql"))["jql"][0]
    assert jql == ('text ~ "login \\"SSO\\"" AND project = "ENG" AND status = "To Do" AND assignee = currentUser() '
                   "ORDER BY updated DESC")
    assert res["tasks"][0]["id"] == "ENG-1" and res["tasks"][0]["url"] == "https://acme.atlassian.net/browse/ENG-1"
    http.add("GET", r"/rest/api/3/myself", json={"accountId": "acc-1"})
    http.add("GET", r"/issue/ENG-1/transitions", json={"transitions": [
        {"id": "11", "name": "Start", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
        {"id": "31", "name": "Finish", "to": {"name": "Done", "statusCategory": {"key": "done"}}}]})
    http.add("POST", r"/issue/ENG-1/transitions", status=204)
    done = await call("pm_complete_task", ctx, task_id="ENG-1")
    assert body_json(http.one("POST", r"/issue/ENG-1/transitions")) == {"transition": {"id": "31"}}
    assert done["status"] == "Done"


# =========================================================================== HubSpot
async def test_hubspot_search_customer(ctx, http):
    ctx.extra["connectors"]["hubspot"] = {"token": "pat-na1-xyz"}
    http.add("POST", r"api\.hubapi\.com/crm/v3/objects/contacts/search", json={"total": 1, "results": [
        {"id": "51", "properties": {"firstname": "Ada", "lastname": "Lovelace", "email": "ada@analytical.io",
                                    "company": "Analytical Engines", "jobtitle": "CTO", "lifecyclestage": "customer",
                                    "lastmodifieddate": "2026-09-30T12:00:00Z"}}]})
    res = await call("crm_search_customer", ctx, query="ada", kind="contact", limit=5)
    req = http.one("POST", r"/contacts/search")
    assert req.headers["Authorization"] == "Bearer pat-na1-xyz"
    payload = body_json(req)
    assert payload["query"] == "ada" and payload["limit"] == 5 and "email" in payload["properties"]
    assert res == {"crm": "HubSpot", "count": 1, "customers": [{
        "id": "51", "type": "contact", "name": "Ada Lovelace", "email": "ada@analytical.io",
        "company": "Analytical Engines", "title": "CTO", "stage": "customer", "updated": "2026-09-30T12:00:00Z"}]}


async def test_hubspot_pipeline_summary_pages_and_weights(ctx, http):
    ctx.extra["connectors"]["hubspot"] = {"token": "pat"}
    http.add("GET", r"/crm/v3/pipelines/deals", json={"results": [{"id": "default", "label": "Sales", "stages": [
        {"id": "qual", "label": "Qualified", "displayOrder": 0, "metadata": {"isClosed": "false", "probability": "0.2"}},
        {"id": "neg", "label": "Negotiation", "displayOrder": 1, "metadata": {"isClosed": "false", "probability": "0.6"}},
        {"id": "won", "label": "Closed won", "displayOrder": 2, "metadata": {"isClosed": "true", "probability": "1.0"}}]}]})

    def deals(request: httpx.Request) -> httpx.Response:
        page2 = body_json(request).get("after") == "p2"
        rows = ([{"id": "3", "properties": {"amount": "5000", "dealstage": "neg"}}] if page2 else
                [{"id": "1", "properties": {"amount": "1000", "dealstage": "qual"}},
                 {"id": "2", "properties": {"amount": "2000", "dealstage": "neg"}},
                 {"id": "4", "properties": {"amount": "9000", "dealstage": "won"}}])
        return httpx.Response(200, json={"results": rows, **({} if page2 else {"paging": {"next": {"after": "p2"}}})})

    http.add("POST", r"/crm/v3/objects/deals/search", deals)
    res = await call("crm_pipeline_summary", ctx)
    assert res["pipeline"] == "Sales"
    assert res["stages"] == [{"stage": "Qualified", "deals": 1, "amount": 1000.0, "probability": 0.2, "weighted": 200.0},
                             {"stage": "Negotiation", "deals": 2, "amount": 7000.0, "probability": 0.6, "weighted": 4200.0}]
    assert res["totals"] == {"deals": 3, "amount": 8000.0, "weighted": 4400.0}
    assert len(http.find("POST", r"/deals/search")) == 2


# =========================================================================== local calendar fallback
async def test_local_calendar_create_list_update_cancel_round_trip(ctx, http):
    created = await call("calendar_create", ctx, title="Dentist", start="2026-10-07T15:00", duration_minutes=45,
                         timezone="Europe/Berlin", location="Main St 1")
    assert created["calendar"] == "Local calendar (workspace)" and created["created"]
    assert created["start"] == "2026-10-07T15:00:00+02:00" and created["end"] == "2026-10-07T15:45:00+02:00"
    assert ctx.workspace.resolve("workspace/.calendar/events.json").exists()
    await call("calendar_create", ctx, title="Offsite", start="2026-10-09", all_day=True)
    listed = await call("calendar_list", ctx, start="2026-10-07", end="2026-10-07", timezone="Europe/Berlin")
    assert [e["title"] for e in listed["events"]] == ["Dentist"]
    week = await call("calendar_list", ctx, start="2026-10-05", days=7)
    assert [e["title"] for e in week["events"]] == ["Dentist", "Offsite"] and week["events"][1]["all_day"] is True
    found = await call("calendar_search", ctx, query="main st", start="2026-10-01", end="2026-10-31")
    assert [e["id"] for e in found["events"]] == [created["id"]]
    upd = await call("calendar_update", ctx, event_id=created["id"], title="Dentist (moved)", start="2026-10-07T16:00",
                     timezone="Europe/Berlin")
    assert upd["title"] == "Dentist (moved)" and upd["end"] == "2026-10-07T16:45:00+02:00"  # duration kept
    avail = await call("calendar_availability", ctx, start="2026-10-07T09:00", end="2026-10-07T18:00",
                       timezone="Europe/Berlin", duration_minutes=60, working_hours="09:00-18:00")
    assert avail["people"]["me"]["busy"] == [{"start": "2026-10-07T16:00:00+02:00", "end": "2026-10-07T16:45:00+02:00"}]
    assert avail["suggested_slots"][0] == {"start": "2026-10-07T09:00:00+02:00", "end": "2026-10-07T16:00:00+02:00",
                                           "minutes": 420}
    linked = await call("calendar_add_meeting_link", ctx, event_id=created["id"], link="https://zoom.us/j/123")
    assert linked["meeting_link"] == "https://zoom.us/j/123"
    gone = await call("calendar_cancel", ctx, event_id=created["id"])
    assert gone["cancelled"] and gone["attendees_notified"] is False
    left = await call("calendar_list", ctx, start="2026-10-05", days=7)
    assert [e["title"] for e in left["events"]] == ["Offsite"]
    assert not http.calls  # purely local


async def test_local_calendar_prepare_meeting_uses_llm(ctx, http):
    ev = await call("calendar_create", ctx, title="Board meeting Q4", start=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                    description="Agenda: budget, hiring")
    brief = await call("calendar_prepare_meeting", ctx, event_id=ev["id"])
    assert brief["event"]["title"] == "Board meeting Q4" and isinstance(brief["briefing"], str) and brief["briefing"]
    assert brief["context"]["emails"] == []  # no mailbox connected
    await call("calendar_cancel", ctx, event_id=ev["id"])


# =========================================================================== local drive fallback
async def test_local_drive_fallback_list_read_search_upload(ctx, http):
    ws = ctx.workspace
    ws.resolve("shared/handbook/leave-policy.md", for_write=True).write_text(
        "# Leave policy\nEveryone gets 25 days of paid leave.", encoding="utf-8")
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "Budget"
    wb.active.append(["Team", "Amount"])
    wb.active.append(["Sales", 1200])
    wb.save(ws.resolve("shared/handbook/budget.xlsx", for_write=True))
    listed = await call("cloud_list", ctx, folder="handbook")
    assert listed["drive"] == "Workspace shared folder"
    assert {i["name"] for i in listed["items"]} == {"leave-policy.md", "budget.xlsx"}
    doc = await call("cloud_read", ctx, file_id="shared/handbook/leave-policy.md")
    assert "25 days of paid leave" in doc["text"] and doc["chars"] == len(doc["text"])
    sheet = await call("cloud_read", ctx, file_id="handbook/budget.xlsx")
    assert "## Sheet: Budget" in sheet["text"] and "Sales\t1200" in sheet["text"]
    hits = await call("cloud_search", ctx, query="paid leave")
    assert [h["id"] for h in hits["files"]] == ["shared/handbook/leave-policy.md"]  # content match
    ws.resolve("output/report.txt", for_write=True).write_text("hello", encoding="utf-8")
    up = await call("cloud_upload", ctx, path="output/report.txt", folder="handbook")
    assert up["id"] == "shared/handbook/report.txt"
    up2 = await call("cloud_upload", ctx, path="output/report.txt", folder="handbook")
    assert up2["id"] == "shared/handbook/report (1).txt"  # never overwrites
    dl = await call("cloud_download", ctx, file_id="shared/handbook/report.txt")
    assert dl["path"].startswith("downloads/report")
    member = ToolContext(org_id=ctx.org_id, user_id=_ORG["member_id"], role="member", extra={"connectors": {}})
    with pytest.raises(ToolError, match="Only workspace admins"):
        await call("cloud_delete", member, file_id="shared/handbook/report (1).txt")
    assert (await call("cloud_delete", ctx, file_id="shared/handbook/report (1).txt"))["deleted"]
    with pytest.raises(ToolError, match="already visible"):
        await call("cloud_share", ctx, file_id="shared/handbook/report.txt", emails=["x@y.com"])
    with pytest.raises(ToolError):
        await call("cloud_read", ctx, file_id="../../etc/passwd")
    assert not http.calls


# =========================================================================== notify
async def test_notify_send_creates_notification_rows(ctx, integ_org):
    from sqlalchemy import select

    from app.models import Notification

    res = await call("notify_send", ctx, message="Your report is ready.", title="Report done", link="/tasks/42")
    other = await call("notify_send", ctx, message="Please review the draft.", user_email=integ_org["member_email"].upper())
    async with ctx.session() as db:
        mine = await db.get(Notification, res["notification_id"])
        theirs = await db.get(Notification, other["notification_id"])
        assert (mine.user_id, mine.title, mine.body, mine.link, mine.kind) == (
            integ_org["owner_id"], "Report done", "Your report is ready.", "/tasks/42", "agent")
        assert theirs.user_id == integ_org["member_id"] and theirs.title == "Please review the draft."
        count = len((await db.execute(select(Notification).where(Notification.org_id == ctx.org_id))).scalars().all())
    assert count >= 2
    with pytest.raises(ToolError, match="not a member"):
        await call("notify_send", ctx, message="x", user_email="stranger@nowhere.dev")
    with pytest.raises(ToolError, match="link must be"):
        await call("notify_send", ctx, message="x", link="javascript:alert(1)")
    admins = await call("notify_notify_admins", ctx, title="Heads up", message="Budget exceeded")
    assert admins["admins_notified"] == 1


async def test_notify_schedule_creates_scheduled_job(ctx, integ_org):
    from app.models import ScheduledJob

    before = datetime.now(timezone.utc)
    res = await call("notify_schedule", ctx, message="Send the weekly update", title="Weekly update", run_at="in 2h")
    async with ctx.session() as db:
        job = await db.get(ScheduledJob, res["job_id"])
        assert (job.kind, job.user_id, job.status, job.cron) == ("notification", integ_org["owner_id"], "scheduled", None)
        assert job.payload["title"] == "Weekly update" and job.payload["body"] == "Send the weekly update"
        run_at = job.run_at if job.run_at.tzinfo else job.run_at.replace(tzinfo=timezone.utc)
        assert timedelta(hours=1, minutes=59) <= run_at - before <= timedelta(hours=2, minutes=1)
    rec = await call("notify_schedule", ctx, message="Standup notes", cron="0 9 * * MON")
    async with ctx.session() as db:
        assert (await db.get(ScheduledJob, rec["job_id"])).cron == "0 9 * * MON"
    local = await call("notify_schedule", ctx, message="Call Ana", run_at="2099-01-02T09:00", timezone="Asia/Kolkata")
    assert local["run_at"].startswith("2099-01-02T03:30:00")
    with pytest.raises(ToolError, match="Invalid cron"):
        await call("notify_schedule", ctx, message="x", cron="every monday")
    with pytest.raises(ToolError, match="past"):
        await call("notify_schedule", ctx, message="x", run_at="2001-01-01T00:00")
    listed = await call("notify_list_schedules", ctx)
    assert {res["job_id"], rec["job_id"]} <= {j["id"] for j in listed["jobs"]}
    member = ToolContext(org_id=ctx.org_id, user_id=integ_org["member_id"], role="member", extra={"connectors": {}})
    with pytest.raises(ToolError, match="your own"):
        await call("notify_cancel_schedule", member, job_id=res["job_id"])
    assert (await call("notify_cancel_schedule", ctx, job_id=res["job_id"]))["cancelled"] is True
    async with ctx.session() as db:
        assert (await db.get(ScheduledJob, res["job_id"])).status == "cancelled"


# =========================================================================== credential broker
async def test_secrets_list_metadata_never_leaks_values(ctx, http, integ_org):
    from app.core.db import session_scope
    from app.core.security import encrypt_json
    from app.models import Connector, Organization

    async with session_scope() as db:
        db.add(Connector(org_id=ctx.org_id, kind="github", name="Acme GitHub", status="error",
                         config={"base_url": "https://ghe.secret-host.example"},
                         secrets_enc=encrypt_json({"token": "ghp_SUPERSECRETVALUE1234567890"}),
                         error="401 for token ghp_SUPERSECRETVALUE1234567890"))
        org = await db.get(Organization, ctx.org_id)
        org.secrets_enc = encrypt_json({"web_search_api_key": "sk-ORG-SECRET-VALUE-0000000000"})
    ctx.extra["connectors"]["slack"] = {"bot_token": "xoxb-OVERRIDE-SECRET-1234567890", "default_channel": "#ops"}
    res = await call("secrets_list_metadata", ctx)
    dump = json.dumps(res)
    for secret in ("ghp_SUPERSECRETVALUE1234567890", "xoxb-OVERRIDE-SECRET-1234567890", "sk-ORG-SECRET-VALUE-0000000000",
                   "ghe.secret-host.example", "#ops"):
        assert secret not in dump, secret
    gh = next(c for c in res["configured"] if c["kind"] == "github")
    assert gh["fields_set"] == ["base_url", "token"] and gh["secret_fields_set"] == ["token"]
    assert gh["integration"] == "GitHub" and "••••" in gh["error"]
    sl = next(c for c in res["configured"] if c["kind"] == "slack")
    assert sl["fields_set"] == ["bot_token", "default_channel"] and sl["secret_fields_set"] == ["bot_token"]
    assert res["org_secrets_set"] == ["web_search_api_key"]
    assert "gmail" in {a["kind"] for a in res["available_integrations"]}
    # check() runs the connector test with the stored credentials but returns only ok + message
    http.add("GET", r"ghe\.secret-host\.example/api/v3/user", json={"login": "octocat"}, headers={"x-oauth-scopes": "repo"})
    chk = await call("secrets_check", ctx, kind="github")
    assert chk["configured"] and chk["ok"] and "octocat" in chk["message"]
    assert http.one("GET", r"/api/v3/user").headers["Authorization"] == "Bearer ghp_SUPERSECRETVALUE1234567890"
    assert "ghp_SUPERSECRET" not in json.dumps(chk)
    missing = await call("secrets_check", ctx, kind="notion")
    assert missing["configured"] is False
    with pytest.raises(ToolError, match="Unknown integration"):
        await call("secrets_check", ctx, kind="myspace")
    async with session_scope() as db:  # leave the isolated org clean for other tests
        from sqlalchemy import delete

        await db.execute(delete(Connector).where(Connector.org_id == ctx.org_id))
        (await db.get(Organization, ctx.org_id)).secrets_enc = None


# =========================================================================== OAuth caching
async def test_google_token_refresh_is_cached_and_refreshed_before_expiry(http, monkeypatch):
    issued = iter(["tok-1", "tok-2", "tok-3"])
    http.add("POST", r"oauth2\.googleapis\.com/token",
             lambda req: httpx.Response(200, json={"access_token": next(issued), "expires_in": 3600}))
    now = [1_000_000.0]
    monkeypatch.setattr(_oauth, "_now", lambda: now[0])
    cfg = {**GOOGLE, "_connector_id": "conn-google-1"}
    results = await asyncio.gather(*[_oauth.google_token(cfg) for _ in range(5)])
    assert results == ["tok-1"] * 5 and len(http.calls) == 1  # concurrent callers share one refresh
    assert form(http.calls[0])["grant_type"] == "refresh_token"
    now[0] += 3600 - 120  # still > 60 s before expiry → cached
    assert await _oauth.google_token(cfg) == "tok-1" and len(http.calls) == 1
    now[0] += 90  # within the 60 s refresh window → refresh
    assert await _oauth.google_token(cfg) == "tok-2" and len(http.calls) == 2
    _oauth.invalidate(cfg)  # e.g. after a 401
    assert await _oauth.google_token(cfg) == "tok-3" and len(http.calls) == 3
    other = {**GOOGLE, "refresh_token": "another"}  # different credentials → separate cache entry
    http.add("POST", r"oauth2\.googleapis\.com/token", json={"access_token": "tok-other", "expires_in": 3600})
    assert await _oauth.google_token(other) == "tok-other"


async def test_google_invalid_grant_is_friendly(http):
    http.add("POST", r"oauth2\.googleapis\.com/token", status=400,
             json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    with pytest.raises(ToolError) as e:
        await _oauth.google_token({**GOOGLE}, service="Gmail")
    msg = str(e.value)
    assert "credentials are invalid or expired" in msg and "reconnect Gmail" in msg and "g-refresh" not in msg


async def test_401_triggers_one_token_refresh_and_retry(ctx, http):
    ctx.extra["connectors"]["gmail"] = dict(GOOGLE)
    tokens = iter(["stale", "fresh"])
    http.add("POST", r"oauth2\.googleapis\.com/token",
             lambda req: httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600}))
    http.add("GET", r"/users/me/profile", lambda req: httpx.Response(
        200 if req.headers["Authorization"] == "Bearer fresh" else 401,
        json={"emailAddress": "me@acme.com"} if req.headers["Authorization"] == "Bearer fresh" else {"error": {"message": "Invalid Credentials", "code": 401}}))
    ok, message = await connector_kinds["gmail"].test(dict(GOOGLE))
    assert ok and "me@acme.com" in message
    assert len(http.find("POST", r"oauth2")) == 2 and len(http.find("GET", r"/profile")) == 2


# =========================================================================== optional dependency
async def test_aws_reports_missing_boto3_gracefully(ctx):
    group = registry.group("aws")
    if importlib.util.find_spec("boto3") is not None:
        assert group.availability() == (True, None)
        pytest.skip("boto3 is installed")
    ok, reason = group.availability()
    assert ok is False and "boto3" in reason
    ctx.extra["connectors"]["aws"] = {"access_key_id": "AKIA0000", "secret_access_key": "s3cr3t", "region": "eu-west-1"}
    with pytest.raises(ToolError, match=r"boto3.*ren-backend\[cloud\]"):
        await call("aws_s3_list_buckets", ctx)
    ok, message = await connector_kinds["aws"].test(ctx.extra["connectors"]["aws"])
    assert ok is False and "boto3" in message


async def test_aws_allowlist_is_enforced_before_any_call(ctx):
    ctx.extra["connectors"]["aws"] = {"access_key_id": "A", "secret_access_key": "S", "allowed_buckets": "reports-*"}
    with pytest.raises(ToolError, match="doesn't allow access to bucket 'payroll'"):
        await call("aws_s3_list_objects", ctx, bucket="payroll")
