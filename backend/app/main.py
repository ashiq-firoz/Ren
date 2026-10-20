"""Ren by f69 Labs — FastAPI application."""
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.routing import Route

from app.core.config import VERSION, settings
from app.core.db import init_db
from app.core.events import bus

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ren")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.agents.approvals import broker
    from app.agents.orchestrator import hub
    from app.agents.task_manager import task_manager
    from app.services.scheduler import scheduler_loop
    from app.tools.builtin import load_builtin_tools
    from app.tools.mcp_client import mcp_manager

    bus.configure()
    task_manager.reset()
    hub.reset()
    broker.reset()
    await init_db()
    loaded = load_builtin_tools()
    from app.tools.base import registry

    log.info("Ren %s — db=%s, tool modules=%d, tools=%d", VERSION, "sqlite" if settings.is_sqlite else "postgresql",
             len(loaded), len(registry.tools))
    background: list[asyncio.Task] = []
    async with AsyncExitStack() as stack:
        try:
            from app.tools.mcp_server import build_mcp_app

            manager, _ = build_mcp_app()  # a session manager can only run once → fresh one per start
            await stack.enter_async_context(manager.run())
            app.state.mcp_session_manager = manager
        except Exception:  # noqa: BLE001
            log.exception("MCP server endpoint unavailable")
        await task_manager.recover()
        await hub.recover()
        background += [
            asyncio.create_task(scheduler_loop(), name="scheduler"),
            asyncio.create_task(broker.expire_loop(), name="approval-sweeper"),
            asyncio.create_task(hub.gc_loop(), name="orchestrator-gc"),
            asyncio.create_task(mcp_manager.startup(), name="mcp-startup"),
        ]
        try:
            yield
        finally:
            for t in background:
                t.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await hub.shutdown()
            await task_manager.shutdown()
            await mcp_manager.shutdown()
            await bus.close()
            from app.llm.client import http_client

            await http_client().aclose()


class _McpEndpoint:
    """ASGI endpoint delegating to the session manager of the current app run."""

    def __init__(self, app: FastAPI):
        self.app = app

    async def __call__(self, scope, receive, send):
        manager = getattr(self.app.state, "mcp_session_manager", None)
        if manager is None:
            await JSONResponse({"detail": "MCP endpoint unavailable"}, status_code=503)(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(title="Ren by f69 Labs", version=VERSION, lifespan=lifespan,
                  description="Agentic swarm orchestrator — orchestrator chat, background agents, STAIR RAG, "
                              "MCP tools, approvals and policy alignment.")
    origins = list(dict.fromkeys([*settings.cors_origins, settings.frontend_url]))
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["Content-Disposition"])

    from app.api import agents, auth, conversations, misc, orgs, policies, providers, schedules, system, tasks, tools

    for r in (auth, orgs, providers, agents, conversations, tasks, schedules, policies, tools, misc, system):
        app.include_router(r.router)
    try:
        from app.api import knowledge

        app.include_router(knowledge.router)
    except Exception:  # noqa: BLE001
        log.exception("knowledge API unavailable")

    endpoint = _McpEndpoint(app)
    app.router.routes.append(Route("/mcp", endpoint=endpoint, methods=["GET", "POST", "DELETE"]))
    app.router.routes.append(Route("/mcp/", endpoint=endpoint, methods=["GET", "POST", "DELETE"]))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # noqa: ARG001
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    @app.get("/")
    async def root():
        return {"name": "Ren by f69 Labs", "version": VERSION, "docs": "/docs", "health": "/api/health"}

    return app


app = create_app()
