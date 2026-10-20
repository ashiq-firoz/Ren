import {
  Database,
  File,
  FileArchive,
  FileAudio,
  FileCode,
  FileImage,
  FileJson,
  FileSpreadsheet,
  FileText,
  FileVideo,
  Globe,
  Presentation,
  type LucideIcon,
} from "lucide-react";
import { api, errorMessage } from "./api";
import type { FileSource } from "./query-keys";
import { toast } from "./toast";
import type { FileKind } from "./types";

export type { FileSource };

/* ------------------------------------------------------------------ */
/* Kinds                                                               */
/* ------------------------------------------------------------------ */

export const FILE_KIND_LABEL: Record<FileKind, string> = {
  document: "Document",
  spreadsheet: "Spreadsheet",
  presentation: "Presentation",
  pdf: "PDF",
  image: "Image",
  data: "Data",
  code: "Code",
  web: "Web page",
  text: "Text",
  audio: "Audio",
  video: "Video",
  archive: "Archive",
  other: "File",
};

/** Icons by kind, plus a couple of friendlier ones within "data". */
export const FILE_ICONS = {
  document: FileText,
  spreadsheet: FileSpreadsheet,
  presentation: Presentation,
  pdf: FileText,
  image: FileImage,
  data: Database,
  json: FileJson,
  code: FileCode,
  web: Globe,
  text: FileText,
  audio: FileAudio,
  video: FileVideo,
  archive: FileArchive,
  other: File,
} satisfies Record<FileKind | "json", LucideIcon>;

export type FileIconKey = keyof typeof FILE_ICONS;

/** Key into FILE_ICONS (look the icon up there so it stays a static component). */
export function fileIconKey(kind: FileKind | null | undefined, name?: string | null): FileIconKey {
  const k = kind ?? (name ? fileKindFromName(name) : "other");
  // Within "data", JSON reads better as braces and CSV as a sheet than as a database.
  if (k === "data" && name) {
    if (/\.(json|jsonl|ndjson)$/i.test(name)) return "json";
    if (/\.(csv|tsv)$/i.test(name)) return "spreadsheet";
  }
  return k in FILE_ICONS ? k : "other";
}

/** Filter options for the Outputs page (in display order). */
export const KIND_FILTERS: { value: FileKind; label: string }[] = [
  { value: "document", label: "Documents" },
  { value: "spreadsheet", label: "Spreadsheets" },
  { value: "presentation", label: "Presentations" },
  { value: "pdf", label: "PDFs" },
  { value: "image", label: "Images" },
  { value: "data", label: "Data" },
  { value: "code", label: "Code" },
  { value: "web", label: "Web pages" },
  { value: "text", label: "Text" },
];

const EXT_KIND: Record<string, FileKind> = {};
const register = (kind: FileKind, exts: string) => exts.split(" ").forEach((e) => (EXT_KIND[e] = kind));
// Mirrors the backend's KIND_EXTS (services/files.py).
register("document", "docx doc odt rtf md markdown");
register("spreadsheet", "xlsx xlsm xls ods");
register("presentation", "pptx ppt odp key");
register("pdf", "pdf");
register("image", "png jpg jpeg gif webp svg bmp ico avif");
register("data", "csv tsv json jsonl ndjson parquet pq xml yaml yml");
register("code", "py js mjs cjs ts tsx jsx sql sh bash ps1 go rs java kt rb php c h cpp hpp cs swift r ipynb toml ini cfg css scss");
register("web", "html htm");
register("text", "txt log rst text");
register("audio", "mp3 wav m4a ogg flac aac");
register("video", "mp4 mov webm mkv avi");
register("archive", "zip tar gz tgz 7z rar bz2 xz");

/** Client-side fallback for when the server didn't send `kind` (e.g. workspace listings). */
export function fileKindFromName(name: string): FileKind {
  const ext = /\.([A-Za-z0-9]+)$/.exec(name)?.[1]?.toLowerCase();
  return (ext && EXT_KIND[ext]) || "other";
}

export function fileNameOf(path: string): string {
  const clean = path.replace(/\/+$/, "");
  return clean.slice(clean.lastIndexOf("/") + 1) || clean;
}

/* ------------------------------------------------------------------ */
/* Workspace paths                                                     */
/* ------------------------------------------------------------------ */

/** Top-level folders of a user's workspace, in the order the browser shows them. */
export const TOP_FOLDERS: { name: string; description: string }[] = [
  { name: "output", description: "What your agents produced" },
  { name: "uploads", description: "Files you attached in chat" },
  { name: "shared", description: "Shared with the whole workspace" },
  { name: "workspace", description: "Agents' working files" },
  { name: "projects", description: "Project folders" },
  { name: "downloads", description: "Files agents downloaded" },
];

const ROOTS = "(?:output|uploads|shared|workspace|projects|downloads|temp)";
/** Optional `./` or `/`, a top-level folder, non-whitespace, ending in a file extension. */
const WORKSPACE_PATH = new RegExp(`^(?:\\./|/)?(${ROOTS}/\\S+\\.[A-Za-z0-9]{1,8})$`);
const CANDIDATE = new RegExp(`(?:\\./|/)?${ROOTS}/\\S+`, "g");
/** A path can't start right after these (it would be part of a longer token or URL). */
const BEFORE_PATH = /[\w./\\@~-]/;
const TRAILING_PUNCT = /[.,;:!?)\]}'"’”»。，；：！？）]+$/;

/** Normalised workspace path (no leading `./` or `/`) when the whole text is one; else null. */
export function workspacePathOf(text: string | null | undefined): string | null {
  if (!text) return null;
  const m = WORKSPACE_PATH.exec(text.trim());
  return m ? m[1] : null;
}

export function isWorkspacePath(text: string | null | undefined): boolean {
  return workspacePathOf(text) !== null;
}

export type TextSegment = { text: string } | { path: string; raw: string };

/** Split prose into text and workspace-path segments (trailing punctuation stays text); null when there are none. */
export function splitWorkspacePaths(value: string): TextSegment[] | null {
  if (!value.includes("/")) return null;
  CANDIDATE.lastIndex = 0;
  const out: TextSegment[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = CANDIDATE.exec(value))) {
    const start = match.index;
    if (start > 0 && BEFORE_PATH.test(value[start - 1])) continue;
    const raw = match[0].replace(TRAILING_PUNCT, "");
    const path = workspacePathOf(raw);
    if (!path) continue;
    if (start > last) out.push({ text: value.slice(last, start) });
    out.push({ path, raw });
    last = start + raw.length;
    CANDIDATE.lastIndex = last;
  }
  if (out.length === 0) return null;
  if (last < value.length) out.push({ text: value.slice(last) });
  return out;
}

/* ------------------------------------------------------------------ */
/* Viewer & downloads                                                  */
/* ------------------------------------------------------------------ */

/** Full-page viewer URL for a file. */
export function viewerHref(source: FileSource): string {
  return "artifactId" in source
    ? `/outputs/view?artifact=${encodeURIComponent(source.artifactId)}`
    : `/outputs/view?path=${encodeURIComponent(source.path)}`;
}

export function sameSource(a: FileSource | null | undefined, b: FileSource | null | undefined): boolean {
  if (!a || !b) return false;
  if ("artifactId" in a) return "artifactId" in b && a.artifactId === b.artifactId;
  return "path" in b && a.path === b.path;
}

/** Download a file by artifact id or workspace path (toasts on failure). */
export async function downloadFile(source: FileSource, name?: string | null): Promise<void> {
  try {
    if ("artifactId" in source) await api.download(`/artifacts/${source.artifactId}/download`, undefined, name || "download");
    else await api.download("/files/download", { path: source.path }, name || fileNameOf(source.path));
  } catch (err) {
    toast.error("Download failed", { description: errorMessage(err) });
  }
}
