"""Per-user sandboxed workspace on disk (what filesystem/shell/code tools operate on).

Virtual layout seen by agents and the UI:
  workspace/  projects/  downloads/  output/  uploads/  temp/   → private to the user
  shared/                                                       → shared by the whole org
Paths are always relative ("output/report.docx"). Anything escaping the sandbox is rejected.
"""
from __future__ import annotations

import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings

PRIVATE_DIRS = ("workspace", "projects", "downloads", "output", "uploads", "temp")
TOP_DIRS = PRIVATE_DIRS + ("shared",)


class WorkspaceError(Exception):
    pass


class Workspace:
    def __init__(self, org_id: str, user_id: str | None):
        self.org_id = org_id
        self.user_id = user_id or "_system"
        base = settings.workspaces_dir / org_id
        self.user_root = (base / "users" / self.user_id).resolve()
        self.shared_root = (base / "shared").resolve()
        for d in PRIVATE_DIRS:
            (self.user_root / d).mkdir(parents=True, exist_ok=True)
        self.shared_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def normalize(self, path: str | None) -> str:
        p = (path or "").replace("\\", "/").strip()
        while p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if p in ("", "."):
            return ""
        first = p.split("/", 1)[0]
        if first not in TOP_DIRS:
            p = f"workspace/{p}"
        return p

    def resolve(self, path: str | None, *, must_exist: bool = False, for_write: bool = False) -> Path:
        """Map a virtual path to an absolute path inside the sandbox."""
        vp = self.normalize(path)
        if vp == "":
            return self.user_root
        if ".." in Path(vp).parts:
            raise WorkspaceError("Path traversal ('..') is not allowed.")
        first, _, rest = vp.partition("/")
        root = self.shared_root if first == "shared" else self.user_root / first
        target = (root / rest).resolve() if rest else root.resolve()
        allowed_root = self.shared_root if first == "shared" else self.user_root
        if target != allowed_root and allowed_root not in target.parents:
            raise WorkspaceError("Path escapes the workspace sandbox.")
        if must_exist and not target.exists():
            raise WorkspaceError(f"Not found: {vp}")
        if for_write:
            target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def virtual(self, abs_path: Path) -> str:
        abs_path = abs_path.resolve()
        if abs_path == self.shared_root or self.shared_root in abs_path.parents:
            rel = abs_path.relative_to(self.shared_root).as_posix()
            return "shared" if rel == "." else f"shared/{rel}"
        rel = abs_path.relative_to(self.user_root).as_posix()
        return "" if rel == "." else rel

    def is_shared(self, path: str) -> bool:
        return self.normalize(path).startswith("shared")

    def info(self, abs_path: Path) -> dict:
        st = abs_path.stat()
        return {
            "name": abs_path.name,
            "path": self.virtual(abs_path),
            "is_dir": abs_path.is_dir(),
            "size": 0 if abs_path.is_dir() else st.st_size,
            "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "mime": None if abs_path.is_dir() else (mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"),
        }

    def list(self, path: str | None = "", recursive: bool = False, limit: int = 500) -> list[dict]:
        if self.normalize(path) == "":
            return [self.info(self.user_root / d) for d in PRIVATE_DIRS] + [self.info(self.shared_root)]
        root = self.resolve(path, must_exist=True)
        if root.is_file():
            return [self.info(root)]
        out = []
        it = root.rglob("*") if recursive else root.iterdir()
        for p in sorted(it, key=lambda x: (not x.is_dir(), x.name.lower())):
            out.append(self.info(p))
            if len(out) >= limit:
                break
        return out

    def env(self) -> dict[str, str]:
        """Environment for subprocesses: minimal, no backend secrets."""
        keep = ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL", "HOME", "USERPROFILE")
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        env["REN_WORKSPACE"] = str(self.user_root)
        env["REN_SHARED"] = str(self.shared_root)
        env["PYTHONIOENCODING"] = "utf-8"
        return env
