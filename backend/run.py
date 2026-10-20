"""Dev/prod runner. Uses the Proactor event loop on Windows (needed for subprocesses and stdio MCP servers).

    python run.py                 # 0.0.0.0:8000
    python run.py --port 8010 --reload
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    # A single worker process runs the agent runtime (tasks + orchestrators live in-process).
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload, workers=args.workers,
                loop="asyncio", proxy_headers=True, forwarded_allow_ips="*", timeout_graceful_shutdown=15)


if __name__ == "__main__":
    main()
