"""Outbound HTTP helpers for tools: SSRF guard + a shared client with sane timeouts."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import weakref
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.tools.base import ToolError

USER_AGENT = "Mozilla/5.0 (compatible; RenAgent/1.0; +https://f69labs.com/ren)"

_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = weakref.WeakKeyDictionary()


def http() -> httpx.AsyncClient:
    """Shared client for the running event loop."""
    loop = asyncio.get_running_loop()
    c = _clients.get(loop)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=50),
        )
        _clients[loop] = c
    return c


def _is_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # unresolvable → let the request fail naturally
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


async def assert_public_url(url: str) -> str:
    """Reject non-http(s) URLs and (unless allowed) private/loopback/link-local targets."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError(f"Only http(s) URLs are allowed (got '{parsed.scheme or url}').")
    host = parsed.hostname or ""
    if not host:
        raise ToolError("URL has no host.")
    if not settings.allow_private_network:
        if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
            raise ToolError("Access to local/internal hosts is blocked by the workspace network policy.")
        if await asyncio.to_thread(_is_private, host):
            raise ToolError("Access to private network addresses is blocked by the workspace network policy.")
    return url
