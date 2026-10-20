"""Knowledge-base tools over Notion (integration token; blocks ↔ markdown) and Confluence Cloud (REST v2 pages +
CQL search; storage XHTML ↔ markdown). Notion databases can be queried, created and updated.
"""
from __future__ import annotations

import html as htmllib
import re
from typing import Any
from urllib.parse import quote

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin import _http as H
from app.tools.builtin._oauth import bearer
from app.tools.builtin.pm import site_url
from app.tools.connectors import ConnectorField, ConnectorKindSpec, register_connector_kind

KB_KINDS = ("notion", "confluence")
NOTION = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_BLOCKS_READ = 400
NOTION_CODE_LANGS = {
    "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript", "c++", "c#", "css", "dart", "diff", "docker",
    "elixir", "elm", "erlang", "flow", "fortran", "f#", "gherkin", "glsl", "go", "graphql", "groovy", "haskell", "html",
    "java", "javascript", "json", "julia", "kotlin", "latex", "less", "lisp", "livescript", "lua", "makefile",
    "markdown", "markup", "matlab", "mermaid", "nix", "objective-c", "ocaml", "pascal", "perl", "php", "plain text",
    "powershell", "prolog", "protobuf", "python", "r", "reason", "ruby", "rust", "sass", "scala", "scheme", "scss",
    "shell", "sql", "swift", "typescript", "vb.net", "verilog", "vhdl", "visual basic", "webassembly", "xml", "yaml",
}
_LANG_ALIASES = {"py": "python", "js": "javascript", "ts": "typescript", "sh": "shell", "yml": "yaml", "md": "markdown",
                 "cpp": "c++", "cs": "c#", "csharp": "c#", "ps1": "powershell", "rb": "ruby", "text": "plain text",
                 "txt": "plain text", "": "plain text"}
_INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\([^)\s]+\)|(?<![*\w])\*[^*\s][^*\n]*\*(?![*\w])|~~[^~\n]+~~)")


def _q(v: str) -> str:
    return quote(str(v), safe="")


def notion_id(ref: str) -> str:
    """Accept a raw id, a dashed UUID or a notion.so URL."""
    r = (ref or "").strip()
    m = re.search(r"([0-9a-f]{32})(?:[?#].*)?$", r.replace("-", "")) if "notion" in r else None
    if m:
        return m.group(1)
    compact = r.replace("-", "")
    if re.fullmatch(r"[0-9a-fA-F]{32}", compact):
        return compact
    raise ToolError(f"'{ref}' is not a Notion page/database id or URL.")


# =========================================================================== Notion rich text / blocks
def _rt_chunks(text: str, annotations: dict | None = None, link: str | None = None) -> list[dict]:
    out = []
    for i in range(0, max(len(text), 1), 2000):
        piece = text[i:i + 2000]
        if not piece:
            continue
        node: dict[str, Any] = {"type": "text", "text": {"content": piece}}
        if link:
            node["text"]["link"] = {"url": link}
        if annotations:
            node["annotations"] = annotations
        out.append(node)
    return out


def md_rich_text(text: str) -> list[dict]:
    out: list[dict] = []
    pos = 0
    for m in _INLINE.finditer(text or ""):
        if m.start() > pos:
            out += _rt_chunks(text[pos:m.start()])
        tok = m.group(0)
        if tok.startswith("**"):
            out += _rt_chunks(tok[2:-2], {"bold": True})
        elif tok.startswith("`"):
            out += _rt_chunks(tok[1:-1], {"code": True})
        elif tok.startswith("~~"):
            out += _rt_chunks(tok[2:-2], {"strikethrough": True})
        elif tok.startswith("["):
            label, url = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", tok).groups()  # type: ignore[union-attr]
            out += _rt_chunks(label, link=url if url.startswith(("http://", "https://")) else None)
        else:
            out += _rt_chunks(tok[1:-1], {"italic": True})
        pos = m.end()
    if pos < len(text or ""):
        out += _rt_chunks(text[pos:])
    return out


def rich_text_md(rts: list[dict] | None) -> str:
    parts = []
    for rt in rts or []:
        t = rt.get("plain_text") if "plain_text" in rt else ((rt.get("text") or {}).get("content") or "")
        a = rt.get("annotations") or {}
        if not t:
            continue
        if a.get("code"):
            t = f"`{t}`"
        if a.get("bold"):
            t = f"**{t}**"
        if a.get("italic"):
            t = f"*{t}*"
        if a.get("strikethrough"):
            t = f"~~{t}~~"
        href = rt.get("href") or ((rt.get("text") or {}).get("link") or {}).get("url")
        if href:
            t = f"[{t}]({href})"
        parts.append(t)
    return "".join(parts)


def _block(kind: str, text: str, **extra: Any) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": md_rich_text(text), **extra}}


def md_to_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    lines = (text or "").replace("\r\n", "\n").split("\n")
    i = 0
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(_block("paragraph", "\n".join(para)))
            para.clear()

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("```"):
            flush()
            lang = _LANG_ALIASES.get(s[3:].strip().lower(), s[3:].strip().lower())
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append({"object": "block", "type": "code", "code": {
                "rich_text": _rt_chunks("\n".join(code)) or [], "language": lang if lang in NOTION_CODE_LANGS else "plain text"}})
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            blocks.append(_block(f"heading_{min(len(m.group(1)), 3)}", m.group(2)))
        elif re.match(r"^\s*[-*+]\s+\[( |x|X)\]\s+", line):
            flush()
            mm = re.match(r"^\s*[-*+]\s+\[( |x|X)\]\s+(.*)$", line)
            blocks.append(_block("to_do", mm.group(2), checked=mm.group(1).lower() == "x"))  # type: ignore[union-attr]
        elif re.match(r"^\s*[-*+]\s+", line):
            flush()
            blocks.append(_block("bulleted_list_item", re.sub(r"^\s*[-*+]\s+", "", line)))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            flush()
            blocks.append(_block("numbered_list_item", re.sub(r"^\s*\d+[.)]\s+", "", line)))
        elif s.startswith(">"):
            flush()
            blocks.append(_block("quote", s[1:].strip()))
        elif re.match(r"^\s*(---+|\*\*\*+|___+)\s*$", line):
            flush()
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif not s:
            flush()
        else:
            para.append(line)
        i += 1
    flush()
    return blocks


def block_md(b: dict, depth: int = 0) -> str:
    t = b.get("type") or ""
    d = b.get(t) or {}
    ind = "  " * depth
    txt = rich_text_md(d.get("rich_text"))
    if t == "paragraph":
        line = txt
    elif t.startswith("heading_"):
        line = "#" * int(t[-1]) + " " + txt
    elif t == "bulleted_list_item":
        line = "- " + txt
    elif t == "numbered_list_item":
        line = "1. " + txt
    elif t == "to_do":
        line = ("- [x] " if d.get("checked") else "- [ ] ") + txt
    elif t == "toggle":
        line = "- ▸ " + txt
    elif t in ("quote", "callout"):
        icon = (d.get("icon") or {}).get("emoji") or ""
        line = "> " + (icon + " " if icon else "") + txt
    elif t == "code":
        line = f"```{d.get('language') or ''}\n{rich_text_md(d.get('rich_text')).strip('`')}\n```"
    elif t == "divider":
        line = "---"
    elif t == "child_page":
        line = f"📄 {d.get('title')} (page id: {b.get('id')})"
    elif t == "child_database":
        line = f"🗃 {d.get('title')} (database id: {b.get('id')})"
    elif t in ("image", "file", "pdf", "video", "audio"):
        url = (d.get("file") or d.get("external") or {}).get("url") or ""
        cap = rich_text_md(d.get("caption"))
        line = f"[{t}{': ' + cap if cap else ''}]({url})"
    elif t in ("bookmark", "embed", "link_preview"):
        line = d.get("url") or ""
    elif t == "equation":
        line = f"$${d.get('expression', '')}$$"
    elif t == "table_row":
        line = "| " + " | ".join(rich_text_md(c) for c in d.get("cells") or []) + " |"
    elif t in ("column_list", "column", "table", "synced_block"):
        line = ""
    else:
        line = txt
    out = (ind + line) if line else ""
    for child in b.get("_children") or []:
        sub = block_md(child, depth + (1 if t in ("bulleted_list_item", "numbered_list_item", "to_do", "toggle") else 0))
        if sub:
            out += ("\n" if out else "") + sub
    return out


# =========================================================================== storage XHTML (Confluence)
def md_to_storage(text: str) -> str:
    from markdown_it import MarkdownIt

    html = MarkdownIt("commonmark", {"xhtmlOut": True, "html": False}).enable("table").render(text or "")

    def code_macro(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = htmllib.unescape(m.group(2)).rstrip("\n").replace("]]>", "]]]]><![CDATA[>")
        param = f'<ac:parameter ac:name="language">{lang}</ac:parameter>' if lang else ""
        return (f'<ac:structured-macro ac:name="code">{param}<ac:plain-text-body><![CDATA[{code}]]>'
                "</ac:plain-text-body></ac:structured-macro>")

    return re.sub(r'<pre><code(?: class="language-([\w+#-]+)")?>(.*?)</code></pre>', code_macro, html, flags=re.S)


def storage_to_md(storage: str) -> str:
    """Confluence storage format → readable markdown (headings, lists, code macros, tables, links)."""
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(storage or "", "html.parser")

    def walk(node: Any, depth: int = 0) -> str:
        if isinstance(node, NavigableString):
            return str(node)
        if not isinstance(node, Tag):
            return ""
        name = node.name or ""
        kids = lambda: "".join(walk(c, depth) for c in node.children)  # noqa: E731
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            return f"\n{'#' * int(name[1])} {kids().strip()}\n"
        if name == "p":
            return f"\n{kids().strip()}\n"
        if name == "br":
            return "\n"
        if name in ("strong", "b"):
            return f"**{kids()}**"
        if name in ("em", "i"):
            return f"*{kids()}*"
        if name == "code":
            return f"`{kids()}`"
        if name == "a":
            href = node.get("href")
            text = kids().strip()
            return f"[{text}]({href})" if href and text else text
        if name in ("ul", "ol"):
            items = []
            for n, li in enumerate(node.find_all("li", recursive=False), 1):
                prefix = f"{n}. " if name == "ol" else "- "
                items.append("  " * depth + prefix + "".join(walk(c, depth + 1) for c in li.children).strip())
            return "\n" + "\n".join(items) + "\n"
        if name == "pre":
            return f"\n```\n{node.get_text()}\n```\n"
        if name == "ac:structured-macro":
            macro = node.get("ac:name")
            if macro == "code":
                body = node.find("ac:plain-text-body")
                lang = node.find("ac:parameter", attrs={"ac:name": "language"})
                return f"\n```{lang.get_text() if lang else ''}\n{body.get_text() if body else ''}\n```\n"
            rich = node.find("ac:rich-text-body")
            return walk(rich, depth) if rich else ""
        if name == "ac:task":
            done = (node.find("ac:task-status").get_text() if node.find("ac:task-status") else "") == "complete"
            body = node.find("ac:task-body")
            return f"\n- [{'x' if done else ' '}] {body.get_text().strip() if body else ''}"
        if name == "tr":
            return "\n| " + " | ".join(c.get_text(" ", strip=True) for c in node.find_all(["td", "th"], recursive=False)) + " |"
        if name in ("ac:parameter", "ri:attachment", "ri:page", "ac:image"):
            return ""
        return kids()

    md = walk(soup)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


# =========================================================================== adapters
class KBAdapter:
    kind = ""
    label = ""

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        self.ctx = ctx
        self.cfg = cfg


class NotionAdapter(KBAdapter):
    kind = "notion"
    label = "Notion"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        self.api = H.RestClient("Notion", NOTION, headers={
            **bearer(cfg, "token", service="Notion"),
            "Notion-Version": str(cfg.get("notion_version") or NOTION_VERSION)})

    @staticmethod
    def title_of(obj: dict) -> str:
        if obj.get("object") == "database":
            return rich_text_md(obj.get("title")) or "(untitled database)"
        for prop in (obj.get("properties") or {}).values():
            if prop.get("type") == "title":
                return "".join(rt.get("plain_text", "") for rt in prop.get("title") or []) or "(untitled)"
        return "(untitled)"

    def summary(self, obj: dict) -> dict:
        parent = obj.get("parent") or {}
        return H.compact({"id": obj.get("id"), "type": obj.get("object"), "title": self.title_of(obj),
                          "url": obj.get("url"), "edited": obj.get("last_edited_time"),
                          "parent": parent.get("page_id") or parent.get("database_id") or parent.get("type")})

    async def search(self, query, limit, kind, space):
        body: dict[str, Any] = {"query": query, "page_size": max(1, min(limit, 100)),
                                "sort": {"direction": "descending", "timestamp": "last_edited_time"}}
        if kind in ("page", "database"):
            body["filter"] = {"property": "object", "value": kind}
        data = await self.api.post("/search", json=body) or {}
        return [self.summary(o) for o in data.get("results", [])]

    async def _children(self, block_id: str, budget: list[int], depth: int = 0) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while budget[0] > 0:
            data = await self.api.get(f"/blocks/{_q(block_id)}/children",
                                      params={"page_size": 100, "start_cursor": cursor}) or {}
            for b in data.get("results", []):
                budget[0] -= 1
                if b.get("has_children") and depth < 2 and b.get("type") not in ("child_page", "child_database") and budget[0] > 0:
                    b["_children"] = await self._children(b["id"], budget, depth + 1)
                out.append(b)
                if budget[0] <= 0:
                    break
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                break
        return out

    async def get(self, ref):
        pid = notion_id(ref)
        try:
            page = await self.api.get(f"/pages/{pid}")
        except H.ApiError as e:
            if e.status not in (400, 404):
                raise
            db = await self.api.get(f"/databases/{pid}")
            schema = {name: p.get("type") for name, p in (db.get("properties") or {}).items()}
            return {**self.summary(db), "properties": schema,
                    "markdown": f"# {self.title_of(db)}\n\n{rich_text_md(db.get('description'))}".strip(),
                    "note": "This is a database — use kb_database_query to read its rows."}
        budget = [MAX_BLOCKS_READ]
        blocks = await self._children(pid, budget)
        md = "\n".join(x for x in (block_md(b) for b in blocks) if x is not None)
        props = {name: self.flatten(p) for name, p in (page.get("properties") or {}).items() if p.get("type") != "title"}
        out = {**self.summary(page), "properties": H.compact(props) or None,
               "markdown": f"# {self.title_of(page)}\n\n{md}".strip()}
        if budget[0] <= 0:
            out["note"] = f"Only the first {MAX_BLOCKS_READ} blocks were read."
        return out

    async def _append(self, page_id: str, blocks: list[dict]) -> int:
        for i in range(0, len(blocks), 100):
            await self.api.patch(f"/blocks/{_q(page_id)}/children", json={"children": blocks[i:i + 100]})
        return len(blocks)

    async def _schema(self, database_id: str) -> dict[str, dict]:
        db = await self.api.get(f"/databases/{notion_id(database_id)}")
        return db.get("properties") or {}

    async def create(self, title, content, parent_id, database_id, space):
        blocks = md_to_blocks(content or "")
        if database_id:
            schema = await self._schema(database_id)
            title_prop = next((n for n, p in schema.items() if p.get("type") == "title"), "Name")
            body: dict[str, Any] = {"parent": {"database_id": notion_id(database_id)},
                                    "properties": {title_prop: {"title": _rt_chunks(title)}}}
        else:
            parent = parent_id or self.cfg.get("default_parent_page_id")
            if not parent:
                raise ToolError("Notion needs a parent page: pass parent_id (a page shared with the integration) or set a "
                                "default parent page in the Notion connector.")
            body = {"parent": {"page_id": notion_id(parent)}, "properties": {"title": {"title": _rt_chunks(title)}}}
        body["children"] = blocks[:100]
        page = await self.api.post("/pages", json=body)
        if len(blocks) > 100:
            await self._append(page["id"], blocks[100:])
        return {"created": True, **self.summary(page), "blocks": len(blocks)}

    async def update(self, ref, title, content):
        pid = notion_id(ref)
        changed = []
        page = await self.api.get(f"/pages/{pid}")
        if title is not None:
            title_prop = next((n for n, p in (page.get("properties") or {}).items() if p.get("type") == "title"), "title")
            await self.api.patch(f"/pages/{pid}", json={"properties": {title_prop: {"title": _rt_chunks(title)}}})
            changed.append("title")
        if content is not None:
            existing = await self._children(pid, [200])
            for b in existing:
                if b.get("type") not in ("child_page", "child_database"):
                    await self.api.delete(f"/blocks/{_q(b['id'])}")
            await self._append(pid, md_to_blocks(content))
            changed.append("content")
        if not changed:
            raise ToolError("Nothing to update — pass title and/or content.")
        return {"updated": True, "id": pid, "url": page.get("url"), "changed": changed}

    async def append(self, ref, content):
        pid = notion_id(ref)
        n = await self._append(pid, md_to_blocks(content))
        return {"appended": True, "id": pid, "blocks": n}

    @staticmethod
    def flatten(p: dict) -> Any:
        t = p.get("type")
        v = p.get(t)
        if t in ("title", "rich_text"):
            return "".join(rt.get("plain_text", "") for rt in v or [])
        if t in ("select", "status"):
            return (v or {}).get("name")
        if t == "multi_select":
            return [o.get("name") for o in v or []]
        if t == "date":
            return None if not v else (v.get("start") + (f" → {v['end']}" if v.get("end") else ""))
        if t == "people":
            return [u.get("name") or u.get("id") for u in v or []]
        if t == "relation":
            return [r.get("id") for r in v or []]
        if t == "files":
            return [f.get("name") for f in v or []]
        if t == "formula":
            return (v or {}).get((v or {}).get("type"))
        if t == "rollup":
            r = v or {}
            return r.get(r.get("type")) if r.get("type") != "array" else len(r.get("array") or [])
        if t == "unique_id":
            return f"{(v or {}).get('prefix') or ''}-{(v or {}).get('number')}".lstrip("-")
        if t in ("created_by", "last_edited_by"):
            return (v or {}).get("name")
        return v

    @staticmethod
    def _match_filter(match: dict[str, Any], schema: dict[str, dict]) -> dict:
        clauses = []
        for name, value in match.items():
            prop = schema.get(name)
            if prop is None:
                raise ToolError(f"Unknown property '{name}'. Properties: {', '.join(schema)}.")
            t = prop.get("type")
            if t in ("title", "rich_text", "url", "email", "phone_number"):
                op = "contains" if t in ("title", "rich_text") else "equals"
                clauses.append({"property": name, t: {op: str(value)}})
            elif t in ("select", "status"):
                clauses.append({"property": name, t: {"equals": str(value)}})
            elif t == "multi_select":
                clauses.append({"property": name, t: {"contains": str(value)}})
            elif t == "checkbox":
                clauses.append({"property": name, t: {"equals": H.truthy(value)}})
            elif t == "number":
                clauses.append({"property": name, t: {"equals": float(value)}})
            elif t == "date":
                clauses.append({"property": name, t: {"equals": str(value)}})
            else:
                raise ToolError(f"Matching on '{name}' ({t}) isn't supported — pass a raw Notion `filter` instead.")
        return clauses[0] if len(clauses) == 1 else {"and": clauses}

    async def database_query(self, database_id, match, flt, sorts, limit):
        did = notion_id(database_id)
        body: dict[str, Any] = {"page_size": max(1, min(limit, 100))}
        if match:
            body["filter"] = self._match_filter(match, await self._schema(did))
        elif flt:
            body["filter"] = flt
        if sorts:
            body["sorts"] = sorts
        data = await self.api.post(f"/databases/{did}/query", json=body) or {}
        rows = []
        for page in data.get("results", []):
            props = {name: self.flatten(p) for name, p in (page.get("properties") or {}).items()}
            rows.append({"id": page.get("id"), "url": page.get("url"), **H.compact(props)})
        return {"database_id": did, "count": len(rows), "has_more": data.get("has_more") or None, "rows": rows}

    async def database_create(self, parent_page_id, title, properties):
        props: dict[str, Any] = {}
        title_set = False
        for name, spec in (properties or {}).items():
            kind, _, opts = str(spec).partition(":")
            kind = kind.strip().lower()
            if kind == "text":
                kind = "rich_text"
            allowed = {"title", "rich_text", "number", "select", "multi_select", "date", "people", "files", "checkbox",
                       "url", "email", "phone_number", "created_time", "last_edited_time"}
            if kind not in allowed:
                raise ToolError(f"Unsupported property type '{kind}' for '{name}'. Use: {', '.join(sorted(allowed))}.")
            if kind == "title":
                title_set = True
            conf: dict[str, Any] = {}
            if kind in ("select", "multi_select") and opts:
                conf = {"options": [{"name": o.strip()} for o in opts.split(",") if o.strip()]}
            props[name] = {kind: conf}
        if not title_set:
            props = {"Name": {"title": {}}, **props}
        db = await self.api.post("/databases", json={"parent": {"type": "page_id", "page_id": notion_id(parent_page_id)},
                                                     "title": _rt_chunks(title), "properties": props})
        return {"created": True, "id": db.get("id"), "title": title, "url": db.get("url"), "properties": list(props)}

    @staticmethod
    def _value(prop: dict, value: Any, name: str) -> dict:
        t = prop.get("type")
        if t in ("title", "rich_text"):
            return {t: _rt_chunks(str(value)) if value not in (None, "") else []}
        if t == "number":
            return {t: None if value in (None, "") else float(value)}
        if t in ("select", "status"):
            return {t: {"name": str(value)} if value not in (None, "") else None}
        if t == "multi_select":
            return {t: [{"name": str(v)} for v in H.as_list(value)]}
        if t == "date":
            return {t: {"start": str(value)} if value else None}
        if t == "checkbox":
            return {t: H.truthy(value)}
        if t in ("url", "email", "phone_number"):
            return {t: str(value) if value else None}
        if t == "relation":
            return {t: [{"id": notion_id(v)} for v in H.as_list(value)]}
        if t == "people":
            return {t: [{"id": v} for v in H.as_list(value)]}
        raise ToolError(f"Property '{name}' ({t}) can't be set through this tool.")

    async def database_update(self, database_id, values, row_id):
        schema = await self._schema(database_id)
        props = {}
        for name, value in (values or {}).items():
            prop = schema.get(name) or next((p for n, p in schema.items() if n.lower() == name.lower()), None)
            if prop is None:
                raise ToolError(f"Unknown property '{name}'. Properties: {', '.join(schema)}.")
            real = next(n for n, p in schema.items() if p is prop)
            props[real] = self._value(prop, value, real)
        if not props:
            raise ToolError("Pass values to set, e.g. {\"Status\": \"Done\"}.")
        if row_id:
            page = await self.api.patch(f"/pages/{notion_id(row_id)}", json={"properties": props})
            return {"updated": True, "row_id": page.get("id"), "url": page.get("url"), "changed": list(props)}
        page = await self.api.post("/pages", json={"parent": {"database_id": notion_id(database_id)}, "properties": props})
        return {"created": True, "row_id": page.get("id"), "url": page.get("url"), "set": list(props)}


class ConfluenceAdapter(KBAdapter):
    kind = "confluence"
    label = "Confluence"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        H.need(cfg, ("site_url", "email", "api_token"), "Confluence")
        self.site = site_url(cfg, "Confluence")
        self.api = H.RestClient("Confluence", f"{self.site}/wiki", basic=(str(cfg["email"]).strip(),
                                                                          str(cfg["api_token"]).strip()),
                                headers={"Accept": "application/json"})

    def link(self, webui: str | None) -> str | None:
        return f"{self.site}/wiki{webui}" if webui else None

    @staticmethod
    def page_id(ref: str) -> str:
        r = (ref or "").strip()
        m = re.search(r"/pages/(\d+)", r) or re.search(r"pageId=(\d+)", r)
        if m:
            return m.group(1)
        if r.isdigit():
            return r
        raise ToolError(f"'{ref}' is not a Confluence page id or URL.")

    async def search(self, query, limit, kind, space):
        esc = query.replace("\\", "\\\\").replace('"', '\\"')
        cql = f'type=page AND text ~ "{esc}"'
        if space or self.cfg.get("default_space"):
            cql += f' AND space = "{(space or self.cfg.get("default_space"))}"'
        data = await self.api.get("/rest/api/search", params={"cql": cql, "limit": max(1, min(limit, 50))}) or {}
        rows = []
        for r in data.get("results", []):
            c = r.get("content") or {}
            rows.append(H.compact({"id": c.get("id"), "type": c.get("type"), "title": c.get("title") or r.get("title"),
                                   "space": (r.get("resultGlobalContainer") or {}).get("title"),
                                   "excerpt": H.clip(re.sub(r"@@@(end)?hl@@@", "", r.get("excerpt") or ""), 300),
                                   "edited": r.get("lastModified"), "url": self.link((c.get("_links") or {}).get("webui")
                                                                                     or r.get("url"))}))
        return rows

    async def _page(self, pid: str) -> dict:
        return await self.api.get(f"/api/v2/pages/{pid}", params={"body-format": "storage"})

    async def get(self, ref):
        p = await self._page(self.page_id(ref))
        storage = ((p.get("body") or {}).get("storage") or {}).get("value") or ""
        return H.compact({"id": p.get("id"), "type": "page", "title": p.get("title"), "space_id": p.get("spaceId"),
                          "version": (p.get("version") or {}).get("number"), "parent": p.get("parentId"),
                          "url": self.link((p.get("_links") or {}).get("webui")),
                          "markdown": f"# {p.get('title')}\n\n{storage_to_md(storage)}".strip()})

    async def _space_id(self, key: str) -> str:
        if key.isdigit():
            return key
        data = await self.api.get("/api/v2/spaces", params={"keys": key, "limit": 1}) or {}
        rows = data.get("results") or []
        if not rows:
            raise ToolError(f"Confluence space '{key}' not found.")
        return rows[0]["id"]

    async def create(self, title, content, parent_id, database_id, space):
        if database_id:
            raise ToolError("Confluence has no databases here — create a page instead (omit database_id).")
        body: dict[str, Any] = {"status": "current", "title": title,
                                "body": {"representation": "storage", "value": md_to_storage(content or "")}}
        if parent_id:
            parent = await self._page(self.page_id(parent_id))
            body["parentId"] = parent["id"]
            body["spaceId"] = parent.get("spaceId")
        else:
            key = space or self.cfg.get("default_space")
            if not key:
                raise ToolError("Pass a space key (or parent_id), or set a default space in the Confluence connector.")
            body["spaceId"] = await self._space_id(str(key))
        p = await self.api.post("/api/v2/pages", json=body)
        return {"created": True, "id": p.get("id"), "title": p.get("title"),
                "url": self.link((p.get("_links") or {}).get("webui"))}

    async def _put(self, page: dict, title: str, storage: str) -> dict:
        version = int((page.get("version") or {}).get("number") or 1) + 1
        return await self.api.put(f"/api/v2/pages/{page['id']}", json={
            "id": page["id"], "status": "current", "title": title,
            "body": {"representation": "storage", "value": storage},
            "version": {"number": version, "message": "Updated by Ren"}})

    async def update(self, ref, title, content):
        page = await self._page(self.page_id(ref))
        if title is None and content is None:
            raise ToolError("Nothing to update — pass title and/or content.")
        storage = md_to_storage(content) if content is not None else ((page.get("body") or {}).get("storage") or {}).get("value", "")
        p = await self._put(page, title or page.get("title"), storage)
        return {"updated": True, "id": page["id"], "version": (p.get("version") or {}).get("number"),
                "url": self.link((p.get("_links") or {}).get("webui"))}

    async def append(self, ref, content):
        page = await self._page(self.page_id(ref))
        current = ((page.get("body") or {}).get("storage") or {}).get("value", "")
        p = await self._put(page, page.get("title"), current + md_to_storage(content))
        return {"appended": True, "id": page["id"], "version": (p.get("version") or {}).get("number")}

    async def database_query(self, *a: Any, **kw: Any):
        raise ToolError("Confluence databases aren't available through its API — use Notion for database tools.")

    database_create = database_update = database_query


ADAPTERS: dict[str, type[KBAdapter]] = {"notion": NotionAdapter, "confluence": ConfluenceAdapter}


async def kb_adapter(ctx: ToolContext, account: str | None = None) -> Any:
    kind, cfg = await H.pick_connector(ctx, KB_KINDS, account, what="Knowledge base")
    return ADAPTERS[kind](ctx, cfg)


# =========================================================================== tools
group = ToolGroup(
    "knowledge_base", "Knowledge base",
    "Search, read and write pages in Notion or Confluence (as markdown); query and update Notion databases.",
    prefix="kb", icon="book-open", category="knowledge", connector_kinds=KB_KINDS,
)


def _page_preview(title: str):
    def render(a: dict) -> str:
        return H.preview(title, [("Knowledge base", a.get("account") or "default"), ("Page", a.get("page_id")),
                                 ("Title", a.get("title")), ("Parent", a.get("parent_id")),
                                 ("Database", a.get("database_id")), ("Space", a.get("space"))],
                         a.get("content") if a.get("content") is not None else None, body_label="Content")
    return render


@group.tool(risk=Risk.LOW, scope="read")
async def search(ctx: ToolContext, query: str, limit: int = 10, kind: str | None = None, space: str | None = None,
                 account: str | None = None) -> dict:
    """Search pages (and Notion databases) by title/content.

    Args:
        query: Search words.
        limit: Maximum results.
        kind: Notion only: "page" or "database".
        space: Confluence only: space key to search in.
        account: Knowledge-base connector name or kind (notion, confluence).
    """
    ad = await kb_adapter(ctx, account)
    rows = await ad.search(query, max(1, min(int(limit or 10), 100)), kind, space)
    return {"source": ad.label, "count": len(rows), "results": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def get(ctx: ToolContext, page_id: str, max_chars: int = 20000, offset: int = 0,
              account: str | None = None) -> dict:
    """Read a page as markdown (Notion database ids return the schema).

    Args:
        page_id: Page id or URL.
        max_chars: Maximum markdown characters.
        offset: Character offset for long pages.
        account: Knowledge-base connector name or kind.
    """
    ad = await kb_adapter(ctx, account)
    page = await ad.get(page_id)
    md = page.pop("markdown", "")
    return {"source": ad.label, **page, **H.window(md, max(500, min(int(max_chars or 20000), 200_000)), int(offset or 0))}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("title", "content"), preview=_page_preview("Create page"))
async def create(ctx: ToolContext, title: str, content: str = "", parent_id: str | None = None,
                 database_id: str | None = None, space: str | None = None, account: str | None = None) -> dict:
    """Create a page from markdown (under a parent page, in a Notion database, or in a Confluence space).

    Args:
        title: Page title.
        content: Markdown body (headings, lists, to-dos, quotes, code blocks, links).
        parent_id: Parent page id/URL.
        database_id: Notion: create the page as a row of this database.
        space: Confluence space key (when no parent_id).
        account: Knowledge-base connector name or kind.
    """
    ad = await kb_adapter(ctx, account)
    return {"source": ad.label, **await ad.create(title, content, parent_id, database_id, space)}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("title", "content"), preview=_page_preview("Update page"))
async def update(ctx: ToolContext, page_id: str, title: str | None = None, content: str | None = None,
                 account: str | None = None) -> dict:
    """Rename a page and/or replace its whole content with new markdown.

    Args:
        page_id: Page id or URL.
        title: New title.
        content: New markdown content (replaces the existing body).
        account: Knowledge-base connector name or kind.
    """
    ad = await kb_adapter(ctx, account)
    return {"source": ad.label, **await ad.update(page_id, title, content)}


@group.tool(risk=Risk.MEDIUM, scope="write", content_fields=("content",), preview=_page_preview("Append to page"))
async def append(ctx: ToolContext, page_id: str, content: str, account: str | None = None) -> dict:
    """Append markdown to the end of a page.

    Args:
        page_id: Page id or URL.
        content: Markdown to add.
        account: Knowledge-base connector name or kind.
    """
    ad = await kb_adapter(ctx, account)
    return {"source": ad.label, **await ad.append(page_id, content)}


@group.tool(risk=Risk.LOW, scope="read")
async def database_query(ctx: ToolContext, database_id: str, match: dict[str, Any] | None = None,
                         filter: dict[str, Any] | None = None, sorts: list[dict] | None = None, limit: int = 25,
                         account: str | None = None) -> dict:
    """Query a Notion database and return its rows with simplified property values.

    Args:
        database_id: Database id or URL.
        match: Simple filter {property: value} (text contains / select equals / checkbox...; AND-combined).
        filter: Raw Notion filter object (alternative to match).
        sorts: Notion sorts, e.g. [{"property": "Due", "direction": "ascending"}].
        limit: Maximum rows (1-100).
        account: Knowledge-base connector name or kind.
    """
    ad = await kb_adapter(ctx, account)
    return {"source": ad.label, **await ad.database_query(database_id, match, filter, sorts,
                                                          max(1, min(int(limit or 25), 100)))}


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("title",),
            preview=lambda a: H.preview("Create Notion database", [
                ("Parent page", a.get("parent_page_id")), ("Title", a.get("title")),
                ("Properties", a.get("properties"))]))
async def database_create(ctx: ToolContext, parent_page_id: str, title: str,
                          properties: dict[str, str] | None = None, account: str | None = None) -> dict:
    """Create a Notion database under a page.

    Args:
        parent_page_id: Page id/URL to create the database in.
        title: Database title.
        properties: Columns as {name: type}; types: title, text, number, select, multi_select, date, checkbox, url, email, phone_number, people, files. Options: "select:Todo,Doing,Done". A "Name" title column is added if none is given.
        account: Knowledge-base connector name or kind.
    """
    ad = await kb_adapter(ctx, account)
    return {"source": ad.label, **await ad.database_create(parent_page_id, title, properties)}


@group.tool(risk=Risk.MEDIUM, scope="write",
            preview=lambda a: H.preview("Update Notion database row" if a.get("row_id") else "Add Notion database row", [
                ("Database", a.get("database_id")), ("Row", a.get("row_id")), ("Values", a.get("values"))]))
async def database_update(ctx: ToolContext, database_id: str, values: dict[str, Any], row_id: str | None = None,
                          account: str | None = None) -> dict:
    """Add a row to a Notion database, or update an existing row's properties.

    Args:
        database_id: Database id/URL.
        values: Property values, e.g. {"Name": "Launch plan", "Status": "Doing", "Due": "2026-11-01", "Tags": ["q4"]}.
        row_id: Existing row (page) id to update; omit to add a new row.
        account: Knowledge-base connector name or kind.
    """
    ad = await kb_adapter(ctx, account)
    return {"source": ad.label, **await ad.database_update(database_id, values, row_id)}


# =========================================================================== connector kinds
@H.connector_test
async def _test_notion(cfg: dict) -> tuple[bool, str]:
    ad = NotionAdapter(ToolContext(org_id="_test", user_id=None), cfg)
    me = await ad.api.get("/users/me") or {}
    ws = ((me.get("bot") or {}).get("workspace_name")) or "workspace"
    return True, f"Connected to Notion ({ws}) as integration '{me.get('name') or 'bot'}'."


@H.connector_test
async def _test_confluence(cfg: dict) -> tuple[bool, str]:
    ad = ConfluenceAdapter(ToolContext(org_id="_test", user_id=None), cfg)
    me = await ad.api.get("/rest/api/user/current") or {}
    return True, f"Connected to Confluence ({ad.site}) as {me.get('displayName') or me.get('email') or 'user'}."


register_connector_kind(ConnectorKindSpec(
    kind="notion", name="Notion", description="Notion pages and databases shared with your integration.",
    icon="book-open", category="knowledge", tool_groups=["knowledge_base"],
    fields=[
        ConnectorField("token", "Integration token", type="password", required=True, secret=True,
                       placeholder="ntn_… or secret_…",
                       help="notion.so/my-integrations → New integration. Then share pages/databases with it "
                            "(… → Connections)."),
        ConnectorField("default_parent_page_id", "Default parent page (optional)",
                       help="Page id or URL where new pages are created when no parent is given."),
    ],
    docs_url="https://developers.notion.com/docs/authorization", test=_test_notion,
))

register_connector_kind(ConnectorKindSpec(
    kind="confluence", name="Confluence", description="Confluence Cloud pages and spaces.",
    icon="book-open", category="knowledge", tool_groups=["knowledge_base"],
    fields=[
        ConnectorField("site_url", "Site URL", type="url", required=True, placeholder="https://your-company.atlassian.net"),
        ConnectorField("email", "Account email", required=True),
        ConnectorField("api_token", "API token", type="password", required=True, secret=True,
                       help="Create at id.atlassian.com → Security → API tokens."),
        ConnectorField("default_space", "Default space key (optional)", placeholder="ENG"),
    ],
    docs_url="https://developer.atlassian.com/cloud/confluence/rest/v2/intro/", test=_test_confluence,
))
