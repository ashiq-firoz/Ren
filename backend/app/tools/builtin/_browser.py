"""Web helpers shared by the search / browser / computer tool groups (not auto-imported).

- `safe_fetch` / `safe_download`: httpx GET through the shared client with the SSRF guard applied to the
  initial URL *and every redirect hop*, streaming with a byte cap.
- `readable`: readability-style main-content extraction (HTML → title + structured plain text).
- `extract_links` / `extract_tables` / `static_dom`: page analysis helpers that work on any HTML.
- `BrowserManager`: optional Playwright (async) Chromium — one persistent context per (org, user), idle
  cleanup after 10 minutes, max 5 tabs, every request (including sub-resources) passed through the SSRF guard.
- Static mode: a tiny per-user "current page" + history, used when Playwright is not installed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from app.tools import net
from app.tools.base import ToolContext, ToolError

log = logging.getLogger("ren.tools.browser")

MAX_FETCH_BYTES = 15 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
IDLE_SECONDS = 600
MAX_TABS = 5
VIEWPORT = {"width": 1280, "height": 800}
INTERACTIVE_HINT = ("Interactive browsing needs Playwright: `pip install playwright && playwright install chromium` "
                    "(or `pip install 'ren-backend[browser]'`). Static mode only supports open, get_page, get_dom, "
                    "find, "
                    "extract_links, extract_table, back/forward/reload and download.")
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ============================================================================ fetching
@dataclass
class Fetched:
    url: str
    final_url: str
    status: int
    content_type: str
    content: bytes
    truncated: bool
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def mime(self) -> str:
        return (self.content_type or "").split(";")[0].strip().lower()

    @property
    def is_pdf(self) -> bool:
        return self.mime == "application/pdf" or self.content[:5] == b"%PDF-"

    @property
    def is_html(self) -> bool:
        if self.mime in ("text/html", "application/xhtml+xml"):
            return True
        head = self.content[:512].lstrip().lower()
        if not self.mime:
            return True
        return self.mime == "application/octet-stream" and head.startswith((b"<!doctype html", b"<html"))

    def text(self) -> str:
        m = re.search(r"charset=([\w-]+)", self.content_type or "", re.I)
        for enc in ([m.group(1)] if m else []) + ["utf-8", "cp1252", "latin-1"]:
            try:
                return self.content.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.content.decode("utf-8", errors="replace")


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ToolError("A URL is required.")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
        if re.match(r"^(localhost|[\w.-]+\.[a-z]{2,}|\d{1,3}(\.\d{1,3}){3}):\d+", url, re.I):
            return "https://" + url  # "example.com:8080/x" parses as scheme "example.com"
        return url
    if url.startswith("//"):
        return "https:" + url
    return "https://" + url


async def safe_fetch(url: str, *, max_bytes: int = MAX_FETCH_BYTES, headers: dict | None = None,
                     timeout: float = 30.0, max_redirects: int = 5) -> Fetched:
    """GET with the SSRF guard on every hop (redirects are followed manually)."""
    original = normalize_url(url)
    current = original
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    for _ in range(max_redirects + 1):
        await net.assert_public_url(current)
        try:
            async with net.http().stream("GET", current, headers=hdrs, follow_redirects=False,
                                         timeout=timeout) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = urljoin(str(resp.url), resp.headers["location"])
                    continue
                buf = bytearray()
                truncated = False
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        truncated = True
                        del buf[max_bytes:]
                        break
                return Fetched(url=original, final_url=str(resp.url), status=resp.status_code,
                               content_type=resp.headers.get("content-type", ""), content=bytes(buf),
                               truncated=truncated, headers={k.lower(): v for k, v in resp.headers.items()})
        except httpx.TimeoutException as e:
            raise ToolError(f"Timed out fetching {current}.") from e
        except httpx.HTTPError as e:
            raise ToolError(f"Could not fetch {current}: {e.__class__.__name__}: {e}") from e
    raise ToolError(f"Too many redirects while fetching {original}.")


def _filename_from(resp_headers: dict[str, str], url: str) -> str:
    cd = resp_headers.get("content-disposition", "")
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    name = unquote(Path(urlparse(url).path).name)
    return name or "download"


async def safe_download(url: str, dest_dir: Path, *, filename: str | None = None,
                        max_bytes: int = MAX_DOWNLOAD_BYTES, timeout: float = 120.0,
                        max_redirects: int = 5) -> tuple[Path, int, str, str]:
    """Stream a public URL to `dest_dir`. Returns (path, size, mime, final_url)."""
    from app.tools.builtin._office import safe_filename, unique_path

    current = normalize_url(url)
    for _ in range(max_redirects + 1):
        await net.assert_public_url(current)
        try:
            async with net.http().stream("GET", current, headers=DEFAULT_HEADERS, follow_redirects=False,
                                         timeout=timeout) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = urljoin(str(resp.url), resp.headers["location"])
                    continue
                if resp.status_code >= 400:
                    raise ToolError(f"Download failed: HTTP {resp.status_code} from {current}.")
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                declared = int(hdrs.get("content-length") or 0)
                if declared and declared > max_bytes:
                    raise ToolError(f"File is too large ({declared / 1e6:.0f} MB; limit {max_bytes / 1e6:.0f} MB).")
                name = safe_filename(filename or _filename_from(hdrs, str(resp.url))) or "download"
                if "." not in name:
                    import mimetypes

                    ext = mimetypes.guess_extension(hdrs.get("content-type", "").split(";")[0].strip()) or ""
                    name += ext
                dest_dir.mkdir(parents=True, exist_ok=True)
                target = unique_path(dest_dir / name)
                size = 0
                with open(target, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            f.close()
                            target.unlink(missing_ok=True)
                            raise ToolError(f"File exceeds the {max_bytes / 1e6:.0f} MB download limit.")
                        f.write(chunk)
                return target, size, hdrs.get("content-type", "").split(";")[0].strip(), str(resp.url)
        except httpx.TimeoutException as e:
            raise ToolError(f"Timed out downloading {current}.") from e
        except httpx.HTTPError as e:
            raise ToolError(f"Could not download {current}: {e.__class__.__name__}: {e}") from e
    raise ToolError("Too many redirects.")


def pdf_bytes_text(data: bytes, max_pages: int = 60) -> str:
    """Text of an in-memory PDF (first `max_pages` pages), with page markers."""
    import io

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                return ""
        parts = []
        for i, p in enumerate(reader.pages[:max_pages], 1):
            t = (p.extract_text() or "").strip()
            if t:
                parts.append(f"--- Page {i} ---\n{t}")
        if len(reader.pages) > max_pages:
            parts.append(f"…[{len(reader.pages) - max_pages} more pages not shown]")
        return "\n\n".join(parts)
    except Exception:  # noqa: BLE001
        return ""


def pdf_bytes_title(data: bytes) -> str:
    import io

    from pypdf import PdfReader

    try:
        meta = PdfReader(io.BytesIO(data)).metadata
        return str(meta.title).strip() if meta and meta.title else ""
    except Exception:  # noqa: BLE001
        return ""


# ============================================================================ HTML analysis
def make_soup(html: str | bytes):
    from bs4 import BeautifulSoup

    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return BeautifulSoup(html, "html.parser")


def page_title(soup) -> str:
    for sel in (("meta", {"property": "og:title"}), ("meta", {"name": "twitter:title"})):
        m = soup.find(*sel)
        if m and m.get("content"):
            return " ".join(m["content"].split())[:300]
    if soup.title and soup.title.string:
        return " ".join(soup.title.string.split())[:300]
    h1 = soup.find("h1")
    return " ".join(h1.get_text(" ", strip=True).split())[:300] if h1 else ""


_DROP_TAGS = ["script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed", "form",
              "button", "input", "select", "textarea", "nav", "aside", "footer", "dialog", "link", "meta"]
_BOILER_RE = re.compile(
    r"(^|[\s_-])(nav|navbar|navigation|menu|footer|sidebar|side-bar|breadcrumbs?|cookie|cookies|consent|banner|"
    r"advert|advertisement|ads?|promo|newsletter|subscribe|social|share|sharing|related|recommended|comments?|"
    r"popup|modal|overlay|skip-link|toolbar|masthead|signup|paywall|outbrain|taboola)([\s_-]|$)", re.I)
_BLOCK = {"p", "div", "section", "article", "main", "header", "ul", "ol", "table", "thead", "tbody", "tfoot", "tr",
          "pre", "blockquote", "dl", "dt", "dd", "figure", "figcaption", "hr", "address", "details", "summary",
          "h1", "h2", "h3", "h4", "h5", "h6", "li", "body", "html", "center", "caption"}


def _text_len(el) -> int:
    return len(el.get_text(" ", strip=True)) if el is not None else 0


def _link_density(el) -> float:
    total = _text_len(el)
    if not total:
        return 1.0
    links = sum(len(a.get_text(" ", strip=True)) for a in el.find_all("a"))
    return links / total


def _clean_dom(body) -> None:
    from bs4 import Comment

    for c in body.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for t in body.find_all(_DROP_TAGS):
        t.decompose()
    for t in body.find_all(attrs={"aria-hidden": "true"}):
        t.decompose()
    for t in body.find_all(attrs={"role": ["navigation", "banner", "contentinfo", "complementary", "dialog", "alert",
                                           "search", "menu", "menubar"]}):
        t.decompose()
    for t in body.find_all("header"):
        if not t.find_parent(["article", "main"]):
            t.decompose()
    total = max(1, _text_len(body))
    for el in list(body.find_all(True)):
        if getattr(el, "decomposed", False) or el.parent is None or el.name in ("body", "html", "main", "article"):
            continue
        ident = " ".join(el.get("class") or []) + " " + (el.get("id") or "")
        if ident.strip() and _BOILER_RE.search(ident):
            tl = _text_len(el)
            if tl < 0.4 * total or _link_density(el) > 0.5:
                el.decompose()


def _pick_root(body):
    body_len = max(1, _text_len(body))
    cands = body.find_all(["article", "main"]) + body.find_all(attrs={"role": "main"})
    if cands:
        best = max(cands, key=_text_len)
        if _text_len(best) >= 0.25 * body_len:
            return best
    scores: dict[int, list] = {}
    for p in body.find_all(["p", "pre", "li"]):
        n = _text_len(p)
        if n < 25:
            continue
        parent = p.parent
        if parent is None:
            continue
        scores.setdefault(id(parent), [parent, 0.0])[1] += n
        gp = parent.parent
        if gp is not None:
            scores.setdefault(id(gp), [gp, 0.0])[1] += n / 2
    if scores:
        el, _ = max(scores.values(), key=lambda x: x[1])
        if _text_len(el) >= 0.3 * body_len and _link_density(el) < 0.5:
            return el
    return body


class _TextBuilder:
    def __init__(self) -> None:
        self.blocks: list[tuple[str, str]] = []  # (kind, text)
        self.buf: list[str] = []
        self.prefix = ""
        self.kind = "p"

    def text(self, s: str) -> None:
        self.buf.append(s)

    def flush(self) -> None:
        t = "".join(self.buf)
        t = re.sub(r"[ \t\r\f\v ]+", " ", t)
        t = re.sub(r" *\n *", "\n", t).strip()
        if t:
            self.blocks.append((self.kind, self.prefix + t))
        self.buf, self.prefix, self.kind = [], "", "p"

    def walk(self, node, depth: int = 0) -> None:
        from bs4 import NavigableString, Tag

        for ch in node.children:
            if isinstance(ch, NavigableString):
                if type(ch).__name__ in ("Comment", "Doctype", "Declaration", "ProcessingInstruction", "CData"):
                    continue
                self.text(str(ch))
                continue
            if not isinstance(ch, Tag):
                continue
            name = ch.name
            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self.flush()
                t = " ".join(ch.get_text(" ", strip=True).split())
                if t:
                    self.blocks.append(("h", "#" * int(name[1]) + " " + t))
            elif name == "br":
                self.text("\n")
            elif name == "li":
                self.flush()
                ordered = ch.parent is not None and ch.parent.name == "ol"
                self.prefix = "  " * depth + ("1. " if ordered else "- ")
                self.kind = "li"
                for sub in ch.children:
                    if isinstance(sub, Tag) and sub.name in ("ul", "ol"):
                        self.flush()
                        self.walk(sub, depth + 1)
                    elif isinstance(sub, Tag):
                        if sub.name in ("p", "div") and not self.buf:
                            self.walk(sub, depth)
                        else:
                            self.walk_inline(sub)
                    elif isinstance(sub, NavigableString):
                        self.text(str(sub))
                self.flush()
            elif name == "pre":
                self.flush()
                code = ch.get_text()
                if code.strip():
                    self.blocks.append(("pre", "```\n" + code.strip("\n") + "\n```"))
            elif name == "table":
                self.flush()
                rows = []
                for tr in ch.find_all("tr"):
                    cells = [" ".join(c.get_text(" ", strip=True).split()) for c in tr.find_all(["th", "td"])]
                    if any(cells):
                        rows.append("| " + " | ".join(cells) + " |")
                if rows:
                    self.blocks.append(("table", "\n".join(rows[:200])))
            elif name == "img":
                continue
            elif name == "hr":
                self.flush()
            elif name in ("ul", "ol"):
                self.flush()
                self.walk(ch, depth)
                self.flush()
            elif name == "blockquote":
                self.flush()
                inner = _TextBuilder()
                inner.walk(ch)
                inner.flush()
                for _, t in inner.blocks:
                    self.blocks.append(("p", "> " + t.replace("\n", "\n> ")))
            elif name in _BLOCK:
                self.flush()
                self.walk(ch, depth)
                self.flush()
            else:
                self.walk_inline(ch)

    def walk_inline(self, node) -> None:
        from bs4 import Tag

        if isinstance(node, Tag) and (node.name in _BLOCK or node.find(list(_BLOCK - {"body", "html"}))):
            self.walk(node)
            return
        if isinstance(node, Tag) and node.name == "br":
            self.text("\n")
            return
        self.text(node.get_text() if isinstance(node, Tag) else str(node))

    def result(self) -> str:
        out: list[str] = []
        prev = None
        for kind, t in self.blocks:
            if out and not (kind == "li" and prev == "li"):
                out.append("\n\n")
            elif out:
                out.append("\n")
            out.append(t)
            prev = kind
        return "".join(out).strip()


def readable(html: str | bytes, base_url: str = "", *, max_chars: int | None = None) -> dict[str, Any]:
    """Main readable content of an HTML page: {title, text, description, lang, links_count, chars, truncated}."""
    soup = make_soup(html)
    title = page_title(soup)
    desc = ""
    m = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if m and m.get("content"):
        desc = " ".join(m["content"].split())[:500]
    lang = (soup.html.get("lang") if soup.html else None) or None
    links_count = len({urljoin(base_url, a["href"]).split("#")[0] for a in soup.find_all("a", href=True)
                       if not a["href"].lower().startswith(("javascript:", "#", "mailto:", "tel:"))})
    body = soup.body or soup
    _clean_dom(body)
    root = _pick_root(body)
    tb = _TextBuilder()
    tb.walk(root)
    tb.flush()
    text = tb.result()
    if not text and title:
        text = desc or ""
    truncated = False
    if max_chars and len(text) > max_chars:
        cut = text.rfind("\n\n", 0, max_chars)
        text = text[: cut if cut > max_chars * 0.6 else max_chars].rstrip()
        truncated = True
    return {"title": title, "text": text, "description": desc, "lang": lang, "links_count": links_count,
            "chars": len(text), "truncated": truncated}


def extract_links(html: str | bytes, base_url: str = "", *, contains: str | None = None, same_domain: bool = False,
                  limit: int = 100) -> tuple[list[dict], int]:
    soup = make_soup(html)
    base_host = urlparse(base_url).hostname or ""
    seen: set[str] = set()
    out: list[dict] = []
    total = 0
    needle = (contains or "").lower()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.lower().startswith(("javascript:", "#", "data:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if not url.lower().startswith(("http://", "https://", "mailto:")):
            continue
        if url in seen:
            continue
        seen.add(url)
        if same_domain and (urlparse(url).hostname or "") != base_host:
            continue
        text = " ".join((a.get_text(" ", strip=True) or a.get("title") or a.get("aria-label") or "").split())[:120]
        if needle and needle not in url.lower() and needle not in text.lower():
            continue
        total += 1
        if len(out) < limit:
            out.append({"text": text, "url": url})
    return out, total


def extract_tables(html: str | bytes, *, max_rows: int = 200) -> list[dict]:
    soup = make_soup(html)
    tables = []
    for t in soup.find_all("table"):
        if t.find("table"):  # layout table wrapping other tables
            continue
        rows: list[list[str]] = []
        header: list[str] | None = None
        for tr in t.find_all("tr"):
            if tr.find_parent("table") is not t:
                continue
            cells = []
            for c in tr.find_all(["th", "td"]):
                txt = " ".join(c.get_text(" ", strip=True).split())
                try:
                    span = max(1, min(20, int(c.get("colspan", 1))))
                except (TypeError, ValueError):
                    span = 1
                cells.extend([txt] * span)
            if not any(cells):
                continue
            is_head = header is None and not rows and (tr.find_parent("thead") is not None or
                                                       all(c.name == "th" for c in tr.find_all(["th", "td"])))
            if is_head:
                header = cells
            else:
                rows.append(cells)
        if not rows and not header:
            continue
        if header is None and rows and len(rows) > 1:
            header, rows = rows[0], rows[1:]
        cap = t.find("caption")
        tables.append({"index": len(tables), "caption": cap.get_text(" ", strip=True) if cap else "",
                       "header": header or [], "rows": rows[:max_rows], "n_rows": len(rows),
                       "n_cols": max([len(header or [])] + [len(r) for r in rows])})
    return tables


_INTERACTIVE_ROLES = {"button", "link", "checkbox", "radio", "tab", "menuitem", "switch", "option", "combobox",
                      "textbox", "searchbox"}
_KEEP_ATTRS = {"href", "type", "name", "value", "placeholder", "aria-label", "role", "data-ren-ref", "id", "alt",
               "title", "for", "action", "method", "checked", "selected", "disabled"}


def _is_interactive(el) -> bool:
    name = el.name
    if name == "a":
        return el.has_attr("href")
    if name == "input":
        return (el.get("type") or "").lower() != "hidden"
    if name in ("button", "select", "textarea", "summary"):
        return True
    return (el.get("role") or "").lower() in _INTERACTIVE_ROLES or el.has_attr("onclick")


def _label(el) -> str:
    t = el.get("aria-label") or el.get_text(" ", strip=True) or el.get("value") or el.get("placeholder") or \
        el.get("title") or el.get("name") or el.get("alt") or ""
    return " ".join(str(t).split())[:80]


def static_dom(html: str | bytes, base_url: str = "", *, max_chars: int = 15000, max_elements: int = 150) -> dict:
    """Trimmed HTML with interactive elements annotated `data-ren-ref="eN"` (document order → stable)."""
    soup = make_soup(html)
    for t in soup.find_all(["script", "style", "noscript", "svg", "template", "iframe", "canvas", "link", "meta",
                            "head"]):
        t.decompose()
    from bs4 import Comment

    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    elements = []
    n = 0
    for el in soup.find_all(True):
        if _is_interactive(el):
            n += 1
            ref = f"e{n}"
            el["data-ren-ref"] = ref
            if len(elements) < max_elements:
                elements.append({"ref": ref, "tag": el.name, "type": el.get("type"), "label": _label(el),
                                 "href": urljoin(base_url, el["href"]) if el.name == "a" and el.get("href") else None})
    for el in soup.find_all(True):
        for attr in list(el.attrs):
            if attr not in _KEEP_ATTRS:
                del el.attrs[attr]
            elif attr == "href" and isinstance(el.attrs[attr], str) and len(el.attrs[attr]) > 200:
                el.attrs[attr] = el.attrs[attr][:200]
    root = soup.body or soup
    html_out = re.sub(r"\s+", " ", str(root))
    html_out = re.sub(r">\s+<", "><", html_out)
    total = len(html_out)
    return {"html": html_out[:max_chars], "html_chars": total, "truncated": total > max_chars,
            "elements": elements, "interactive_count": n}


def find_text(html: str | bytes, needle: str, *, limit: int = 20) -> list[dict]:
    soup = make_soup(html)
    for t in soup.find_all(["script", "style", "noscript", "template"]):
        t.decompose()
    n = needle.lower().strip()
    out = []
    refs = {}
    counter = 0
    for el in soup.find_all(True):
        if _is_interactive(el):
            counter += 1
            refs[id(el)] = f"e{counter}"
    for el in soup.find_all(True):
        text = " ".join(el.get_text(" ", strip=True).split())
        if not text or n not in text.lower():
            continue
        if any(n in " ".join(c.get_text(" ", strip=True).split()).lower() for c in el.find_all(True, recursive=False)):
            continue  # a child matches → prefer the deepest element
        out.append({"ref": refs.get(id(el)), "tag": el.name, "text": text[:160]})
        if len(out) >= limit:
            break
    return out


# ============================================================================ static-mode sessions
@dataclass
class StaticPage:
    url: str
    final_url: str
    status: int
    title: str
    html: str
    content_type: str
    fetched_at: float = field(default_factory=time.time)


@dataclass
class StaticSession:
    history: list[StaticPage] = field(default_factory=list)
    index: int = -1
    last_used: float = field(default_factory=time.monotonic)

    @property
    def current(self) -> StaticPage | None:
        return self.history[self.index] if 0 <= self.index < len(self.history) else None

    def push(self, page: StaticPage) -> None:
        self.history = self.history[: self.index + 1] + [page]
        if len(self.history) > 30:
            self.history = self.history[-30:]
        self.index = len(self.history) - 1


_static: dict[tuple[str, str], StaticSession] = {}
_modes: dict[tuple[str, str], str] = {}


def session_key(ctx: ToolContext) -> tuple[str, str]:
    return ctx.org_id, ctx.user_id or "_system"


def static_session(ctx: ToolContext) -> StaticSession:
    now = time.monotonic()
    for k, s in list(_static.items()):
        if now - s.last_used > 30 * 60:
            _static.pop(k, None)
    s = _static.setdefault(session_key(ctx), StaticSession())
    s.last_used = now
    return s


def set_mode(ctx: ToolContext, mode: str) -> None:
    _modes[session_key(ctx)] = mode


def current_mode(ctx: ToolContext) -> str:
    return _modes.get(session_key(ctx), "static")


async def static_open(ctx: ToolContext, url: str, *, push: bool = True) -> StaticPage:
    import html as _html

    f = await safe_fetch(url, max_bytes=5 * 1024 * 1024)
    if f.is_pdf:
        text = pdf_bytes_text(f.content)
        name = _html.escape(Path(urlparse(f.final_url).path).name or "document.pdf")
        html = f"<html><head><title>{name}</title></head><body><pre>{_html.escape(text)}</pre></body></html>"
    elif f.is_html or f.mime.startswith("text/") or f.mime in ("application/xml", "application/json"):
        html = f.text()
        if not f.is_html:
            html = f"<html><body><pre>{_html.escape(html)}</pre></body></html>"
    else:
        raise ToolError(f"{f.final_url} is a '{f.mime or 'binary'}' file, not a web page. Use browser_download.")
    page = StaticPage(url=url, final_url=f.final_url, status=f.status, title=page_title(make_soup(html)),
                      html=html, content_type=f.mime)
    if push:
        static_session(ctx).push(page)
    return page


# ============================================================================ Playwright (optional)
def playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


class BrowserUnavailable(Exception):
    pass


class _Tabs:
    def __init__(self, key: tuple[str, str], context: Any):
        self.key = key
        self.context = context
        self.active: Any = None
        self.last_used = time.monotonic()
        self.lock = asyncio.Lock()
        self.notes: list[str] = []

    @property
    def pages(self) -> list[Any]:
        return [p for p in self.context.pages if not p.is_closed()]

    def on_page(self, page: Any) -> None:
        if len(self.pages) > MAX_TABS:
            self.notes.append(f"A new tab ({page.url}) was blocked: the {MAX_TABS}-tab limit was reached.")
            asyncio.ensure_future(page.close())
            return
        self.active = page

    async def page(self) -> Any:
        if self.active is None or self.active.is_closed():
            pages = self.pages
            self.active = pages[-1] if pages else await self.context.new_page()
        self.last_used = time.monotonic()
        return self.active


class BrowserManager:
    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._sessions: dict[tuple[str, str], _Tabs] = {}
        self._lock: asyncio.Lock | None = None
        self._loop: Any = None
        self._janitor: asyncio.Task | None = None
        self._launch_error: str | None = None
        self._launch_error_at = 0.0
        self._host_cache: dict[str, tuple[bool, float]] = {}

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:  # objects from another (closed) loop are unusable — start fresh
            self._pw = self._browser = None
            self._sessions = {}
            self._lock = asyncio.Lock()
            self._janitor = None
            self._loop = loop

    @property
    def launch_failed_recently(self) -> bool:
        return bool(self._launch_error) and time.monotonic() - self._launch_error_at < 300

    async def _ensure_browser(self) -> Any:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self.launch_failed_recently:
            raise BrowserUnavailable(self._launch_error or "browser failed to start")
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise BrowserUnavailable("Playwright is not installed") from e
        if self._pw is None:
            self._pw = await async_playwright().start()
        last: Exception | None = None
        for extra in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
            try:
                self._browser = await self._pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"],
                                                               **extra)
                self._launch_error = None
                return self._browser
            except Exception as e:  # noqa: BLE001
                last = e
        self._launch_error = f"{last.__class__.__name__}: {str(last).splitlines()[0][:300]}" if last else "unknown"
        self._launch_error_at = time.monotonic()
        raise BrowserUnavailable(self._launch_error)

    async def _allowed(self, url: str) -> bool:
        if url.startswith(("data:", "blob:", "about:")):
            return True
        host = (urlparse(url).hostname or "").lower()
        cached = self._host_cache.get(host)
        if cached and time.monotonic() - cached[1] < 300 and urlparse(url).scheme in ("http", "https"):
            return cached[0]
        try:
            await net.assert_public_url(url)
            ok = True
        except ToolError:
            ok = False
        if urlparse(url).scheme in ("http", "https"):
            self._host_cache[host] = (ok, time.monotonic())
        return ok

    async def _guard(self, route: Any) -> None:
        try:
            if await self._allowed(route.request.url):
                await route.continue_()
            else:
                await route.abort("blockedbyclient")
        except Exception:  # noqa: BLE001  (page closed mid-request etc.)
            pass

    async def session(self, ctx: ToolContext) -> _Tabs:
        self._check_loop()
        key = session_key(ctx)
        assert self._lock is not None
        async with self._lock:
            s = self._sessions.get(key)
            if s is not None:
                try:
                    _ = s.context.pages
                    if self._browser is None or not self._browser.is_connected():
                        s = None
                except Exception:  # noqa: BLE001
                    s = None
            if s is None:
                browser = await self._ensure_browser()
                context = await browser.new_context(viewport=dict(VIEWPORT), accept_downloads=True, locale="en-US",
                                                    java_script_enabled=True)
                await context.route("**/*", self._guard)
                s = _Tabs(key, context)
                context.on("page", s.on_page)
                self._sessions[key] = s
                self._start_janitor()
            s.last_used = time.monotonic()
            return s

    def _start_janitor(self) -> None:
        if self._janitor is None or self._janitor.done():
            self._janitor = asyncio.ensure_future(self._janitor_loop())

    async def _janitor_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                now = time.monotonic()
                for key, s in list(self._sessions.items()):
                    if now - s.last_used > IDLE_SECONDS:
                        self._sessions.pop(key, None)
                        try:
                            await s.context.close()
                        except Exception:  # noqa: BLE001
                            pass
                if not self._sessions:
                    await self.shutdown()
                    return
        except asyncio.CancelledError:
            return

    async def close_session(self, ctx: ToolContext) -> None:
        s = self._sessions.pop(session_key(ctx), None)
        if s is not None:
            try:
                await s.context.close()
            except Exception:  # noqa: BLE001
                pass

    async def shutdown(self) -> None:
        for s in list(self._sessions.values()):
            try:
                await s.context.close()
            except Exception:  # noqa: BLE001
                pass
        self._sessions = {}
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
        self._browser = self._pw = None


manager = BrowserManager()


async def interactive_page(ctx: ToolContext) -> tuple[_Tabs, Any]:
    """Current Playwright page for this user (raises a helpful ToolError when unavailable)."""
    if not playwright_available():
        raise ToolError(INTERACTIVE_HINT)
    try:
        tabs = await manager.session(ctx)
    except BrowserUnavailable as e:
        raise ToolError(f"The interactive browser could not start ({e}). Install a browser with "
                        "`playwright install chromium`.") from e
    return tabs, await tabs.page()


async def save_screenshot(ctx: ToolContext, page: Any, *, full_page: bool = False, target: str | None = None,
                          prefix: str = "screenshot", register: bool = True) -> tuple[Path, dict]:
    """PNG of the page (or an element) under output/screenshots/, registered as an artifact."""
    from datetime import datetime

    from app.tools.builtin import _office as O

    O.require_write(ctx)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    path = O.ws_path(ctx, f"output/screenshots/{prefix}-{stamp}.png", for_write=True)
    try:
        if target:
            loc = await locate(page, target)
            await loc.screenshot(path=str(path), timeout=20000)
        else:
            await page.screenshot(path=str(path), full_page=full_page, timeout=20000)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Screenshot failed: {str(e).splitlines()[0][:200]}") from e
    art = await O.publish(ctx, path) if register else {"path": O.vpath(ctx, path)}
    return path, art


async def settle(page: Any, timeout_ms: int = 4000) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:  # noqa: BLE001
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=1500)
    except Exception:  # noqa: BLE001
        pass


_REF_RE = re.compile(r"^e\d+$")
_HTML_TAGS = {"a", "button", "input", "select", "textarea", "form", "div", "span", "p", "li", "ul", "ol", "table",
              "tr", "td", "th", "img", "label", "h1", "h2", "h3", "h4", "h5", "h6", "nav", "header", "footer", "main",
              "section", "article", "option", "summary", "details", "iframe", "body", "svg", "path"}


def looks_like_css(target: str) -> bool:
    t = target.strip()
    if not t:
        return False
    if t[0] in "#.[*" or t.startswith(("xpath=", "css=", "text=")):
        return True
    if " > " in t or ("[" in t and "]" in t):
        return True
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9-]*)([#.\[:].*)?$", t)
    return bool(m and m.group(1).lower() in _HTML_TAGS)


async def locate(page: Any, target: str, by: str = "auto") -> Any:
    """Element locator from a ref ('e12'), CSS selector, or visible text/label."""
    target = (target or "").strip()
    if not target:
        raise ToolError("An element target is required (ref like 'e12', CSS selector, or visible text).")
    by = (by or "auto").lower()
    if by == "ref" or (by == "auto" and _REF_RE.match(target)):
        loc = page.locator(f'[data-ren-ref="{target}"]')
    elif by == "css" or (by == "auto" and looks_like_css(target)):
        loc = page.locator(target)
    elif by == "xpath":
        loc = page.locator(f"xpath={target}")
    else:
        loc = None
        for role in ("button", "link", "menuitem", "tab", "checkbox", "radio", "option", "textbox", "combobox"):
            cand = page.get_by_role(role, name=target)
            try:
                if await cand.count():
                    loc = cand
                    break
            except Exception:  # noqa: BLE001
                continue
        if loc is None:
            for cand in (page.get_by_label(target), page.get_by_placeholder(target), page.get_by_text(target)):
                try:
                    if await cand.count():
                        loc = cand
                        break
                except Exception:  # noqa: BLE001
                    continue
        if loc is None:
            loc = page.get_by_text(target)
    try:
        n = await loc.count()
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Invalid element target '{target}': {str(e).splitlines()[0][:200]}") from e
    if n == 0:
        raise ToolError(f"No element matches '{target}'. Use browser_find or browser_get_dom to get element refs.")
    return loc.first


DOM_JS = r"""
([maxChars, maxEls]) => {
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],' +
    '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=switch],[role=option],[role=combobox],' +
    '[role=textbox],[role=searchbox],[contenteditable=""],[contenteditable="true"],[onclick]';
  let seq = window.__renRefSeq || 0;
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const label = (el) => (el.getAttribute('aria-label') || el.innerText || el.value ||
    el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') ||
    el.getAttribute('alt') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const elements = [];
  for (const el of document.querySelectorAll(SEL)) {
    if (!vis(el)) continue;
    if (!el.getAttribute('data-ren-ref')) el.setAttribute('data-ren-ref', 'e' + (++seq));
    if (elements.length < maxEls) {
      const r = el.getBoundingClientRect();
      elements.push({ref: el.getAttribute('data-ren-ref'), tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type'), role: el.getAttribute('role'), label: label(el),
        href: el.getAttribute('href'), x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)});
    }
  }
  window.__renRefSeq = seq;
  const KEEP = new Set(['href', 'type', 'name', 'value', 'placeholder', 'aria-label', 'role', 'data-ren-ref',
    'id', 'alt', 'title', 'for', 'action', 'method', 'checked', 'selected', 'disabled']);
  const root = document.body || document.documentElement;
  const clone = root.cloneNode(true);
  clone.querySelectorAll('script,style,noscript,svg,template,iframe,canvas,link,meta').forEach(n => n.remove());
  const walker = document.createTreeWalker(clone, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_COMMENT);
  const drop = [];
  let node = walker.currentNode;
  while (node) {
    if (node.nodeType === 8) drop.push(node);
    else if (node.attributes) {
      for (const a of Array.from(node.attributes)) {
        if (!KEEP.has(a.name)) node.removeAttribute(a.name);
        else if (a.name === 'href' && a.value.length > 200) node.setAttribute('href', a.value.slice(0, 200));
      }
    }
    node = walker.nextNode();
  }
  drop.forEach(n => n.remove());
  let html = clone.outerHTML.replace(/\s+/g, ' ').replace(/>\s+</g, '><');
  const total = html.length;
  return {html: html.slice(0, maxChars), html_chars: total, truncated: total > maxChars, elements,
    interactive_count: seq};
}
"""

FIND_JS = r"""
([needle, limit]) => {
  const n = needle.toLowerCase();
  let seq = window.__renRefSeq || 0;
  const out = [];
  const all = document.body ? document.body.querySelectorAll('*') : [];
  for (const el of all) {
    if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'].includes(el.tagName)) continue;
    const t = (el.innerText || el.value || el.getAttribute('aria-label') ||
      el.getAttribute('placeholder') || '').trim();
    if (!t || !t.toLowerCase().includes(n)) continue;
    let child = false;
    for (const c of el.children) {
      if ((c.innerText || c.value || '').toLowerCase().includes(n)) { child = true; break; }
    }
    if (child) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (!el.getAttribute('data-ren-ref')) el.setAttribute('data-ren-ref', 'e' + (++seq));
    out.push({ref: el.getAttribute('data-ren-ref'), tag: el.tagName.toLowerCase(),
      text: t.replace(/\s+/g, ' ').slice(0, 160), x: Math.round(r.x + r.width / 2),
      y: Math.round(r.y + r.height / 2)});
    if (out.length >= limit) break;
  }
  window.__renRefSeq = seq;
  return out;
}
"""

ELEMENTS_JS = r"""
(maxEls) => {
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],' +
    '[role=checkbox],[role=tab],[role=menuitem],[contenteditable="true"]';
  const out = [];
  for (const el of document.querySelectorAll(SEL)) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.bottom < 0 || r.right < 0) continue;
    if (r.top > innerHeight || r.left > innerWidth) continue;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') continue;
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder') ||
      el.getAttribute('title') || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    out.push({tag: el.tagName.toLowerCase(), label, x: Math.round(r.x + r.width / 2),
      y: Math.round(r.y + r.height / 2)});
    if (out.length >= maxEls) break;
  }
  return out;
}
"""
