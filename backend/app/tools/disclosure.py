"""Progressive tool disclosure.

Agents are granted whole tool groups (often 80–110 tools ≈ 15k tokens of JSON schema). Sending every schema on
every step is slow and expensive, and large toolboxes hurt tool selection. Instead each task starts with a compact
*core* set per group; the system prompt lists the full catalog in one line per group, and the agent loads more with
`find_tools` / `load_tools` (calling an unloaded-but-granted tool also loads it on the fly).
"""
from __future__ import annotations

import re
from collections import defaultdict

from app.tools.base import ToolSpec, registry

# The tools an agent is most likely to need first, per group. Groups not listed get their first two tools.
CORE_TOOLS: dict[str, list[str]] = {
    "agents": ["agent_delegate", "agent_wait"],
    "analytics": ["analytics_profile", "analytics_query", "analytics_visualize"],
    "approval": ["approval_request"],
    "artifacts": ["artifact_create"],
    "audio": ["audio_transcribe"],
    "browser": ["browser_open", "browser_get_page", "browser_extract_links"],
    "calendar": ["calendar_list", "calendar_create", "calendar_availability"],
    "chat": ["chat_search", "chat_send"],
    "code": ["code_python"],
    "computer": ["computer_screenshot", "computer_left_click", "computer_type"],
    "crm": ["crm_search_customer", "crm_get_customer", "crm_list_deals"],
    "database": ["db_list_tables", "db_describe_table", "db_query"],
    "docker": ["docker_list", "docker_logs"],
    "documents": ["doc_create", "doc_read"],
    "drive": ["cloud_search", "cloud_read", "cloud_upload"],
    "email": ["email_search", "email_get", "email_create_draft", "email_send"],
    "filesystem": ["fs_list", "fs_read", "fs_write", "fs_search"],
    "git": ["git_status", "git_diff", "git_commit"],
    "github": ["github_list_issues", "github_get_issue", "github_get_pr", "github_create_issue"],
    "identity": ["identity_whoami"],
    "knowledge_base": ["kb_search", "kb_get"],
    "kubernetes": ["k8s_get", "k8s_logs"],
    "memory": ["memory_search", "memory_store"],
    "notify": ["notify_send"],
    "pdf": ["pdf_read", "pdf_create"],
    "pm": ["pm_search", "pm_create_task", "pm_update_task"],
    "rag": ["rag_search", "rag_search_structured", "rag_answer", "rag_get_section"],
    "schedule": ["schedule_task"],
    "search": ["search_web", "search_fetch", "search_news"],
    "secrets": ["secrets_list_metadata"],
    "sheets": ["sheet_create", "sheet_read", "sheet_write"],
    "shell": ["shell_exec"],
    "skills": ["skill_list", "skill_load"],
    "slides": ["ppt_create", "ppt_read"],
    "tasks": ["task_status"],
    "vision": ["vision_describe", "vision_ocr"],
}
DEFAULT_CORE_PER_GROUP = 2
LOAD_ALL_BELOW = 24  # small toolboxes are sent whole

FIND_LOAD_TOOLS = [
    {"type": "function", "function": {
        "name": "find_tools",
        "description": ("Search your granted-but-not-yet-loaded tools by keyword (e.g. 'pivot table', 'merge pdf', "
                        "'reply email'). Returns names and one-line descriptions; then call load_tools."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What you want to do."},
            "group": {"type": "string", "description": "Optional tool group id to restrict the search."}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "load_tools",
        "description": "Load tools by name (from the catalog or find_tools) so you can call them from your next step.",
        "parameters": {"type": "object", "properties": {
            "names": {"type": "array", "items": {"type": "string"}, "description": "Tool names, e.g. ['sheet_pivot']."}},
            "required": ["names"]}}},
]


def _first_line(text: str, limit: int = 110) -> str:
    line = (text or "").strip().split("\n", 1)[0]
    return line if len(line) <= limit else line[: limit - 1] + "…"


def initial_tools(granted: dict[str, ToolSpec]) -> set[str]:
    """Core subset of the granted tools (everything when the toolbox is small)."""
    if len(granted) <= LOAD_ALL_BELOW:
        return set(granted)
    by_group: dict[str, list[ToolSpec]] = defaultdict(list)
    for spec in granted.values():
        by_group[spec.group].append(spec)
    core: set[str] = set()
    for gid, specs in by_group.items():
        wanted = [n for n in CORE_TOOLS.get(gid, []) if n in granted]
        core.update(wanted or [s.name for s in specs[:DEFAULT_CORE_PER_GROUP]])
    return core


def catalog_text(granted: dict[str, ToolSpec], active: set[str]) -> str:
    """One line per granted group: what it does, which tools are loaded, how many more are available."""
    by_group: dict[str, list[ToolSpec]] = defaultdict(list)
    for spec in granted.values():
        by_group[spec.group].append(spec)
    lines = []
    for gid in sorted(by_group):
        specs = by_group[gid]
        group = registry.group(gid)
        desc = _first_line(group.description if group else gid, 90)
        loaded = [s.name for s in specs if s.name in active]
        more = [s.name.split("_", 1)[-1] for s in specs if s.name not in active]
        line = f"- **{gid}** — {desc}. Loaded: {', '.join(loaded) or 'none'}"
        if more:
            line += f". Also available: {', '.join(more)}"
        lines.append(line)
    return "\n".join(lines)


def find_tools(granted: dict[str, ToolSpec], active: set[str], query: str, group: str | None = None,
               limit: int = 12) -> list[dict]:
    terms = [t for t in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(t) > 1]
    scored = []
    for spec in granted.values():
        if spec.name in active or (group and spec.group != group):
            continue
        hay = f"{spec.name} {spec.display_name} {spec.description}".lower()
        score = sum(3 if t in spec.name else 1 for t in terms if t in hay)
        if score or not terms:
            scored.append((score, spec))
    scored.sort(key=lambda x: (-x[0], x[1].name))
    return [{"name": s.name, "group": s.group, "risk": s.effective_risk({}).value,
             "description": _first_line(s.description, 160)} for _, s in scored[:limit]]


def load_tools(granted: dict[str, ToolSpec], active: set[str], names: list[str]) -> tuple[list[str], list[str]]:
    loaded, unknown = [], []
    for raw in names or []:
        name = str(raw).strip().replace(".", "_", 1)
        if name in granted:
            if name not in active:
                active.add(name)
            loaded.append(name)
        else:
            unknown.append(str(raw))
    return loaded, unknown
