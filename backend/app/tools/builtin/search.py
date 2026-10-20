"""Web search group: web / news / image search (DuckDuckGo by default; Tavily, Brave or SearXNG when an admin
configures them), scholarly search (arXiv + Semantic Scholar), and reading pages (fetch → clean text, extract →
structured JSON, summarize → brief)."""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse

from app.core.config import settings
from app.tools import net
from app.tools.base import Risk, ToolContext, ToolError, ToolGroup, ToolResult
from app.tools.builtin import _browser as B
from app.tools.builtin import _office as O

log = logging.getLogger("ren.tools.search")

search = ToolGroup(
    "search", "Web Search",
    "Search the web, news, images and scholarly papers; read web pages and PDFs as clean text; extract structured "
    "data or summaries from pages and documents.",
    prefix="search", icon="search", category="core",
)

TimeRange = Literal["any", "day", "week", "month", "year"]
SafeSearch = Literal["on", "moderate", "off"]
PROVIDERS = ("duckduckgo", "tavily", "brave", "searxng")


# ----------------------------------------------------------------------------------------- provider config
async def provider_config(ctx: ToolContext) -> dict[str, Any]:
    """Org setting `web_search.provider` (+ `base_url`) with the key from org secrets or the environment."""
    override = ctx.extra.get("web_search")
    if isinstance(override, dict):
        cfg = {"provider": override.get("provider"), "api_key": override.get("api_key"),
               "base_url": override.get("base_url")}
    else:
        cfg = {"provider": settings.web_search_provider, "api_key": None, "base_url": None}
        try:
            from app.core.security import decrypt_json
            from app.models import Organization

            async with ctx.session() as db:
                org = await db.get(Organization, ctx.org_id)
                if org is not None:
                    ws = (org.settings or {}).get("web_search") or {}
                    if ws.get("provider"):
                        cfg["provider"] = ws["provider"]
                    if ws.get("base_url"):
                        cfg["base_url"] = ws["base_url"]
                    cfg["api_key"] = decrypt_json(org.secrets_enc).get("web_search_api_key")
        except Exception:  # noqa: BLE001
            log.warning("could not load org web-search settings", exc_info=True)
    p = str(cfg.get("provider") or "duckduckgo").lower().strip()
    cfg["provider"] = {"ddg": "duckduckgo", "ddgs": "duckduckgo", "searx": "searxng"}.get(p, p)
    if not cfg.get("api_key"):
        cfg["api_key"] = {"tavily": settings.tavily_api_key, "brave": settings.brave_api_key}.get(cfg["provider"])
    if cfg["provider"] == "searxng" and not cfg.get("base_url"):
        cfg["base_url"] = settings.searxng_url
    return cfg


# ----------------------------------------------------------------------------------------- helpers
def _domain(url: str | None) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _clean(text: Any, limit: int = 400) -> str:
    s = _html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))
    s = " ".join(s.split())
    return s[:limit] + "…" if len(s) > limit else s


_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$|igshid$)")


def _url_key(u: str | None) -> str:
    try:
        p = urlparse(u or "")
    except ValueError:
        return u or ""
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k.lower())]
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = p.path.rstrip("/") or "/"
    return f"{host}{path}?{urlencode(q)}" if q else f"{host}{path}"


def dedupe(results: list[dict], key: str = "url") -> list[dict]:
    seen: set[str] = set()
    out = []
    for r in results:
        k = _url_key(r.get(key) or r.get("url"))
        if not k or k in seen:
            continue
        seen.add(k)
        out.append({k2: v for k2, v in r.items() if v not in (None, "", [])})
    return out


def _raise_for(resp: Any, name: str) -> None:
    if resp.status_code in (401, 403):
        raise ToolError(f"{name} rejected the API key (HTTP {resp.status_code}).")
    if resp.status_code == 429:
        raise ToolError(f"{name} rate limit reached (HTTP 429).")
    if resp.status_code >= 400:
        raise ToolError(f"{name} returned HTTP {resp.status_code}.")


# ----------------------------------------------------------------------------------------- providers
def _ddgs_sync(kind: str, query: str, max_results: int, timelimit: str | None, region: str,
               safesearch: str) -> list[dict]:
    """DuckDuckGo (via the `ddgs` metasearch package). Blocking — always called in a thread."""
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    try:
        with DDGS(timeout=10) as d:
            fn = {"web": d.text, "news": d.news, "images": d.images}[kind]
            return list(fn(query, region=region, safesearch=safesearch, timelimit=timelimit,
                           max_results=max_results) or [])
    except DDGSException as e:
        if "no results" in str(e).lower():
            return []
        raise


async def _search_ddg(cfg: dict, kind: str, query: str, n: int, time_range: str, region: str,
                      safesearch: str) -> list[dict]:
    tl = {"day": "d", "week": "w", "month": "m", "year": "y"}.get(time_range)
    raw = await asyncio.to_thread(_ddgs_sync, kind, query, n, tl, region, safesearch)
    out = []
    for r in raw:
        if kind == "web":
            url = r.get("href") or r.get("url")
            out.append({"title": _clean(r.get("title"), 200), "url": url, "snippet": _clean(r.get("body")),
                        "source": _domain(url)})
        elif kind == "news":
            url = r.get("url") or r.get("href")
            out.append({"title": _clean(r.get("title"), 200), "url": url, "snippet": _clean(r.get("body")),
                        "source": r.get("source") or _domain(url), "published": r.get("date")})
        else:
            out.append({"title": _clean(r.get("title"), 200), "url": r.get("url"), "image": r.get("image"),
                        "thumbnail": r.get("thumbnail"), "width": r.get("width"), "height": r.get("height"),
                        "source": r.get("source") or _domain(r.get("url"))})
    return out


async def _search_tavily(cfg: dict, kind: str, query: str, n: int, time_range: str, region: str,
                         safesearch: str) -> list[dict]:
    body: dict[str, Any] = {"query": query, "max_results": min(n, 20), "search_depth": "basic",
                            "topic": "news" if kind == "news" else "general", "include_images": kind == "images",
                            "include_image_descriptions": kind == "images", "api_key": cfg["api_key"]}
    if time_range != "any":
        body["time_range"] = time_range
    resp = await net.http().post("https://api.tavily.com/search", json=body, timeout=30,
                                 headers={"Authorization": f"Bearer {cfg['api_key']}"})
    _raise_for(resp, "Tavily")
    data = resp.json()
    if kind == "images":
        out = []
        for i in data.get("images") or []:
            src = i.get("url") if isinstance(i, dict) else str(i)
            desc = i.get("description") if isinstance(i, dict) else ""
            out.append({"title": _clean(desc, 200) or query, "url": src, "image": src, "source": _domain(src)})
        return out
    return [{"title": _clean(r.get("title"), 200), "url": r.get("url"), "snippet": _clean(r.get("content")),
             "source": _domain(r.get("url")), "published": r.get("published_date")}
            for r in data.get("results") or []]


async def _search_brave(cfg: dict, kind: str, query: str, n: int, time_range: str, region: str,
                        safesearch: str) -> list[dict]:
    path = {"web": "web/search", "news": "news/search", "images": "images/search"}[kind]
    params: dict[str, Any] = {"q": query, "count": min(n, 100 if kind == "images" else 20)}
    fresh = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}.get(time_range)
    if fresh and kind != "images":
        params["freshness"] = fresh
    params["safesearch"] = ({"on": "strict", "moderate": "strict", "off": "off"} if kind == "images" else
                            {"on": "strict", "moderate": "moderate", "off": "off"})[safesearch]
    resp = await net.http().get(f"https://api.search.brave.com/res/v1/{path}", params=params, timeout=30,
                                headers={"X-Subscription-Token": cfg["api_key"], "Accept": "application/json"})
    _raise_for(resp, "Brave Search")
    data = resp.json()
    items = ((data.get("web") or {}).get("results") or []) if kind == "web" else (data.get("results") or [])
    out = []
    for r in items:
        url = r.get("url")
        if kind == "images":
            out.append({"title": _clean(r.get("title"), 200), "url": url,
                        "image": (r.get("properties") or {}).get("url"),
                        "thumbnail": (r.get("thumbnail") or {}).get("src"), "source": r.get("source") or _domain(url)})
        else:
            meta = r.get("meta_url") or {}
            out.append({"title": _clean(r.get("title"), 200), "url": url, "snippet": _clean(r.get("description")),
                        "source": (r.get("profile") or {}).get("name") or meta.get("hostname") or _domain(url),
                        "published": r.get("page_age") or r.get("age")})
    return out


async def _search_searxng(cfg: dict, kind: str, query: str, n: int, time_range: str, region: str,
                          safesearch: str) -> list[dict]:
    base = str(cfg["base_url"]).rstrip("/")  # admin-configured (may be an internal host) → no SSRF check
    params: dict[str, Any] = {"q": query, "format": "json",
                              "categories": {"web": "general", "news": "news", "images": "images"}[kind],
                              "safesearch": {"off": 0, "moderate": 1, "on": 2}[safesearch]}
    if time_range != "any":
        params["time_range"] = time_range
    resp = await net.http().get(f"{base}/search", params=params, timeout=30, headers={"Accept": "application/json"})
    _raise_for(resp, "SearXNG")
    out = []
    for r in (resp.json().get("results") or [])[: n * 2]:
        url = r.get("url")
        if kind == "images":
            out.append({"title": _clean(r.get("title"), 200), "url": url, "image": r.get("img_src"),
                        "thumbnail": r.get("thumbnail_src"), "source": _domain(url)})
        else:
            out.append({"title": _clean(r.get("title"), 200), "url": url, "snippet": _clean(r.get("content")),
                        "source": _domain(url), "published": r.get("publishedDate")})
    return out


_DISPATCH = {"duckduckgo": _search_ddg, "tavily": _search_tavily, "brave": _search_brave, "searxng": _search_searxng}


async def run_search(ctx: ToolContext, kind: str, query: str, max_results: int, time_range: str = "any",
                     region: str = "us-en", safesearch: str = "moderate") -> tuple[list[dict], str, str | None]:
    query = (query or "").strip()
    if not query:
        raise ToolError("A search query is required.")
    n = max(1, min(int(max_results or 8), 25 if kind != "images" else 50))
    cfg = await provider_config(ctx)
    p = cfg["provider"]
    note = None
    if p not in PROVIDERS:
        note, p = f"Unknown search provider '{p}'; used DuckDuckGo.", "duckduckgo"
    elif p in ("tavily", "brave") and not cfg.get("api_key"):
        note, p = f"{p.title()} is selected but no API key is configured; used DuckDuckGo.", "duckduckgo"
    elif p == "searxng" and not cfg.get("base_url"):
        note, p = "SearXNG is selected but no base URL is configured; used DuckDuckGo.", "duckduckgo"
    try:
        results = await _DISPATCH[p](cfg, kind, query, n, time_range, region, safesearch)
    except Exception as e:  # noqa: BLE001
        if p == "duckduckgo":
            if isinstance(e, ToolError):
                raise
            raise ToolError(f"Web search failed ({e.__class__.__name__}: {str(e)[:200]}). "
                            "Try again or rephrase.") from e
        log.warning("%s search failed, falling back to DuckDuckGo: %s", p, e)
        note = f"{p} search failed ({str(e)[:160]}); used DuckDuckGo instead."
        p = "duckduckgo"
        results = await _search_ddg(cfg, kind, query, n, time_range, region, safesearch)
    results = dedupe(results, key="image" if kind == "images" else "url")[:n]
    return results, p, note


def _result(kind: str, query: str, results: list[dict], provider: str, note: str | None) -> ToolResult:
    content: dict[str, Any] = {"query": query, "provider": provider, "count": len(results), "results": results}
    if note:
        content["note"] = note
    if not results:
        content["hint"] = "No results. Try broader or different keywords."
    return ToolResult(content=content, display=f"{len(results)} {kind} results for '{query[:60]}'",
                      citations=[{"title": r.get("title"), "url": r.get("url")} for r in results if r.get("url")])


# ----------------------------------------------------------------------------------------- tools: search
@search.tool(risk=Risk.LOW, scope="read", timeout=60)
async def web(ctx: ToolContext, query: str, max_results: int = 8, time_range: TimeRange = "any",
              site: str | None = None, region: str = "us-en", safesearch: SafeSearch = "moderate") -> ToolResult:
    """Search the web and return ranked results (title, url, snippet, source).

    Use for current facts, companies, products, people, documentation or anything outside the knowledge base.
    Read promising results in full with search_fetch.

    Args:
        query: What to search for. Quotes and operators like site: work.
        max_results: Number of results (1-25).
        time_range: Only results from the last day/week/month/year.
        site: Restrict results to one domain, e.g. "sec.gov".
        region: Region/language code, e.g. "us-en", "uk-en", "de-de".
        safesearch: Safe-search level.
    """
    q = f"{query} site:{site.strip()}" if site and site.strip() else query
    results, provider, note = await run_search(ctx, "web", q, max_results, time_range, region, safesearch)
    return _result("web", q, results, provider, note)


@search.tool(risk=Risk.LOW, scope="read", timeout=60)
async def news(ctx: ToolContext, query: str, max_results: int = 8, time_range: TimeRange = "week",
               region: str = "us-en", safesearch: SafeSearch = "moderate") -> ToolResult:
    """Search recent news articles (title, url, snippet, source, published date).

    Args:
        query: News topic, company or event.
        max_results: Number of articles (1-25).
        time_range: How recent the articles must be.
        region: Region/language code, e.g. "us-en".
        safesearch: Safe-search level.
    """
    results, provider, note = await run_search(ctx, "news", query, max_results, time_range, region, safesearch)
    return _result("news", query, results, provider, note)


@search.tool(risk=Risk.LOW, scope="read", timeout=60)
async def images(ctx: ToolContext, query: str, max_results: int = 10, region: str = "us-en",
                 safesearch: SafeSearch = "moderate") -> ToolResult:
    """Search for images; returns image URLs, thumbnails, sizes and the page each image is on.

    Args:
        query: What the images should show.
        max_results: Number of images (1-50).
        region: Region/language code.
        safesearch: Safe-search level.
    """
    results, provider, note = await run_search(ctx, "images", query, max_results, "any", region, safesearch)
    return _result("image", query, results, provider, note)


_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"


async def _arxiv(query: str, n: int) -> list[dict]:
    terms = re.findall(r'"[^"]+"|\S+', query)[:8]
    sq = " AND ".join(f"all:{t}" for t in terms) or f"all:{query}"
    resp = await net.http().get("https://export.arxiv.org/api/query", timeout=30,
                                params={"search_query": sq, "start": 0, "max_results": n, "sortBy": "relevance"})
    _raise_for(resp, "arXiv")
    root = ET.fromstring(resp.content)
    out = []
    for e in root.findall(f"{_ATOM}entry"):
        title = _clean(e.findtext(f"{_ATOM}title"), 300)
        if not title or title.lower() == "error":
            continue
        pdf = next((ln.get("href") for ln in e.findall(f"{_ATOM}link") if ln.get("title") == "pdf"), None)
        authors = [a.findtext(f"{_ATOM}name") or "" for a in e.findall(f"{_ATOM}author")]
        cat = e.find(f"{_ARXIV}primary_category")
        out.append({"title": title, "url": (e.findtext(f"{_ATOM}id") or "").strip(),
                    "snippet": _clean(e.findtext(f"{_ATOM}summary"), 350), "source": "arXiv",
                    "published": (e.findtext(f"{_ATOM}published") or "")[:10],
                    "authors": ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else ""),
                    "venue": cat.get("term") if cat is not None else None, "pdf": pdf})
    return out


async def _semantic_scholar(query: str, n: int, year_from: int | None) -> list[dict]:
    params: dict[str, Any] = {"query": query, "limit": n,
                              "fields": "title,url,abstract,year,venue,authors,citationCount,openAccessPdf,"
                                        "externalIds,publicationDate"}
    if year_from:
        params["year"] = f"{year_from}-"
    resp = await net.http().get("https://api.semanticscholar.org/graph/v1/paper/search", params=params, timeout=30)
    _raise_for(resp, "Semantic Scholar")
    out = []
    for p in resp.json().get("data") or []:
        authors = [a.get("name", "") for a in p.get("authors") or []]
        doi = (p.get("externalIds") or {}).get("DOI")
        url = p.get("url") or (f"https://doi.org/{doi}" if doi else None)
        out.append({"title": _clean(p.get("title"), 300), "url": url,
                    "snippet": _clean(p.get("abstract"), 350), "source": "Semantic Scholar",
                    "published": p.get("publicationDate") or (str(p["year"]) if p.get("year") else None),
                    "authors": ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else ""),
                    "venue": p.get("venue") or None, "citations": p.get("citationCount"),
                    "pdf": (p.get("openAccessPdf") or {}).get("url"), "doi": doi})
    return out


@search.tool(risk=Risk.LOW, scope="read", timeout=60)
async def scholar(ctx: ToolContext, query: str, max_results: int = 8,
                  source: Literal["all", "arxiv", "semantic_scholar"] = "all",
                  year_from: int | None = None) -> ToolResult:
    """Search scholarly papers on arXiv and Semantic Scholar (title, authors, abstract snippet, venue, citations,
    PDF link when open access).

    Args:
        query: Topic or paper title.
        max_results: Number of papers (1-25).
        source: Which index to search.
        year_from: Only papers published in or after this year (Semantic Scholar).
    """
    if not (query or "").strip():
        raise ToolError("A search query is required.")
    n = max(1, min(int(max_results or 8), 25))
    jobs: dict[str, Any] = {}
    if source in ("all", "arxiv"):
        jobs["arXiv"] = _arxiv(query, n)
    if source in ("all", "semantic_scholar"):
        jobs["Semantic Scholar"] = _semantic_scholar(query, n, year_from)
    outcomes = await asyncio.gather(*jobs.values(), return_exceptions=True)
    lists, errors = [], []
    for name, res in zip(jobs, outcomes):
        if isinstance(res, BaseException):
            errors.append(f"{name}: {str(res)[:160] or res.__class__.__name__}")
        else:
            if year_from and name == "arXiv":
                res = [r for r in res if (r.get("published") or "0")[:4] >= str(year_from)]
            lists.append(res)
    if not lists:
        raise ToolError("Scholarly search failed — " + "; ".join(errors))
    merged, seen = [], set()
    for i in range(max((len(x) for x in lists), default=0)):
        for lst in lists:
            if i < len(lst):
                key = re.sub(r"[^a-z0-9]", "", lst[i]["title"].lower())[:80]
                if key and key not in seen:
                    seen.add(key)
                    merged.append({k: v for k, v in lst[i].items() if v not in (None, "", [])})
    merged = merged[:n]
    content: dict[str, Any] = {"query": query, "count": len(merged), "results": merged}
    if errors:
        content["errors"] = errors
    return ToolResult(content=content, display=f"{len(merged)} papers for '{query[:60]}'",
                      citations=[{"title": r["title"], "url": r.get("url")} for r in merged if r.get("url")])


# ----------------------------------------------------------------------------------------- tools: reading
async def fetch_page(url: str, *, include_links: bool = False, links_limit: int = 50) -> dict[str, Any]:
    """Fetch a public URL and turn it into readable text (HTML readability, PDFs via pypdf, text as-is)."""
    f = await B.safe_fetch(url)
    if f.status >= 400:
        raise ToolError(f"HTTP {f.status} fetching {f.final_url}.")
    page: dict[str, Any] = {"url": f.final_url, "content_type": f.mime or "unknown"}
    if f.is_pdf:
        page["title"] = B.pdf_bytes_title(f.content) or urlparse(f.final_url).path.rsplit("/", 1)[-1]
        page["text"] = B.pdf_bytes_text(f.content)
        page["kind"] = "pdf"
        if not page["text"]:
            page["note"] = "The PDF has no extractable text (scanned?). Download it and use vision_ocr."
    elif f.is_html:
        r = B.readable(f.content, f.final_url)
        page.update(title=r["title"], text=r["text"], kind="html", links_count=r["links_count"])
        if r.get("description"):
            page["description"] = r["description"]
        if len(r["text"]) < 200:
            page["note"] = ("Very little text was found — the page may need JavaScript. Try browser_open, which "
                            "renders pages when the interactive browser is available.")
        if include_links:
            page["links"], _ = B.extract_links(f.content, f.final_url, limit=links_limit)
    elif f.mime.startswith("text/") or f.mime in ("application/json", "application/xml", "application/rss+xml",
                                                  "application/atom+xml", "application/ld+json"):
        page.update(title="", text=f.text(), kind="text")
    elif f.mime.startswith("image/"):
        raise ToolError(f"{f.final_url} is an image. Save it with browser_download, then use the vision tools.")
    else:
        raise ToolError(f"{f.final_url} is a '{f.mime or 'binary'}' file, not a page. Use browser_download to save it.")
    if f.truncated:
        page["note"] = (page.get("note", "") + " Only the first 15 MB were downloaded.").strip()
    return page


@search.tool(risk=Risk.LOW, scope="read", timeout=90)
async def fetch(ctx: ToolContext, url: str, max_chars: int = 20000, include_links: bool = False) -> ToolResult:
    """Read a web page or online PDF as clean text (main content only — navigation, ads and footers removed).

    Args:
        url: Public http(s) URL to read.
        max_chars: Maximum characters of text to return.
        include_links: Also return the page's links (up to 50).
    """
    page = await fetch_page(url, include_links=include_links)
    text, truncated = O.clip(page.pop("text", ""), max(500, min(int(max_chars or 20000), 100000)))
    content: dict[str, Any] = {"url": page.pop("url"), "title": page.pop("title", ""), **page,
                               "chars": len(text), "truncated": truncated, "content": text}
    return ToolResult(content=content, display=f"Read {content['title'] or content['url']}"[:120],
                      citations=[{"title": content["title"] or content["url"], "url": content["url"]}])


async def source_text(ctx: ToolContext, url: str | None, path: str | None, text: str | None) -> tuple[str, str, str]:
    """(text, title, source label) from exactly one of url / workspace path / inline text."""
    if text and text.strip():
        return text, "", "provided text"
    if path and path.strip():
        p = O.ws_input(ctx, path)
        body = await asyncio.to_thread(O.extract_text, p)
        return body, p.name, O.vpath(ctx, p)
    if url and url.strip():
        page = await fetch_page(url)
        if not page.get("text"):
            raise ToolError(f"No readable text found at {page['url']}.")
        return page["text"], page.get("title") or "", page["url"]
    raise ToolError("Provide a url, a workspace path, or text.")


_FIELD_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def _schema_from_fields(fields: list[str]) -> dict:
    props: dict[str, Any] = {}
    for f in fields:
        name, _, typ = str(f).partition(":")
        name = name.strip()
        if not name:
            continue
        typ = typ.strip().lower() or "string"
        if typ in ("list", "array"):
            props[name] = {"type": "array", "items": {"type": "string"}}
        elif typ in _FIELD_TYPES:
            props[name] = {"type": typ}
        else:
            props[name] = {"type": "string", "description": typ}
    return {"type": "object", "properties": props}


@search.tool(risk=Risk.LOW, scope="read", timeout=180)
async def extract(ctx: ToolContext, instructions: str, url: str | None = None, path: str | None = None,
                  text: str | None = None, fields: list[str] | None = None, json_schema: dict[str, Any] | None = None,
                  max_chars: int = 40000) -> dict:
    """Extract structured JSON from a web page, workspace document or text (e.g. pricing tiers, contacts,
    line items, key terms).

    Args:
        instructions: What to extract, e.g. "all pricing plans with monthly price and included seats".
        url: Public URL to read (alternative to path/text).
        path: Workspace document to read (PDF, DOCX, XLSX, HTML, text...).
        text: Raw text to extract from.
        fields: Simple field list instead of a schema, e.g. ["company", "revenue:number", "products:array"].
        json_schema: Full JSON Schema for the output (overrides fields).
        max_chars: Maximum characters of the source to consider.
    """
    from app.llm.client import LLMError

    body, title, src = await source_text(ctx, url, path, text)
    body, truncated = O.clip(body, max(2000, min(int(max_chars or 40000), 150000)))
    if json_schema:
        out_schema = json_schema if json_schema.get("type") else {"type": "object", "properties": json_schema}
    elif fields:
        out_schema = _schema_from_fields(fields)
    else:
        out_schema = {"type": "object", "properties": {
            "data": {"description": "The extracted data, structured as the instructions request"},
            "notes": {"type": "string", "description": "Caveats, e.g. values that were missing or ambiguous"}}}
    messages = [
        {"role": "system", "content": "You extract structured data from source documents. Use only facts stated in "
                                      "the source; use null for anything not present; never invent values. Keep "
                                      "numbers as numbers and copy names exactly."},
        {"role": "user", "content": f"Instructions: {instructions}\n\nSource ({src}{' — ' + title if title else ''}):"
                                    f"\n\"\"\"\n{body}\n\"\"\""},
    ]
    llm = await ctx.llm()
    try:
        data = await llm.json(messages, out_schema, max_tokens=4000, name="extraction")
    except LLMError as e:
        raise ToolError(f"Extraction failed: {e}") from e
    if data is None:
        raise ToolError("The model did not return valid JSON. Try simpler instructions or a fields list.")
    result: dict[str, Any] = {"source": src, "data": data}
    if title:
        result["title"] = title
    if truncated:
        result["note"] = "Source was truncated before extraction."
    return result


_LENGTHS = {"short": "3-5 bullet points (under 100 words)",
            "medium": "a one-sentence takeaway followed by 5-8 bullet points (150-250 words)",
            "long": "a short overview paragraph, then sections with bullet points (400-600 words)"}


async def summarize_text(ctx: ToolContext, body: str, *, length: str = "medium", focus: str | None = None,
                         title: str = "", source: str = "", chunk_chars: int = 24000) -> tuple[str, int]:
    """Summarize arbitrarily long text (map-reduce over chunks). Returns (summary, chunks_used)."""
    from app.llm.client import LLMError

    llm = await ctx.llm()
    style = _LENGTHS.get(length, _LENGTHS["medium"])
    focus_line = f"Focus on: {focus}\n" if focus else ""
    system = ("You are a precise analyst writing for busy business readers. Lead with what matters most, keep "
              "numbers, dates and names exact, separate facts from claims, and never add information that is not "
              "in the source. Use markdown.")
    chunks = [body[i:i + chunk_chars] for i in range(0, len(body), chunk_chars)][:8] or [""]
    try:
        if len(chunks) == 1:
            res = await llm.chat([{"role": "system", "content": system},
                                  {"role": "user", "content": f"{focus_line}Summarize as {style}.\n\nSource: "
                                                              f"{title or source}\n\"\"\"\n{chunks[0]}\n\"\"\""}],
                                 temperature=0.2, max_tokens=1500)
            return res.content.strip(), 1
        partials = []
        for i, ch in enumerate(chunks, 1):
            await ctx.progress(f"Summarizing part {i} of {len(chunks)}", int(100 * i / (len(chunks) + 1)))
            res = await llm.chat([{"role": "system", "content": system},
                                  {"role": "user", "content": f"{focus_line}Extract the key points (facts, numbers, "
                                                              f"decisions) of part {i}/{len(chunks)} as bullets.\n"
                                                              f"\"\"\"\n{ch}\n\"\"\""}],
                                 temperature=0.2, max_tokens=900)
            partials.append(res.content.strip())
        res = await llm.chat([{"role": "system", "content": system},
                              {"role": "user", "content": f"{focus_line}Combine these notes from consecutive parts of "
                                                          f"'{title or source}' into one summary formatted as {style}."
                                                          "\n\n" + "\n\n".join(partials)}],
                             temperature=0.2, max_tokens=1500)
        return res.content.strip(), len(chunks)
    except LLMError as e:
        raise ToolError(f"Summarization failed: {e}") from e


@search.tool(risk=Risk.LOW, scope="read", timeout=300)
async def summarize(ctx: ToolContext, url: str | None = None, path: str | None = None, text: str | None = None,
                    length: Literal["short", "medium", "long"] = "medium", focus: str | None = None) -> dict:
    """Summarize a web page, online PDF, workspace document or text into a concise brief.

    Long sources are summarized in parts and combined.

    Args:
        url: Public URL to summarize (alternative to path/text).
        path: Workspace document to summarize (PDF, DOCX, PPTX, XLSX, HTML, text...).
        text: Raw text to summarize.
        length: Summary length.
        focus: Optional angle, e.g. "pricing changes" or "risks for a buyer".
    """
    body, title, src = await source_text(ctx, url, path, text)
    if len(body) > 8 * 24000:
        body = body[: 8 * 24000]
        clipped = True
    else:
        clipped = False
    summary, parts = await summarize_text(ctx, body, length=length, focus=focus, title=title, source=src)
    out: dict[str, Any] = {"source": src, "summary": summary, "chars_read": len(body)}
    if title:
        out["title"] = title
    if parts > 1:
        out["parts"] = parts
    if clipped:
        out["note"] = "Only the first ~190k characters were summarized."
    return out
