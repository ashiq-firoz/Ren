"""Cloud drive tools with one interface over Google Drive (v3), OneDrive / SharePoint (Microsoft Graph), Dropbox
and Box — falling back to the organization's `shared/` workspace folder when no drive is connected.

`read` downloads a file and extracts text (pdf, docx, xlsx, pptx, txt, md, html; Google Docs/Sheets/Slides are
exported first); `download` saves the file into the workspace `downloads/` folder.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin import _http as H
from app.tools.builtin._oauth import box_token, dropbox_token, google_client, graph_client, invalidate
from app.tools.builtin.email import google_oauth_fields, norm_addresses
from app.tools.connectors import ConnectorField, ConnectorKindSpec, register_connector_kind

DRIVE_KINDS = ("google_drive", "microsoft365", "dropbox", "box")
GDRIVE = "https://www.googleapis.com/drive/v3"
GDRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
GDRIVE_SCOPES = ("https://www.googleapis.com/auth/drive",)
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
GOOGLE_EXPORTS: dict[str, dict[str, tuple[str, str]]] = {
    "application/vnd.google-apps.document": {
        "read": ("text/plain", ".txt"), "default": (DOCX, ".docx"), "docx": (DOCX, ".docx"),
        "pdf": ("application/pdf", ".pdf"), "txt": ("text/plain", ".txt"), "md": ("text/markdown", ".md"),
        "html": ("text/html", ".html"), "odt": ("application/vnd.oasis.opendocument.text", ".odt")},
    "application/vnd.google-apps.spreadsheet": {
        "read": (XLSX, ".xlsx"), "default": (XLSX, ".xlsx"), "xlsx": (XLSX, ".xlsx"), "csv": ("text/csv", ".csv"),
        "pdf": ("application/pdf", ".pdf"), "ods": ("application/vnd.oasis.opendocument.spreadsheet", ".ods")},
    "application/vnd.google-apps.presentation": {
        "read": ("text/plain", ".txt"), "default": (PPTX, ".pptx"), "pptx": (PPTX, ".pptx"),
        "pdf": ("application/pdf", ".pdf"), "txt": ("text/plain", ".txt")},
    "application/vnd.google-apps.drawing": {
        "default": ("image/png", ".png"), "png": ("image/png", ".png"), "pdf": ("application/pdf", ".pdf"),
        "svg": ("image/svg+xml", ".svg")},
}
ROLE_ALIASES = {"reader": "reader", "viewer": "reader", "read": "reader", "view": "reader", "commenter": "commenter",
                "comment": "commenter", "writer": "writer", "editor": "writer", "edit": "writer", "write": "writer"}
GRAPH_SIMPLE_UPLOAD = 4 * 1024 * 1024
GRAPH_CHUNK = 320 * 1024 * 16  # multiple of 320 KiB


def _q(v: str) -> str:
    return quote(str(v), safe="")


def _role(role: str) -> str:
    r = ROLE_ALIASES.get((role or "reader").strip().lower())
    if r is None:
        raise ToolError("role must be reader, commenter or writer.")
    return r


class DriveAdapter:
    kind = ""
    label = ""
    remote = True

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any] | None = None):
        self.ctx = ctx
        self.cfg = cfg or {}

    async def search(self, query: str, limit: int) -> list[dict]: raise NotImplementedError
    async def list(self, folder: str | None, limit: int) -> list[dict]: raise NotImplementedError
    async def get(self, ref: str) -> dict: raise NotImplementedError
    async def content(self, ref: str, purpose: str = "read", export_format: str | None = None) -> tuple[bytes, str, str]: raise NotImplementedError
    async def upload(self, data: bytes, name: str, folder: str | None, mime: str) -> dict: raise NotImplementedError
    async def create_folder(self, name: str, parent: str | None) -> dict: raise NotImplementedError
    async def move(self, ref: str, dest: str) -> dict: raise NotImplementedError
    async def copy(self, ref: str, dest: str | None, name: str | None) -> dict: raise NotImplementedError
    async def rename(self, ref: str, new_name: str) -> dict: raise NotImplementedError
    async def delete(self, ref: str, permanent: bool) -> dict: raise NotImplementedError
    async def share(self, ref: str, emails: list[str], role: str, link_scope: str | None, message: str | None,
                    notify: bool) -> dict: raise NotImplementedError


# --------------------------------------------------------------------------- Google Drive
_GID_PATTERNS = (re.compile(r"/d/([A-Za-z0-9_-]{10,})"), re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
                 re.compile(r"/folders/([A-Za-z0-9_-]{10,})"))


def google_file_id(ref: str) -> str:
    r = (ref or "").strip()
    if r.startswith("http"):
        for pat in _GID_PATTERNS:
            m = pat.search(r)
            if m:
                return m.group(1)
        raise ToolError("Couldn't find a file id in that Google URL.")
    return r


def _gq(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveAdapter(DriveAdapter):
    kind = "google_drive"
    label = "Google Drive"
    FIELDS = "id,name,mimeType,size,modifiedTime,webViewLink,parents,owners(displayName,emailAddress),shortcutDetails"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        self.api = google_client(cfg, GDRIVE, service="Google Drive", scopes=GDRIVE_SCOPES)

    @staticmethod
    def norm(f: dict) -> dict:
        mime = f.get("mimeType") or ""
        size = f.get("size")
        return H.compact({
            "id": f.get("id"), "name": f.get("name"), "type": "folder" if mime == FOLDER_MIME else "file",
            "mime": mime, "size": int(size) if size and str(size).isdigit() else None,
            "modified": f.get("modifiedTime"), "url": f.get("webViewLink"),
            "owner": ((f.get("owners") or [{}])[0] or {}).get("emailAddress"),
        })

    async def _meta(self, ref: str) -> dict:
        return await self.api.get(f"/files/{_q(google_file_id(ref))}",
                                  params={"fields": self.FIELDS + ",trashed", "supportsAllDrives": True})

    async def _files(self, q: str, limit: int, order: str | None = None) -> list[dict]:
        data = await self.api.get("/files", params={
            "q": q, "pageSize": max(1, min(limit, 200)), "fields": f"files({self.FIELDS})", "orderBy": order,
            "supportsAllDrives": True, "includeItemsFromAllDrives": True}) or {}
        return [self.norm(f) for f in data.get("files", [])]

    async def search(self, query, limit):
        esc = _gq(query.strip())
        return await self._files(f"(name contains '{esc}' or fullText contains '{esc}') and trashed = false", limit)

    async def list(self, folder, limit):
        fid = google_file_id(folder) if folder and folder.strip() not in ("/", "root") else "root"
        return await self._files(f"'{_gq(fid)}' in parents and trashed = false", limit, "folder,name")

    async def get(self, ref):
        return self.norm(await self._meta(ref))

    async def content(self, ref, purpose="read", export_format=None):
        meta = await self._meta(ref)
        if meta.get("mimeType") == SHORTCUT_MIME and (meta.get("shortcutDetails") or {}).get("targetId"):
            meta = await self._meta(meta["shortcutDetails"]["targetId"])
        mime, name, fid = meta.get("mimeType") or "", meta.get("name") or "file", meta["id"]
        if mime == FOLDER_MIME:
            raise ToolError(f"'{name}' is a folder — use cloud_list to see its contents.")
        if mime.startswith("application/vnd.google-apps."):
            exports = GOOGLE_EXPORTS.get(mime)
            if not exports:
                raise ToolError(f"Google file type {mime.rsplit('.', 1)[-1]} can't be exported.")
            key = (export_format or "").lower().lstrip(".") or ("read" if purpose == "read" else "default")
            if key not in exports:
                opts = ", ".join(k for k in exports if k not in ("read", "default"))
                raise ToolError(f"Can't export '{name}' as {export_format}; options: {opts}.")
            emime, ext = exports[key]
            data, _ = await self.api.fetch(f"/files/{_q(fid)}/export", params={"mimeType": emime})
            base = name[: -len(ext)] if name.lower().endswith(ext) else name
            return data, base + ext, emime
        if export_format:
            raise ToolError("export_format only applies to Google Docs, Sheets, Slides and Drawings.")
        data, _ = await self.api.fetch(f"/files/{_q(fid)}", params={"alt": "media", "supportsAllDrives": True})
        return data, name, mime

    async def upload(self, data, name, folder, mime):
        meta: dict[str, Any] = {"name": name}
        if folder and folder.strip() not in ("/", "root"):
            meta["parents"] = [google_file_id(folder)]
        params = {"supportsAllDrives": True, "fields": self.FIELDS}
        if len(data) <= 5 * 1024 * 1024:
            boundary = "ren" + uuid.uuid4().hex
            body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                    f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--".encode()
            res = await self.api.post(GDRIVE_UPLOAD, params={**params, "uploadType": "multipart"}, content=body,
                                      headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        else:
            resp = await self.api.request("POST", GDRIVE_UPLOAD, params={**params, "uploadType": "resumable"},
                                          json=meta, raw=True, headers={"X-Upload-Content-Type": mime,
                                                                        "X-Upload-Content-Length": str(len(data))})
            session = resp.headers.get("location")
            if not session:
                raise ToolError("Google Drive did not return an upload session.")
            res = await self.api.put(session, content=data, headers={"Content-Type": mime})
        return self.norm(res)

    async def create_folder(self, name, parent):
        body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
        if parent and parent.strip() not in ("/", "root"):
            body["parents"] = [google_file_id(parent)]
        return self.norm(await self.api.post("/files", json=body, params={"supportsAllDrives": True,
                                                                          "fields": self.FIELDS}))

    async def move(self, ref, dest):
        meta = await self._meta(ref)
        dest_id = google_file_id(dest) if dest.strip() not in ("/", "root") else "root"
        res = await self.api.patch(f"/files/{_q(meta['id'])}", json={}, params={
            "addParents": dest_id, "removeParents": ",".join(meta.get("parents") or []) or None,
            "supportsAllDrives": True, "fields": self.FIELDS})
        return self.norm(res)

    async def copy(self, ref, dest, name):
        meta = await self._meta(ref)
        if meta.get("mimeType") == FOLDER_MIME:
            raise ToolError("Google Drive can't copy folders — copy the files individually.")
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if dest:
            body["parents"] = [google_file_id(dest) if dest.strip() not in ("/", "root") else "root"]
        return self.norm(await self.api.post(f"/files/{_q(meta['id'])}/copy", json=body,
                                             params={"supportsAllDrives": True, "fields": self.FIELDS}))

    async def rename(self, ref, new_name):
        return self.norm(await self.api.patch(f"/files/{_q(google_file_id(ref))}", json={"name": new_name},
                                              params={"supportsAllDrives": True, "fields": self.FIELDS}))

    async def delete(self, ref, permanent):
        fid = google_file_id(ref)
        if permanent:
            await self.api.delete(f"/files/{_q(fid)}", params={"supportsAllDrives": True})
            return {"deleted": True, "permanent": True, "id": fid}
        await self.api.patch(f"/files/{_q(fid)}", json={"trashed": True}, params={"supportsAllDrives": True})
        return {"deleted": True, "permanent": False, "id": fid, "note": "Moved to the Drive trash (restorable)."}

    async def share(self, ref, emails, role, link_scope, message, notify):
        meta = await self._meta(ref)
        fid = meta["id"]
        role = _role(role)
        done: list[str] = []
        for addr in emails:
            await self.api.post(f"/files/{_q(fid)}/permissions", json={"type": "user", "role": role, "emailAddress": addr},
                                params={"sendNotificationEmail": notify, "emailMessage": message if notify and message else None,
                                        "supportsAllDrives": True})
            done.append(addr)
        link = None
        if link_scope:
            if link_scope == "anyone":
                perm = {"type": "anyone", "role": role}
            else:
                domain = str(self.cfg.get("domain") or "").strip() or (
                    (meta.get("owners") or [{}])[0].get("emailAddress", "").split("@")[-1])
                if not domain:
                    raise ToolError("Couldn't determine the organization's domain for link sharing.")
                perm = {"type": "domain", "role": role, "domain": domain}
            await self.api.post(f"/files/{_q(fid)}/permissions", json=perm, params={"supportsAllDrives": True})
            link = meta.get("webViewLink")
        return H.compact({"shared": True, "id": fid, "name": meta.get("name"), "role": role, "with": done,
                          "link_scope": link_scope, "link": link})


# --------------------------------------------------------------------------- OneDrive / SharePoint (Graph)
class OneDriveAdapter(DriveAdapter):
    kind = "microsoft365"
    label = "OneDrive / SharePoint"
    SELECT = "id,name,size,file,folder,lastModifiedDateTime,webUrl,parentReference"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)
        drive_id = str(cfg.get("drive_id") or "").strip()
        self.api = graph_client(cfg, service="OneDrive", reconnect="Microsoft 365", user_scoped=not drive_id)
        self.drive = f"/drives/{_q(drive_id)}" if drive_id else "/drive"

    def _item(self, ref: str | None) -> str:
        r = (ref or "").strip()
        if r in ("", "/", "root"):
            return f"{self.drive}/root"
        if r.startswith("/"):
            return f"{self.drive}/root:{quote(r.rstrip('/'))}:"
        return f"{self.drive}/items/{_q(r)}"

    def _child(self, folder: str | None, name: str) -> str:
        r = (folder or "").strip()
        if r in ("", "/", "root"):
            return f"{self.drive}/root:/{quote(name)}:"
        if r.startswith("/"):
            return f"{self.drive}/root:{quote(r.rstrip('/'))}/{quote(name)}:"
        return f"{self.drive}/items/{_q(r)}:/{quote(name)}:"

    @staticmethod
    def norm(i: dict) -> dict:
        parent = (i.get("parentReference") or {}).get("path") or ""
        parent = parent.split("root:", 1)[-1] if "root:" in parent else ""
        return H.compact({
            "id": i.get("id"), "name": i.get("name"), "type": "folder" if "folder" in i else "file",
            "mime": (i.get("file") or {}).get("mimeType"), "size": i.get("size") if "folder" not in i else None,
            "modified": i.get("lastModifiedDateTime"), "url": i.get("webUrl"),
            "path": f"{parent}/{i.get('name')}" if i.get("name") else None,
            "children": (i.get("folder") or {}).get("childCount") if "folder" in i else None,
        })

    async def search(self, query, limit):
        esc = query.strip().replace("'", "''")
        data = await self.api.get(f"{self.drive}/root/search(q='{_q(esc)}')",
                                  params={"$top": max(1, min(limit, 200)), "$select": self.SELECT}) or {}
        return [self.norm(i) for i in data.get("value", [])][:limit]

    async def list(self, folder, limit):
        data = await self.api.get(f"{self._item(folder)}/children",
                                  params={"$top": max(1, min(limit, 200)), "$select": self.SELECT}) or {}
        return [self.norm(i) for i in data.get("value", [])]

    async def get(self, ref):
        return self.norm(await self.api.get(self._item(ref), params={"$select": self.SELECT}))

    async def content(self, ref, purpose="read", export_format=None):
        meta = await self.api.get(self._item(ref), params={"$select": self.SELECT})
        if "folder" in meta:
            raise ToolError(f"'{meta.get('name')}' is a folder — use cloud_list to see its contents.")
        name = meta.get("name") or "file"
        mime = (meta.get("file") or {}).get("mimeType") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        if export_format:
            fmt = export_format.lower().lstrip(".")
            if fmt != "pdf":
                raise ToolError("OneDrive can only convert files to pdf (export_format='pdf').")
            data, _ = await self.api.fetch(f"{self._item(meta['id'])}/content", params={"format": "pdf"})
            return data, re.sub(r"\.[^.]+$", "", name) + ".pdf", "application/pdf"
        data, _ = await self.api.fetch(f"{self._item(meta['id'])}/content")
        return data, name, mime

    async def upload(self, data, name, folder, mime):
        target = self._child(folder, name)
        if len(data) <= GRAPH_SIMPLE_UPLOAD:
            res = await self.api.put(f"{target}/content", content=data,
                                     params={"@microsoft.graph.conflictBehavior": "rename"},
                                     headers={"Content-Type": mime})
            return self.norm(res)
        session = await self.api.post(f"{target}/createUploadSession",
                                      json={"item": {"@microsoft.graph.conflictBehavior": "rename"}})
        url = session["uploadUrl"]
        res: Any = None
        for start in range(0, len(data), GRAPH_CHUNK):
            part = data[start:start + GRAPH_CHUNK]
            res = await H.api("PUT", url, service="OneDrive", content=part, headers={  # pre-authorized URL: no token
                "Content-Range": f"bytes {start}-{start + len(part) - 1}/{len(data)}",
                "Content-Length": str(len(part))})
        return self.norm(res or {})

    async def create_folder(self, name, parent):
        res = await self.api.post(f"{self._item(parent)}/children", json={
            "name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"})
        return self.norm(res)

    async def _id(self, ref: str) -> str:
        if ref.strip().startswith("/") or ref.strip() in ("", "root"):
            return (await self.api.get(self._item(ref), params={"$select": "id"}))["id"]
        return ref.strip()

    async def move(self, ref, dest):
        res = await self.api.patch(self._item(ref), json={"parentReference": {"id": await self._id(dest)}})
        return self.norm(res)

    async def copy(self, ref, dest, name):
        body: dict[str, Any] = {}
        if dest:
            parent = await self.api.get(self._item(dest), params={"$select": "id,parentReference"})
            body["parentReference"] = {"id": parent["id"],
                                       "driveId": (parent.get("parentReference") or {}).get("driveId")}
            body["parentReference"] = H.compact(body["parentReference"])
        if name:
            body["name"] = name
        resp = await self.api.request("POST", f"{self._item(ref)}/copy", json=body, raw=True)
        return {"copy_started": True, "status_url": resp.headers.get("location"),
                "note": "OneDrive copies run in the background; the copy appears shortly."}

    async def rename(self, ref, new_name):
        return self.norm(await self.api.patch(self._item(ref), json={"name": new_name}))

    async def delete(self, ref, permanent):
        item = self._item(ref)
        if permanent:
            await self.api.post(f"{item}/permanentDelete", content=b"")
            return {"deleted": True, "permanent": True}
        await self.api.delete(item)
        return {"deleted": True, "permanent": False, "note": "Moved to the recycle bin (restorable)."}

    async def share(self, ref, emails, role, link_scope, message, notify):
        role = _role(role)
        item = self._item(ref)
        out: dict[str, Any] = {"shared": True, "role": role}
        if emails:
            await self.api.post(f"{item}/invite", json={
                "recipients": [{"email": e} for e in emails], "message": message or "", "requireSignIn": True,
                "sendInvitation": notify, "roles": ["write" if role == "writer" else "read"]})
            out["with"] = emails
        if link_scope:
            res = await self.api.post(f"{item}/createLink", json={
                "type": "edit" if role == "writer" else "view",
                "scope": "anonymous" if link_scope == "anyone" else "organization"})
            out["link"] = ((res or {}).get("link") or {}).get("webUrl")
            out["link_scope"] = link_scope
        return out


# --------------------------------------------------------------------------- Dropbox
DROPBOX_API = "https://api.dropboxapi.com/2"
DROPBOX_CONTENT = "https://content.dropboxapi.com/2"


def _dbx_arg(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=True)  # header must be ASCII (non-ASCII escaped as \\uXXXX)


class DropboxAdapter(DriveAdapter):
    kind = "dropbox"
    label = "Dropbox"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)

        async def auth() -> dict[str, str]:
            return {"Authorization": f"Bearer {await dropbox_token(cfg)}"}

        self.api = H.RestClient("Dropbox", DROPBOX_API, auth=auth, on_unauthorized=lambda: invalidate(cfg))

    @staticmethod
    def path(ref: str | None) -> str:
        r = (ref or "").strip()
        if r in ("", "/", "root"):
            return ""
        if r.startswith(("id:", "/", "ns:", "rev:")):
            return r.rstrip("/") if r.startswith("/") else r
        return "/" + r.rstrip("/")

    @staticmethod
    def norm(m: dict) -> dict:
        return H.compact({
            "id": m.get("id"), "name": m.get("name"), "type": "folder" if m.get(".tag") == "folder" else "file",
            "size": m.get("size"), "modified": m.get("server_modified"), "path": m.get("path_display"),
        })

    async def _meta(self, ref: str) -> dict:
        return await self.api.post("/files/get_metadata", json={"path": self.path(ref)})

    async def search(self, query, limit):
        data = await self.api.post("/files/search_v2", json={
            "query": query, "options": {"max_results": max(1, min(limit, 100)), "file_status": "active"}}) or {}
        return [self.norm((m.get("metadata") or {}).get("metadata") or {}) for m in data.get("matches", [])]

    async def list(self, folder, limit):
        data = await self.api.post("/files/list_folder", json={"path": self.path(folder),
                                                                "limit": max(1, min(limit, 2000))}) or {}
        return [self.norm(m) for m in data.get("entries", [])][:limit]

    async def get(self, ref):
        return self.norm(await self._meta(ref))

    async def content(self, ref, purpose="read", export_format=None):
        meta = await self._meta(ref)
        if meta.get(".tag") == "folder":
            raise ToolError(f"'{meta.get('name')}' is a folder — use cloud_list to see its contents.")
        if export_format:
            raise ToolError("export_format isn't supported for Dropbox files.")
        data, _ = await self.api.fetch(f"{DROPBOX_CONTENT}/files/download", method="POST",
                                       headers={"Dropbox-API-Arg": _dbx_arg({"path": meta.get("id") or self.path(ref)})})
        name = meta.get("name") or "file"
        return data, name, mimetypes.guess_type(name)[0] or "application/octet-stream"

    async def upload(self, data, name, folder, mime):
        if len(data) > 150 * 1024 * 1024:
            raise ToolError("Dropbox uploads are limited to 150 MB per file here.")
        res = await self.api.post(f"{DROPBOX_CONTENT}/files/upload", content=data, headers={
            "Dropbox-API-Arg": _dbx_arg({"path": f"{self.path(folder)}/{name}", "mode": "add", "autorename": True}),
            "Content-Type": "application/octet-stream"})
        return self.norm({**(res or {}), ".tag": "file"})

    async def create_folder(self, name, parent):
        res = await self.api.post("/files/create_folder_v2", json={"path": f"{self.path(parent)}/{name}",
                                                                    "autorename": True})
        return self.norm({**((res or {}).get("metadata") or {}), ".tag": "folder"})

    async def _display_path(self, ref: str) -> str:
        p = self.path(ref)
        if p.startswith("/"):
            return p
        return (await self._meta(ref)).get("path_display") or p

    async def move(self, ref, dest):
        src = await self._display_path(ref)
        res = await self.api.post("/files/move_v2", json={
            "from_path": src, "to_path": f"{await self._display_path(dest) if self.path(dest) else ''}/{src.rsplit('/', 1)[-1]}",
            "autorename": True})
        return self.norm((res or {}).get("metadata") or {})

    async def copy(self, ref, dest, name):
        src = await self._display_path(ref)
        folder = (await self._display_path(dest) if self.path(dest) else "") if dest else src.rsplit("/", 1)[0]
        res = await self.api.post("/files/copy_v2", json={
            "from_path": src, "to_path": f"{folder}/{name or src.rsplit('/', 1)[-1]}", "autorename": True})
        return self.norm((res or {}).get("metadata") or {})

    async def rename(self, ref, new_name):
        src = await self._display_path(ref)
        res = await self.api.post("/files/move_v2", json={
            "from_path": src, "to_path": f"{src.rsplit('/', 1)[0]}/{new_name}", "autorename": False})
        return self.norm((res or {}).get("metadata") or {})

    async def delete(self, ref, permanent):
        p = self.path(ref)
        if permanent:
            await self.api.post("/files/permanently_delete", json={"path": p})
            return {"deleted": True, "permanent": True, "path": p}
        res = await self.api.post("/files/delete_v2", json={"path": p})
        return {"deleted": True, "permanent": False, **self.norm((res or {}).get("metadata") or {}),
                "note": "Deleted files can be restored from Dropbox for a limited time."}

    async def share(self, ref, emails, role, link_scope, message, notify):
        meta = await self._meta(ref)
        role = _role(role)
        out: dict[str, Any] = {"shared": True, "name": meta.get("name"), "role": role}
        if emails:
            if meta.get(".tag") == "folder":
                raise ToolError("Sharing a Dropbox folder with people isn't supported here — share a link instead, "
                                "or share the folder from Dropbox.")
            await self.api.post("/sharing/add_file_member", json={
                "file": meta.get("id") or self.path(ref), "members": [{".tag": "email", "email": e} for e in emails],
                "custom_message": message or None, "quiet": not notify,
                "access_level": "editor" if role == "writer" else "viewer"})
            out["with"] = emails
        if link_scope:
            settings = {"audience": "public" if link_scope == "anyone" else "team",
                        "access": "editor" if role == "writer" else "viewer"}
            try:
                res = await self.api.post("/sharing/create_shared_link_with_settings",
                                          json={"path": meta.get("path_lower") or self.path(ref), "settings": settings})
                out["link"] = (res or {}).get("url")
            except H.ApiError as e:
                if "shared_link_already_exists" not in str(e):
                    raise
                links = await self.api.post("/sharing/list_shared_links", json={
                    "path": meta.get("path_lower") or self.path(ref), "direct_only": True}) or {}
                out["link"] = ((links.get("links") or [{}])[0]).get("url")
                out["note"] = "A shared link already existed; its existing settings were kept."
            out["link_scope"] = link_scope
        return out


# --------------------------------------------------------------------------- Box
BOX_API = "https://api.box.com/2.0"
BOX_UPLOAD = "https://upload.box.com/api/2.0/files/content"


class BoxAdapter(DriveAdapter):
    kind = "box"
    label = "Box"
    FIELDS = "id,type,name,size,modified_at,path_collection,shared_link"

    def __init__(self, ctx: ToolContext, cfg: dict[str, Any]):
        super().__init__(ctx, cfg)

        async def auth() -> dict[str, str]:
            return {"Authorization": f"Bearer {await box_token(cfg)}"}

        self.api = H.RestClient("Box", BOX_API, auth=auth, on_unauthorized=lambda: invalidate(cfg))

    @staticmethod
    def ref(ref: str | None, default: str = "folder") -> tuple[str, str]:
        r = (ref or "").strip()
        if r in ("", "/", "root", "0"):
            return "folder", "0"
        m = re.match(r"^(file|folder)[:_](\d+)$", r)
        if m:
            return m.group(1), m.group(2)
        m = re.search(r"app\.box\.com/(file|folder)/(\d+)", r)
        if m:
            return m.group(1), m.group(2)
        if r.isdigit():
            return default, r
        raise ToolError(f"Invalid Box item '{ref}' — use ids like 'file:123' or 'folder:456'.")

    @staticmethod
    def norm(i: dict) -> dict:
        entries = ((i.get("path_collection") or {}).get("entries") or [])[1:]
        path = "/".join(e.get("name", "") for e in entries)
        typ = i.get("type") or "file"
        return H.compact({
            "id": f"{typ}:{i.get('id')}", "name": i.get("name"), "type": "folder" if typ == "folder" else "file",
            "size": i.get("size"), "modified": i.get("modified_at"),
            "path": f"/{path}/{i.get('name')}" if path else f"/{i.get('name')}",
            "url": f"https://app.box.com/{typ}/{i.get('id')}",
            "shared_link": (i.get("shared_link") or {}).get("url"),
        })

    async def search(self, query, limit):
        data = await self.api.get("/search", params={"query": query, "limit": max(1, min(limit, 200)),
                                                     "fields": self.FIELDS}) or {}
        return [self.norm(i) for i in data.get("entries", [])]

    async def list(self, folder, limit):
        typ, fid = self.ref(folder, "folder")
        data = await self.api.get(f"/folders/{fid}/items", params={"limit": max(1, min(limit, 1000)),
                                                                   "fields": self.FIELDS}) or {}
        return [self.norm(i) for i in data.get("entries", [])]

    async def get(self, ref):
        typ, iid = self.ref(ref, "file")
        return self.norm(await self.api.get(f"/{typ}s/{iid}", params={"fields": self.FIELDS}))

    async def content(self, ref, purpose="read", export_format=None):
        typ, iid = self.ref(ref, "file")
        if typ == "folder":
            raise ToolError("That's a folder — use cloud_list to see its contents.")
        if export_format:
            raise ToolError("export_format isn't supported for Box files.")
        meta = await self.api.get(f"/files/{iid}", params={"fields": "id,name"})
        data, _ = await self.api.fetch(f"/files/{iid}/content")
        name = meta.get("name") or "file"
        return data, name, mimetypes.guess_type(name)[0] or "application/octet-stream"

    async def upload(self, data, name, folder, mime):
        typ, fid = self.ref(folder, "folder")
        for attempt in range(2):
            attrs = json.dumps({"name": name, "parent": {"id": fid}})
            try:
                res = await self.api.post(BOX_UPLOAD, files={"attributes": (None, attrs), "file": (name, data, mime)})
                return self.norm(((res or {}).get("entries") or [{}])[0])
            except H.ApiError as e:
                if e.status != 409 or attempt:
                    raise
                stem, dot, ext = name.rpartition(".")
                name = f"{stem or ext} ({uuid.uuid4().hex[:4]}){'.' + ext if stem else ''}"
        raise ToolError("Box upload failed.")  # pragma: no cover

    async def create_folder(self, name, parent):
        _, pid = self.ref(parent, "folder")
        return self.norm(await self.api.post("/folders", json={"name": name, "parent": {"id": pid}},
                                             params={"fields": self.FIELDS}))

    async def move(self, ref, dest):
        typ, iid = self.ref(ref, "file")
        _, did = self.ref(dest, "folder")
        return self.norm(await self.api.put(f"/{typ}s/{iid}", json={"parent": {"id": did}},
                                            params={"fields": self.FIELDS}))

    async def copy(self, ref, dest, name):
        typ, iid = self.ref(ref, "file")
        if dest:
            _, did = self.ref(dest, "folder")
        else:
            cur = await self.api.get(f"/{typ}s/{iid}", params={"fields": "parent"})
            did = (cur.get("parent") or {}).get("id") or "0"
        body: dict[str, Any] = {"parent": {"id": did}}
        if name:
            body["name"] = name
        return self.norm(await self.api.post(f"/{typ}s/{iid}/copy", json=body, params={"fields": self.FIELDS}))

    async def rename(self, ref, new_name):
        typ, iid = self.ref(ref, "file")
        return self.norm(await self.api.put(f"/{typ}s/{iid}", json={"name": new_name}, params={"fields": self.FIELDS}))

    async def delete(self, ref, permanent):
        typ, iid = self.ref(ref, "file")
        await self.api.delete(f"/{typ}s/{iid}", params={"recursive": True} if typ == "folder" else None)
        if permanent:
            await self.api.delete(f"/{typ}s/{iid}/trash")
        return {"deleted": True, "permanent": permanent, "id": f"{typ}:{iid}",
                "note": None if permanent else "Moved to the Box trash (restorable)."}

    async def share(self, ref, emails, role, link_scope, message, notify):
        typ, iid = self.ref(ref, "file")
        role = _role(role)
        out: dict[str, Any] = {"shared": True, "id": f"{typ}:{iid}", "role": role}
        for e in emails:
            await self.api.post("/collaborations", params={"notify": notify}, json={
                "item": {"type": typ, "id": iid}, "accessible_by": {"type": "user", "login": e},
                "role": "editor" if role == "writer" else "viewer"})
        if emails:
            out["with"] = emails
        if link_scope:
            res = await self.api.put(f"/{typ}s/{iid}", params={"fields": "shared_link"}, json={
                "shared_link": {"access": "open" if link_scope == "anyone" else "company"}})
            out["link"] = ((res or {}).get("shared_link") or {}).get("url")
            out["link_scope"] = link_scope
        return H.compact(out)


# --------------------------------------------------------------------------- local fallback: shared/ folder
class LocalDriveAdapter(DriveAdapter):
    kind = "local"
    label = "Workspace shared folder"
    remote = False
    TEXTY = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml", ".log"}

    def __init__(self, ctx: ToolContext):
        super().__init__(ctx, {})
        self.root = ctx.workspace.shared_root

    def _abs(self, ref: str | None, *, must_exist: bool = True) -> Path:
        r = (ref or "").strip().replace("\\", "/").lstrip("/")
        if r in ("", "root", "shared"):
            return self.root
        vpath = r if r == "shared" or r.startswith("shared/") else f"shared/{r}"
        return H.ws_path(self.ctx, vpath, must_exist=must_exist)

    def norm(self, p: Path) -> dict:
        info = self.ctx.workspace.info(p)
        return H.compact({"id": info["path"], "name": info["name"], "type": "folder" if info["is_dir"] else "file",
                          "mime": info["mime"], "size": None if info["is_dir"] else info["size"],
                          "modified": info["modified_at"]})

    def _writable(self) -> None:
        if not self.ctx.can_write:
            raise ToolError("Your role is read-only, so files can't be changed.")

    def _search_sync(self, query: str, limit: int) -> list[dict]:
        terms = [t for t in query.lower().split() if t]
        hits: list[Path] = []
        scanned: list[Path] = []
        for n, p in enumerate(self.root.rglob("*")):
            if n > 20000:
                break
            if all(t in p.name.lower() for t in terms):
                hits.append(p)
            elif p.is_file() and p.suffix.lower() in self.TEXTY and p.stat().st_size <= 512 * 1024:
                scanned.append(p)
            if len(hits) >= limit:
                break
        for p in scanned:
            if len(hits) >= limit:
                break
            try:
                text = p.read_text("utf-8", errors="ignore").lower()
            except OSError:
                continue
            if all(t in text for t in terms):
                hits.append(p)
        return [self.norm(p) for p in hits[:limit]]

    async def search(self, query, limit):
        return await asyncio.to_thread(self._search_sync, query, limit)

    async def list(self, folder, limit):
        p = self._abs(folder)
        if p.is_file():
            return [self.norm(p)]
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        return [self.norm(x) for x in items[:limit]]

    async def get(self, ref):
        return self.norm(self._abs(ref))

    async def content(self, ref, purpose="read", export_format=None):
        p = self._abs(ref)
        if p.is_dir():
            raise ToolError(f"'{self.ctx.workspace.virtual(p)}' is a folder — use cloud_list to see its contents.")
        if export_format:
            raise ToolError("export_format isn't supported for workspace files.")
        data = await asyncio.to_thread(p.read_bytes)
        return data, p.name, mimetypes.guess_type(p.name)[0] or "application/octet-stream"

    async def upload(self, data, name, folder, mime):
        self._writable()
        dest_dir = self._abs(folder or "", must_exist=False)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = H.unique_path(self.ctx, self.ctx.workspace.virtual(dest_dir), name)
        await asyncio.to_thread(target.write_bytes, data)
        return self.norm(target)

    async def create_folder(self, name, parent):
        self._writable()
        base = self._abs(parent or "", must_exist=True)
        target = self._abs(f"{self.ctx.workspace.virtual(base)}/{H.safe_filename(name, 'folder')}", must_exist=False)
        target.mkdir(parents=True, exist_ok=True)
        return self.norm(target)

    async def move(self, ref, dest):
        self._writable()
        src, dst_dir = self._abs(ref), self._abs(dest)
        if not dst_dir.is_dir():
            raise ToolError("The destination must be a folder.")
        if src == self.root:
            raise ToolError("Can't move the shared root folder.")
        target = dst_dir / src.name
        if target.exists():
            raise ToolError(f"'{self.ctx.workspace.virtual(target)}' already exists.")
        await asyncio.to_thread(shutil.move, str(src), str(target))
        return self.norm(target)

    async def copy(self, ref, dest, name):
        self._writable()
        src = self._abs(ref)
        dst_dir = self._abs(dest) if dest else src.parent
        target = H.unique_path(self.ctx, self.ctx.workspace.virtual(dst_dir), name or src.name)
        if src.is_dir():
            await asyncio.to_thread(shutil.copytree, str(src), str(target))
        else:
            await asyncio.to_thread(shutil.copy2, str(src), str(target))
        return self.norm(target)

    async def rename(self, ref, new_name):
        self._writable()
        src = self._abs(ref)
        if src == self.root:
            raise ToolError("Can't rename the shared root folder.")
        target = src.parent / H.safe_filename(new_name)
        if target.exists():
            raise ToolError(f"'{self.ctx.workspace.virtual(target)}' already exists.")
        src.rename(target)
        return self.norm(target)

    async def delete(self, ref, permanent):
        self._writable()
        if not self.ctx.is_admin:
            raise ToolError("Only workspace admins can delete files in the shared folder.")
        p = self._abs(ref)
        if p == self.root:
            raise ToolError("Can't delete the shared root folder.")
        vpath = self.ctx.workspace.virtual(p)
        if p.is_dir():
            await asyncio.to_thread(shutil.rmtree, p)
        else:
            p.unlink()
        return {"deleted": True, "permanent": True, "id": vpath}

    async def share(self, ref, emails, role, link_scope, message, notify):
        raise ToolError("Files in shared/ are already visible to everyone in this workspace. Sharing with outside "
                        "people needs a connected cloud drive (Google Drive, OneDrive, Dropbox or Box).")


ADAPTERS: dict[str, type[DriveAdapter]] = {"google_drive": GoogleDriveAdapter, "microsoft365": OneDriveAdapter,
                                           "dropbox": DropboxAdapter, "box": BoxAdapter}


async def drive_adapter(ctx: ToolContext, account: str | None = None) -> DriveAdapter:
    if account and account.strip().lower() in ("local", "shared", "workspace"):
        return LocalDriveAdapter(ctx)
    if not await H.connected(ctx, DRIVE_KINDS):
        if account:
            raise ToolError(f"No drive account named '{account}' is connected (use account='local' for the "
                            "workspace shared folder).")
        return LocalDriveAdapter(ctx)
    kind, cfg = await H.pick_connector(ctx, DRIVE_KINDS, account, what="Cloud drive")
    return ADAPTERS[kind](ctx, cfg)


# =========================================================================== tools
group = ToolGroup(
    "drive", "Cloud drive",
    "Search, read, upload, organise and share files in Google Drive, OneDrive/SharePoint, Dropbox or Box (or the "
    "workspace's shared folder when no drive is connected).",
    prefix="cloud", icon="hard-drive", category="productivity", connector_kinds=DRIVE_KINDS, connector_optional=True,
)


def _lim(n: int, hi: int = 200) -> int:
    return max(1, min(int(n or 20), hi))


@group.tool(risk=Risk.LOW, scope="read")
async def search(ctx: ToolContext, query: str, limit: int = 20, account: str | None = None) -> dict:
    """Search files by name and content.

    Args:
        query: Words to look for.
        limit: Maximum results.
        account: Drive connector name or kind (google_drive, microsoft365, dropbox, box, or "local").
    """
    ad = await drive_adapter(ctx, account)
    rows = await ad.search(query, _lim(limit))
    return {"drive": ad.label, "count": len(rows), "files": rows}


@group.tool("list", risk=Risk.LOW, scope="read")
async def list_files(ctx: ToolContext, folder: str | None = None, limit: int = 50, account: str | None = None) -> dict:
    """List the files and folders inside a folder (default: the root).

    Args:
        folder: Folder id, path (e.g. "/Reports/2026" for OneDrive/Dropbox) or URL; empty = root.
        limit: Maximum entries.
        account: Drive connector name or kind (or "local").
    """
    ad = await drive_adapter(ctx, account)
    rows = await ad.list(folder, _lim(limit, 1000))
    return {"drive": ad.label, "folder": folder or "/", "count": len(rows), "items": rows}


@group.tool(risk=Risk.LOW, scope="read")
async def get(ctx: ToolContext, file_id: str, account: str | None = None) -> dict:
    """Get a file's or folder's metadata (name, type, size, modified time, link).

    Args:
        file_id: File id, path or URL.
        account: Drive connector name or kind (or "local").
    """
    ad = await drive_adapter(ctx, account)
    return {"drive": ad.label, **await ad.get(file_id)}


@group.tool(risk=Risk.LOW, scope="read")
async def read(ctx: ToolContext, file_id: str, max_chars: int = 20000, offset: int = 0,
               account: str | None = None) -> dict:
    """Read a document's text: pdf, docx, xlsx, pptx, txt, md, html, csv (Google Docs/Sheets/Slides are exported).

    Args:
        file_id: File id, path or URL.
        max_chars: Maximum characters to return.
        offset: Character offset to continue reading a long document.
        account: Drive connector name or kind (or "local").
    """
    ad = await drive_adapter(ctx, account)
    data, name, mime = await ad.content(file_id, "read")
    text = await H.extract_text_async(data, name, mime)
    return {"drive": ad.label, "name": name, "mime": mime, **H.window(text, max(200, min(int(max_chars or 20000), 200_000)),
                                                                      int(offset or 0))}


@group.tool(risk=Risk.LOW, scope="write")
async def download(ctx: ToolContext, file_id: str, export_format: str | None = None, account: str | None = None) -> dict:
    """Download a file into the workspace `downloads/` folder (never overwrites existing files).

    Args:
        file_id: File id, path or URL.
        export_format: For Google Docs/Sheets/Slides: docx, xlsx, pptx, pdf, csv, txt, md...; for OneDrive: pdf.
        account: Drive connector name or kind (or "local").
    """
    ad = await drive_adapter(ctx, account)
    data, name, mime = await ad.content(file_id, "download", export_format)
    saved = await H.save_bytes(ctx, data, name, "downloads")
    return {**saved, "source": f"{ad.label}: {file_id}"}


@group.tool(risk=Risk.MEDIUM, scope="write",
            preview=lambda a: H.preview("Upload file to cloud drive", [
                ("Drive", a.get("account") or "default drive"), ("Workspace file", a.get("path")),
                ("Destination folder", a.get("folder") or "root"), ("Name", a.get("name"))]))
async def upload(ctx: ToolContext, path: str, folder: str | None = None, name: str | None = None,
                 account: str | None = None) -> dict:
    """Upload a workspace file to the drive (an existing name gets a numbered copy rather than being overwritten).

    Args:
        path: Workspace file path, e.g. "output/report.pdf".
        folder: Destination folder id or path (default: root).
        name: File name in the drive (default: the workspace file's name).
        account: Drive connector name or kind (or "local").
    """
    ad = await drive_adapter(ctx, account)
    data, fname, mime = await H.read_workspace_file(ctx, path)
    res = await ad.upload(data, name or fname, folder, mime)
    return {"drive": ad.label, "uploaded": True, **res}


@group.tool(risk=Risk.MEDIUM, scope="write")
async def create_folder(ctx: ToolContext, name: str, parent: str | None = None, account: str | None = None) -> dict:
    """Create a folder.

    Args:
        name: Folder name.
        parent: Parent folder id or path (default: root).
        account: Drive connector name or kind (or "local").
    """
    ad = await drive_adapter(ctx, account)
    return {"drive": ad.label, "created": True, **await ad.create_folder(name, parent)}


@group.tool(risk=Risk.MEDIUM, scope="write")
async def move(ctx: ToolContext, file_id: str, destination_folder: str, account: str | None = None) -> dict:
    """Move a file or folder into another folder.

    Args:
        file_id: File/folder id or path.
        destination_folder: Target folder id or path ("root" for the top level).
        account: Drive connector name or kind (or "local").
    """
    ad = await drive_adapter(ctx, account)
    return {"drive": ad.label, "moved": True, **await ad.move(file_id, destination_folder)}


@group.tool("copy", risk=Risk.MEDIUM, scope="write")
async def copy_file(ctx: ToolContext, file_id: str, destination_folder: str | None = None, name: str | None = None,
                    account: str | None = None) -> dict:
    """Copy a file (to the same folder by default).

    Args:
        file_id: File id or path.
        destination_folder: Target folder id or path (default: the same folder).
        name: Name of the copy.
        account: Drive connector name or kind (or "local").
    """
    ad = await drive_adapter(ctx, account)
    return {"drive": ad.label, "copied": True, **await ad.copy(file_id, destination_folder, name)}


@group.tool(risk=Risk.MEDIUM, scope="write")
async def rename(ctx: ToolContext, file_id: str, new_name: str, account: str | None = None) -> dict:
    """Rename a file or folder.

    Args:
        file_id: File/folder id or path.
        new_name: New name (include the extension for files).
        account: Drive connector name or kind (or "local").
    """
    if not new_name.strip() or "/" in new_name or "\\" in new_name:
        raise ToolError("new_name must be a plain file name.")
    ad = await drive_adapter(ctx, account)
    return {"drive": ad.label, "renamed": True, **await ad.rename(file_id, new_name.strip())}


@group.tool(risk=Risk.CRITICAL, scope="write",
            preview=lambda a: H.preview("DELETE file from cloud drive", [
                ("Drive", a.get("account") or "default drive"), ("File / folder", a.get("file_id")),
                ("Permanent (skips trash)", bool(a.get("permanent")))],
                note="Folders are deleted with everything inside them."))
async def delete(ctx: ToolContext, file_id: str, permanent: bool = False, account: str | None = None) -> dict:
    """Delete a file or folder (moved to the trash/recycle bin unless permanent; workspace files are removed).

    Args:
        file_id: File/folder id or path.
        permanent: Skip the trash and delete permanently.
        account: Drive connector name or kind (or "local").
    """
    ad = await drive_adapter(ctx, account)
    return {"drive": ad.label, **H.compact(await ad.delete(file_id, permanent))}


def _share_preview(a: dict) -> str:
    scope = a.get("link_scope")
    scope_txt = {"anyone": "ANYONE WITH THE LINK (public)", "organization": "everyone in the organization"}.get(
        scope or "", scope)
    return H.preview("Share file", [
        ("Drive", a.get("account") or "default drive"), ("File", a.get("file_id")),
        ("Share with", H.as_list(a.get("emails"))), ("Access", a.get("role") or "reader"),
        ("Create link for", scope_txt), ("Notify people by email", a.get("notify", True))],
        a.get("message") if a.get("message") else None, body_label="Message to recipients")


@group.tool(risk=Risk.HIGH, scope="write", content_fields=("message",), preview=_share_preview)
async def share(ctx: ToolContext, file_id: str, emails: list[str] | None = None, role: str = "reader",
                link_scope: str | None = None, message: str | None = None, notify: bool = True,
                account: str | None = None) -> dict:
    """Share a file or folder with people (by email) and/or create a sharing link.

    Args:
        file_id: File/folder id, path or URL.
        emails: People to give access.
        role: reader (view), commenter (Google only) or writer (edit).
        link_scope: Create a link: "organization" (anyone in your org) or "anyone" (public). Omit for no link.
        message: Note included in the invitation email.
        notify: Email the people you share with.
        account: Drive connector name or kind.
    """
    if link_scope and link_scope not in ("anyone", "organization"):
        raise ToolError("link_scope must be 'organization' or 'anyone'.")
    addrs = [a.split("<")[-1].strip(" >") for a in norm_addresses(emails, "emails")]
    if not addrs and not link_scope:
        raise ToolError("Pass emails to share with and/or a link_scope.")
    ad = await drive_adapter(ctx, account)
    return {"drive": ad.label, **await ad.share(file_id, addrs, role, link_scope, message, notify)}


# =========================================================================== connector kinds
@H.connector_test
async def _test_google_drive(cfg: dict) -> tuple[bool, str]:
    api = google_client(cfg, GDRIVE, service="Google Drive", scopes=GDRIVE_SCOPES)
    data = await api.get("/about", params={"fields": "user(displayName,emailAddress)"}) or {}
    user = data.get("user") or {}
    return True, f"Connected to Google Drive as {user.get('emailAddress') or user.get('displayName') or 'unknown'}."


@H.connector_test
async def _test_dropbox(cfg: dict) -> tuple[bool, str]:
    ad = DropboxAdapter(ToolContext(org_id="_test", user_id=None), cfg)
    data = await ad.api.post("/users/get_current_account", content=b"null",
                             headers={"Content-Type": "application/json"}) or {}
    return True, f"Connected to Dropbox as {data.get('email') or (data.get('name') or {}).get('display_name', 'unknown')}."


@H.connector_test
async def _test_box(cfg: dict) -> tuple[bool, str]:
    ad = BoxAdapter(ToolContext(org_id="_test", user_id=None), cfg)
    me = await ad.api.get("/users/me", params={"fields": "name,login"}) or {}
    return True, f"Connected to Box as {me.get('login') or me.get('name') or 'unknown'}."


register_connector_kind(ConnectorKindSpec(
    kind="google_drive", name="Google Drive", description="Files, Google Docs/Sheets/Slides and sharing in Google Drive.",
    icon="hard-drive", category="productivity", tool_groups=["drive"],
    fields=google_oauth_fields("https://www.googleapis.com/auth/drive") + [
        ConnectorField("domain", "Organization domain (optional)", placeholder="company.com",
                       help="Used for 'organization' link sharing; defaults to the file owner's domain."),
    ],
    docs_url="https://developers.google.com/drive/api/guides/about-sdk", test=_test_google_drive,
))

register_connector_kind(ConnectorKindSpec(
    kind="dropbox", name="Dropbox", description="Files, folders and shared links in Dropbox.",
    icon="hard-drive", category="productivity", tool_groups=["drive"],
    fields=[
        ConnectorField("app_key", "App key", placeholder="abc123xyz", help="Dropbox App Console → your app."),
        ConnectorField("app_secret", "App secret", type="password", secret=True),
        ConnectorField("refresh_token", "Refresh token", type="password", secret=True,
                       help="From the OAuth flow with token_access_type=offline (recommended; doesn't expire)."),
        ConnectorField("access_token", "Access token (alternative)", type="password", secret=True,
                       help="A short-lived token for quick tests — expires after ~4 hours."),
    ],
    docs_url="https://developers.dropbox.com/oauth-guide", test=_test_dropbox,
))

register_connector_kind(ConnectorKindSpec(
    kind="box", name="Box", description="Files, folders, collaborations and shared links in Box.",
    icon="hard-drive", category="productivity", tool_groups=["drive"],
    fields=[
        ConnectorField("client_id", "Client ID", help="Box Developer Console → Platform app with Client Credentials Grant."),
        ConnectorField("client_secret", "Client secret", type="password", secret=True),
        ConnectorField("subject_type", "Act as", type="select", options=["enterprise", "user"],
                       help="enterprise = the app's service account; user = a specific managed user."),
        ConnectorField("subject_id", "Enterprise ID or user ID", placeholder="123456"),
        ConnectorField("developer_token", "Developer token (alternative)", type="password", secret=True,
                       help="For quick tests only — expires after 60 minutes."),
    ],
    docs_url="https://developer.box.com/guides/authentication/client-credentials/", test=_test_box,
))
