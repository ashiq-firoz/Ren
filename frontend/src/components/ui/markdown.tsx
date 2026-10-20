"use client";

import Link from "next/link";
import { memo, useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { CitationChip } from "@/components/domain/citations";
import { FileLink } from "@/components/files/file-link";
import { splitWorkspacePaths, viewerHref, workspacePathOf } from "@/lib/files";
import type { Citation } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CopyButton } from "./copy-button";

/* ------------------------------------------------------------------ */
/* remark plugin: turn `[n]` / `[1, 2]` into citation nodes            */
/* ------------------------------------------------------------------ */

interface MdNode {
  type: string;
  value?: string;
  url?: string;
  title?: string | null;
  children?: MdNode[];
  data?: Record<string, unknown>;
}

const CITE_RE = /\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]/g;
const SKIP = new Set(["link", "linkReference", "code", "inlineCode", "definition", "html"]);

function citeNode(n: number): MdNode {
  return {
    type: "citation",
    data: { hName: "sup", hProperties: { dataCite: String(n) } },
    children: [{ type: "text", value: String(n) }],
  };
}

function splitText(value: string, ids: Set<number>): MdNode[] | null {
  CITE_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  let last = 0;
  const out: MdNode[] = [];
  let found = false;
  while ((match = CITE_RE.exec(value))) {
    const nums = match[1].split(",").map((x) => Number(x.trim()));
    if (!nums.some((n) => ids.has(n))) continue;
    found = true;
    if (match.index > last) out.push({ type: "text", value: value.slice(last, match.index) });
    nums.forEach((n) => out.push(ids.has(n) ? citeNode(n) : { type: "text", value: `[${n}]` }));
    last = match.index + match[0].length;
  }
  if (!found) return null;
  if (last < value.length) out.push({ type: "text", value: value.slice(last) });
  return out;
}

function remarkCitations(options: { ids: number[] }) {
  const ids = new Set(options.ids);
  return (tree: MdNode) => {
    if (ids.size === 0) return;
    const visit = (node: MdNode) => {
      if (!node.children || SKIP.has(node.type)) return;
      let changed = false;
      const next: MdNode[] = [];
      for (const child of node.children) {
        if (child.type === "text" && child.value && child.value.includes("[")) {
          const parts = splitText(child.value, ids);
          if (parts) {
            next.push(...parts);
            changed = true;
            continue;
          }
        }
        visit(child);
        next.push(child);
      }
      if (changed) node.children = next;
    };
    visit(tree);
  };
}

/* ------------------------------------------------------------------ */
/* remark plugin: workspace paths → links to the file viewer           */
/* ------------------------------------------------------------------ */

const FILE_SKIP = new Set(["link", "linkReference", "code", "inlineCode", "definition", "html", "citation", "image", "imageReference"]);

/** A link the `a` renderer turns into a <FileLink> (marked with `data-file`). */
function fileLinkNode(path: string, children: MdNode[]): MdNode {
  return { type: "link", url: viewerHref({ path }), title: null, children, data: { hProperties: { dataFile: path } } };
}

function safeDecode(url: string) {
  try {
    return decodeURIComponent(url);
  } catch {
    return url;
  }
}

/**
 * `output/report.md` in backticks, bare workspace paths in prose, and links whose target is a workspace
 * path all open the file viewer. Paths inside code blocks and existing links stay as they are.
 */
function remarkFilePaths() {
  return (tree: MdNode) => {
    const visit = (node: MdNode) => {
      if (!node.children) return;
      let changed = false;
      const next: MdNode[] = [];
      for (const child of node.children) {
        if (child.type === "inlineCode") {
          const path = workspacePathOf(child.value);
          if (path) {
            next.push(fileLinkNode(path, [{ type: "text", value: path }]));
            changed = true;
            continue;
          }
        } else if (child.type === "text" && child.value) {
          const parts = splitWorkspacePaths(child.value);
          if (parts) {
            for (const part of parts) {
              next.push("path" in part ? fileLinkNode(part.path, [{ type: "text", value: part.path }]) : { type: "text", value: part.text });
            }
            changed = true;
            continue;
          }
        } else if (child.type === "link" && child.url) {
          // [the report](output/report.md) — keep the label, point it at the viewer.
          const path = workspacePathOf(safeDecode(child.url));
          if (path) {
            const hProperties = { ...((child.data?.hProperties as Record<string, unknown> | undefined) ?? {}), dataFile: path, dataFileLabel: "custom" };
            child.url = viewerHref({ path });
            child.data = { ...child.data, hProperties };
          }
        }
        if (!FILE_SKIP.has(child.type)) visit(child);
        next.push(child);
      }
      if (changed) node.children = next;
    };
    visit(tree);
  };
}

/* ------------------------------------------------------------------ */
/* Renderers                                                           */
/* ------------------------------------------------------------------ */

interface HastLike {
  type?: string;
  value?: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastLike[];
}

function hastText(node: HastLike | undefined): string {
  if (!node) return "";
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(hastText).join("");
}

function codeLanguage(node: HastLike | undefined): string | null {
  const code = node?.children?.find((c) => c.tagName === "code");
  const cls = code?.properties?.className;
  const list = Array.isArray(cls) ? cls : typeof cls === "string" ? cls.split(" ") : [];
  const lang = list.map(String).find((c) => c.startsWith("language-"));
  return lang ? lang.slice("language-".length) : null;
}

function buildComponents(citations: Citation[] | undefined): Components {
  const byId = new Map((citations ?? []).map((c) => [c.id, c]));
  return {
    sup({ node, children, ...props }) {
      const cite = (node as HastLike | undefined)?.properties?.dataCite;
      if (cite !== undefined && cite !== null) {
        const n = Number(cite);
        return <CitationChip n={n} citation={byId.get(n)} />;
      }
      return <sup {...props}>{children}</sup>;
    },
    pre({ node, children }) {
      const hast = node as HastLike | undefined;
      const text = hastText(hast).replace(/\n$/, "");
      const lang = codeLanguage(hast);
      return (
        <div className="code-block group/code">
          <div className="flex h-8 items-center justify-between border-b border-border px-3 text-[11px] text-subtle">
            <span className="font-mono">{lang ?? "text"}</span>
            <CopyButton text={text} size="xs" label="Copy code" showLabel className="opacity-80 group-hover/code:opacity-100" />
          </div>
          <pre>{children}</pre>
        </div>
      );
    },
    table({ children }) {
      return (
        <div className="table-wrap">
          <table>{children}</table>
        </div>
      );
    },
    a({ node, href, children }) {
      const props = (node as HastLike | undefined)?.properties;
      if (typeof props?.dataFile === "string" && props.dataFile) {
        return (
          <FileLink path={props.dataFile} mono={props.dataFileLabel === undefined}>
            {children}
          </FileLink>
        );
      }
      const url = href ?? "";
      if (url.startsWith("/")) return <Link href={url}>{children}</Link>;
      return (
        <a href={url} target="_blank" rel="noopener noreferrer">
          {children}
        </a>
      );
    },
  };
}

export interface MarkdownProps {
  content: string;
  citations?: Citation[];
  streaming?: boolean;
  size?: "sm" | "md";
  className?: string;
}

function MarkdownImpl({ content, citations, streaming, size = "md", className }: MarkdownProps) {
  const ids = useMemo(() => (citations ?? []).map((c) => c.id), [citations]);
  const components = useMemo(() => buildComponents(citations), [citations]);
  const remarkPlugins = useMemo(
    () =>
      [remarkGfm, [remarkCitations, { ids }], remarkFilePaths] as NonNullable<Parameters<typeof ReactMarkdown>[0]["remarkPlugins"]>,
    [ids],
  );
  return (
    <div className={cn("md", size === "sm" && "md-sm", streaming && "is-streaming", className)}>
      <ReactMarkdown remarkPlugins={remarkPlugins} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}

export const Markdown = memo(MarkdownImpl);

/** Plain-text fallback renderer for short previews (strips markdown syntax). */
export function stripMarkdown(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[#>*_~|-]{1,}/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}
