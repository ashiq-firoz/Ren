"""Markdown parser: ATX/setext headings (via markdown-it-py, so '#' inside code fences is ignored); the source
text between headings is kept verbatim (tables, lists and code stay markdown)."""
from __future__ import annotations

import re

from markdown_it import MarkdownIt

from app.rag.parsers import Block, ParsedDocument

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n(?:---|\.\.\.)\s*(?:\n|\Z)", re.S)
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


def plain_heading(text: str) -> str:
    t = _LINK_RE.sub(r"\1", text or "")
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"[*_`~]+", "", t)
    t = re.sub(r"\s+#+\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_markdown(text: str) -> ParsedDocument:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    title = ""
    fm = _FRONT_MATTER_RE.match(text)
    if fm:
        mt = re.search(r"(?m)^title\s*:\s*['\"]?(.+?)['\"]?\s*$", fm.group(1))
        if mt:
            title = mt.group(1).strip()
        # keep line numbering stable for markdown-it maps: blank out the front matter
        text = "\n" * fm.group(0).count("\n") + text[fm.end():]
    md = MarkdownIt("commonmark").enable("table").enable("strikethrough")
    tokens = md.parse(text)
    lines = text.split("\n")
    heads: list[tuple[int, int, int, str]] = []
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.map and tok.level == 0:
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            htext = plain_heading(inline.content if inline is not None else "")
            if htext:
                heads.append((tok.map[0], tok.map[1], int(tok.tag[1]), htext))
    blocks: list[Block] = []
    pos = 0

    def add_text(seg: list[str]) -> None:
        body = "\n".join(seg).strip("\n")
        if body.strip():
            blocks.append(Block("text", body))

    for start, end, level, htext in heads:
        add_text(lines[pos:start])
        blocks.append(Block("heading", htext, level))
        pos = end
    add_text(lines[pos:])
    return ParsedDocument(title=title, blocks=blocks, format="markdown")
