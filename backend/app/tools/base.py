"""Tool framework (MCP-compatible).

A *ToolGroup* is the unit an org enables and an agent is granted (≈ one MCP server). Tools are plain
async functions; their JSON schema is derived from the signature + Google-style docstring:

    fs = ToolGroup("filesystem", "Files", "Read and write files in the workspace", prefix="fs",
                   icon="folder", category="core")

    @fs.tool(risk=Risk.LOW, scope="read")
    async def read(ctx: ToolContext, path: str, max_chars: int = 20000) -> str:
        '''Read a text file from the workspace.

        Args:
            path: Workspace path, e.g. "workspace/notes.md" or "shared/policies/hr.pdf".
            max_chars: Maximum characters to return.
        '''
        ...

Registered as `fs_read` (OpenAI function names cannot contain dots; the UI shows `fs.read`).
Handlers return str | dict | list | ToolResult and raise ToolError for expected failures.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, get_type_hints

from pydantic import Field, ValidationError, create_model

from app.services.workspace import Workspace

log = logging.getLogger("ren.tools")


class Risk(str, Enum):
    LOW = "low"  # read-only, no side effects
    MEDIUM = "medium"  # modifies workspace data / drafts
    HIGH = "high"  # external side effects: send email, post message, execute shell, write to SaaS
    CRITICAL = "critical"  # destructive or irreversible: delete remote data, deploy, financial actions


RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SCOPES = ("read", "write", "admin")


class ToolError(Exception):
    """Expected, user-facing failure. The message is returned to the agent so it can adapt."""


@dataclass
class ToolResult:
    content: Any
    ok: bool = True
    artifacts: list[dict] = field(default_factory=list)  # [{"path", "name", "mime"}]
    display: str | None = None  # short human-readable summary for the UI
    citations: list[dict] | None = None


@dataclass
class ToolContext:
    """Everything a tool may need about who is acting, for whom, and where."""

    org_id: str
    user_id: str | None
    role: str = "member"
    agent_id: str | None = None
    agent_name: str | None = None
    task_id: str | None = None
    conversation_id: str | None = None
    org_mode: str = "business"
    collection_ids: list[str] | None = None  # agent's knowledge scope (None = all accessible)
    depth: int = 0  # delegation depth
    extra: dict[str, Any] = field(default_factory=dict)
    _workspace: Workspace | None = field(default=None, repr=False)

    @property
    def workspace(self) -> Workspace:
        if self._workspace is None:
            self._workspace = Workspace(self.org_id, self.user_id)
        return self._workspace

    @property
    def can_write(self) -> bool:
        return self.role in ("member", "admin", "owner")

    @property
    def is_admin(self) -> bool:
        return self.role in ("admin", "owner")

    def session(self):
        """New DB session (async context manager that commits on exit)."""
        from app.core.db import session_scope

        return session_scope()

    async def connector(self, kind: str, *, name: str | None = None) -> dict[str, Any]:
        """Return merged config + decrypted secrets of the org's connector of `kind`.

        Raises ToolError with a helpful message when the integration is not connected.
        """
        overrides = self.extra.get("connectors") or {}
        if kind in overrides:
            return dict(overrides[kind])
        from sqlalchemy import select

        from app.core.security import decrypt_json
        from app.models import Connector

        async with self.session() as db:
            q = select(Connector).where(Connector.org_id == self.org_id, Connector.kind == kind)
            if name:
                q = q.where(Connector.name == name)
            row = (await db.execute(q.order_by(Connector.created_at))).scalars().first()
        if row is None:
            raise ToolError(
                f"The '{kind}' integration is not connected for this workspace. "
                "An admin can connect it in Connectors → Integrations."
            )
        data = dict(row.config or {})
        data.update(decrypt_json(row.secrets_enc))
        data["_connector_id"] = row.id
        data["_connector_name"] = row.name
        return data

    async def has_connector(self, kind: str) -> bool:
        try:
            await self.connector(kind)
            return True
        except ToolError:
            return False

    async def llm(self, purpose: str = "utility"):
        """LLM client for tools that need a model (vision, summarisation, ...)."""
        from app.llm.providers import get_llm

        async with self.session() as db:
            return await get_llm(db, self.org_id, purpose=purpose)

    async def add_artifact(self, path: str, name: str | None = None, mime: str | None = None) -> dict:
        """Register a produced workspace file as a task/conversation artifact (shown in the UI)."""
        import mimetypes

        abs_path = self.workspace.resolve(path, must_exist=True)
        vpath = self.workspace.virtual(abs_path)
        info = {
            "name": name or abs_path.name,
            "path": vpath,
            "mime": mime or mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream",
            "size": abs_path.stat().st_size,
        }
        if self.extra.get("no_db"):
            return info
        try:
            from app.services.artifacts import register_artifact

            return await register_artifact(self, info)
        except Exception:  # noqa: BLE001
            log.exception("artifact registration failed")
            return info

    async def progress(self, note: str, progress: int | None = None) -> None:
        cb = self.extra.get("progress_cb")
        if cb is not None:
            try:
                await cb(note, progress)
            except Exception:  # noqa: BLE001
                log.exception("progress callback failed")


Handler = Callable[..., Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    group: str
    description: str
    parameters: dict
    handler: Handler
    risk: Risk
    scope: str
    display_name: str
    args_model: Any
    content_fields: tuple[str, ...] = ()
    timeout: float = 120.0
    preview: Callable[[dict], str] | None = None
    external: bool = False  # provided by an external MCP server
    risk_fn: Callable[[dict], Risk] | None = None  # dynamic risk from args (e.g. sandboxed vs local execution)

    def effective_risk(self, args: dict) -> Risk:
        if self.risk_fn is not None:
            try:
                return Risk(self.risk_fn(args or {}))
            except Exception:  # noqa: BLE001
                return self.risk
        return self.risk

    def openai_schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}

    def validate(self, args: dict) -> dict:
        if self.args_model is None:
            return args or {}
        try:
            obj = self.args_model.model_validate(args or {})
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
            raise ToolError(f"Invalid arguments for {self.display_name}: {problems}") from e
        return obj.model_dump()

    def render_preview(self, args: dict) -> str:
        if self.preview:
            try:
                return self.preview(args)
            except Exception:  # noqa: BLE001
                pass
        lines = [f"**{self.display_name}**"]
        for k, v in (args or {}).items():
            sv = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
            if len(sv) > 600:
                sv = sv[:600] + "…"
            lines.append(f"- **{k}**: {sv}")
        return "\n".join(lines)


class ToolGroup:
    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        *,
        prefix: str | None = None,
        icon: str = "wrench",
        category: str = "core",
        connector_kinds: tuple[str, ...] | list[str] = (),
        connector_optional: bool = False,  # usable without a connector (local fallback)
        requires: tuple[str, ...] | list[str] = (),  # python modules that must be importable
        default_enabled: bool = True,
        register: bool = True,
    ):
        self.id = id
        self.name = name
        self.description = description
        self.prefix = prefix or id
        self.icon = icon
        self.category = category
        self.connector_kinds = tuple(connector_kinds)
        self.connector_optional = connector_optional
        self.requires = tuple(requires)
        self.default_enabled = default_enabled
        self.tools: list[ToolSpec] = []
        if register:
            registry.register(self)

    @property
    def connector_kind(self) -> str | None:
        return self.connector_kinds[0] if self.connector_kinds else None

    def availability(self) -> tuple[bool, str | None]:
        missing = [m for m in self.requires if importlib.util.find_spec(m) is None]
        if missing:
            return False, f"Missing dependency: {', '.join(missing)} (pip install {' '.join(missing)})"
        return True, None

    def tool(
        self,
        name: str | None = None,
        *,
        risk: Risk = Risk.LOW,
        scope: str = "read",
        content_fields: tuple[str, ...] | list[str] = (),
        timeout: float = 120.0,
        preview: Callable[[dict], str] | None = None,
        description: str | None = None,
        risk_fn: Callable[[dict], Risk] | None = None,
    ) -> Callable[[Handler], Handler]:
        if scope not in SCOPES:
            raise ValueError(f"invalid scope {scope}")

        def deco(fn: Handler) -> Handler:
            short = name or fn.__name__.rstrip("_")
            full = f"{self.prefix}_{short}"
            desc, arg_docs = _parse_docstring(fn.__doc__ or "")
            params, model = _schema_from_signature(fn, arg_docs, full)
            spec = ToolSpec(
                name=full, group=self.id, description=(description or desc or short).strip()[:1024],
                parameters=params, handler=fn, risk=Risk(risk), scope=scope,
                display_name=f"{self.prefix}.{short}", args_model=model,
                content_fields=tuple(content_fields), timeout=timeout, preview=preview, risk_fn=risk_fn,
            )
            self.tools.append(spec)
            registry.index(spec)
            return fn

        return deco


class ToolRegistry:
    def __init__(self) -> None:
        self.groups: dict[str, ToolGroup] = {}
        self.tools: dict[str, ToolSpec] = {}

    def register(self, group: ToolGroup) -> None:
        if group.id in self.groups and self.groups[group.id] is not group:
            log.warning("tool group %s registered twice; replacing", group.id)
        self.groups[group.id] = group

    def index(self, spec: ToolSpec) -> None:
        if spec.name in self.tools and self.tools[spec.name].handler is not spec.handler:
            log.warning("duplicate tool name %s", spec.name)
        self.tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    def group(self, group_id: str) -> ToolGroup | None:
        return self.groups.get(group_id)


registry = ToolRegistry()


# --------------------------------------------------------------------------- schema helpers
_ARGS_HDR = re.compile(r"^\s*(Args|Arguments|Parameters)\s*:\s*$", re.I)
_SECTION_HDR = re.compile(r"^\s*(Returns|Raises|Yields|Examples?|Notes?)\s*:\s*$", re.I)
_ARG_LINE = re.compile(r"^\s{2,}(\w+)\s*(\([^)]*\))?\s*:\s*(.*)$")


def _parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    doc = inspect.cleandoc(doc or "")
    lines = doc.splitlines()
    desc_lines: list[str] = []
    args: dict[str, str] = {}
    mode = "desc"
    current: str | None = None
    for raw in lines:
        if _ARGS_HDR.match(raw):
            mode, current = "args", None
            continue
        if _SECTION_HDR.match(raw):
            mode, current = "other", None
            continue
        if mode == "desc":
            desc_lines.append(raw)
        elif mode == "args":
            m = _ARG_LINE.match("  " + raw if not raw.startswith("  ") else raw)
            if m and not raw.startswith("        "):
                current = m.group(1)
                args[current] = m.group(3).strip()
            elif current and raw.strip():
                args[current] += " " + raw.strip()
    desc = "\n".join(desc_lines).strip()
    return desc, args


def _schema_from_signature(fn: Handler, arg_docs: dict[str, str], name: str) -> tuple[dict, Any]:
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:  # noqa: BLE001
        hints = {}
    fields: dict[str, Any] = {}
    for pname, param in sig.parameters.items():
        if pname in ("ctx", "self", "context"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        ann = hints.get(pname, str)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[pname] = (ann, Field(default, description=arg_docs.get(pname) or None))
    model = create_model(f"{name}_Args", **fields)  # type: ignore[call-overload]
    schema = model.model_json_schema()
    return _simplify_schema(schema), model


def _simplify_schema(schema: dict) -> dict:
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"].split("/")[-1]
                merged = {**defs.get(ref, {}), **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged)
            if "anyOf" in node:
                variants = [v for v in node["anyOf"] if v.get("type") != "null"]
                if len(variants) == 1:
                    base = {k: v for k, v in node.items() if k != "anyOf"}
                    return resolve({**variants[0], **base})
            out = {}
            for k, v in node.items():
                if k == "title" and not isinstance(v, dict):  # pydantic's annotation, not a param named "title"
                    continue
                if k == "default" and v is None:
                    continue
                out[k] = resolve(v)
            return out
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    s = resolve(schema)
    s.setdefault("type", "object")
    s.setdefault("properties", {})
    if not s.get("required"):
        s.pop("required", None)
    return s


def result_to_text(value: Any, limit: int = 16000) -> str:
    """Convert a tool return value into text for the model (truncated)."""
    if isinstance(value, ToolResult):
        value = value.content
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, indent=1)
        except Exception:  # noqa: BLE001
            text = str(value)
    if len(text) > limit:
        text = text[:limit] + f"\n…[truncated {len(text) - limit} chars]"
    return text
