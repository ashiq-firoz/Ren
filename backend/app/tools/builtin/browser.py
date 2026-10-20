"""Browser group: navigate and read websites, interact with pages (click, type, forms), screenshots, tables,
links and downloads.

Interactive mode uses Playwright's headless Chromium (optional dependency: `pip install 'ren-backend[browser]'`
then `playwright install chromium`). Without it the group runs in *static mode* (httpx + BeautifulSoup): pages
can be opened, read and analysed, but nothing can be clicked or typed. Every navigation passes the SSRF guard.
"""
from __future__ import annotations

import mimetypes
import re
from typing import Any, Literal

from app.tools import net
from app.tools.base import Risk, ToolContext, ToolError, ToolGroup, ToolResult
from app.tools.builtin import _browser as B
from app.tools.builtin import _office as O

browser = ToolGroup(
    "browser", "Web Browser",
    "Browse websites like a person: open and read pages, click, type, fill and submit forms, take screenshots, "
    "extract tables and links, and download files. Uses headless Chromium (Playwright) when installed; otherwise a "
    "static reader that can open and read pages but not interact with them.",
    prefix="browser", icon="globe", category="core",
)

By = Literal["auto", "ref", "css", "text", "xpath"]
_SENSITIVE = re.compile(r"pass(word)?|pwd|secret|token|card|cvv|cvc|ssn|iban|pin\b", re.I)


# ----------------------------------------------------------------------------------------- helpers
def _pw_error(e: Exception, what: str) -> ToolError:
    msg = str(e).splitlines()[0] if str(e) else e.__class__.__name__
    if "ERR_BLOCKED_BY_CLIENT" in msg:
        return ToolError(f"{what}: blocked by the workspace network policy (private/internal address).")
    if "Timeout" in e.__class__.__name__ or "timeout" in msg.lower():
        return ToolError(f"{what}: timed out. The page may be slow — try browser_wait or retry.")
    return ToolError(f"{what}: {msg[:300]}")


def _interactive_mode(ctx: ToolContext) -> bool:
    return B.current_mode(ctx) == "interactive" and B.playwright_available()


async def _page_summary(page: Any, *, tabs: Any = None, status: int | None = None, excerpt_chars: int = 1500,
                        note: str | None = None) -> dict:
    html = await page.content()
    r = B.readable(html, page.url, max_chars=excerpt_chars)
    out: dict[str, Any] = {"mode": "interactive", "url": page.url, "title": (await page.title()) or r["title"]}
    if status is not None:
        out["status"] = status
    out["excerpt"] = r["text"]
    out["links_count"] = r["links_count"]
    if tabs is not None:
        out["tabs"] = len(tabs.pages)
        if tabs.notes:
            out["notes"] = tabs.notes[-3:]
            tabs.notes.clear()
    if note:
        out["note"] = note
    return out


def _static_summary(page: B.StaticPage, *, excerpt_chars: int = 1500, note: str | None = None) -> dict:
    r = B.readable(page.html, page.final_url, max_chars=excerpt_chars)
    out = {"mode": "static", "url": page.final_url, "title": page.title or r["title"], "status": page.status,
           "excerpt": r["text"], "links_count": r["links_count"],
           "note": note or "Static mode: JavaScript is not executed and pages can't be clicked or typed into."}
    return out


async def _interactive(ctx: ToolContext) -> tuple[Any, Any]:
    tabs, page = await B.interactive_page(ctx)
    if not page.url or page.url == "about:blank":
        raise ToolError("No page is open. Use browser_open first.")
    return tabs, page


async def _current_html(ctx: ToolContext) -> tuple[str, str, str]:
    """(html, url, title) of the current page in whichever mode is active."""
    if _interactive_mode(ctx):
        _, page = await B.interactive_page(ctx)
        if page.url and page.url != "about:blank":
            return await page.content(), page.url, await page.title()
    cur = B.static_session(ctx).current
    if cur is None:
        raise ToolError("No page is open. Use browser_open first.")
    return cur.html, cur.final_url, cur.title


# ----------------------------------------------------------------------------------------- navigation
@browser.tool(name="open", risk=Risk.LOW, scope="read", timeout=90)
async def open_(ctx: ToolContext, url: str, new_tab: bool = False,
                wait_until: Literal["domcontentloaded", "load", "networkidle"] = "domcontentloaded") -> dict:
    """Open a web page and return its title, URL and a readable excerpt.

    Args:
        url: Public http(s) URL to open.
        new_tab: Open in a new tab instead of the current one (interactive mode, max 5 tabs).
        wait_until: When navigation counts as finished (interactive mode).
    """
    url = B.normalize_url(url)
    await net.assert_public_url(url)
    note = None
    if B.playwright_available():
        try:
            tabs = await B.manager.session(ctx)
            if new_tab:
                if len(tabs.pages) >= B.MAX_TABS:
                    raise ToolError(f"Tab limit reached ({B.MAX_TABS}). Close a tab with browser_close_tab first.")
                page = await tabs.context.new_page()
                tabs.active = page
            else:
                page = await tabs.page()
            try:
                resp = await page.goto(url, wait_until=wait_until, timeout=45000)
            except Exception as e:  # noqa: BLE001
                raise _pw_error(e, f"Could not open {url}") from e
            await B.settle(page)
            B.set_mode(ctx, "interactive")
            return await _page_summary(page, tabs=tabs, status=resp.status if resp is not None else None)
        except B.BrowserUnavailable as e:
            note = (f"Interactive browser unavailable ({e}); opened in static mode — run `playwright install "
                    "chromium` to enable clicking and typing.")
    page = await B.static_open(ctx, url)
    B.set_mode(ctx, "static")
    return _static_summary(page, note=note)


async def _history(ctx: ToolContext, direction: str) -> dict:
    if _interactive_mode(ctx):
        tabs, page = await _interactive(ctx)
        try:
            if direction == "back":
                resp = await page.go_back(timeout=30000)
            elif direction == "forward":
                resp = await page.go_forward(timeout=30000)
            else:
                resp = await page.reload(timeout=45000)
        except Exception as e:  # noqa: BLE001
            raise _pw_error(e, f"Could not go {direction}") from e
        if resp is None and direction != "reload":
            raise ToolError(f"There is no page to go {direction} to.")
        await B.settle(page)
        return await _page_summary(page, tabs=tabs, status=resp.status if resp is not None else None)
    s = B.static_session(ctx)
    if s.current is None:
        raise ToolError("No page is open. Use browser_open first.")
    if direction == "back":
        if s.index <= 0:
            raise ToolError("There is no page to go back to.")
        s.index -= 1
    elif direction == "forward":
        if s.index >= len(s.history) - 1:
            raise ToolError("There is no page to go forward to.")
        s.index += 1
    else:
        s.history[s.index] = await B.static_open(ctx, s.current.url, push=False)
    return _static_summary(s.current)


@browser.tool(risk=Risk.LOW, scope="read", timeout=60)
async def back(ctx: ToolContext) -> dict:
    """Go back to the previous page in this tab's history."""
    return await _history(ctx, "back")


@browser.tool(risk=Risk.LOW, scope="read", timeout=60)
async def forward(ctx: ToolContext) -> dict:
    """Go forward in this tab's history."""
    return await _history(ctx, "forward")


@browser.tool(risk=Risk.LOW, scope="read", timeout=60)
async def reload(ctx: ToolContext) -> dict:
    """Reload the current page."""
    return await _history(ctx, "reload")


# ----------------------------------------------------------------------------------------- reading
@browser.tool(risk=Risk.LOW, scope="read", timeout=60)
async def get_page(ctx: ToolContext, max_chars: int = 20000, include_links: bool = False) -> dict:
    """Readable text of the current page (main content; menus, ads and footers removed).

    Args:
        max_chars: Maximum characters to return.
        include_links: Also return up to 50 links.
    """
    html, url, title = await _current_html(ctx)
    r = B.readable(html, url, max_chars=max(500, min(int(max_chars or 20000), 100000)))
    out: dict[str, Any] = {"url": url, "title": title or r["title"], "chars": r["chars"], "truncated": r["truncated"],
                           "content": r["text"]}
    if include_links:
        out["links"], out["links_total"] = B.extract_links(html, url, limit=50)
    return out


@browser.tool(risk=Risk.LOW, scope="read", timeout=60)
async def get_dom(ctx: ToolContext, max_chars: int = 15000) -> dict:
    """Trimmed HTML of the current page with interactive elements tagged `data-ren-ref="eN"`, plus a list of those
    elements. Use the refs (e.g. "e12") as targets for click/type/select.

    Args:
        max_chars: Maximum characters of HTML to return.
    """
    max_chars = max(1000, min(int(max_chars or 15000), 60000))
    if _interactive_mode(ctx):
        _, page = await _interactive(ctx)
        data = await page.evaluate(B.DOM_JS, [max_chars, 150])
        return {"url": page.url, "title": await page.title(), **data}
    html, url, title = await _current_html(ctx)
    return {"url": url, "title": title, **B.static_dom(html, url, max_chars=max_chars)}


@browser.tool(risk=Risk.LOW, scope="read", timeout=60)
async def find(ctx: ToolContext, text: str, limit: int = 20) -> dict:
    """Find elements on the current page whose visible text contains `text`; returns refs usable as click/type
    targets (and viewport coordinates in interactive mode).

    Args:
        text: Text to look for (case-insensitive).
        limit: Maximum matches.
    """
    if not (text or "").strip():
        raise ToolError("Text to find is required.")
    limit = max(1, min(int(limit or 20), 100))
    if _interactive_mode(ctx):
        _, page = await _interactive(ctx)
        matches = await page.evaluate(B.FIND_JS, [text, limit])
        return {"url": page.url, "count": len(matches), "matches": matches}
    html, url, _ = await _current_html(ctx)
    matches = B.find_text(html, text, limit=limit)
    return {"url": url, "count": len(matches), "matches": matches,
            "note": "Static mode: refs are informational (elements can't be clicked without Playwright)."}


@browser.tool(risk=Risk.LOW, scope="read", timeout=60)
async def extract_links(ctx: ToolContext, contains: str | None = None, same_domain: bool = False,
                        limit: int = 100) -> dict:
    """List links on the current page (text + absolute URL), optionally filtered.

    Args:
        contains: Only links whose URL or text contains this string.
        same_domain: Only links to the same website.
        limit: Maximum links to return.
    """
    html, url, _ = await _current_html(ctx)
    links, total = B.extract_links(html, url, contains=contains, same_domain=same_domain,
                                   limit=max(1, min(int(limit or 100), 500)))
    return {"url": url, "total": total, "returned": len(links), "links": links}


@browser.tool(risk=Risk.LOW, scope="read", timeout=90,
              risk_fn=lambda a: Risk.MEDIUM if a.get("save_as") else Risk.LOW)
async def extract_table(ctx: ToolContext, index: int | None = None, max_rows: int = 100,
                        save_as: str | None = None) -> dict | ToolResult:
    """Extract HTML tables from the current page as rows. Without `index`, lists all tables with a preview;
    with `index`, returns that table's rows. Optionally saves the table to the workspace.

    Args:
        index: Which table (0-based) to return in full.
        max_rows: Maximum rows to return.
        save_as: Optional output file (.csv or .xlsx), e.g. "output/prices.xlsx".
    """
    html, url, _ = await _current_html(ctx)
    tables = B.extract_tables(html, max_rows=5000)
    if not tables:
        return {"url": url, "count": 0, "tables": [], "hint": "No HTML tables found. Try browser_get_page."}
    if index is None and not save_as:
        return {"url": url, "count": len(tables), "tables": [
            {"index": t["index"], "caption": t["caption"], "rows": t["n_rows"], "cols": t["n_cols"],
             "header": t["header"], "preview": t["rows"][:3]} for t in tables[:30]]}
    idx = 0 if index is None else int(index)
    if idx < 0 or idx >= len(tables):
        raise ToolError(f"Table index {idx} is out of range (0–{len(tables) - 1}).")
    t = tables[idx]
    n = max(1, min(int(max_rows or 100), 2000))
    out = {"url": url, "index": idx, "caption": t["caption"], "header": t["header"], "rows": t["rows"][:n],
           "total_rows": t["n_rows"], "truncated": t["n_rows"] > n}
    if save_as:
        ext = ".xlsx" if str(save_as).lower().endswith(".xlsx") else ".csv"
        target = O.ws_output(ctx, save_as, default_name=t["caption"] or "table", ext=ext)
        rows = [[O.coerce_scalar(c) for c in r] for r in t["rows"]]
        if ext == ".xlsx":
            O.write_xlsx(target, {(t["caption"] or "Table")[:31]: (t["header"] or None, rows)})
        else:
            O.write_csv(target, t["header"] or None, rows)
        out.pop("rows")
        return await O.produced(ctx, target, out)
    return out


# ----------------------------------------------------------------------------------------- interaction
@browser.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def click(ctx: ToolContext, target: str, by: By = "auto", double: bool = False) -> dict:
    """Click an element on the current page.

    Args:
        target: Element ref from browser_get_dom/browser_find (e.g. "e12"), a CSS selector, or visible text
            such as "Sign in".
        by: How to interpret target ("auto" guesses).
        double: Double-click instead of a single click.
    """
    tabs, page = await _interactive(ctx)
    loc = await B.locate(page, target, by)
    try:
        if double:
            await loc.dblclick(timeout=10000)
        else:
            await loc.click(timeout=10000)
    except Exception as e:  # noqa: BLE001
        raise _pw_error(e, f"Could not click '{target}'") from e
    await B.settle(page)
    page = await tabs.page()
    return await _page_summary(page, tabs=tabs, excerpt_chars=800)


@browser.tool(name="type", risk=Risk.MEDIUM, scope="write", timeout=60, content_fields=("text",))
async def type_(ctx: ToolContext, target: str, text: str, by: By = "auto", clear: bool = True,
                submit: bool = False) -> dict:
    """Type text into an input, textarea or editable element.

    Args:
        target: Element ref (e.g. "e7"), CSS selector, or the field's label/placeholder text.
        text: Text to type.
        by: How to interpret target.
        clear: Replace the existing value (otherwise append keystrokes).
        submit: Press Enter afterwards (e.g. to run a search).
    """
    tabs, page = await _interactive(ctx)
    loc = await B.locate(page, target, by)
    try:
        if clear:
            await loc.fill(text, timeout=10000)
        else:
            await loc.press_sequentially(text, delay=15, timeout=20000)
        if submit:
            await loc.press("Enter")
            await B.settle(page)
    except Exception as e:  # noqa: BLE001
        raise _pw_error(e, f"Could not type into '{target}'") from e
    if submit:
        return await _page_summary(page, tabs=tabs, excerpt_chars=800)
    return {"ok": True, "url": page.url, "typed_chars": len(text)}


@browser.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def select(ctx: ToolContext, target: str, value: str, by: By = "auto") -> dict:
    """Choose an option in a <select> dropdown (by visible label or value).

    Args:
        target: The select element (ref, CSS selector or label text).
        value: Option label or value to select.
        by: How to interpret target.
    """
    _, page = await _interactive(ctx)
    loc = await B.locate(page, target, by)
    try:
        try:
            chosen = await loc.select_option(label=value, timeout=5000)
        except Exception:  # noqa: BLE001
            chosen = await loc.select_option(value=value, timeout=5000)
    except Exception as e:  # noqa: BLE001
        raise _pw_error(e, f"Could not select '{value}'") from e
    await B.settle(page)
    return {"ok": True, "url": page.url, "selected": chosen}


@browser.tool(risk=Risk.LOW, scope="read", timeout=60)
async def hover(ctx: ToolContext, target: str, by: By = "auto") -> dict:
    """Move the mouse over an element (reveals menus and tooltips).

    Args:
        target: Element ref, CSS selector or visible text.
        by: How to interpret target.
    """
    _, page = await _interactive(ctx)
    loc = await B.locate(page, target, by)
    try:
        await loc.hover(timeout=10000)
    except Exception as e:  # noqa: BLE001
        raise _pw_error(e, f"Could not hover '{target}'") from e
    await page.wait_for_timeout(300)
    return {"ok": True, "url": page.url}


@browser.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def scroll(ctx: ToolContext, direction: Literal["down", "up", "top", "bottom"] = "down", amount: float = 1.0,
                 target: str | None = None) -> dict:
    """Scroll the page (by screens) or scroll a specific element into view.

    Args:
        direction: Scroll direction, or jump to top/bottom.
        amount: Number of screen heights to scroll (for up/down).
        target: Element to scroll into view instead (ref, CSS selector or text).
    """
    _, page = await _interactive(ctx)
    try:
        if target:
            loc = await B.locate(page, target)
            await loc.scroll_into_view_if_needed(timeout=10000)
        elif direction == "top":
            await page.evaluate("() => window.scrollTo(0, 0)")
        elif direction == "bottom":
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        else:
            h = (page.viewport_size or B.VIEWPORT)["height"]
            dy = int(h * max(0.1, min(float(amount or 1), 20)) * (1 if direction == "down" else -1))
            await page.mouse.wheel(0, dy)
        await page.wait_for_timeout(400)
        pos = await page.evaluate("() => ({y: Math.round(window.scrollY), height: document.body.scrollHeight, "
                                  "viewport: window.innerHeight})")
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _pw_error(e, "Could not scroll") from e
    return {"ok": True, "url": page.url, "scroll_y": pos.get("y"), "page_height": pos.get("height"),
            "viewport_height": pos.get("viewport")}


@browser.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def press(ctx: ToolContext, key: str, target: str | None = None) -> dict:
    """Press a key or shortcut, e.g. "Enter", "Escape", "Tab", "ArrowDown", "Control+A".

    Args:
        key: Key name or combination (Playwright key syntax).
        target: Optional element to focus first (ref, CSS selector or text).
    """
    tabs, page = await _interactive(ctx)
    try:
        if target:
            loc = await B.locate(page, target)
            await loc.press(key, timeout=10000)
        else:
            await page.keyboard.press(key)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _pw_error(e, f"Could not press '{key}'") from e
    await B.settle(page, 2500)
    return {"ok": True, "url": page.url, "title": await page.title()}


@browser.tool(risk=Risk.LOW, scope="write", timeout=60)
async def screenshot(ctx: ToolContext, full_page: bool = False, target: str | None = None) -> ToolResult:
    """Take a PNG screenshot of the current page (or one element); saved under output/screenshots/.
    Use vision_describe on the returned path to look at it.

    Args:
        full_page: Capture the whole scrollable page instead of the viewport.
        target: Capture only this element (ref, CSS selector or text).
    """
    _, page = await _interactive(ctx)
    path, art = await B.save_screenshot(ctx, page, full_page=full_page, target=target)
    vs = page.viewport_size or B.VIEWPORT
    return ToolResult(content={"path": O.vpath(ctx, path), "url": page.url, "viewport": vs},
                      artifacts=[art], display=f"Screenshot of {page.url}"[:120])


@browser.tool(risk=Risk.LOW, scope="read", timeout=60)
async def wait(ctx: ToolContext, selector: str | None = None, text: str | None = None, timeout: float = 10,
               state: Literal["visible", "attached", "hidden", "detached"] = "visible") -> dict:
    """Wait until an element or text appears (or disappears), or just pause.

    Args:
        selector: CSS selector to wait for.
        text: Visible text to wait for.
        timeout: Maximum seconds to wait (max 60).
        state: Element state to wait for.
    """
    secs = max(0.1, min(float(timeout or 10), 60))
    if not _interactive_mode(ctx):
        html, url, _ = await _current_html(ctx)
        found = None
        if text:
            found = text.lower() in B.readable(html, url)["text"].lower()
        elif selector:
            try:
                found = bool(B.make_soup(html).select_one(selector))
            except Exception:  # noqa: BLE001
                found = None
        return {"mode": "static", "found": found,
                "note": "Static mode: the page was fetched once; nothing to wait for."}
    _, page = await _interactive(ctx)
    try:
        if selector:
            await page.wait_for_selector(selector, state=state, timeout=secs * 1000)
        elif text:
            await page.get_by_text(text).first.wait_for(state=state, timeout=secs * 1000)
        else:
            await page.wait_for_timeout(secs * 1000)
    except Exception as e:  # noqa: BLE001
        raise _pw_error(e, "Wait failed") from e
    return {"ok": True, "url": page.url, "title": await page.title()}


# ----------------------------------------------------------------------------------------- tabs
@browser.tool(risk=Risk.LOW, scope="read", timeout=90)
async def new_tab(ctx: ToolContext, url: str | None = None) -> dict:
    """Open a new browser tab (max 5), optionally navigating to a URL.

    Args:
        url: Public http(s) URL to open in the new tab.
    """
    if url:
        return await open_(ctx, url=url, new_tab=True)
    tabs, _ = await B.interactive_page(ctx)
    if len(tabs.pages) >= B.MAX_TABS:
        raise ToolError(f"Tab limit reached ({B.MAX_TABS}). Close a tab first.")
    page = await tabs.context.new_page()
    tabs.active = page
    B.set_mode(ctx, "interactive")
    return {"ok": True, "tabs": len(tabs.pages), "active": len(tabs.pages) - 1}


async def _tab_list(tabs: Any) -> list[dict]:
    out = []
    for i, p in enumerate(tabs.pages):
        try:
            title = await p.title()
        except Exception:  # noqa: BLE001
            title = ""
        out.append({"index": i, "url": p.url, "title": title, "active": p is tabs.active})
    return out


@browser.tool(risk=Risk.LOW, scope="read", timeout=30)
async def close_tab(ctx: ToolContext, index: int | None = None) -> dict:
    """Close a tab (default: the active one).

    Args:
        index: Tab index from browser_tabs.
    """
    tabs, page = await B.interactive_page(ctx)
    pages = tabs.pages
    target = page if index is None else (pages[int(index)] if 0 <= int(index) < len(pages) else None)
    if target is None:
        raise ToolError(f"No tab with index {index}.")
    await target.close()
    remaining = tabs.pages
    tabs.active = remaining[-1] if remaining else None
    return {"ok": True, "tabs": await _tab_list(tabs)}


@browser.tool(risk=Risk.LOW, scope="read", timeout=30)
async def tabs(ctx: ToolContext, activate: int | None = None) -> dict:
    """List open tabs, or switch to one.

    Args:
        activate: Index of the tab to make active.
    """
    if not _interactive_mode(ctx) and not B.playwright_available():
        cur = B.static_session(ctx).current
        return {"mode": "static", "tabs": [{"index": 0, "url": cur.final_url, "title": cur.title, "active": True}]
                if cur else []}
    t, _ = await B.interactive_page(ctx)
    if activate is not None:
        pages = t.pages
        if not 0 <= int(activate) < len(pages):
            raise ToolError(f"No tab with index {activate}.")
        t.active = pages[int(activate)]
        await t.active.bring_to_front()
    return {"mode": "interactive", "tabs": await _tab_list(t)}


# ----------------------------------------------------------------------------------------- forms
def _truthy(v: Any) -> bool:
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on", "checked", "y")


def _css_str(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


async def _fill_one(scope: Any, key: str, value: Any) -> tuple[str, Any]:
    cands = []
    if re.match(r"^e\d+$", key):
        cands.append(scope.locator(f'[data-ren-ref="{key}"]'))
    if B.looks_like_css(key):
        cands.append(scope.locator(key))
    cands += [scope.get_by_label(key), scope.get_by_placeholder(key), scope.locator(f'[name="{_css_str(key)}"]'),
              scope.locator(f'[id="{_css_str(key)}"]')]
    loc = None
    for c in cands:
        try:
            if await c.count():
                loc = c.first
                break
        except Exception:  # noqa: BLE001
            continue
    if loc is None:
        return "not found", None
    try:
        tag = await loc.evaluate("e => e.tagName.toLowerCase()")
        typ = ((await loc.get_attribute("type")) or "").lower()
        if tag == "select":
            try:
                await loc.select_option(label=str(value), timeout=5000)
            except Exception:  # noqa: BLE001
                await loc.select_option(value=str(value), timeout=5000)
        elif typ in ("checkbox", "radio"):
            if _truthy(value):
                await loc.check(timeout=5000)
            else:
                await loc.uncheck(timeout=5000)
        elif typ == "file":
            return "skipped (file uploads are not supported)", loc
        else:
            await loc.fill("" if value is None else str(value), timeout=10000)
        return "ok", loc
    except Exception as e:  # noqa: BLE001
        return f"error: {str(e).splitlines()[0][:120]}", loc


async def _fill(page: Any, fields: dict[str, Any], form: str | None) -> tuple[dict[str, str], Any]:
    scope = page.locator(form).first if form else page
    results: dict[str, str] = {}
    first_loc = None
    for k, v in (fields or {}).items():
        status, loc = await _fill_one(scope, str(k), v)
        results[str(k)] = status
        if first_loc is None and status == "ok":
            first_loc = loc
    return results, first_loc


def _mask(k: str, v: Any) -> str:
    s = "" if v is None else str(v)
    if _SENSITIVE.search(k):
        return "••••" if s else ""
    s = s.replace("|", "\\|").replace("\n", " ")
    return s[:200] + ("…" if len(s) > 200 else "")


def _form_preview(title: str):
    def render(args: dict) -> str:
        lines = [f"**{title}**"]
        if args.get("form"):
            lines.append(f"- Form: `{args['form']}`")
        if args.get("submit"):
            lines.append(f"- Submit control: `{args['submit']}`")
        fields = args.get("fields") or {}
        if fields:
            lines += ["", "| Field | Value |", "|---|---|"]
            lines += [f"| {k} | {_mask(str(k), v)} |" for k, v in fields.items()]
        else:
            lines.append("- Submits the form exactly as it is currently filled in the browser.")
        return "\n".join(lines)

    return render


@browser.tool(risk=Risk.MEDIUM, scope="write", timeout=90, content_fields=("fields",),
              preview=_form_preview("Fill a web form (not submitted)"))
async def fill_form(ctx: ToolContext, fields: dict[str, Any], form: str | None = None) -> dict:
    """Fill several form fields at once without submitting. Fields are matched by ref, CSS selector, label,
    placeholder, name or id. Checkboxes take true/false; selects take the option label or value.

    Args:
        fields: Map of field → value, e.g. {"Email": "ana@acme.com", "Company": "Acme", "Subscribe": false}.
        form: Optional CSS selector of the form to fill within.
    """
    if not fields:
        raise ToolError("Provide at least one field to fill.")
    _, page = await _interactive(ctx)
    results, _ = await _fill(page, fields, form)
    missing = [k for k, v in results.items() if v != "ok"]
    out: dict[str, Any] = {"url": page.url, "filled": sum(1 for v in results.values() if v == "ok"), "fields": results}
    if missing:
        out["hint"] = "Some fields were not filled — use browser_get_dom to find their refs."
    return out


@browser.tool(risk=Risk.HIGH, scope="write", timeout=90, content_fields=("fields",),
              preview=_form_preview("Submit a web form"))
async def submit_form(ctx: ToolContext, fields: dict[str, Any] | None = None, form: str | None = None,
                      submit: str | None = None) -> dict:
    """Submit a form on the current page (optionally filling fields first). This sends data to the website.

    Args:
        fields: Optional map of field → value to fill before submitting.
        form: CSS selector of the form (needed when the page has several forms and no fields are given).
        submit: The submit button to click (ref, CSS selector or text); default submits the form directly.
    """
    tabs, page = await _interactive(ctx)
    results, first_loc = await _fill(page, fields or {}, form)
    try:
        if submit:
            loc = await B.locate(page, submit)
            await loc.click(timeout=10000)
        else:
            if form:
                form_loc = page.locator(form).first
            elif first_loc is not None:
                form_loc = first_loc.locator("xpath=ancestor::form[1]")
            else:
                form_loc = page.locator("form")
                n = await form_loc.count()
                if n == 0:
                    raise ToolError("No form found on the page. Pass `submit` (the button to click) instead.")
                if n > 1:
                    raise ToolError(f"The page has {n} forms — pass `form` (CSS selector) or `submit`.")
            if await form_loc.count() == 0:
                raise ToolError("Could not find the form to submit. Pass `form` or `submit`.")
            await form_loc.first.evaluate("f => f.requestSubmit ? f.requestSubmit() : f.submit()")
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _pw_error(e, "Could not submit the form") from e
    await B.settle(page, 8000)
    page = await tabs.page()
    out = await _page_summary(page, tabs=tabs, excerpt_chars=1200)
    out["submitted"] = True
    if results:
        out["fields"] = results
    return out


# ----------------------------------------------------------------------------------------- downloads
def _download_preview(args: dict) -> str:
    lines = ["**Download a file into the workspace (downloads/)**"]
    if args.get("url"):
        lines.append(f"- URL: {args['url']}")
    if args.get("target"):
        lines.append(f"- By clicking: `{args['target']}` on the current page")
    if args.get("filename"):
        lines.append(f"- Save as: downloads/{args['filename']}")
    return "\n".join(lines)


@browser.tool(risk=Risk.HIGH, scope="write", timeout=300, preview=_download_preview)
async def download(ctx: ToolContext, url: str | None = None, target: str | None = None,
                   filename: str | None = None) -> ToolResult:
    """Download a file into the workspace `downloads/` folder — from a URL, or by clicking a link/button on the
    current page that triggers a download (interactive mode).

    Args:
        url: Public http(s) URL of the file.
        target: Element that starts the download when clicked (ref, CSS selector or text).
        filename: Optional file name to save as.
    """
    O.require_write(ctx)
    dest = O.ws_path(ctx, "downloads", for_write=False)
    if url:
        path, size, mime, final = await B.safe_download(url, dest, filename=filename)
    elif target:
        _, page = await _interactive(ctx)
        loc = await B.locate(page, target)
        try:
            async with page.expect_download(timeout=60000) as info:
                await loc.click(timeout=10000)
            dl = await info.value
        except Exception as e:  # noqa: BLE001
            raise _pw_error(e, "No download started") from e
        if not await B.manager._allowed(dl.url):
            await dl.cancel()
            raise ToolError("The download URL is blocked by the workspace network policy.")
        name = O.safe_filename(filename or dl.suggested_filename) or "download"
        path = O.unique_path(dest / name)
        await dl.save_as(str(path))
        size, final = path.stat().st_size, dl.url
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    else:
        raise ToolError("Provide a url, or a target element that triggers the download.")
    vp = O.vpath(ctx, path)
    return ToolResult(content={"path": vp, "size": O.human_size(size), "mime": mime or "application/octet-stream",
                               "source": final},
                      display=f"Downloaded {vp} ({O.human_size(size)})")
