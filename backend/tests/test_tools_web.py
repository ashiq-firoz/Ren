"""Web / vision / audio tools: registration, search providers (mocked ddgs + HTTP), page fetching with an
httpx.MockTransport, the SSRF guard, browser static mode, vision plumbing and audio endpoints. No network."""
from __future__ import annotations

import importlib
import ipaddress
import json
import re

import httpx
import pytest

from app.tools import net
from app.tools.base import Risk, ToolError, ToolResult, registry

MY_GROUPS = ("search", "browser", "computer", "documents", "sheets", "slides", "pdf", "analytics", "artifacts",
             "vision", "audio")


def _c(res):
    return res.content if isinstance(res, ToolResult) else res


def _fake_is_private(host: str) -> bool:
    """DNS-free stand-in for net._is_private: IP literals are classified, names are public."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


@pytest.fixture
def offline_dns(monkeypatch):
    monkeypatch.setattr(net, "_is_private", _fake_is_private)


def use_transport(monkeypatch, handler) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(net, "http", lambda: client)
    return client


# ----------------------------------------------------------------------------------------- registration
def test_tool_groups_registered():
    for m in MY_GROUPS:
        importlib.import_module(f"app.tools.builtin.{m}")
    expected = {
        "search": ("search", "core", "search", "web news images scholar fetch extract summarize"),
        "browser": ("browser", "core", "globe", "open back forward reload get_page get_dom find extract_links "
                                                "extract_table click type select hover scroll press screenshot wait "
                                                "new_tab close_tab tabs fill_form submit_form download"),
        "computer": ("computer", "core", "mouse-pointer-click", "screenshot mouse_move left_click right_click "
                                                                "double_click drag type keypress hotkey scroll"),
        "documents": ("doc", "productivity", "file-text", "create read append replace insert_table insert_image "
                                                          "add_comment compare export_pdf apply_style"),
        "sheets": ("sheet", "data", "sheet", "create read write append clear find filter sort formula calculate "
                                             "create_sheet rename_sheet delete_sheet chart pivot format export"),
        "slides": ("ppt", "productivity", "presentation", "create read add_slide delete_slide duplicate_slide "
                                                          "reorder add_text add_image add_table add_chart apply_theme"),
        "pdf": ("pdf", "productivity", "file-type", "create read extract_tables merge split info watermark"),
        "analytics": ("analytics", "data", "chart-line", "profile query aggregate join reconcile correlate outliers "
                                                         "forecast visualize"),
        "artifacts": ("artifact", "productivity", "package", "create export validate list"),
        "vision": ("vision", "core", "eye", "describe ocr extract_table compare detect crop"),
        "audio": ("audio", "core", "audio-lines", "transcribe translate summarize speak"),
    }
    for gid, (prefix, category, icon, names) in expected.items():
        g = registry.group(gid)
        assert g is not None, gid
        assert (g.prefix, g.category, g.icon) == (prefix, category, icon), gid
        assert {t.name for t in g.tools} == {f"{prefix}_{n}" for n in names.split()}, gid
        for t in g.tools:
            assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t.name)
            assert t.description and t.parameters["type"] == "object"
            assert all(p.get("description") for p in t.parameters["properties"].values()), t.name
            if t.risk == Risk.LOW and t.risk_fn is None and t.scope == "read":
                assert not t.content_fields
    comp = registry.group("computer")
    assert comp.default_enabled is False and comp.requires == ("playwright",)
    assert all(registry.group(g).default_enabled for g in MY_GROUPS if g != "computer")

    def spec(name):
        s = registry.get(name)
        assert s is not None, name
        return s

    assert (spec("browser_open").risk, spec("browser_open").scope) == (Risk.LOW, "read")
    for name in ("browser_click", "browser_type", "browser_select", "browser_press", "browser_scroll",
                 "browser_fill_form"):
        assert (spec(name).risk, spec(name).scope) == (Risk.MEDIUM, "write"), name
    for name in ("browser_submit_form", "browser_download"):
        assert (spec(name).risk, spec(name).scope) == (Risk.HIGH, "write"), name
    assert spec("browser_type").content_fields == ("text",)
    assert spec("browser_submit_form").content_fields == ("fields",)
    for t in registry.group("search").tools:
        assert (t.risk, t.scope) == (Risk.LOW, "read"), t.name
    assert spec("analytics_query").effective_risk({}) == Risk.LOW
    assert spec("analytics_query").effective_risk({"save_as": "output/x.csv"}) == Risk.MEDIUM
    assert spec("doc_create").risk == Risk.MEDIUM and spec("doc_read").risk == Risk.LOW
    assert spec("audio_speak").risk == Risk.MEDIUM and spec("vision_crop").scope == "write"
    preview = spec("browser_submit_form").render_preview(
        {"fields": {"Email": "ana@acme.com", "Password": "hunter2"}, "submit": "Sign up"})
    assert "ana@acme.com" in preview and "hunter2" not in preview and "Sign up" in preview
    assert "downloads/" in spec("browser_download").render_preview({"url": "https://x.test/a.pdf"})
    # validation through the framework (what the executor does)
    v = spec("sheet_read").validate({"path": "a.xlsx", "max_rows": "50"})
    assert v["max_rows"] == 50 and v["header"] == "auto"
    with pytest.raises(ToolError):
        spec("search_web").validate({"query": "x", "time_range": "decade"})


# ----------------------------------------------------------------------------------------- search
async def test_search_with_mocked_ddgs(tool_ctx, monkeypatch):
    from app.tools.builtin import search as S

    calls = []

    def fake_ddgs(kind, query, max_results, timelimit, region, safesearch):
        calls.append((kind, query, max_results, timelimit, region, safesearch))
        if kind == "web":
            return [
                {"title": "Acme — Home", "href": "https://www.acme.com/", "body": "Acme <b>makes</b> widgets"},
                {"title": "Acme home (tracking)", "href": "https://acme.com/?utm_source=x", "body": "dup"},
                {"title": "Acme pricing", "href": "https://acme.com/pricing", "body": "Plans from $10"},
            ]
        if kind == "news":
            return [{"title": "Acme raises $10M", "url": "https://news.example/acme", "body": "Funding round",
                     "source": "Example News", "date": "2026-10-01T10:00:00"}]
        return [{"title": "Widget", "image": "https://img.example/w.png", "thumbnail": "https://img.example/t.png",
                 "url": "https://acme.com/widget", "width": 800, "height": 600, "source": "Bing"}]

    monkeypatch.setattr(S, "_ddgs_sync", fake_ddgs)
    res = await S.web(tool_ctx, query="acme", max_results=5, time_range="week", site="acme.com")
    c = res.content
    assert c["provider"] == "duckduckgo" and c["count"] == 2
    assert [r["url"] for r in c["results"]] == ["https://www.acme.com/", "https://acme.com/pricing"]
    assert c["results"][0] == {"title": "Acme — Home", "url": "https://www.acme.com/",
                               "snippet": "Acme makes widgets", "source": "acme.com"}
    assert calls[0][:4] == ("web", "acme site:acme.com", 5, "w")
    assert res.citations and res.citations[0]["url"] == "https://www.acme.com/"

    news = _c(await S.news(tool_ctx, query="acme funding"))
    assert news["results"][0]["published"] == "2026-10-01T10:00:00" and news["results"][0]["source"] == "Example News"
    assert calls[-1][3] == "w"  # news defaults to the last week
    imgs = _c(await S.images(tool_ctx, query="acme widget", max_results=3))
    assert imgs["results"][0]["image"] == "https://img.example/w.png"

    monkeypatch.setattr(S, "_ddgs_sync", lambda *a: [])
    empty = _c(await S.web(tool_ctx, query="zzzz"))
    assert empty["count"] == 0 and "hint" in empty
    with pytest.raises(ToolError):
        await S.web(tool_ctx, query="  ")


async def test_search_providers_and_fallback(tool_ctx, monkeypatch, offline_dns):
    from app.tools.builtin import search as S

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host, path = request.url.host, request.url.path
        if host == "api.tavily.com":
            body = json.loads(request.content)
            assert body["query"] == "ren agents" and request.headers["authorization"] == "Bearer tvly-test"
            return httpx.Response(200, json={"results": [{"title": "T1", "url": "https://t1.example/a",
                                                          "content": "Tavily hit", "published_date": "2026-01-01"}]})
        if host == "api.search.brave.com":
            assert request.headers["x-subscription-token"] == "brave-key"
            if path.endswith("/web/search"):
                return httpx.Response(200, json={"web": {"results": [
                    {"title": "B1", "url": "https://b1.example/", "description": "<strong>Brave</strong> hit",
                     "profile": {"name": "B1 Site"}, "page_age": "2026-02-02"}]}})
            return httpx.Response(200, json={"results": [{"title": "BN", "url": "https://bn.example/",
                                                          "description": "news", "age": "1 day ago"}]})
        if host == "searx.internal":
            assert request.url.params["format"] == "json" and request.url.params["categories"] == "general"
            return httpx.Response(200, json={"results": [{"title": "S1", "url": "https://s1.example/",
                                                          "content": "Searx hit", "engine": "x"}]})
        if host == "broken.example":
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    use_transport(monkeypatch, handler)
    monkeypatch.setattr(S, "_ddgs_sync", lambda kind, q, *a: [{"title": "DDG", "href": "https://ddg.example/",
                                                                "body": "fallback"}])

    tool_ctx.extra["web_search"] = {"provider": "tavily", "api_key": "tvly-test"}
    r = _c(await S.web(tool_ctx, query="ren agents"))
    assert r["provider"] == "tavily" and r["results"][0]["snippet"] == "Tavily hit"

    tool_ctx.extra["web_search"] = {"provider": "brave", "api_key": "brave-key"}
    r = _c(await S.web(tool_ctx, query="ren agents", time_range="month"))
    assert r["provider"] == "brave" and r["results"][0] == {"title": "B1", "url": "https://b1.example/",
                                                           "snippet": "Brave hit", "source": "B1 Site",
                                                           "published": "2026-02-02"}
    assert seen[-1].url.params["freshness"] == "pm"
    r = _c(await S.news(tool_ctx, query="ren agents"))
    assert r["results"][0]["title"] == "BN"

    tool_ctx.extra["web_search"] = {"provider": "searxng", "base_url": "http://searx.internal:8080"}
    r = _c(await S.web(tool_ctx, query="ren agents"))
    assert r["provider"] == "searxng" and r["results"][0]["snippet"] == "Searx hit"

    tool_ctx.extra["web_search"] = {"provider": "brave"}  # no key → DuckDuckGo with a note
    r = _c(await S.web(tool_ctx, query="ren agents"))
    assert r["provider"] == "duckduckgo" and "no API key" in r["note"]

    tool_ctx.extra["web_search"] = {"provider": "searxng", "base_url": "https://broken.example"}
    r = _c(await S.web(tool_ctx, query="ren agents"))
    assert r["provider"] == "duckduckgo" and "failed" in r["note"]


async def test_search_provider_config_from_org_settings(tool_ctx):
    from app.core.security import decrypt_json, encrypt_json
    from app.models import Organization
    from app.tools.builtin import search as S

    async with tool_ctx.session() as db:
        org = await db.get(Organization, tool_ctx.org_id)
        old_settings, old_secrets = dict(org.settings or {}), org.secrets_enc
        org.settings = {**old_settings, "web_search": {"provider": "brave", "base_url": None}}
        org.secrets_enc = encrypt_json({**decrypt_json(old_secrets), "web_search_api_key": "org-brave-key"})
    try:
        cfg = await S.provider_config(tool_ctx)
        assert cfg["provider"] == "brave" and cfg["api_key"] == "org-brave-key"
    finally:
        async with tool_ctx.session() as db:
            org = await db.get(Organization, tool_ctx.org_id)
            org.settings, org.secrets_enc = old_settings, old_secrets
    cfg = await S.provider_config(tool_ctx)
    assert cfg["provider"] == "duckduckgo"


ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <published>2024-01-02T00:00:00Z</published>
    <title>Retrieval Augmented
      Agents</title>
    <summary>We study agents that retrieve before they act.</summary>
    <author><name>Ana Lee</name></author>
    <author><name>Bo Chen</name></author>
    <link title="pdf" href="http://arxiv.org/pdf/2401.00001v1" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>"""


async def test_scholar_with_mocked_apis(tool_ctx, monkeypatch):
    from app.tools.builtin import search as S

    state = {"s2": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            assert "all:retrieval" in request.url.params["search_query"]
            return httpx.Response(200, text=ARXIV_FEED, headers={"content-type": "application/atom+xml"})
        if request.url.host == "api.semanticscholar.org":
            if state["s2"] != 200:
                return httpx.Response(state["s2"])
            return httpx.Response(200, json={"total": 2, "data": [
                {"title": "Retrieval augmented agents", "url": "https://s2.example/p1", "abstract": "dup",
                 "year": 2024},
                {"title": "Swarm Orchestration at Scale", "url": "https://s2.example/p2", "abstract": "Swarms.",
                 "year": 2025, "venue": "NeurIPS", "authors": [{"name": "C D"}], "citationCount": 12,
                 "openAccessPdf": {"url": "https://s2.example/p2.pdf"}, "externalIds": {"DOI": "10.1/x"},
                 "publicationDate": "2025-05-01"}]})
        return httpx.Response(404)

    use_transport(monkeypatch, handler)
    res = await S.scholar(tool_ctx, query="retrieval agents")
    c = res.content
    assert c["count"] == 2 and "errors" not in c
    first = c["results"][0]
    assert first["title"] == "Retrieval Augmented Agents" and first["source"] == "arXiv"
    assert first["authors"] == "Ana Lee, Bo Chen" and first["pdf"].endswith("2401.00001v1")
    assert first["venue"] == "cs.AI" and first["published"] == "2024-01-02"
    second = c["results"][1]
    assert second["source"] == "Semantic Scholar" and second["citations"] == 12 and second["doi"] == "10.1/x"

    state["s2"] = 429  # rate-limited index → partial results + error note
    c = _c(await S.scholar(tool_ctx, query="retrieval agents"))
    assert c["count"] == 1 and "Semantic Scholar" in c["errors"][0]


# ----------------------------------------------------------------------------------------- fetch / SSRF
PRICING_HTML = """<!doctype html><html lang="en"><head><title>Acme Pricing</title>
<meta name="description" content="Plans and prices"></head>
<body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<div class="cookie-banner">We use cookies <button>Accept</button></div>
<main><article>
<h1>Pricing</h1>
<p>Acme offers three plans for teams of every size, billed monthly or annually with a discount.</p>
<ul><li>Starter — $10 per seat</li><li>Pro — $25 per seat</li></ul>
<table><tr><th>Plan</th><th>Price</th></tr><tr><td>Starter</td><td>$10</td></tr></table>
<p>Contact <a href="https://sales.acme.test/contact">sales</a> for enterprise pricing and volume discounts.</p>
</article></main>
<footer>&copy; Acme <a href="/privacy">Privacy</a></footer>
<script>var tracking = 1;</script>
</body></html>"""


def _site_handler(pdf_bytes: bytes):
    long_html = "<html><head><title>Long</title></head><body><article>" + "".join(
        f"<p>Paragraph {i} with enough words to count as real content for the extractor.</p>" for i in range(300)
    ) + "</article></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://acme.test/pricing":
            return httpx.Response(200, html=PRICING_HTML)
        if url == "https://acme.test/old":
            return httpx.Response(301, headers={"location": "/pricing"})
        if url == "https://acme.test/report.pdf":
            return httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"})
        if url == "https://acme.test/evil":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        if url == "https://acme.test/data.json":
            return httpx.Response(200, json={"ok": True})
        if url == "https://acme.test/logo.png":
            return httpx.Response(200, content=b"\x89PNG\r\n", headers={"content-type": "image/png"})
        if url == "https://acme.test/long":
            return httpx.Response(200, html=long_html)
        if url.startswith("http://127.0.0.1"):
            raise AssertionError("the SSRF guard must stop the request before it is sent")
        return httpx.Response(404, text="not found")

    return handler


async def test_fetch_html_pdf_redirects(tool_ctx, monkeypatch, offline_dns):
    from app.tools.builtin import _office as O
    from app.tools.builtin import search as S

    pdf_bytes = O.markdown_to_pdf("Revenue grew 12% year over year.", title="Annual Report")
    use_transport(monkeypatch, _site_handler(pdf_bytes))

    res = await S.fetch(tool_ctx, url="https://acme.test/pricing", include_links=True)
    c = res.content
    assert c["title"] == "Acme Pricing" and c["kind"] == "html" and c["description"] == "Plans and prices"
    text = c["content"]
    assert "# Pricing" in text and "- Starter — $10 per seat" in text and "| Plan | Price |" in text
    assert "Contact sales for enterprise pricing" in text
    assert "cookies" not in text and "tracking" not in text and "Privacy" not in text and "About" not in text
    urls = {link["url"] for link in c["links"]}
    assert {"https://sales.acme.test/contact", "https://acme.test/about"} <= urls
    assert res.citations == [{"title": "Acme Pricing", "url": "https://acme.test/pricing"}]

    moved = _c(await S.fetch(tool_ctx, url="acme.test/old"))  # bare domain + redirect
    assert moved["url"] == "https://acme.test/pricing" and moved["title"] == "Acme Pricing"

    pdf = _c(await S.fetch(tool_ctx, url="https://acme.test/report.pdf"))
    assert pdf["kind"] == "pdf" and "Revenue grew 12%" in pdf["content"] and pdf["title"] == "Annual Report"

    js = _c(await S.fetch(tool_ctx, url="https://acme.test/data.json"))
    assert js["kind"] == "text" and json.loads(js["content"]) == {"ok": True}

    long = _c(await S.fetch(tool_ctx, url="https://acme.test/long", max_chars=1000))
    assert long["truncated"] and len(long["content"]) < 1200

    with pytest.raises(ToolError, match="private"):
        await S.fetch(tool_ctx, url="https://acme.test/evil")  # redirect hop into the private network
    with pytest.raises(ToolError, match="image"):
        await S.fetch(tool_ctx, url="https://acme.test/logo.png")
    with pytest.raises(ToolError, match="404"):
        await S.fetch(tool_ctx, url="https://acme.test/missing")

    summary = await S.summarize(tool_ctx, url="https://acme.test/pricing", length="short")
    assert summary["source"] == "https://acme.test/pricing" and summary["summary"]


async def test_ssrf_guard_rejects_private_and_non_http_urls(tool_ctx):
    from app.tools.builtin import _browser as B
    from app.tools.builtin import browser as BR
    from app.tools.builtin import search as S

    for url in ("http://127.0.0.1/", "http://127.0.0.1:8000/admin", "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.5/", "http://[::1]/", "http://localhost:8080/", "http://printer.local/"):
        with pytest.raises(ToolError, match="blocked"):
            await S.fetch(tool_ctx, url=url)
    for url in ("file:///etc/passwd", "file://C:/Windows/win.ini", "ftp://example.com/x", "gopher://x"):
        with pytest.raises(ToolError, match="http"):
            await S.fetch(tool_ctx, url=url)
    with pytest.raises(ToolError):
        await BR.open_(tool_ctx, url="http://127.0.0.1:3000/")
    with pytest.raises(ToolError):
        await BR.open_(tool_ctx, url="file:///etc/passwd")
    with pytest.raises(ToolError):
        await BR.download(tool_ctx, url="http://192.168.1.10/backup.zip")
    with pytest.raises(ToolError):
        await S.extract(tool_ctx, instructions="x", url="http://127.0.0.1/")
    assert B.normalize_url("example.com/a") == "https://example.com/a"
    assert B.normalize_url("example.com:8443/x") == "https://example.com:8443/x"
    assert B.normalize_url("//cdn.example.com/a.js") == "https://cdn.example.com/a.js"


# ----------------------------------------------------------------------------------------- browser (static)
SHOP_HOME = """<html><head><title>Shop Home</title></head><body>
<header class="site-header"><a href="/">Shop</a></header>
<main>
<h1>Welcome to the shop</h1>
<p>We sell carefully made widgets and gadgets for teams that care about quality and craftsmanship.</p>
<a href="/about">About us</a> <a href="https://partner.example/deal?id=7">Partner deal</a>
<table><caption>Products</caption>
<thead><tr><th>Product</th><th>Price</th></tr></thead>
<tbody><tr><td>Widget</td><td>10.50</td></tr><tr><td>Gadget</td><td>25</td></tr></tbody></table>
<ul><li>Widget <button>Add to cart</button></li></ul>
<form action="/subscribe"><input name="email" placeholder="Email"><button type="submit">Subscribe</button></form>
</main></body></html>"""
SHOP_ABOUT = """<html><head><title>About Shop</title></head><body><main><h1>About us</h1>
<p>Founded in 2020 by two engineers who wanted better tools for small teams everywhere.</p></main></body></html>"""


async def test_browser_static_mode(tool_ctx, monkeypatch, offline_dns):
    from app.tools.builtin import _browser as B
    from app.tools.builtin import browser as BR

    monkeypatch.setattr(B, "playwright_available", lambda: False)
    counter = {"home": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://shop.test/":
            counter["home"] += 1
            return httpx.Response(200, html=SHOP_HOME)
        if url == "https://shop.test/about":
            return httpx.Response(200, html=SHOP_ABOUT)
        if url == "https://shop.test/files/price-list.csv":
            return httpx.Response(200, text="product,price\nWidget,10.5\n",
                                  headers={"content-type": "text/csv",
                                           "content-disposition": 'attachment; filename="price-list.csv"'})
        return httpx.Response(404)

    use_transport(monkeypatch, handler)
    home = await BR.open_(tool_ctx, url="https://shop.test/")
    assert home["mode"] == "static" and home["title"] == "Shop Home" and home["status"] == 200
    assert "Welcome to the shop" in home["excerpt"] and "Static mode" in home["note"]

    page = await BR.get_page(tool_ctx, include_links=True)
    assert "carefully made widgets" in page["content"]
    assert any(link["url"] == "https://partner.example/deal?id=7" for link in page["links"])
    links = await BR.extract_links(tool_ctx, contains="about")
    assert links["returned"] == 1 and links["links"][0] == {"text": "About us", "url": "https://shop.test/about"}
    assert (await BR.extract_links(tool_ctx, same_domain=True))["total"] == 2

    tables = await BR.extract_table(tool_ctx)
    assert tables["count"] == 1 and tables["tables"][0]["caption"] == "Products"
    t0 = await BR.extract_table(tool_ctx, index=0)
    assert t0["header"] == ["Product", "Price"] and t0["rows"] == [["Widget", "10.50"], ["Gadget", "25"]]
    saved = _c(await BR.extract_table(tool_ctx, index=0, save_as="output/tests/products.csv"))
    assert saved["path"] == "output/tests/products.csv" and saved["url"] == "https://shop.test/"
    assert tool_ctx.workspace.resolve(saved["path"]).read_text(encoding="utf-8-sig").splitlines() == [
        "Product,Price", "Widget,10.5", "Gadget,25"]
    with pytest.raises(ToolError):
        await BR.extract_table(tool_ctx, index=5)

    dom = await BR.get_dom(tool_ctx)
    refs = {e["ref"]: e for e in dom["elements"]}
    assert 'data-ren-ref="e1"' in dom["html"] and "<script" not in dom["html"]
    assert any(e["tag"] == "button" and e["label"] == "Add to cart" for e in refs.values())
    assert any(e["tag"] == "input" and e["label"] == "Email" for e in refs.values())
    assert (await BR.get_dom(tool_ctx))["elements"] == dom["elements"]  # refs are stable
    found = await BR.find(tool_ctx, text="add to cart")
    assert found["count"] == 1 and found["matches"][0]["tag"] == "button"
    assert found["matches"][0]["ref"] in refs

    about = await BR.open_(tool_ctx, url="https://shop.test/about")
    assert about["title"] == "About Shop"
    assert (await BR.back(tool_ctx))["url"] == "https://shop.test/"
    assert (await BR.forward(tool_ctx))["url"] == "https://shop.test/about"
    with pytest.raises(ToolError):
        await BR.forward(tool_ctx)
    await BR.back(tool_ctx)
    before = counter["home"]
    assert (await BR.reload(tool_ctx))["title"] == "Shop Home" and counter["home"] == before + 1
    assert (await BR.wait(tool_ctx, text="welcome to the shop"))["found"] is True
    assert (await BR.wait(tool_ctx, selector="form input[name=email]"))["found"] is True
    tabs = await BR.tabs(tool_ctx)
    assert tabs["mode"] == "static" and tabs["tabs"][0]["url"] == "https://shop.test/"

    for call in (BR.click(tool_ctx, target="e1"), BR.type_(tool_ctx, target="Email", text="a@b.c"),
                 BR.screenshot(tool_ctx), BR.fill_form(tool_ctx, fields={"email": "a@b.c"}),
                 BR.submit_form(tool_ctx, fields={"email": "a@b.c"}), BR.press(tool_ctx, key="Enter"),
                 BR.scroll(tool_ctx), BR.new_tab(tool_ctx)):
        with pytest.raises(ToolError, match="Playwright"):
            await call

    dl = _c(await BR.download(tool_ctx, url="https://shop.test/files/price-list.csv"))
    assert dl["path"].startswith("downloads/price-list") and dl["mime"] == "text/csv"
    assert tool_ctx.workspace.resolve(dl["path"]).read_text() == "product,price\nWidget,10.5\n"


async def test_interactive_tools_need_playwright(tool_ctx, monkeypatch):
    from app.tools.builtin import _browser as B
    from app.tools.builtin import computer as C

    monkeypatch.setattr(B, "playwright_available", lambda: False)
    with pytest.raises(ToolError, match="playwright install chromium"):
        await C.screenshot(tool_ctx)
    with pytest.raises(ToolError, match="Playwright"):
        await C.left_click(tool_ctx, x=10, y=10)
    assert C._key("ctrl") == "Control" and C._key("f5") == "F5" and C._key("a") == "a"
    assert B.looks_like_css("#main .btn") and B.looks_like_css("button") and not B.looks_like_css("Sign in")


# ----------------------------------------------------------------------------------------- extract / summarize
async def test_extract_and_summarize_with_mock_llm(tool_ctx):
    from app.tools.builtin import documents as D
    from app.tools.builtin import search as S

    ex = await S.extract(tool_ctx, instructions="company facts", text="Acme Corp reported revenue of $12M in 2025.",
                         fields=["company", "revenue:number", "products:array"])
    assert ex["source"] == "provided text" and set(ex["data"]) == {"company", "revenue", "products"}
    assert ex["data"]["revenue"] == 0 and isinstance(ex["data"]["products"], list)  # MockLLM fills the schema

    doc = _c(await D.create(tool_ctx, title="Facts", markdown="Acme has 40 employees.", path="output/tests/facts.docx",
                            overwrite=True))["path"]
    ex2 = await S.extract(tool_ctx, instructions="employees", path=doc,
                          json_schema={"type": "object", "properties": {"employees": {"type": "integer"}}})
    assert ex2["source"] == doc and "employees" in ex2["data"]

    short = await S.summarize(tool_ctx, text="Revenue grew 12%. Churn fell to 3%.", length="short")
    assert short["summary"].startswith("(mock)") and "parts" not in short
    long_text = " ".join(f"Sentence {i} about quarterly results and customer growth." for i in range(1200))
    long = await S.summarize(tool_ctx, text=long_text, focus="risks")
    assert long["parts"] >= 2 and long["chars_read"] == len(long_text)
    with pytest.raises(ToolError):
        await S.summarize(tool_ctx)


# ----------------------------------------------------------------------------------------- vision
class _FakeVisionLLM:
    def __init__(self, fail: bool = False):
        self.calls: list[list[dict]] = []
        self.fail = fail

    async def chat(self, messages, **kw):
        from app.llm.client import ChatResult, LLMError

        self.calls.append(messages)
        if self.fail:
            raise LLMError("Test: HTTP 400: image input is not supported by this model", 400)
        return ChatResult(content="A beige rectangle on a white background.")

    async def json(self, messages, schema, **kw):
        self.calls.append(messages)
        if "objects" in schema.get("properties", {}):
            return {"objects": [{"label": "button", "box": [100, 200, 300, 400]},
                                {"label": "bad", "box": [1, 2]}]}
        return {"tables": [{"title": "Invoice", "columns": ["item", "amount"], "rows": [["Widget", 10.5]]}]}


async def test_vision_plumbing(tool_ctx, monkeypatch):
    from PIL import Image, ImageDraw

    from app.tools.builtin import _office as O
    from app.tools.builtin import vision as V

    img_path = tool_ctx.workspace.resolve("uploads/tests/shot.png", for_write=True)
    im = Image.new("RGB", (1000, 500), "white")
    ImageDraw.Draw(im).rectangle((100, 100, 300, 200), fill=(184, 155, 106))
    im.save(img_path)
    im2 = im.copy()
    ImageDraw.Draw(im2).rectangle((600, 300, 700, 400), fill=(18, 18, 17))
    im2.save(tool_ctx.workspace.resolve("uploads/tests/shot2.png", for_write=True))

    plain = await V.describe(tool_ctx, path="uploads/tests/shot.png", detail="brief")  # real MockLLM plumbing
    assert plain["description"].startswith("(mock)")

    fake = _FakeVisionLLM()

    async def fake_llm(purpose: str = "utility"):
        return fake

    monkeypatch.setattr(tool_ctx, "llm", fake_llm)
    d = await V.describe(tool_ctx, path="uploads/tests/shot.png", question="What color is the box?")
    assert d["description"] == "A beige rectangle on a white background."
    parts = fake.calls[-1][-1]["content"]
    assert parts[0]["type"] == "text" and "What color is the box?" in parts[0]["text"]
    assert parts[1]["type"] == "image_url" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")

    det = await V.detect(tool_ctx, path="uploads/tests/shot.png", targets="buttons")
    assert det["image_size"] == [1000, 500] and det["count"] == 1
    assert det["objects"][0]["box"] == [100, 100, 300, 200] and det["objects"][0]["center"] == [200, 150]

    tab = _c(await V.extract_table(tool_ctx, path="uploads/tests/shot.png", save_as="output/tests/vision-table.xlsx"))
    assert tab["tables_found"] == 1 and tab["path"].endswith(".xlsx")

    cmp = await V.compare(tool_ctx, path_a="uploads/tests/shot.png", path_b="uploads/tests/shot2.png")
    assert len(fake.calls[-1][-1]["content"]) == 3 and 0 < cmp["pixels_changed_pct"] < 5

    crop = _c(await V.crop(tool_ctx, path="uploads/tests/shot.png", box=[0.1, 0.2, 0.3, 0.4], scale=2))
    assert crop["box"] == [100, 100, 300, 200] and crop["size"] == [400, 200]
    with Image.open(tool_ctx.workspace.resolve(crop["path"])) as c_im:
        assert c_im.size == (400, 200) and c_im.getpixel((10, 10)) == (184, 155, 106)
    with pytest.raises(ToolError):
        await V.crop(tool_ctx, path="uploads/tests/shot.png", box=[5000, 5000, 6000, 6000])

    n_calls = len(fake.calls)
    pdf = tool_ctx.workspace.resolve("uploads/tests/scan.pdf", for_write=True)
    O.markdown_to_pdf("Invoice INV-7 total due 120 EUR on receipt of goods.", pdf, title="Invoice")
    ocr = await V.ocr(tool_ctx, path="uploads/tests/scan.pdf")
    assert ocr["methods"]["text_layer"] == 1 and "INV-7" in ocr["text"] and len(fake.calls) == n_calls
    ocr_img = await V.ocr(tool_ctx, path="uploads/tests/shot.png")
    assert ocr_img["text"] and len(fake.calls) == n_calls + 1
    page = await V.describe(tool_ctx, path="uploads/tests/scan.pdf", page=1)
    assert page["page"] == 1

    failing = _FakeVisionLLM(fail=True)

    async def failing_llm(purpose: str = "utility"):
        return failing

    monkeypatch.setattr(tool_ctx, "llm", failing_llm)
    with pytest.raises(ToolError, match="vision-capable"):
        await V.describe(tool_ctx, path="uploads/tests/shot.png")
    with pytest.raises(ToolError):
        await V.describe(tool_ctx, path="uploads/tests/scan.pdf", page=9)


# ----------------------------------------------------------------------------------------- audio
async def test_audio_tools_with_mocked_endpoint(tool_ctx, monkeypatch):
    from app.tools.builtin import audio as AU

    rec = tool_ctx.workspace.resolve("uploads/tests/meeting.mp3", for_write=True)
    rec.write_bytes(b"ID3" + b"\x00" * 2048)
    with pytest.raises(ToolError, match="speech-to-text"):  # the mock provider has no /audio endpoints
        await AU.transcribe(tool_ctx, path="uploads/tests/meeting.mp3")

    class _Client:
        def __init__(self, kind, base_url):
            self.kind, self.base_url, self.extra = kind, base_url, {}

    assert not AU._supports_audio(_Client("openrouter", "https://openrouter.ai/api/v1"))
    assert AU._supports_audio(_Client("openai", "https://api.openai.com/v1"))

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer sk-test"
        path = request.url.path
        if path == "/v1/audio/transcriptions":
            body = request.read()
            assert b'name="file"' in body and b"whisper-1" in body
            data = {"text": "Ana will send the deck by Friday.", "language": "english", "duration": 12.5}
            if b"verbose_json" in body:
                data["segments"] = [{"start": 0, "end": 5, "text": " Ana will send"},
                                    {"start": 65, "end": 70, "text": " the deck by Friday."}]
            return httpx.Response(200, json=data)
        if path == "/v1/audio/translations":
            return httpx.Response(200, json={"text": "Hello everyone."})
        if path == "/v1/audio/speech":
            payload = json.loads(request.content)
            assert payload["voice"] == "nova" and payload["model"] == "tts-1"
            return httpx.Response(200, content=b"\xff\xfb" * 100, headers={"content-type": "audio/mpeg"})
        return httpx.Response(404, json={"error": {"message": "no such route"}})

    use_transport(monkeypatch, handler)
    tool_ctx.extra["audio_provider"] = {"base_url": "https://audio.example/v1", "api_key": "sk-test",
                                        "name": "Test Audio"}
    tr = await AU.transcribe(tool_ctx, path="uploads/tests/meeting.mp3", language="en", timestamps=True)
    assert tr["text"] == "[0:00] Ana will send\n[1:05] the deck by Friday."
    assert tr["language"] == "english" and tr["duration"] == "0:12" and tr["segments"] == 2
    saved = _c(await AU.transcribe(tool_ctx, path="uploads/tests/meeting.mp3", save_as="output/tests/transcript.md"))
    assert saved["path"] == "output/tests/transcript.md"
    assert "Ana will send" in tool_ctx.workspace.resolve(saved["path"]).read_text(encoding="utf-8")

    tl = await AU.translate(tool_ctx, path="uploads/tests/meeting.mp3")
    assert tl["text"] == "Hello everyone." and tl["target_language"] == "en"
    sm = await AU.summarize(tool_ctx, path="uploads/tests/meeting.mp3", include_transcript=True)
    assert sm["notes"] and sm["transcript"].startswith("Ana will send")

    sp = _c(await AU.speak(tool_ctx, text="Quarterly results are in.", voice="nova"))
    assert sp["path"].endswith(".mp3") and tool_ctx.workspace.resolve(sp["path"]).stat().st_size == 200
    with pytest.raises(ToolError, match="too long"):
        await AU.speak(tool_ctx, text="word " * 2000, format="wav")

    tool_ctx.extra["audio_provider"]["base_url"] = "https://audio.example/v2"
    with pytest.raises(ToolError, match="doesn't support"):
        await AU.translate(tool_ctx, path="uploads/tests/meeting.mp3")
    monkeypatch.setattr(AU, "MAX_UPLOAD", 100)
    with pytest.raises(ToolError, match="25 MB"):
        await AU.transcribe(tool_ctx, path="uploads/tests/meeting.mp3")
