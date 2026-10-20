"""HTML parser: h1–h6 headings; paragraphs, lists, tables (→ markdown) and code blocks in document order.
Navigation, scripts, styles, forms, cookie banners and page chrome are stripped."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.rag.parsers import Block, ParsedDocument, table_to_markdown
from app.rag.textutil import clean_inline

REMOVE_TAGS = ["script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed", "nav",
               "form", "button", "select", "input", "textarea", "dialog", "menu", "link", "meta", "head"]
CHROME_TAGS = ["header", "footer", "aside"]
NOISE_RE = re.compile(
    r"(?:^|[-_\s])(nav|navbar|navigation|menu|footer|sidebar|breadcrumbs?|cookie|consent|banner|advert|ads|social|"
    r"share|sharing|popup|modal|subscribe|newsletter|skip-link)(?:$|[-_\s])", re.I)
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
TEXT_BLOCKS = {"p", "blockquote", "dd", "dt", "figcaption", "address", "summary", "caption", "legend"}
BLOCKISH = {"p", "div", "section", "article", "main", "ul", "ol", "dl", "table", "pre", "blockquote", "figure",
            "h1", "h2", "h3", "h4", "h5", "h6", "details", "hr", "li"}


def _is_noise(tag: Tag) -> bool:
    if tag.name in ("body", "main", "article", "html"):
        return False
    attrs = " ".join([*(tag.get("class") or []), str(tag.get("id") or ""), str(tag.get("role") or "")])
    if not attrs.strip() or not NOISE_RE.search(attrs):
        return False
    return tag.find(["h1", "h2"]) is None


def _list_text(lst: Tag, depth: int = 0) -> str:
    lines: list[str] = []
    ordered = lst.name == "ol"
    n = 0
    for li in lst.find_all("li", recursive=False):
        n += 1
        nested = [c for c in li.find_all(["ul", "ol"], recursive=False)]
        for c in nested:
            c.extract()
        text = clean_inline(li.get_text(" ", strip=True))
        if text:
            marker = f"{n}." if ordered else "-"
            lines.append("  " * depth + f"{marker} {text}")
        for c in nested:
            sub = _list_text(c, depth + 1)
            if sub:
                lines.append(sub)
    return "\n".join(lines)


def _table_md(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue  # rows of nested tables are rendered inside their cell text
        cells = [clean_inline(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"], recursive=False)]
        if any(cells):
            rows.append(cells)
    return table_to_markdown(rows) if rows else ""


class _Walker:
    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.pending: list[str] = []

    def flush(self) -> None:
        text = clean_inline(" ".join(self.pending))
        self.pending = []
        if text:
            self.blocks.append(Block("text", text))

    def walk(self, el: Tag) -> None:
        for child in el.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                t = str(child)
                if t.strip():
                    self.pending.append(t.strip())
                continue
            if not isinstance(child, Tag):
                continue
            name = (child.name or "").lower()
            if name in HEADINGS:
                self.flush()
                text = clean_inline(child.get_text(" ", strip=True))
                if text:
                    self.blocks.append(Block("heading", text, HEADINGS[name]))
            elif name == "table":
                self.flush()
                md = _table_md(child)
                if md:
                    self.blocks.append(Block("table", md))
            elif name == "pre":
                self.flush()
                code = child.get_text("", strip=False).strip("\n")
                if code.strip():
                    self.blocks.append(Block("code", f"```\n{code}\n```"))
            elif name in ("ul", "ol"):
                self.flush()
                text = _list_text(child)
                if text:
                    self.blocks.append(Block("text", text))
            elif name in TEXT_BLOCKS:
                if child.find(list(BLOCKISH)) is not None:
                    self.flush()
                    self.walk(child)
                    self.flush()
                else:
                    self.flush()
                    text = clean_inline(child.get_text(" ", strip=True))
                    if text:
                        self.blocks.append(Block("text", text))
            elif name == "br":
                self.pending.append("\n")
            elif name == "hr":
                self.flush()
            elif child.find(list(BLOCKISH)) is None and name not in ("div", "section", "article", "main", "body"):
                text = child.get_text(" ", strip=True)  # inline element (a, span, b, em, code …)
                if text:
                    self.pending.append(text)
            else:
                self.flush()
                self.walk(child)
                self.flush()


def parse_html(data: bytes | str) -> ParsedDocument:
    soup = BeautifulSoup(data, "html.parser")
    title = ""
    if soup.title and soup.title.string:
        title = clean_inline(soup.title.string)
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            title = clean_inline(og["content"])
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for t in soup(REMOVE_TAGS):
        t.decompose()
    for t in soup.find_all(attrs={"role": ["navigation", "banner", "contentinfo", "search", "complementary"]}):
        t.decompose()
    for t in soup(CHROME_TAGS):
        if t.find_parent(["article", "main"]) is None:
            t.decompose()
    for t in list(soup.find_all(True)):
        if t.parent is not None and _is_noise(t):
            t.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    walker = _Walker()
    walker.walk(root)
    walker.flush()
    if not title:
        h1 = next((b for b in walker.blocks if b.kind == "heading" and b.level == 1), None)
        title = h1.text if h1 else ""
    return ParsedDocument(title=title, blocks=walker.blocks, format="html")
