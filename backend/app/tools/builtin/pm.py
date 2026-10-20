"""Project-management tools over Jira Cloud (REST v3, Atlassian Document Format), Linear (GraphQL) and Asana (REST).
One normalized task/project shape: id, title, status, assignee, due, priority, project, url.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin import _http as H
from app.tools.builtin._oauth import bearer
from app.tools.connectors import ConnectorField, ConnectorKindSpec, register_connector_kind

PM_KINDS = ("jira", "linear", "asana")
_DONE_WORDS = {"done", "complete", "completed", "closed", "resolved", "finished"}


def _q(v: Any) -> str:
    return quote(str(v), safe="")


def site_url(cfg: dict[str, Any], service: str) -> str:
    raw = str(cfg.get("site_url") or "").strip().rstrip("/")
    if not raw:
        raise ToolError(f"The {service} connector has no site URL.")
    if not raw.startswith("http"):
        raw = "https://" + raw
    m = re.match(r"^(https?://[^/]+)", raw)
    return m.group(1) if m else raw


# =========================================================================== Atlassian Document Format
_INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\([^)\s]+\)|(?<![*\w])\*[^*\s][^*\n]*\*(?![*\w])|"
                     r"(?<![_\w])_[^_\s][^_\n]*_(?![_\w]))")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE = re.compile(r"^\s*(---+|\*\*\*+|___+)\s*$")


def adf_inline(text: str) -> list[dict]:
    out: list[dict] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append({"type": "text", "text": text[pos:m.start()]})
        tok = m.group(0)
        if tok.startswith("**"):
            out.append({"type": "text", "text": tok[2:-2], "marks": [{"type": "strong"}]})
        elif tok.startswith("`"):
            out.append({"type": "text", "text": tok[1:-1], "marks": [{"type": "code"}]})
        elif tok.startswith("["):
            label, url = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", tok).groups()  # type: ignore[union-attr]
            out.append({"type": "text", "text": label, "marks": [{"type": "link", "attrs": {"href": url}}]})
        else:
            out.append({"type": "text", "text": tok[1:-1], "marks": [{"type": "em"}]})
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": text[pos:]})
    return [n for n in out if n.get("text")]


def _special(line: str) -> bool:
    s = line.strip()
    return bool(s.startswith("```") or _HEADING.match(line) or _BULLET.match(line) or _ORDERED.match(line)
                or s.startswith(">") or _RULE.match(line))


def md_to_adf(text: str) -> dict:
    """Markdown-ish text → ADF document (paragraphs, headings, lists, quotes, code blocks, rules, inline marks)."""
    blocks: list[dict] = []
    lines = (text or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("```"):
            lang = s[3:].strip()
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            node: dict[str, Any] = {"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code)}] if code else []}
            if lang:
                node["attrs"] = {"language": lang}
            blocks.append(node)
            continue
        m = _HEADING.match(line)
        if m:
            blocks.append({"type": "heading", "attrs": {"level": len(m.group(1))}, "content": adf_inline(m.group(2))})
            i += 1
            continue
        if _RULE.match(line):
            blocks.append({"type": "rule"})
            i += 1
            continue
        for pattern, kind in ((_BULLET, "bulletList"), (_ORDERED, "orderedList")):
            if pattern.match(line):
                items = []
                while i < len(lines) and pattern.match(lines[i]):
                    items.append({"type": "listItem", "content": [
                        {"type": "paragraph", "content": adf_inline(pattern.match(lines[i]).group(1))}]})  # type: ignore[union-attr]
                    i += 1
                blocks.append({"type": kind, "content": items})
                break
        else:
            if s.startswith(">"):
                quoted = []
                while i < len(lines) and lines[i].strip().startswith(">"):
                    quoted.append(lines[i].strip()[1:].strip())
                    i += 1
                blocks.append({"type": "blockquote", "content": [{"type": "paragraph", "content": adf_inline(" ".join(quoted))}]})
                continue
            if not s:
                i += 1
                continue
            para = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not _special(lines[i]):
                para.append(lines[i])
                i += 1
            content: list[dict] = []
            for j, pl in enumerate(para):
                if j:
                    content.append({"type": "hardBreak"})
                content += adf_inline(pl)
            blocks.append({"type": "paragraph", "content": content})
            continue
        continue
    return {"type": "doc", "version": 1, "content": blocks or [{"type": "paragraph", "content": []}]}


def adf_to_text(node: Any) -> str:
    """ADF → readable markdown-ish text."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    t = node.get("type")
    attrs = node.get("attrs") or {}
    kids = node.get("content") or []
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return attrs.get("text") or "@user"
    if t == "emoji":
        return attrs.get("text") or attrs.get("shortName") or ""
    if t in ("inlineCard", "blockCard"):
        return attrs.get("url", "")
    if t == "bulletList":
        return "\n".join("- " + adf_to_text(c).strip() for c in kids) + "\n"
    if t == "orderedList":
        return "\n".join(f"{n}. " + adf_to_text(c).strip() for n, c in enumerate(kids, 1)) + "\n"
    if t == "codeBlock":
        return "```\n" + "".join(adf_to_text(c) for c in kids) + "\n```\n"
    if t == "heading":
        return "#" * int(attrs.get("level") or 1) + " " + "".join(adf_to_text(c) for c in kids) + "\n"
    if t == "rule":
        return "---\n"
    if t == "tableRow":
        return " | ".join(adf_to_text(c).strip() for c in kids) + "\n"
    inner = "".join(adf_to_text(c) for c in kids)
    if t in ("paragraph", "blockquote", "panel", "table", "mediaSingle", "expand", "taskItem", "decisionItem"):
        return inner + "\n"
    return inner


# =========================================================================== adapters
class PMAdapter:
    kind = ""
    label = ""

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        self.ctx = ctx
        self.cfg = cfg


# --------------------------------------------------------------------------- Jira
JIRA_LIST_FIELDS = "summary,status,assignee,priority,issuetype,updated,project,duedate,labels"
_JQL_HINT = re.compile(r"(\s(=|!=|~|in|is|was|>|<)\s|order\s+by|\bcurrentUser\(\)|^\s*(project|status|assignee|text)\s*[=~!])",
                       re.I)


def _jql_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


class JiraAdapter(PMAdapter):
    kind = "jira"
    label = "Jira"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        H.need(cfg, ("site_url", "email", "api_token"), "Jira")
        self.site = site_url(cfg, "Jira")
        self.api = H.RestClient("Jira", f"{self.site}/rest/api/3", basic=(str(cfg["email"]).strip(), str(cfg["api_token"]).strip()),
                                headers={"Accept": "application/json"})
        self._me: str | None = None

    def norm(self, i: dict) -> dict:
        f = i.get("fields") or {}
        return H.compact({
            "id": i.get("key"), "title": f.get("summary"), "status": (f.get("status") or {}).get("name"),
            "type": (f.get("issuetype") or {}).get("name"), "assignee": (f.get("assignee") or {}).get("displayName"),
            "priority": (f.get("priority") or {}).get("name"), "due": f.get("duedate"),
            "project": (f.get("project") or {}).get("key"), "labels": f.get("labels") or None,
            "updated": f.get("updated"), "url": f"{self.site}/browse/{i.get('key')}",
        })

    async def me(self) -> str:
        if self._me is None:
            self._me = (await self.api.get("/myself")).get("accountId") or ""
        return self._me

    async def account_id(self, who: str | None) -> str | None:
        w = (who or "").strip()
        if not w or w.lower() in ("none", "unassigned", "null"):
            return None
        if w.lower() == "me":
            return await self.me()
        if re.fullmatch(r"[0-9a-f]{24}|\d{5,7}:[0-9a-f-]{36}", w):
            return w
        users = await self.api.get("/user/search", params={"query": w, "maxResults": 5}) or []
        if not users:
            raise ToolError(f"No Jira user matching '{who}'.")
        return users[0]["accountId"]

    async def search(self, query, project, status, assignee, limit):
        q = (query or "").strip()
        if q and _JQL_HINT.search(q):
            jql = q
        else:
            parts = []
            if q:
                parts.append(f"text ~ {_jql_str(q)}")
            if project:
                parts.append(f"project = {_jql_str(project)}")
            if status:
                parts.append(f"status = {_jql_str(status)}")
            who = (assignee or "").strip().lower()
            if who == "me":
                parts.append("assignee = currentUser()")  # JQL resolves the caller; no lookup needed
            elif who:
                aid = await self.account_id(assignee)
                parts.append("assignee is EMPTY" if aid is None else f"assignee = {_jql_str(aid)}")
            jql = (" AND ".join(parts) or "created >= -30d") + " ORDER BY updated DESC"
        data = await self.api.get("/search/jql", params={"jql": jql, "maxResults": limit, "fields": JIRA_LIST_FIELDS}) or {}
        return {"jql": jql, "tasks": [self.norm(i) for i in data.get("issues", [])]}

    async def list_projects(self, query, limit):
        data = await self.api.get("/project/search", params={"query": query or None, "maxResults": limit,
                                                             "expand": "lead"}) or {}
        return [H.compact({"id": p.get("key"), "name": p.get("name"), "type": p.get("projectTypeKey"),
                           "lead": (p.get("lead") or {}).get("displayName"), "url": f"{self.site}/browse/{p.get('key')}"})
                for p in data.get("values", [])]

    async def get_project(self, project):
        p = await self.api.get(f"/project/{_q(project)}", params={"expand": "description,lead,issueTypes"})
        return H.compact({"id": p.get("key"), "name": p.get("name"), "description": H.clip(p.get("description"), 3000),
                          "type": p.get("projectTypeKey"), "lead": (p.get("lead") or {}).get("displayName"),
                          "issue_types": [t.get("name") for t in p.get("issueTypes") or []] or None,
                          "url": f"{self.site}/browse/{p.get('key')}"})

    async def get_task(self, task_id):
        i = await self.api.get(f"/issue/{_q(task_id)}", params={
            "fields": JIRA_LIST_FIELDS + ",description,reporter,created,parent,subtasks,attachment,comment"})
        f = i.get("fields") or {}
        out = self.norm(i)
        out.update(H.compact({
            "description": H.clip(adf_to_text(f.get("description")).strip(), 6000),
            "reporter": (f.get("reporter") or {}).get("displayName"), "created": f.get("created"),
            "parent": (f.get("parent") or {}).get("key"),
            "subtasks": [{"id": s.get("key"), "title": (s.get("fields") or {}).get("summary"),
                          "status": ((s.get("fields") or {}).get("status") or {}).get("name")} for s in f.get("subtasks") or []] or None,
            "comments": ((f.get("comment") or {}).get("total")) or None,
            "attachments": len(f.get("attachment") or []) or None,
        }))
        return out

    async def _fields(self, *, title=None, description=None, priority=None, labels=None, due=None) -> dict:
        fields: dict[str, Any] = {}
        if title is not None:
            fields["summary"] = title
        if description is not None:
            fields["description"] = md_to_adf(description)
        if priority:
            fields["priority"] = {"name": priority}
        if labels is not None:
            fields["labels"] = [re.sub(r"\s+", "-", lb) for lb in labels]
        if due is not None:
            fields["duedate"] = due or None
        return fields

    async def create_task(self, *, project, title, description, assignee, priority, labels, due, task_type, parent):
        proj = project or self.cfg.get("default_project")
        if not proj:
            raise ToolError("Pass the Jira project key (e.g. 'ENG') or set a default project in the connector.")
        fields = await self._fields(title=title, description=description, priority=priority, labels=labels, due=due)
        fields["project"] = {"key": str(proj).strip().upper()}
        fields["issuetype"] = {"name": task_type or ("Sub-task" if parent else "Task")}
        if parent:
            fields["parent"] = {"key": parent}
        if assignee:
            fields["assignee"] = {"accountId": await self.account_id(assignee)}
        res = await self.api.post("/issue", json={"fields": fields})
        return {"created": True, "id": res.get("key"), "url": f"{self.site}/browse/{res.get('key')}"}

    async def _transition(self, task_id: str, *, name: str | None = None, done: bool = False) -> str:
        data = await self.api.get(f"/issue/{_q(task_id)}/transitions") or {}
        trans = data.get("transitions") or []
        pick = None
        for t in trans:
            to = t.get("to") or {}
            if done and ((to.get("statusCategory") or {}).get("key") == "done"):
                pick = t
                break
            if name and name.lower() in ((t.get("name") or "").lower(), (to.get("name") or "").lower()):
                pick = t
                break
        if pick is None:
            avail = ", ".join(sorted({(t.get("to") or {}).get("name") or t.get("name") for t in trans})) or "none"
            raise ToolError(f"No transition to '{name or 'Done'}' is available from the current status. Options: {avail}.")
        await self.api.post(f"/issue/{_q(task_id)}/transitions", json={"transition": {"id": pick["id"]}})
        return (pick.get("to") or {}).get("name") or pick.get("name")

    async def update_task(self, task_id, *, title, description, status, priority, labels, due):
        fields = await self._fields(title=title, description=description, priority=priority, labels=labels, due=due)
        changed: list[str] = list(fields)
        if fields:
            await self.api.put(f"/issue/{_q(task_id)}", json={"fields": fields})
        new_status = None
        if status:
            new_status = await self._transition(task_id, name=status, done=status.lower() in _DONE_WORDS)
            changed.append("status")
        if not changed:
            raise ToolError("Nothing to update.")
        return H.compact({"updated": True, "id": task_id, "changed": changed, "status": new_status})

    async def complete_task(self, task_id):
        return {"completed": True, "id": task_id, "status": await self._transition(task_id, done=True)}

    async def assign_task(self, task_id, assignee):
        aid = await self.account_id(assignee)
        await self.api.put(f"/issue/{_q(task_id)}/assignee", json={"accountId": aid})
        return {"assigned": aid is not None, "id": task_id, "assignee": assignee if aid else None}

    async def create_project(self, *, name, key, description, team, project_type):
        ptype = (project_type or "software").lower()
        templates = {"software": "com.pyxis.greenhopper.jira:gh-simplified-kanban-classic",
                     "business": "com.atlassian.jira-core-project-templates:jira-core-simplified-task-tracking"}
        k = (key or re.sub(r"[^A-Z]", "", name.upper())[:6] or "PRJ").upper()
        body = H.compact({"key": k, "name": name, "projectTypeKey": ptype, "projectTemplateKey": templates.get(ptype),
                          "description": description, "leadAccountId": await self.me(), "assigneeType": "UNASSIGNED"})
        res = await self.api.post("/project", json=body)
        return {"created": True, "id": res.get("key") or k, "url": f"{self.site}/browse/{res.get('key') or k}"}

    async def update_project(self, project, *, name, description, lead):
        body = H.compact({"name": name, "description": description,
                          "leadAccountId": await self.account_id(lead) if lead else None})
        if not body:
            raise ToolError("Nothing to update.")
        p = await self.api.put(f"/project/{_q(project)}", json=body)
        return {"updated": True, "id": (p or {}).get("key") or project, "changed": list(body)}

    async def comments(self, task_id, limit):
        data = await self.api.get(f"/issue/{_q(task_id)}/comment", params={"orderBy": "-created", "maxResults": limit}) or {}
        return [{"id": c.get("id"), "author": (c.get("author") or {}).get("displayName"), "created": c.get("created"),
                 "body": H.clip(adf_to_text(c.get("body")).strip(), 2000)} for c in data.get("comments", [])]

    async def add_comment(self, task_id, body):
        c = await self.api.post(f"/issue/{_q(task_id)}/comment", json={"body": md_to_adf(body)})
        return {"commented": True, "id": c.get("id"), "task": task_id}

    async def attachments(self, task_id):
        i = await self.api.get(f"/issue/{_q(task_id)}", params={"fields": "attachment"})
        return [H.compact({"id": a.get("id"), "filename": a.get("filename"), "size": a.get("size"),
                           "mime": a.get("mimeType"), "created": a.get("created"),
                           "author": (a.get("author") or {}).get("displayName"), "url": a.get("content")})
                for a in (i.get("fields") or {}).get("attachment") or []]


# --------------------------------------------------------------------------- Linear
LINEAR_URL = "https://api.linear.app/graphql"
LINEAR_ISSUE = ("id identifier title url priority priorityLabel dueDate updatedAt state { name type } "
                "assignee { name email } team { id key name } project { id name } labels { nodes { name } }")
LINEAR_PRIORITY = {"none": 0, "no priority": 0, "urgent": 1, "high": 2, "medium": 3, "normal": 3, "low": 4}


class LinearAdapter(PMAdapter):
    kind = "linear"
    label = "Linear"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        key = str(cfg.get("api_key") or "").strip()
        if not key:
            raise ToolError("The Linear connector has no API key.")
        self.auth = key if key.startswith("lin_api_") else f"Bearer {key}"

    async def gql(self, query: str, variables: dict | None = None) -> dict:
        res = await H.api("POST", LINEAR_URL, service="Linear", headers={"Authorization": self.auth,
                                                                         "Content-Type": "application/json"},
                          json={"query": query, "variables": variables or {}})
        if not isinstance(res, dict):
            raise ToolError("Unexpected response from Linear.")
        if res.get("errors"):
            msg = str((res["errors"][0] or {}).get("message") or "GraphQL error")
            if "authenticat" in msg.lower():
                raise ToolError("Linear credentials are invalid or expired — reconnect Linear in Connectors.")
            raise ToolError(f"Linear: {msg}")
        return res.get("data") or {}

    @staticmethod
    def norm(i: dict) -> dict:
        return H.compact({
            "id": i.get("identifier") or i.get("id"), "title": i.get("title"), "status": (i.get("state") or {}).get("name"),
            "assignee": (i.get("assignee") or {}).get("name"), "priority": i.get("priorityLabel"), "due": i.get("dueDate"),
            "project": (i.get("project") or {}).get("name"), "team": (i.get("team") or {}).get("key"),
            "labels": [lb.get("name") for lb in ((i.get("labels") or {}).get("nodes") or [])] or None,
            "updated": i.get("updatedAt"), "url": i.get("url"),
        })

    async def _issue(self, ref: str, fields: str = "id team { id }") -> dict:
        data = await self.gql(f"query($id: String!) {{ issue(id: $id) {{ {fields} }} }}", {"id": ref})
        if not data.get("issue"):
            raise ToolError(f"Linear issue '{ref}' not found.")
        return data["issue"]

    async def _user_id(self, who: str | None) -> str | None:
        w = (who or "").strip()
        if not w or w.lower() in ("none", "unassigned", "null"):
            return None
        if w.lower() == "me":
            return (await self.gql("{ viewer { id } }"))["viewer"]["id"]
        flt = {"email": {"eq": w}} if "@" in w else {"or": [{"name": {"eqIgnoreCase": w}}, {"displayName": {"eqIgnoreCase": w}}]}
        data = await self.gql("query($f: UserFilter) { users(filter: $f, first: 2) { nodes { id } } }", {"f": flt})
        nodes = (data.get("users") or {}).get("nodes") or []
        if not nodes:
            raise ToolError(f"No Linear user matching '{who}'.")
        return nodes[0]["id"]

    async def _team(self, ref: str | None) -> tuple[str, str | None]:
        """(team_id, project_id) from a team key/name or project name; default team otherwise."""
        r = (ref or str(self.cfg.get("default_team") or "")).strip()
        if r:
            data = await self.gql("query($k: String!) { teams(filter: {or: [{key: {eqIgnoreCase: $k}}, {name: {eqIgnoreCase: $k}}]}, first: 1) { nodes { id } } }",
                                  {"k": r})
            nodes = (data.get("teams") or {}).get("nodes") or []
            if nodes:
                return nodes[0]["id"], None
            data = await self.gql("query($n: String!) { projects(filter: {name: {eqIgnoreCase: $n}}, first: 1) { nodes { id teams { nodes { id } } } } }",
                                  {"n": r})
            pn = (data.get("projects") or {}).get("nodes") or []
            if pn and ((pn[0].get("teams") or {}).get("nodes")):
                return pn[0]["teams"]["nodes"][0]["id"], pn[0]["id"]
            raise ToolError(f"No Linear team or project named '{r}'.")
        data = await self.gql("{ teams(first: 1) { nodes { id } } }")
        nodes = (data.get("teams") or {}).get("nodes") or []
        if not nodes:
            raise ToolError("No Linear teams are available to this API key.")
        return nodes[0]["id"], None

    def _filter(self, project, status, assignee_id_or_me) -> dict:
        f: dict[str, Any] = {}
        if project:
            f["or"] = [{"project": {"name": {"containsIgnoreCase": project}}}, {"team": {"key": {"eqIgnoreCase": project}}}]
        if status:
            f["state"] = {"name": {"eqIgnoreCase": status}}
        if assignee_id_or_me == "me":
            f["assignee"] = {"isMe": {"eq": True}}
        elif assignee_id_or_me:
            f["assignee"] = {"id": {"eq": assignee_id_or_me}}
        return f

    async def search(self, query, project, status, assignee, limit):
        who = "me" if (assignee or "").strip().lower() == "me" else (await self._user_id(assignee) if assignee else None)
        flt = self._filter(project, status, who)
        if query and query.strip():
            try:
                data = await self.gql(f"query($t: String!, $n: Int, $f: IssueFilter) {{ searchIssues(term: $t, first: $n, filter: $f) {{ nodes {{ {LINEAR_ISSUE} }} }} }}",
                                      {"t": query.strip(), "n": limit, "f": flt or None})
                return {"tasks": [self.norm(i) for i in (data.get("searchIssues") or {}).get("nodes") or []]}
            except ToolError as e:
                if "searchIssues" not in str(e) and "Cannot query field" not in str(e):
                    raise
                flt = {**flt, "title": {"containsIgnoreCase": query.strip()}}
        data = await self.gql(f"query($n: Int, $f: IssueFilter) {{ issues(first: $n, filter: $f, orderBy: updatedAt) {{ nodes {{ {LINEAR_ISSUE} }} }} }}",
                              {"n": limit, "f": flt or None})
        return {"tasks": [self.norm(i) for i in (data.get("issues") or {}).get("nodes") or []]}

    async def list_projects(self, query, limit):
        data = await self.gql("query($n: Int, $f: ProjectFilter) { projects(first: $n, filter: $f) { nodes { id name state progress targetDate url lead { name } teams { nodes { key } } } } }",
                              {"n": limit, "f": {"name": {"containsIgnoreCase": query}} if query else None})
        return [H.compact({"id": p.get("id"), "name": p.get("name"), "state": p.get("state"),
                           "progress": round(float(p.get("progress") or 0) * 100), "target": p.get("targetDate"),
                           "lead": (p.get("lead") or {}).get("name"),
                           "teams": [t.get("key") for t in (p.get("teams") or {}).get("nodes") or []] or None,
                           "url": p.get("url")}) for p in (data.get("projects") or {}).get("nodes") or []]

    async def _project_id(self, ref: str) -> str:
        if re.fullmatch(r"[0-9a-f-]{36}", ref.strip()):
            return ref.strip()
        rows = await self.list_projects(ref.strip(), 5)
        exact = [p for p in rows if p["name"].lower() == ref.strip().lower()] or rows
        if not exact:
            raise ToolError(f"No Linear project named '{ref}'.")
        return exact[0]["id"]

    async def get_project(self, project):
        data = await self.gql("query($id: String!) { project(id: $id) { id name description state progress startDate targetDate url lead { name } teams { nodes { key } } issues(first: 50) { nodes { state { type } } } } }",
                              {"id": await self._project_id(project)})
        p = data.get("project") or {}
        counts: dict[str, int] = {}
        for i in (p.get("issues") or {}).get("nodes") or []:
            k = (i.get("state") or {}).get("type") or "unknown"
            counts[k] = counts.get(k, 0) + 1
        return H.compact({"id": p.get("id"), "name": p.get("name"), "description": H.clip(p.get("description"), 3000),
                          "state": p.get("state"), "progress": round(float(p.get("progress") or 0) * 100),
                          "start": p.get("startDate"), "target": p.get("targetDate"), "lead": (p.get("lead") or {}).get("name"),
                          "teams": [t.get("key") for t in (p.get("teams") or {}).get("nodes") or []] or None,
                          "issue_states": counts or None, "url": p.get("url")})

    async def get_task(self, task_id):
        i = await self._issue(task_id, LINEAR_ISSUE + " description createdAt estimate parent { identifier title } "
                                                      "children { nodes { identifier title state { name } } } creator { name }")
        out = self.norm(i)
        out.update(H.compact({
            "description": H.clip(i.get("description"), 6000), "created": i.get("createdAt"), "estimate": i.get("estimate"),
            "creator": (i.get("creator") or {}).get("name"), "parent": (i.get("parent") or {}).get("identifier"),
            "subtasks": [{"id": c.get("identifier"), "title": c.get("title"), "status": (c.get("state") or {}).get("name")}
                         for c in (i.get("children") or {}).get("nodes") or []] or None}))
        return out

    async def _label_ids(self, names: list[str], team_id: str) -> list[str]:
        if not names:
            return []
        data = await self.gql("query($n: [String!]) { issueLabels(filter: {name: {in: $n}}, first: 50) { nodes { id name team { id } } } }",
                              {"n": names})
        nodes = [n for n in (data.get("issueLabels") or {}).get("nodes") or []
                 if not n.get("team") or (n.get("team") or {}).get("id") == team_id]
        return [n["id"] for n in nodes]

    async def _state_id(self, team_id: str, *, name: str | None = None, done: bool = False) -> str:
        data = await self.gql("query($t: ID!) { workflowStates(filter: {team: {id: {eq: $t}}}, first: 50) { nodes { id name type } } }",
                              {"t": team_id})
        states = (data.get("workflowStates") or {}).get("nodes") or []
        for s in states:
            if (done and s.get("type") == "completed") or (name and (s.get("name") or "").lower() == name.lower()):
                return s["id"]
        raise ToolError(f"No workflow state '{name or 'completed'}'. Options: {', '.join(s.get('name') for s in states)}.")

    async def create_task(self, *, project, title, description, assignee, priority, labels, due, task_type, parent):
        team_id, project_id = await self._team(project)
        inp: dict[str, Any] = {"teamId": team_id, "title": title}
        if project_id:
            inp["projectId"] = project_id
        if description:
            inp["description"] = description
        if assignee:
            inp["assigneeId"] = await self._user_id(assignee)
        if priority:
            p = LINEAR_PRIORITY.get(str(priority).strip().lower())
            if p is None:
                raise ToolError("priority must be urgent, high, medium, low or none.")
            inp["priority"] = p
        if labels:
            inp["labelIds"] = await self._label_ids(labels, team_id)
        if due:
            inp["dueDate"] = due
        if parent:
            inp["parentId"] = (await self._issue(parent))["id"]
        data = await self.gql(f"mutation($i: IssueCreateInput!) {{ issueCreate(input: $i) {{ success issue {{ {LINEAR_ISSUE} }} }} }}",
                              {"i": inp})
        issue = (data.get("issueCreate") or {}).get("issue") or {}
        return {"created": True, **self.norm(issue)}

    async def _update(self, task_id: str, inp: dict) -> dict:
        issue = await self._issue(task_id)
        data = await self.gql(f"mutation($id: String!, $i: IssueUpdateInput!) {{ issueUpdate(id: $id, input: $i) {{ success issue {{ {LINEAR_ISSUE} }} }} }}",
                              {"id": issue["id"], "i": inp})
        return self.norm((data.get("issueUpdate") or {}).get("issue") or {})

    async def update_task(self, task_id, *, title, description, status, priority, labels, due):
        issue = await self._issue(task_id)
        team_id = (issue.get("team") or {}).get("id")
        inp: dict[str, Any] = {}
        if title is not None:
            inp["title"] = title
        if description is not None:
            inp["description"] = description
        if status:
            inp["stateId"] = await self._state_id(team_id, name=status, done=status.lower() in _DONE_WORDS)
        if priority:
            inp["priority"] = LINEAR_PRIORITY.get(str(priority).strip().lower(), 0)
        if labels is not None:
            inp["labelIds"] = await self._label_ids(labels, team_id)
        if due is not None:
            inp["dueDate"] = due or None
        if not inp:
            raise ToolError("Nothing to update.")
        return {"updated": True, "changed": list(inp), **await self._update(task_id, inp)}

    async def complete_task(self, task_id):
        issue = await self._issue(task_id)
        state = await self._state_id((issue.get("team") or {}).get("id"), done=True)
        return {"completed": True, **await self._update(task_id, {"stateId": state})}

    async def assign_task(self, task_id, assignee):
        uid = await self._user_id(assignee)
        return {"assigned": uid is not None, **await self._update(task_id, {"assigneeId": uid})}

    async def create_project(self, *, name, key, description, team, project_type):
        team_id, _ = await self._team(team)
        data = await self.gql("mutation($i: ProjectCreateInput!) { projectCreate(input: $i) { success project { id name url } } }",
                              {"i": H.compact({"name": name, "description": description, "teamIds": [team_id]})})
        p = (data.get("projectCreate") or {}).get("project") or {}
        return {"created": True, "id": p.get("id"), "name": p.get("name"), "url": p.get("url")}

    async def update_project(self, project, *, name, description, lead):
        inp = H.compact({"name": name, "description": description, "leadId": await self._user_id(lead) if lead else None})
        if not inp:
            raise ToolError("Nothing to update.")
        data = await self.gql("mutation($id: String!, $i: ProjectUpdateInput!) { projectUpdate(id: $id, input: $i) { success project { id name url } } }",
                              {"id": await self._project_id(project), "i": inp})
        p = (data.get("projectUpdate") or {}).get("project") or {}
        return {"updated": True, "id": p.get("id"), "name": p.get("name"), "changed": list(inp)}

    async def comments(self, task_id, limit):
        i = await self._issue(task_id, f"comments(first: {int(limit)}) {{ nodes {{ id body createdAt user {{ name }} }} }}")
        return [{"id": c.get("id"), "author": (c.get("user") or {}).get("name"), "created": c.get("createdAt"),
                 "body": H.clip(c.get("body"), 2000)} for c in (i.get("comments") or {}).get("nodes") or []]

    async def add_comment(self, task_id, body):
        issue = await self._issue(task_id)
        data = await self.gql("mutation($i: CommentCreateInput!) { commentCreate(input: $i) { success comment { id url } } }",
                              {"i": {"issueId": issue["id"], "body": body}})
        c = (data.get("commentCreate") or {}).get("comment") or {}
        return {"commented": True, "id": c.get("id"), "url": c.get("url"), "task": task_id}

    async def attachments(self, task_id):
        i = await self._issue(task_id, "attachments { nodes { id title subtitle url createdAt } }")
        return [H.compact({"id": a.get("id"), "title": a.get("title"), "subtitle": a.get("subtitle"),
                           "url": a.get("url"), "created": a.get("createdAt")})
                for a in (i.get("attachments") or {}).get("nodes") or []]


# --------------------------------------------------------------------------- Asana
ASANA = "https://app.asana.com/api/1.0"
ASANA_TASK_FIELDS = "name,completed,assignee.name,due_on,projects.name,permalink_url,modified_at,memberships.section.name,tags.name"


class AsanaAdapter(PMAdapter):
    kind = "asana"
    label = "Asana"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        self.api = H.RestClient("Asana", ASANA, headers={**bearer(cfg, "token", service="Asana"),
                                                         "Accept": "application/json"})
        self._ws: str | None = str(cfg.get("workspace_gid") or "").strip() or None

    async def _data(self, method: str, path: str, **kw: Any) -> Any:
        res = await self.api.request(method, path, **kw)
        return (res or {}).get("data") if isinstance(res, dict) else res

    async def ws(self) -> str:
        if not self._ws:
            rows = await self._data("GET", "/workspaces", params={"limit": 10}) or []
            if not rows:
                raise ToolError("No Asana workspace is available to this token.")
            self._ws = rows[0]["gid"]
        return self._ws

    @staticmethod
    def norm(t: dict) -> dict:
        section = next(((m.get("section") or {}).get("name") for m in t.get("memberships") or [] if m.get("section")), None)
        return H.compact({
            "id": t.get("gid"), "title": t.get("name"),
            "status": "completed" if t.get("completed") else (section or "open"),
            "assignee": (t.get("assignee") or {}).get("name"), "due": t.get("due_on"),
            "project": ", ".join(p.get("name", "") for p in t.get("projects") or []) or None,
            "tags": [x.get("name") for x in t.get("tags") or []] or None, "updated": t.get("modified_at"),
            "url": t.get("permalink_url"),
        })

    async def _project_gid(self, ref: str) -> str:
        r = ref.strip()
        if r.isdigit():
            return r
        for p in await self.list_projects(r, 100):
            if p["name"].lower() == r.lower():
                return p["id"]
        raise ToolError(f"No Asana project named '{ref}'.")

    async def search(self, query, project, status, assignee, limit):
        q = (query or "").strip()
        if project:
            rows = await self._data("GET", f"/projects/{await self._project_gid(project)}/tasks", params={
                "opt_fields": ASANA_TASK_FIELDS, "limit": 100,
                "completed_since": "now" if (status or "open").lower() not in _DONE_WORDS | {"all"} else None}) or []
            tasks = [self.norm(t) for t in rows]
            if q:
                tasks = [t for t in tasks if all(w in (t.get("title") or "").lower() for w in q.lower().split())]
            if status and status.lower() not in {"open", "all"} | _DONE_WORDS:
                tasks = [t for t in tasks if (t.get("status") or "").lower() == status.lower()]
            return {"tasks": tasks[:limit]}
        ws = await self.ws()
        params = {"text": q or None, "opt_fields": ASANA_TASK_FIELDS, "limit": limit, "sort_by": "modified_at",
                  "completed": False if (status or "open").lower() not in _DONE_WORDS | {"all"} else None,
                  "assignee.any": "me" if (assignee or "").strip().lower() == "me" else (assignee or None)}
        try:
            rows = await self._data("GET", f"/workspaces/{ws}/tasks/search", params=params) or []
            return {"tasks": [self.norm(t) for t in rows]}
        except H.ApiError as e:
            if e.status not in (402, 403):
                raise
        if not q:
            raise ToolError("Asana task search across a workspace needs a paid plan — pass `project` or a search query.")
        rows = await self._data("GET", f"/workspaces/{ws}/typeahead", params={
            "resource_type": "task", "query": q, "count": limit, "opt_fields": ASANA_TASK_FIELDS}) or []
        return {"tasks": [self.norm(t) for t in rows], "note": "Basic name search (advanced search needs Asana Premium)."}

    async def list_projects(self, query, limit):
        rows = await self._data("GET", "/projects", params={
            "workspace": await self.ws(), "archived": False, "limit": 100,
            "opt_fields": "name,owner.name,due_on,permalink_url,team.name,current_status_update.title"}) or []
        out = [H.compact({"id": p.get("gid"), "name": p.get("name"), "owner": (p.get("owner") or {}).get("name"),
                          "team": (p.get("team") or {}).get("name"), "due": p.get("due_on"),
                          "status": (p.get("current_status_update") or {}).get("title"), "url": p.get("permalink_url")})
               for p in rows]
        if query:
            out = [p for p in out if query.lower() in (p.get("name") or "").lower()]
        return out[:limit]

    async def get_project(self, project):
        p = await self._data("GET", f"/projects/{await self._project_gid(project)}", params={
            "opt_fields": "name,notes,owner.name,due_on,start_on,permalink_url,archived,team.name,members.name,"
                          "current_status_update.title,current_status_update.text"}) or {}
        return H.compact({"id": p.get("gid"), "name": p.get("name"), "description": H.clip(p.get("notes"), 3000),
                          "owner": (p.get("owner") or {}).get("name"), "team": (p.get("team") or {}).get("name"),
                          "start": p.get("start_on"), "due": p.get("due_on"), "archived": p.get("archived") or None,
                          "members": [m.get("name") for m in p.get("members") or []][:30] or None,
                          "status": (p.get("current_status_update") or {}).get("title"),
                          "status_text": H.clip((p.get("current_status_update") or {}).get("text"), 1000),
                          "url": p.get("permalink_url")})

    async def get_task(self, task_id):
        t = await self._data("GET", f"/tasks/{_q(task_id)}", params={
            "opt_fields": ASANA_TASK_FIELDS + ",notes,created_at,completed_at,parent.name,num_subtasks,"
                                              "custom_fields.name,custom_fields.display_value"}) or {}
        out = self.norm(t)
        out.update(H.compact({
            "description": H.clip(t.get("notes"), 6000), "created": t.get("created_at"),
            "completed_at": t.get("completed_at"), "parent": (t.get("parent") or {}).get("name"),
            "subtasks": t.get("num_subtasks") or None,
            "fields": {f["name"]: f.get("display_value") for f in t.get("custom_fields") or []
                       if f.get("name") and f.get("display_value")} or None}))
        return out

    async def create_task(self, *, project, title, description, assignee, priority, labels, due, task_type, parent):
        data: dict[str, Any] = {"name": title}
        if description:
            data["notes"] = description
        if assignee:
            data["assignee"] = assignee if assignee.lower() != "none" else None
        if due:
            data["due_on"] = due
        proj = project or self.cfg.get("default_project")
        if parent:
            t = await self._data("POST", f"/tasks/{_q(parent)}/subtasks", json={"data": data}, params={"opt_fields": ASANA_TASK_FIELDS})
        else:
            if proj:
                data["projects"] = [await self._project_gid(str(proj))]
            else:
                data["workspace"] = await self.ws()
            t = await self._data("POST", "/tasks", json={"data": data}, params={"opt_fields": ASANA_TASK_FIELDS})
        out = {"created": True, **self.norm(t or {})}
        if labels:
            out["note"] = "Asana tags weren't applied (add them in Asana if needed)."
        if priority:
            out["note"] = (out.get("note", "") + " Priority is a custom field in Asana and wasn't set.").strip()
        return out

    async def update_task(self, task_id, *, title, description, status, priority, labels, due):
        data: dict[str, Any] = {}
        if title is not None:
            data["name"] = title
        if description is not None:
            data["notes"] = description
        if due is not None:
            data["due_on"] = due or None
        moved = None
        if status:
            s = status.strip().lower()
            if s in _DONE_WORDS:
                data["completed"] = True
            elif s in ("open", "incomplete", "todo", "to do", "reopen", "reopened"):
                data["completed"] = False
            else:
                t = await self._data("GET", f"/tasks/{_q(task_id)}", params={"opt_fields": "projects.gid"}) or {}
                for p in t.get("projects") or []:
                    sections = await self._data("GET", f"/projects/{p['gid']}/sections", params={"opt_fields": "name"}) or []
                    match = [x for x in sections if (x.get("name") or "").lower() == s]
                    if match:
                        await self._data("POST", f"/sections/{match[0]['gid']}/addTask", json={"data": {"task": task_id}})
                        moved = match[0].get("name")
                        break
                if moved is None:
                    raise ToolError(f"No section named '{status}' in the task's project(s).")
        if not data and moved is None:
            raise ToolError("Nothing to update.")
        t = await self._data("PUT", f"/tasks/{_q(task_id)}", json={"data": data},
                             params={"opt_fields": ASANA_TASK_FIELDS}) if data else None
        out = {"updated": True, "changed": list(data) + (["section"] if moved else []), **(self.norm(t) if t else {"id": task_id})}
        if moved:
            out["section"] = moved
        return out

    async def complete_task(self, task_id):
        t = await self._data("PUT", f"/tasks/{_q(task_id)}", json={"data": {"completed": True}},
                             params={"opt_fields": ASANA_TASK_FIELDS})
        return {"completed": True, **self.norm(t or {})}

    async def assign_task(self, task_id, assignee):
        who = None if assignee.strip().lower() in ("none", "unassigned", "null") else assignee.strip()
        t = await self._data("PUT", f"/tasks/{_q(task_id)}", json={"data": {"assignee": who}},
                             params={"opt_fields": ASANA_TASK_FIELDS})
        return {"assigned": who is not None, **self.norm(t or {})}

    async def create_project(self, *, name, key, description, team, project_type):
        data: dict[str, Any] = {"name": name, "workspace": await self.ws()}
        if description:
            data["notes"] = description
        team_ref = team or self.cfg.get("team_gid")
        if team_ref:
            if not str(team_ref).isdigit():
                teams = await self._data("GET", f"/organizations/{await self.ws()}/teams", params={"opt_fields": "name"}) or []
                match = [t for t in teams if (t.get("name") or "").lower() == str(team_ref).lower()]
                if not match:
                    raise ToolError(f"No Asana team named '{team_ref}'.")
                team_ref = match[0]["gid"]
            data["team"] = str(team_ref)
        p = await self._data("POST", "/projects", json={"data": data}, params={"opt_fields": "name,permalink_url"}) or {}
        return {"created": True, "id": p.get("gid"), "name": p.get("name"), "url": p.get("permalink_url")}

    async def update_project(self, project, *, name, description, lead):
        data = H.compact({"name": name, "notes": description, "owner": lead})
        if not data:
            raise ToolError("Nothing to update.")
        p = await self._data("PUT", f"/projects/{await self._project_gid(project)}", json={"data": data},
                             params={"opt_fields": "name,permalink_url"}) or {}
        return {"updated": True, "id": p.get("gid"), "name": p.get("name"), "changed": list(data)}

    async def comments(self, task_id, limit):
        rows = await self._data("GET", f"/tasks/{_q(task_id)}/stories", params={
            "opt_fields": "type,resource_subtype,text,created_by.name,created_at", "limit": 100}) or []
        rows = [s for s in rows if s.get("type") == "comment" or s.get("resource_subtype") == "comment_added"]
        return [{"id": s.get("gid"), "author": (s.get("created_by") or {}).get("name"), "created": s.get("created_at"),
                 "body": H.clip(s.get("text"), 2000)} for s in rows[-limit:]]

    async def add_comment(self, task_id, body):
        s = await self._data("POST", f"/tasks/{_q(task_id)}/stories", json={"data": {"text": body}}) or {}
        return {"commented": True, "id": s.get("gid"), "task": task_id}

    async def attachments(self, task_id):
        rows = await self._data("GET", "/attachments", params={
            "parent": task_id, "opt_fields": "name,size,created_at,download_url,permanent_url,host"}) or []
        return [H.compact({"id": a.get("gid"), "filename": a.get("name"), "size": a.get("size"),
                           "created": a.get("created_at"), "host": a.get("host"),
                           "url": a.get("permanent_url") or a.get("download_url")}) for a in rows]


ADAPTERS: dict[str, type[PMAdapter]] = {"jira": JiraAdapter, "linear": LinearAdapter, "asana": AsanaAdapter}


async def pm_adapter(ctx: ToolContext, account: str | None = None) -> Any:
    kind, cfg = await H.pick_connector(ctx, PM_KINDS, account, what="Project management")
    return ADAPTERS[kind](ctx, cfg)


# =========================================================================== tools
group = ToolGroup(
    "pm", "Project management",
    "Find, create and update tasks and projects in Jira, Linear or Asana; comment, assign and complete work.",
    prefix="pm", icon="kanban", category="business", connector_kinds=PM_KINDS,
)


def _lim(n: int, hi: int = 100) -> int:
    return max(1, min(int(n or 20), hi))


def _due(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    if not v:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    d = H.parse_datetime(v)
    if d is None:
        raise ToolError("due must be a date like 2026-10-31.")
    return d.date().isoformat()


@group.tool(risk=Risk.LOW, scope="read")
async def search(ctx: ToolContext, query: str = "", project: str | None = None, status: str | None = None,
                 assignee: str | None = None, limit: int = 20, account: str | None = None) -> dict:
    """Search tasks/issues by text and filters, most recently updated first.

    Args:
        query: Search words (Jira also accepts raw JQL, e.g. "project = ENG AND status = 'In Progress'").
        project: Project key/name (Jira key like "ENG", Linear team key or project name, Asana project name/gid).
        status: Status name, e.g. "In Progress", or "open"/"done".
        assignee: "me", an email or a name.
        limit: Maximum results.
        account: PM connector name or kind (jira, linear, asana).
    """
    ad = await pm_adapter(ctx, account)
    res = await ad.search(query, project, status, assignee, _lim(limit))
    return {"tool": ad.label, "count": len(res.get("tasks") or []), **res}


@group.tool(risk=Risk.LOW, scope="read")
async def list_projects(ctx: ToolContext, query: str | None = None, limit: int = 50, account: str | None = None) -> dict:
    """List projects (optionally filtered by name).

    Args:
        query: Words in the project name.
        limit: Maximum projects.
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    rows = await ad.list_projects(query, _lim(limit))
    return {"tool": ad.label, "count": len(rows), "projects": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def get_project(ctx: ToolContext, project: str, account: str | None = None) -> dict:
    """Get a project's details (description, lead, status/progress).

    Args:
        project: Project key, id or name.
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    return {"tool": ad.label, **await ad.get_project(project)}


@group.tool(risk=Risk.LOW, scope="read")
async def get_task(ctx: ToolContext, task_id: str, account: str | None = None) -> dict:
    """Get a task/issue with its description, status, assignee, subtasks and counts.

    Args:
        task_id: Task id/key, e.g. "ENG-123" (Jira/Linear) or an Asana task gid.
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    return {"tool": ad.label, **await ad.get_task(task_id)}


def _task_preview(title: str):
    def render(a: dict) -> str:
        return H.preview(title, [("Tool", a.get("account") or "default"), ("Task", a.get("task_id")),
                                 ("Project", a.get("project")), ("Title", a.get("title")), ("Status", a.get("status")),
                                 ("Assignee", a.get("assignee")), ("Priority", a.get("priority")),
                                 ("Due", a.get("due")), ("Labels", H.as_list(a.get("labels")))],
                         a.get("description") if a.get("description") is not None else None, body_label="Description")
    return render


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("title", "description"), preview=_task_preview("Create task"))
async def create_task(ctx: ToolContext, title: str, project: str | None = None, description: str | None = None,
                      assignee: str | None = None, priority: str | None = None, labels: list[str] | None = None,
                      due: str | None = None, task_type: str | None = None, parent: str | None = None,
                      account: str | None = None) -> dict:
    """Create a task/issue.

    Args:
        title: Task title.
        project: Jira project key, Linear team key/project name, or Asana project name/gid (default from connector).
        description: Details (Markdown; converted to Jira's document format).
        assignee: "me", an email or a name.
        priority: e.g. High, Medium, Low (Linear: urgent/high/medium/low).
        labels: Label names.
        due: Due date YYYY-MM-DD.
        task_type: Jira issue type (default Task, or Sub-task with parent).
        parent: Parent task id to create a subtask.
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    res = await ad.create_task(project=project, title=title, description=description, assignee=assignee,
                               priority=priority, labels=H.as_list(labels) or None, due=_due(due) or None,
                               task_type=task_type, parent=parent)
    return {"tool": ad.label, **res}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("title", "description"), preview=_task_preview("Update task"))
async def update_task(ctx: ToolContext, task_id: str, title: str | None = None, description: str | None = None,
                      status: str | None = None, priority: str | None = None, labels: list[str] | None = None,
                      due: str | None = None, account: str | None = None) -> dict:
    """Update a task's title, description, status (workflow transition), priority, labels or due date.

    Args:
        task_id: Task id/key.
        title: New title.
        description: New description (replaces the old one).
        status: Target status name, e.g. "In Progress" or "Done".
        priority: New priority.
        labels: Replace labels with these.
        due: New due date YYYY-MM-DD ("" clears it).
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    res = await ad.update_task(task_id, title=title, description=description, status=status, priority=priority,
                               labels=H.as_list(labels) if labels is not None else None, due=_due(due))
    return {"tool": ad.label, **res}


@group.tool(risk=Risk.MEDIUM, scope="write")
async def complete_task(ctx: ToolContext, task_id: str, account: str | None = None) -> dict:
    """Mark a task as done (moves it to the workflow's completed status).

    Args:
        task_id: Task id/key.
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    return {"tool": ad.label, **await ad.complete_task(task_id)}


@group.tool(risk=Risk.MEDIUM, scope="write")
async def assign_task(ctx: ToolContext, task_id: str, assignee: str, account: str | None = None) -> dict:
    """Assign a task to someone ("none" to unassign).

    Args:
        task_id: Task id/key.
        assignee: "me", an email, a name, or "none".
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    return {"tool": ad.label, **await ad.assign_task(task_id, assignee)}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("name", "description"),
            preview=lambda a: H.preview("Create project", [
                ("Tool", a.get("account") or "default"), ("Name", a.get("name")), ("Key", a.get("key")),
                ("Team", a.get("team")), ("Type", a.get("project_type"))],
                a.get("description") if a.get("description") else None, body_label="Description"))
async def create_project(ctx: ToolContext, name: str, key: str | None = None, description: str | None = None,
                         team: str | None = None, project_type: str | None = None, account: str | None = None) -> dict:
    """Create a new project (Jira needs project-admin rights).

    Args:
        name: Project name.
        key: Jira project key (2-10 uppercase letters; derived from the name if omitted).
        description: Project description.
        team: Linear team key / Asana team name (required for Asana organizations).
        project_type: Jira: software or business.
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    return {"tool": ad.label, **await ad.create_project(name=name, key=key, description=description, team=team,
                                                         project_type=project_type)}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("name", "description"))
async def update_project(ctx: ToolContext, project: str, name: str | None = None, description: str | None = None,
                         lead: str | None = None, account: str | None = None) -> dict:
    """Update a project's name, description or lead/owner.

    Args:
        project: Project key, id or name.
        name: New name.
        description: New description.
        lead: New lead/owner (email or name).
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    return {"tool": ad.label, **await ad.update_project(project, name=name, description=description, lead=lead)}


@group.tool(risk=Risk.LOW, scope="read")
async def comments(ctx: ToolContext, task_id: str, limit: int = 20, account: str | None = None) -> dict:
    """List a task's comments (latest first for Jira; chronological otherwise).

    Args:
        task_id: Task id/key.
        limit: Maximum comments.
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    rows = await ad.comments(task_id, _lim(limit))
    return {"tool": ad.label, "task": task_id, "count": len(rows), "comments": rows}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("body",),
            preview=lambda a: H.preview("Comment on task", [("Tool", a.get("account") or "default"),
                                                            ("Task", a.get("task_id"))], a.get("body", ""),
                                        body_label="Comment"))
async def add_comment(ctx: ToolContext, task_id: str, body: str, account: str | None = None) -> dict:
    """Add a comment to a task.

    Args:
        task_id: Task id/key.
        body: Comment text (Markdown).
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    return {"tool": ad.label, **await ad.add_comment(task_id, body)}


@group.tool(risk=Risk.LOW, scope="read")
async def attachments(ctx: ToolContext, task_id: str, account: str | None = None) -> dict:
    """List a task's attachments (name, size, link).

    Args:
        task_id: Task id/key.
        account: PM connector name or kind.
    """
    ad = await pm_adapter(ctx, account)
    rows = await ad.attachments(task_id)
    return {"tool": ad.label, "task": task_id, "count": len(rows), "attachments": rows}


# =========================================================================== connector kinds
@H.connector_test
async def _test_jira(cfg: dict) -> tuple[bool, str]:
    ad = JiraAdapter(ToolContext(org_id="_test", user_id=None), cfg)
    me = await ad.api.get("/myself")
    return True, f"Connected to Jira ({ad.site}) as {me.get('displayName') or me.get('emailAddress')}."


@H.connector_test
async def _test_linear(cfg: dict) -> tuple[bool, str]:
    ad = LinearAdapter(ToolContext(org_id="_test", user_id=None), cfg)
    data = await ad.gql("{ viewer { name email } organization { name } }")
    return True, (f"Connected to Linear workspace '{(data.get('organization') or {}).get('name')}' as "
                  f"{(data.get('viewer') or {}).get('name')}.")


@H.connector_test
async def _test_asana(cfg: dict) -> tuple[bool, str]:
    ad = AsanaAdapter(ToolContext(org_id="_test", user_id=None), cfg)
    me = await ad._data("GET", "/users/me", params={"opt_fields": "name,email,workspaces.name"}) or {}
    ws = ", ".join(w.get("name", "") for w in me.get("workspaces") or [])
    return True, f"Connected to Asana as {me.get('name')} (workspaces: {ws or 'none'})."


register_connector_kind(ConnectorKindSpec(
    kind="jira", name="Jira", description="Jira Cloud issues, projects, transitions and comments.",
    icon="kanban", category="business", tool_groups=["pm"],
    fields=[
        ConnectorField("site_url", "Site URL", type="url", required=True, placeholder="https://your-company.atlassian.net"),
        ConnectorField("email", "Account email", required=True, placeholder="you@company.com"),
        ConnectorField("api_token", "API token", type="password", required=True, secret=True,
                       help="Create at id.atlassian.com → Security → API tokens."),
        ConnectorField("default_project", "Default project key (optional)", placeholder="ENG"),
    ],
    docs_url="https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/", test=_test_jira,
))

register_connector_kind(ConnectorKindSpec(
    kind="linear", name="Linear", description="Linear issues, projects, workflow states and comments.",
    icon="kanban", category="business", tool_groups=["pm"],
    fields=[
        ConnectorField("api_key", "API key", type="password", required=True, secret=True, placeholder="lin_api_…",
                       help="Linear → Settings → Security & access → Personal API keys."),
        ConnectorField("default_team", "Default team key (optional)", placeholder="ENG"),
    ],
    docs_url="https://developers.linear.app/docs/graphql/working-with-the-graphql-api", test=_test_linear,
))

register_connector_kind(ConnectorKindSpec(
    kind="asana", name="Asana", description="Asana tasks, projects, sections and comments.",
    icon="kanban", category="business", tool_groups=["pm"],
    fields=[
        ConnectorField("token", "Personal access token", type="password", required=True, secret=True,
                       help="Asana → My settings → Apps → Developer apps → Personal access token."),
        ConnectorField("workspace_gid", "Workspace GID (optional)", placeholder="1200000000000000",
                       help="Defaults to the token's first workspace."),
        ConnectorField("team_gid", "Default team GID (optional)", help="Needed to create projects in organizations."),
        ConnectorField("default_project", "Default project (optional)", placeholder="Marketing launches"),
    ],
    docs_url="https://developers.asana.com/docs/personal-access-token", test=_test_asana,
))
