"""Computer-use group: coordinate-based control of the agent's "computer" — the headless Playwright browser
viewport shared with the Web Browser tools. Every action returns a fresh screenshot (saved under
output/screenshots/) plus the viewport size, so the agent can look (vision_describe / vision_detect) and act.

Full desktop control (native apps, OS dialogs) requires a desktop sandbox and is not provided here."""
from __future__ import annotations

import re
from typing import Any, Literal

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup, ToolResult
from app.tools.builtin import _browser as B
from app.tools.builtin import _office as O

computer = ToolGroup(
    "computer", "Computer Use",
    "Coordinate-based control of the agent's computer — the headless browser viewport (1280×800): screenshots, "
    "mouse moves, clicks and drags, typing, key presses, hotkeys and scrolling, each returning a fresh screenshot. "
    "Shares the session with the Web Browser tools (open a page with browser_open first). Full desktop control "
    "requires a desktop sandbox.",
    prefix="computer", icon="mouse-pointer-click", category="core", requires=("playwright",),
    default_enabled=False,
)

_KEY_ALIASES = {"ctrl": "Control", "control": "Control", "cmd": "Meta", "command": "Meta", "win": "Meta",
                "super": "Meta", "meta": "Meta", "alt": "Alt", "option": "Alt", "shift": "Shift", "enter": "Enter",
                "return": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": "Space",
                "backspace": "Backspace", "del": "Delete", "delete": "Delete", "up": "ArrowUp", "down": "ArrowDown",
                "left": "ArrowLeft", "right": "ArrowRight", "pgup": "PageUp", "pageup": "PageUp",
                "pgdn": "PageDown", "pagedown": "PageDown", "home": "Home", "end": "End", "insert": "Insert"}


def _key(k: str) -> str:
    k = str(k).strip()
    low = k.lower()
    if low in _KEY_ALIASES:
        return _KEY_ALIASES[low]
    if re.fullmatch(r"f([1-9]|1[0-2])", low):
        return low.upper()
    return k


async def _page(ctx: ToolContext) -> tuple[Any, Any]:
    tabs, page = await B.interactive_page(ctx)
    B.set_mode(ctx, "interactive")
    return tabs, page


def _check_xy(page: Any, x: float, y: float) -> tuple[float, float]:
    vs = page.viewport_size or B.VIEWPORT
    if not (0 <= x < vs["width"] and 0 <= y < vs["height"]):
        raise ToolError(f"({x}, {y}) is outside the {vs['width']}×{vs['height']} viewport.")
    return float(x), float(y)


async def _snapshot(ctx: ToolContext, page: Any, action: str, *, screenshot: bool = True,
                    extra: dict | None = None) -> ToolResult:
    try:
        await page.wait_for_timeout(350)
        await page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:  # noqa: BLE001
        pass
    vs = page.viewport_size or B.VIEWPORT
    try:
        title = await page.title()
    except Exception:  # noqa: BLE001
        title = ""
    content: dict[str, Any] = {"action": action, "url": page.url, "title": title,
                               "viewport": {"width": vs["width"], "height": vs["height"]}}
    if extra:
        content.update(extra)
    arts = []
    if screenshot:
        path, art = await B.save_screenshot(ctx, page, prefix="computer")
        content["screenshot"] = O.vpath(ctx, path)
        arts.append(art)
    return ToolResult(content=content, artifacts=arts, display=action)


async def _with_modifiers(page: Any, modifiers: list[str] | None, fn) -> None:
    mods = [_key(m) for m in (modifiers or [])]
    for m in mods:
        await page.keyboard.down(m)
    try:
        await fn()
    finally:
        for m in reversed(mods):
            await page.keyboard.up(m)


def _err(e: Exception, what: str) -> ToolError:
    return ToolError(f"{what} failed: {str(e).splitlines()[0][:200] if str(e) else e.__class__.__name__}")


@computer.tool(risk=Risk.LOW, scope="write", timeout=120)
async def screenshot(ctx: ToolContext, describe: bool = False, include_elements: bool = True,
                     model: str | None = None) -> ToolResult:
    """Capture the current screen (browser viewport). Also lists visible clickable elements with their centre
    coordinates, and — with describe=true — a vision-model description of the screen.

    Args:
        describe: Ask the vision model to describe what's on screen.
        include_elements: Include visible interactive elements with (x, y) centres.
        model: Optional vision-capable model id for describe.
    """
    _, page = await _page(ctx)
    extra: dict[str, Any] = {}
    if include_elements:
        try:
            extra["elements"] = await page.evaluate(B.ELEMENTS_JS, 40)
        except Exception:  # noqa: BLE001
            extra["elements"] = []
    res = await _snapshot(ctx, page, "Screenshot", extra=extra)
    if describe:
        from app.tools.builtin.vision import describe_image

        path = O.ws_path(ctx, res.content["screenshot"])
        res.content["description"] = await describe_image(
            ctx, path, "This is a screenshot of a browser viewport. Describe the page and list the main interactive "
                       "elements with their approximate pixel coordinates (x, y from the top-left).", model=model)
    return res


@computer.tool(risk=Risk.LOW, scope="write", timeout=60)
async def mouse_move(ctx: ToolContext, x: float, y: float, screenshot: bool = True) -> ToolResult:
    """Move the mouse to (x, y) in the viewport (hover).

    Args:
        x: Horizontal pixel coordinate from the left.
        y: Vertical pixel coordinate from the top.
        screenshot: Return a fresh screenshot.
    """
    _, page = await _page(ctx)
    x, y = _check_xy(page, x, y)
    try:
        await page.mouse.move(x, y, steps=6)
    except Exception as e:  # noqa: BLE001
        raise _err(e, "Mouse move") from e
    return await _snapshot(ctx, page, f"Moved mouse to ({x:.0f}, {y:.0f})", screenshot=screenshot)


async def _click(ctx: ToolContext, x: float, y: float, *, button: str = "left", count: int = 1,
                 modifiers: list[str] | None = None, screenshot: bool = True, label: str = "Clicked") -> ToolResult:
    tabs, page = await _page(ctx)
    x, y = _check_xy(page, x, y)
    try:
        await page.mouse.move(x, y, steps=4)
        await _with_modifiers(page, modifiers,
                              lambda: page.mouse.click(x, y, button=button, click_count=count, delay=40))
    except Exception as e:  # noqa: BLE001
        raise _err(e, "Click") from e
    page = await tabs.page()
    return await _snapshot(ctx, page, f"{label} at ({x:.0f}, {y:.0f})", screenshot=screenshot)


@computer.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def left_click(ctx: ToolContext, x: float, y: float, modifiers: list[str] | None = None,
                     screenshot: bool = True) -> ToolResult:
    """Left-click at (x, y).

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.
        modifiers: Keys held during the click, e.g. ["Shift"] or ["Control"].
        screenshot: Return a fresh screenshot.
    """
    return await _click(ctx, x, y, modifiers=modifiers, screenshot=screenshot, label="Left-clicked")


@computer.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def right_click(ctx: ToolContext, x: float, y: float, screenshot: bool = True) -> ToolResult:
    """Right-click at (x, y) (opens context menus).

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.
        screenshot: Return a fresh screenshot.
    """
    return await _click(ctx, x, y, button="right", screenshot=screenshot, label="Right-clicked")


@computer.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def double_click(ctx: ToolContext, x: float, y: float, screenshot: bool = True) -> ToolResult:
    """Double-click at (x, y) (e.g. select a word, open an item).

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.
        screenshot: Return a fresh screenshot.
    """
    return await _click(ctx, x, y, count=2, screenshot=screenshot, label="Double-clicked")


@computer.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def drag(ctx: ToolContext, start_x: float, start_y: float, end_x: float, end_y: float,
               screenshot: bool = True) -> ToolResult:
    """Drag with the left mouse button from one point to another (sliders, drag-and-drop, selections).

    Args:
        start_x: Start x.
        start_y: Start y.
        end_x: End x.
        end_y: End y.
        screenshot: Return a fresh screenshot.
    """
    _, page = await _page(ctx)
    sx, sy = _check_xy(page, start_x, start_y)
    ex, ey = _check_xy(page, end_x, end_y)
    try:
        await page.mouse.move(sx, sy, steps=4)
        await page.mouse.down()
        await page.mouse.move(ex, ey, steps=15)
        await page.mouse.up()
    except Exception as e:  # noqa: BLE001
        raise _err(e, "Drag") from e
    return await _snapshot(ctx, page, f"Dragged ({sx:.0f}, {sy:.0f}) → ({ex:.0f}, {ey:.0f})", screenshot=screenshot)


@computer.tool(name="type", risk=Risk.MEDIUM, scope="write", timeout=120, content_fields=("text",))
async def type_(ctx: ToolContext, text: str, screenshot: bool = True) -> ToolResult:
    """Type text at the current focus (click a field first).

    Args:
        text: Text to type.
        screenshot: Return a fresh screenshot.
    """
    _, page = await _page(ctx)
    try:
        await page.keyboard.type(text, delay=12)
    except Exception as e:  # noqa: BLE001
        raise _err(e, "Typing") from e
    return await _snapshot(ctx, page, f"Typed {len(text)} characters", screenshot=screenshot)


@computer.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def keypress(ctx: ToolContext, key: str, repeat: int = 1, screenshot: bool = True) -> ToolResult:
    """Press a key (optionally several times): Enter, Escape, Tab, Backspace, ArrowDown, PageDown, F5…

    Args:
        key: Key name (aliases like "esc", "return", "down" work).
        repeat: Number of presses (max 50).
        screenshot: Return a fresh screenshot.
    """
    _, page = await _page(ctx)
    k = "+".join(_key(p) for p in str(key).split("+"))
    try:
        for _ in range(max(1, min(int(repeat or 1), 50))):
            await page.keyboard.press(k)
    except Exception as e:  # noqa: BLE001
        raise _err(e, f"Key '{key}'") from e
    return await _snapshot(ctx, page, f"Pressed {k}" + (f" ×{repeat}" if repeat and repeat > 1 else ""),
                           screenshot=screenshot)


@computer.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def hotkey(ctx: ToolContext, keys: list[str], screenshot: bool = True) -> ToolResult:
    """Press a key combination, e.g. ["Control", "A"] (select all) or ["Control", "Shift", "Tab"].

    Args:
        keys: Keys pressed together, modifiers first.
        screenshot: Return a fresh screenshot.
    """
    if not keys:
        raise ToolError("Provide at least one key.")
    _, page = await _page(ctx)
    combo = "+".join(_key(k) for k in keys)
    try:
        await page.keyboard.press(combo)
    except Exception as e:  # noqa: BLE001
        raise _err(e, f"Hotkey '{combo}'") from e
    return await _snapshot(ctx, page, f"Pressed {combo}", screenshot=screenshot)


@computer.tool(risk=Risk.MEDIUM, scope="write", timeout=60)
async def scroll(ctx: ToolContext, x: float, y: float, direction: Literal["down", "up", "left", "right"] = "down",
                 amount: int = 3, screenshot: bool = True) -> ToolResult:
    """Scroll at (x, y) by mouse-wheel steps (≈100 px each).

    Args:
        x: Horizontal position of the pointer while scrolling.
        y: Vertical position of the pointer while scrolling.
        direction: Scroll direction.
        amount: Number of wheel steps (1-30).
        screenshot: Return a fresh screenshot.
    """
    _, page = await _page(ctx)
    x, y = _check_xy(page, x, y)
    steps = max(1, min(int(amount or 3), 30)) * 100
    dx = steps if direction == "right" else -steps if direction == "left" else 0
    dy = steps if direction == "down" else -steps if direction == "up" else 0
    try:
        await page.mouse.move(x, y)
        await page.mouse.wheel(dx, dy)
    except Exception as e:  # noqa: BLE001
        raise _err(e, "Scroll") from e
    return await _snapshot(ctx, page, f"Scrolled {direction} at ({x:.0f}, {y:.0f})", screenshot=screenshot)
