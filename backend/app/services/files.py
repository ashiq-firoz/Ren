"""Outputs: file kinds, path resolution across workspaces and in-app previews (markdown, tables, code, media)."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select

from app.models import Artifact
from app.services.workspace import Workspace, WorkspaceError

KIND_EXTS: dict[str, tuple[str, ...]] = {
    "document": (".docx", ".doc", ".odt", ".rtf", ".md", ".markdown"),
    "spreadsheet": (".xlsx", ".xlsm", ".xls", ".ods"),
    "presentation": (".pptx", ".ppt", ".odp", ".key"),
    "pdf": (".pdf",),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif"),
    "data": (".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet", ".pq", ".xml", ".yaml", ".yml"),
    "code": (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sql", ".sh", ".bash", ".ps1", ".go", ".rs",
             ".java", ".kt", ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".r", ".ipynb", ".toml",
             ".ini", ".cfg", ".css", ".scss"),
    "web": (".html", ".htm"),
    "text": (".txt", ".log", ".rst", ".text"),
    "audio": (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"),
    "video": (".mp4", ".mov", ".webm", ".mkv", ".avi"),
    "archive": (".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz"),
}
_EXT_KIND = {ext: kind for kind, exts in KIND_EXTS.items() for ext in exts}
_LANG = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript",
         ".tsx": "tsx", ".jsx": "jsx", ".json": "json", ".jsonl": "json", ".ndjson": "json", ".ipynb": "json",
         ".sql": "sql", ".sh": "bash", ".bash": "bash", ".ps1": "powershell", ".go": "go", ".rs": "rust",
         ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c", ".cpp": "cpp",
         ".hpp": "cpp", ".cs": "csharp", ".swift": "swift", ".r": "r", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
         ".css": "css", ".scss": "scss", ".xml": "xml", ".yaml": "yaml", ".yml": "yaml", ".html": "html",
         ".htm": "html"}
_OFFICE = {".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx", ".xlsm": "xlsx"}

MAX_TEXT_BYTES = 1_000_000  # read at most ~1 MB of a text file for preview
MAX_CHARS = 300_000
MAX_TABLE_ROWS = 500


def file_kind(name: str | None) -> str:
    return _EXT_KIND.get(Path(name or "").suffix.lower(), "other")


def kind_filter(column, kind: str):
    """SQL condition: `column` (a file name/path) has an extension of the given kind."""
    if kind == "other":
        known = [ext for exts in KIND_EXTS.values() for ext in exts]
        return ~or_(*[column.ilike(f"%{ext}") for ext in known])
    exts = KIND_EXTS.get(kind)
    if not exts:
        raise ValueError(f"Unknown file kind '{kind}'")
    return or_(*[column.ilike(f"%{ext}") for ext in exts])


# ----------------------------------------------------------------------------------------- path resolution
async def resolve_file(ctx: Any, path: str) -> tuple[Path, str, Artifact | None]:
    """(absolute path, normalized virtual path, matching artifact) for a workspace path the caller may read.

    The caller's own workspace wins; otherwise a registered artifact with this path that the caller may see
    (admins: anyone's in the workspace) resolves in its owner's workspace."""
    own = Workspace(ctx.org_id, ctx.user_id)
    norm = own.normalize(path)
    if not norm:
        raise WorkspaceError("A file path is required.")
    q = select(Artifact).where(Artifact.org_id == ctx.org_id, Artifact.path == norm)
    if not ctx.is_admin:
        q = q.where(or_(Artifact.user_id == ctx.user_id, Artifact.user_id.is_(None)))
    arts = (await ctx.db.execute(q.order_by(Artifact.created_at.desc()).limit(10))).scalars().all()
    mine = next((a for a in arts if a.user_id in (None, ctx.user_id)), None)
    try:
        p = own.resolve(norm, must_exist=True)
        if p.is_file():
            return p, norm, mine
    except WorkspaceError:
        pass
    for a in arts:
        if not a.user_id or a.user_id == ctx.user_id:
            continue
        try:
            p = Workspace(ctx.org_id, a.user_id).resolve(norm, must_exist=True)
        except WorkspaceError:
            continue
        if p.is_file():
            return p, norm, a
    raise WorkspaceError(f"Not found: {norm}")


def artifact_file(ctx: Any, a: Artifact) -> Path:
    p = Workspace(ctx.org_id, a.user_id or ctx.user_id).resolve(a.path, must_exist=True)
    if not p.is_file():
        raise WorkspaceError(f"Not found: {a.path}")
    return p


# ----------------------------------------------------------------------------------------- preview
def _read_text(p: Path, limit: int = MAX_TEXT_BYTES) -> tuple[str, bool]:
    with p.open("rb") as f:
        raw = f.read(limit + 1)
    truncated = len(raw) > limit
    raw = raw[:limit]
    encodings = ("utf-16",) if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252")
    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_CHARS:
        text, truncated = text[:MAX_CHARS], True
    return text, truncated


def _looks_textual(p: Path) -> bool:
    with p.open("rb") as f:
        head = f.read(4096)
    return b"\x00" not in head


def _table(p: Path) -> dict:
    text, truncated = _read_text(p)
    delimiter = "\t" if p.suffix.lower() == ".tsv" else ","
    if delimiter == ",":
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            pass
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if truncated and rows:
        rows = rows[:-1]  # the last line may be cut mid-row
    header, body = (rows[0], rows[1:]) if rows else ([], [])
    total = len(body)
    if truncated:  # count the remaining rows without loading them
        with p.open("rb") as f:
            total = max(total, sum(1 for _ in f) - 1)
    width = max([len(header)] + [len(r) for r in body[:MAX_TABLE_ROWS]] or [0])
    columns = header + [f"Column {i + 1}" for i in range(len(header), width)]
    return {"columns": columns, "rows": [r + [""] * (width - len(r)) for r in body[:MAX_TABLE_ROWS]],
            "total_rows": total, "truncated": truncated or total > MAX_TABLE_ROWS}


def _office_markdown(p: Path) -> str:
    from app.tools.builtin import _office as O

    return O.extract_text(p, max_rows=200)


async def build_preview(p: Path, *, name: str, path: str, download: dict, artifact: Artifact | None = None,
                        task: Any = None, agent: Any = None) -> dict:
    ext = p.suffix.lower()
    kind = file_kind(name)
    st = p.stat()
    out: dict[str, Any] = {
        "name": name, "path": path, "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "size": st.st_size, "kind": kind,
        "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "render": "none", "content": None, "language": None, "table": None, "truncated": False,
        "converted_from": None, "download": download,
        "artifact_id": artifact.id if artifact is not None else None,
        "task_id": artifact.task_id if artifact is not None else None,
        "task_title": task.title if task is not None else None,
        "agent_name": agent.name if agent is not None else None,
        "created_at": artifact.created_at.isoformat().replace("+00:00", "Z") if artifact is not None else None,
    }
    if kind in ("image", "pdf", "audio", "video"):
        out["render"] = kind
        return out
    try:
        if ext in (".md", ".markdown"):
            out["content"], out["truncated"] = _read_text(p)
            out["render"] = "markdown"
        elif ext in (".csv", ".tsv"):
            table = await asyncio.to_thread(_table, p)
            out["truncated"] = table.pop("truncated")
            out["table"], out["render"] = table, "table"
        elif ext in _OFFICE:
            text = await asyncio.to_thread(_office_markdown, p)
            if len(text) > MAX_CHARS:
                text, out["truncated"] = text[:MAX_CHARS], True
            out.update(content=text, render="markdown", converted_from=_OFFICE[ext])
        elif ext in (".html", ".htm"):
            out["content"], out["truncated"] = _read_text(p)
            out["render"] = "html"
        elif ext in (".json", ".ipynb"):
            text, truncated = _read_text(p)
            if not truncated:
                try:
                    text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
            out.update(content=text, truncated=truncated, render="code", language="json")
        elif kind in ("code", "text") or (kind in ("data", "other") and _looks_textual(p)):
            out["content"], out["truncated"] = _read_text(p)
            out.update(render="code", language=_LANG.get(ext, "text"))
    except Exception:  # noqa: BLE001 — a broken file still gets metadata + download
        out.update(render="none", content=None, table=None)
    return out
