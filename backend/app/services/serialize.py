"""ORM → API dicts (shapes defined in docs/API.md). Shared by routes and background workers."""
from __future__ import annotations

import copy
from typing import Any

from app.core.config import settings
from app.core.db import iso

DEFAULT_ORG_SETTINGS: dict[str, Any] = {
    "default_provider_id": None,
    "orchestrator_model": None,
    "utility_model": None,
    "autonomy_default": "balanced",
    "max_concurrent_tasks": 6,
    "alignment": {
        "enabled": True,
        "mode": "post",
        "block_threshold": 0.7,
        "warn_threshold": 0.45,
        "judge_fallback": True,
        "laya_mode": "triage",  # triage: Laya screens, judge confirms | authoritative: Laya decides (fine-tuned)
        "escalate_threshold": 0.5,  # triage: Laya score at/above which the judge reviews
    },
    "retrieval": {
        "stair_enabled": True,
        "generate_questions": True,
        "structure_augmentation": True,
        "rerank": True,
        "top_k": 8,
    },
    "web_search": {"provider": "duckduckgo", "base_url": None},
    "disabled_tool_groups": [],
}


def deep_merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def org_settings(org: Any) -> dict:
    return deep_merge(DEFAULT_ORG_SETTINGS, org.settings or {})


def user_out(u: Any) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "created_at": iso(u.created_at),
            "timezone": getattr(u, "timezone", None)}


def org_summary(org: Any, role: str) -> dict:
    return {"id": org.id, "name": org.name, "slug": org.slug, "mode": org.mode, "role": role}


def org_out(org: Any, member_count: int = 1, has_web_key: bool = False) -> dict:
    s = org_settings(org)
    public = {k: v for k, v in s.items() if k not in ("disabled_tool_groups", "utility_model")}
    public["web_search"] = {**s["web_search"], "has_key": has_web_key}
    return {"id": org.id, "name": org.name, "slug": org.slug, "mode": org.mode, "created_at": iso(org.created_at),
            "settings": public, "member_count": member_count}


def member_out(m: Any, u: Any) -> dict:
    return {"user_id": u.id, "email": u.email, "name": u.name, "role": m.role, "title": m.title,
            "joined_at": iso(m.created_at), "last_active_at": iso(u.last_active_at)}


def invite_out(i: Any, inviter_name: str | None = None) -> dict:
    return {"id": i.id, "email": i.email, "role": i.role, "token": i.token,
            "link": f"{settings.frontend_url.rstrip('/')}/invite/{i.token}", "created_at": iso(i.created_at),
            "expires_at": iso(i.expires_at), "accepted": i.accepted, "invited_by_name": inviter_name}


def provider_out(p: Any) -> dict:
    return {"id": p.id, "name": p.name, "kind": p.kind, "base_url": p.base_url, "default_model": p.default_model,
            "has_key": bool(p.api_key_enc), "is_default": p.is_default, "supports_tools": p.supports_tools,
            "extra": {k: v for k, v in (p.extra or {}).items() if k not in ("headers",)},
            "created_at": iso(p.created_at)}


def agent_out(a: Any, stats: dict | None = None) -> dict:
    d = {"id": a.id, "name": a.name, "slug": a.slug, "role": a.role, "description": a.description,
         "system_prompt": a.system_prompt, "provider_id": a.provider_id, "model": a.model,
         "temperature": a.temperature, "tool_groups": list(a.tool_groups or []), "policy_ids": list(a.policy_ids or []),
         "collection_ids": list(a.collection_ids or []), "autonomy": a.autonomy, "max_steps": a.max_steps,
         "color": a.color, "icon": a.icon, "is_orchestrator": a.is_orchestrator, "enabled": a.enabled,
         "created_at": iso(a.created_at), "updated_at": iso(a.updated_at)}
    if stats is not None:
        d["stats"] = stats
    return d


def conversation_out(c: Any, last_preview: str | None = None, active_tasks: int = 0, status: str = "idle") -> dict:
    return {"id": c.id, "title": c.title, "pinned": c.pinned, "archived": c.archived, "created_at": iso(c.created_at),
            "updated_at": iso(c.updated_at), "last_message_preview": last_preview, "active_tasks": active_tasks,
            "status": status}


def message_out(m: Any) -> dict:
    return {"id": m.id, "conversation_id": m.conversation_id, "role": m.role, "content": m.content or "",
            "agent_id": m.agent_id, "task_id": m.task_id, "status": m.status, "metadata": dict(m.meta or {}),
            "created_at": iso(m.created_at), "user_id": m.user_id}


def artifact_out(a: Any, task: Any = None, agent: Any = None) -> dict:
    from app.services.files import file_kind

    return {"id": a.id, "name": a.name, "path": a.path, "mime": a.mime, "size": a.size, "task_id": a.task_id,
            "conversation_id": a.conversation_id, "created_at": iso(a.created_at), "kind": file_kind(a.name),
            "source": "generated" if a.task_id else "uploaded",
            "task_title": task.title if task is not None else None,
            "agent_id": task.agent_id if task is not None else None,
            "agent_name": agent.name if agent is not None else None,
            "schedule_id": getattr(task, "schedule_id", None) if task is not None else None}


def task_out(t: Any, agent: Any = None, children_count: int = 0, artifacts: list | None = None) -> dict:
    return {"id": t.id, "title": t.title, "instructions": t.instructions, "status": t.status, "priority": t.priority,
            "agent_id": t.agent_id, "agent_name": agent.name if agent else "Agent",
            "agent_color": agent.color if agent else "#56665D", "conversation_id": t.conversation_id,
            "parent_id": t.parent_id, "skill_id": t.skill_id, "schedule_id": getattr(t, "schedule_id", None),
            "progress": t.progress, "current_step": t.current_step,
            "result": t.result, "error": t.error, "steps": t.steps, "tokens": t.tokens, "created_by": t.created_by,
            "created_at": iso(t.created_at), "started_at": iso(t.started_at), "finished_at": iso(t.finished_at),
            "children_count": children_count, "artifacts": [artifact_out(a, t, agent) for a in (artifacts or [])]}


def task_event_out(e: Any) -> dict:
    return {"id": e.id, "task_id": e.task_id, "kind": e.kind, "content": dict(e.content or {}),
            "created_at": iso(e.created_at)}


def approval_out(a: Any, task_title: str | None = None, agent_name: str | None = None,
                 decided_by_name: str | None = None, tool_display: str | None = None) -> dict:
    return {"id": a.id, "task_id": a.task_id, "task_title": task_title, "agent_id": a.agent_id,
            "agent_name": agent_name, "tool": a.tool, "tool_display": tool_display or a.tool.replace("_", ".", 1),
            "args": dict(a.args or {}), "risk": a.risk, "reason": a.reason, "preview": a.preview,
            "status": a.status, "requested_for_user_id": a.requested_for_user_id,
            "conversation_id": a.conversation_id, "created_at": iso(a.created_at),
            "decided_by_name": decided_by_name, "decided_at": iso(a.decided_at), "comment": a.comment,
            "expires_at": iso(a.expires_at)}


def collection_out(c: Any, document_count: int = 0) -> dict:
    return {"id": c.id, "name": c.name, "description": c.description, "access": c.access,
            "member_ids": list(c.member_ids or []), "document_count": document_count, "created_at": iso(c.created_at)}


def document_out(d: Any, collection_name: str | None = None, uploader_name: str | None = None) -> dict:
    return {"id": d.id, "collection_id": d.collection_id, "collection_name": collection_name or "",
            "title": d.title, "filename": d.filename, "mime": d.mime, "size": d.size, "status": d.status,
            "progress": d.progress, "error": d.error, "page_count": d.page_count, "section_count": d.section_count,
            "leaf_count": d.leaf_count, "chunk_count": d.chunk_count, "question_count": d.question_count,
            "structure_source": d.structure_source, "uploaded_by_name": uploader_name,
            "created_at": iso(d.created_at), "updated_at": iso(d.updated_at)}


def policy_out(p: Any) -> dict:
    return {"id": p.id, "name": p.name, "description": p.description, "category": p.category,
            "rules": list(p.rules or []), "patterns": list(p.patterns or []), "severity": p.severity,
            "scope": p.scope, "applies_to": p.applies_to, "agent_ids": list(p.agent_ids or []), "enabled": p.enabled,
            "created_at": iso(p.created_at), "updated_at": iso(p.updated_at)}


def alignment_check_out(c: Any, agent_name: str | None = None) -> dict:
    return {"id": c.id, "agent_id": c.agent_id, "agent_name": agent_name, "task_id": c.task_id,
            "message_id": c.message_id, "kind": c.kind, "verdict": c.verdict, "checker": c.checker,
            "latency_ms": c.latency_ms, "violations": list(c.violations or []), "excerpt": c.excerpt,
            "created_at": iso(c.created_at)}


def skill_out(s: Any) -> dict:
    return {"id": s.id, "name": s.name, "slug": s.slug, "description": s.description, "category": s.category,
            "icon": s.icon, "instructions": s.instructions, "agent_id": s.agent_id,
            "tool_groups": list(s.tool_groups or []), "inputs": list(s.inputs or []), "builtin": s.builtin,
            "created_at": iso(s.created_at)}


def memory_out(m: Any) -> dict:
    return {"id": m.id, "content": m.content, "kind": m.kind, "scope": m.scope, "agent_id": m.agent_id,
            "importance": m.importance, "source": m.source, "created_at": iso(m.created_at),
            "updated_at": iso(m.updated_at)}


def audit_out(a: Any, user_name: str | None = None, agent_name: str | None = None) -> dict:
    return {"id": a.id, "action": a.action, "target_type": a.target_type, "target_id": a.target_id,
            "user_id": a.user_id, "user_name": user_name, "agent_id": a.agent_id, "agent_name": agent_name,
            "task_id": a.task_id, "risk": a.risk, "details": dict(a.details or {}), "created_at": iso(a.created_at)}


def notification_out(n: Any) -> dict:
    return {"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "link": n.link, "read": n.read,
            "created_at": iso(n.created_at)}


def connector_out(c: Any, secret_keys: set[str] | None = None, set_secret_keys: set[str] | None = None) -> dict:
    cfg = dict(c.config or {})
    for k in (set_secret_keys or set()):
        cfg[k] = "••••"
    return {"id": c.id, "kind": c.kind, "name": c.name, "status": c.status, "config": cfg, "error": c.error,
            "created_at": iso(c.created_at)}


def mcp_server_out(s: Any) -> dict:
    return {"id": s.id, "name": s.name, "transport": s.transport, "command": s.command, "args": list(s.args or []),
            "url": s.url, "enabled": s.enabled, "status": s.status, "error": s.error,
            "tools": [{"name": t.get("name"), "description": t.get("description") or ""} for t in (s.tools_cache or [])],
            "env_keys": list(s.env_keys or []), "header_keys": list(s.header_keys or []),
            "created_at": iso(s.created_at)}
