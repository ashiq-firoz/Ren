"""Execution sandbox server (sandbox/server.py) and the backend's remote routing through it."""
import asyncio
import importlib.util
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.tools.builtin.code  # noqa: F401
import app.tools.builtin.shell  # noqa: F401
from app.core.config import settings
from app.tools.base import Risk, ToolError, registry
from app.tools.builtin import _exec

SERVER = Path(__file__).resolve().parents[2] / "sandbox" / "server.py"


@pytest.fixture(scope="module")
def sandbox():
    spec = importlib.util.spec_from_file_location("ren_sandbox_server", SERVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ren_sandbox_server"] = module
    spec.loader.exec_module(module)
    yield module
    module._kill_all()


@pytest.fixture
def root(tmp_path, monkeypatch):
    workspaces = tmp_path / "workspaces"
    (workspaces / "org1" / "users" / "u1" / "workspace").mkdir(parents=True)
    (workspaces / "org2" / "users" / "u2" / "workspace").mkdir(parents=True)
    monkeypatch.setenv("SANDBOX_ROOT", str(workspaces))
    monkeypatch.setenv("SANDBOX_INSECURE", "1")
    monkeypatch.delenv("SANDBOX_TOKEN", raising=False)
    return workspaces


def _cwd(root: Path) -> str:
    return str(root / "org1" / "users" / "u1" / "workspace")


def test_health_and_exec(sandbox, root):
    client = TestClient(sandbox.app)
    health = client.get("/health").json()
    assert health["ok"] and health["auth"] == "insecure" and health["root_ready"]
    res = client.post("/exec", json={"command": "echo sandboxed", "cwd": _cwd(root), "timeout": 30})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["exit_code"] == 0 and "sandboxed" in body["stdout"] and set(body) >= {
        "exit_code", "stdout", "stderr", "duration_ms", "truncated"}
    res = client.post("/exec", json={"command": [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                                     "cwd": _cwd(root), "timeout": 30, "stdin": "abc"})
    assert "ABC" in res.json()["stdout"]
    res = client.post("/exec", json={"command": [sys.executable, "-c", "import time; time.sleep(10)"],
                                     "cwd": _cwd(root), "timeout": 1})
    assert res.json()["timed_out"] and "timeout" in res.json()["stderr"]
    res = client.post("/exec", json={"command": [sys.executable, "-c", "print('y' * 50000)"], "cwd": _cwd(root),
                                     "timeout": 30, "max_output": 2000})
    assert res.json()["truncated"] and len(res.json()["stdout"]) < 2200


def test_rejects_cwd_outside_root(sandbox, root, tmp_path):
    client = TestClient(sandbox.app)
    cases = [
        (str(tmp_path), 403),  # parent of the root
        ("relative/path", 400),
        (str(root / "org1" / "users" / "u1" / "workspace" / ".." / ".." / ".." / ".." / ".."), 403),
        (str(root / "org1" / "users" / "u1" / "missing"), 404),
    ]
    for cwd, status in cases:
        res = client.post("/exec", json={"command": "echo x", "cwd": cwd, "timeout": 10})
        assert res.status_code == status, (cwd, res.text)
    res = client.post("/exec", json={"command": "echo x", "cwd": _cwd(root), "timeout": 10,
                                     "roots": [str(root / "org2")]})
    assert res.status_code == 403
    res = client.post("/exec", json={"command": "echo x", "cwd": _cwd(root), "timeout": 10,
                                     "roots": [str(root / "org1" / "users" / "u1")]})
    assert res.status_code == 200
    assert client.post("/exec", json={"command": "", "cwd": _cwd(root)}).status_code == 400


def test_python_endpoint_and_minimal_env(sandbox, root, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_FOR_SANDBOX", "s3cr3t-value")
    client = TestClient(sandbox.app)
    code = "import os\nopen('made.txt', 'w').write('hi')\nprint(sorted(os.environ))"
    res = client.post("/python", json={"code": code, "cwd": _cwd(root), "timeout": 60,
                                       "env": {"REN_MARKER": "1", "LD_PRELOAD": "/evil.so", "BASH_ENV": "/evil"}})
    body = res.json()
    assert body["exit_code"] == 0, body
    assert (Path(_cwd(root)) / "made.txt").read_text() == "hi"
    assert "REN_MARKER" in body["stdout"]
    for leaked in ("SUPER_SECRET_FOR_SANDBOX", "LD_PRELOAD", "BASH_ENV", "SANDBOX_ROOT", "SANDBOX_INSECURE"):
        assert leaked not in body["stdout"], leaked
    res = client.post("/python", json={"code": "raise SystemExit(4)", "cwd": _cwd(root), "timeout": 30})
    assert res.json()["exit_code"] == 4


def test_auth_required(sandbox, root, monkeypatch):
    client = TestClient(sandbox.app)
    payload = {"command": "echo ok", "cwd": _cwd(root), "timeout": 10}
    monkeypatch.delenv("SANDBOX_INSECURE")
    assert client.post("/exec", json=payload).status_code == 503  # no token configured → refuse everything
    monkeypatch.setenv("SANDBOX_TOKEN", "tok-123")
    assert client.post("/exec", json=payload).status_code == 401
    assert client.post("/exec", json=payload, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/jobs", headers={"Authorization": "Basic tok-123"}).status_code == 401
    res = client.post("/exec", json=payload, headers={"Authorization": "Bearer tok-123"})
    assert res.status_code == 200 and "ok" in res.json()["stdout"]
    assert client.get("/health").status_code == 200


def test_background_jobs(sandbox, root):
    client = TestClient(sandbox.app)
    owner = f"org1:{uuid.uuid4().hex[:6]}"
    code = "import time; print('one', flush=True); time.sleep(0.5); print('two', flush=True)"
    res = client.post("/jobs", json={"command": [sys.executable, "-c", code], "cwd": _cwd(root), "owner": owner,
                                     "cwd_label": "workspace"})
    assert res.status_code == 200, res.text
    job = res.json()
    assert job["status"] == "running" and job["cwd"] == "workspace"
    output, status = "", {}
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = client.get(f"/jobs/{job['job_id']}", params={"owner": owner}).json()
        output += status["output"]
        if status["status"] != "running":
            break
        time.sleep(0.1)
    output += client.get(f"/jobs/{job['job_id']}", params={"owner": owner}).json()["output"]
    assert status["status"] == "exited" and status["exit_code"] == 0
    assert "one" in output and "two" in output
    assert client.get(f"/jobs/{job['job_id']}", params={"owner": "someone:else"}).status_code == 404
    assert client.delete(f"/jobs/{job['job_id']}", params={"owner": "someone:else"}).status_code == 404

    long_job = client.post("/jobs", json={"command": [sys.executable, "-c", "import time; time.sleep(30)"],
                                          "cwd": _cwd(root), "owner": owner}).json()
    killed = client.delete(f"/jobs/{long_job['job_id']}", params={"owner": owner}).json()
    assert killed["status"] == "killed"
    listed = client.get("/jobs", params={"owner": owner}).json()["jobs"]
    assert {j["job_id"] for j in listed} == {job["job_id"], long_job["job_id"]}


# =========================================================================== backend → sandbox routing
@pytest.fixture
def remote(sandbox, monkeypatch):
    """Route the backend's execution through the sandbox app (in-process ASGI transport)."""
    monkeypatch.setattr(settings, "sandbox_url", "http://sandbox.test")
    monkeypatch.setattr(settings, "sandbox_token", "tok-remote")
    monkeypatch.setenv("SANDBOX_TOKEN", "tok-remote")
    monkeypatch.delenv("SANDBOX_INSECURE", raising=False)
    monkeypatch.setenv("SANDBOX_ROOT", str(settings.workspaces_dir))
    monkeypatch.setattr(_exec, "_sandbox_transport", httpx.ASGITransport(app=sandbox.app))
    return sandbox


async def call(ctx, tool, /, **args):
    spec = registry.get(tool)
    return await spec.handler(ctx, **spec.validate(args))


async def test_backend_routes_commands_to_sandbox(tool_ctx, remote):
    assert registry.get("shell_exec").effective_risk({}) == Risk.MEDIUM
    out = await call(tool_ctx, "shell_exec", command="echo via-sandbox")
    assert out.startswith("exit code 0") and "via-sandbox" in out

    name = f"remote-{uuid.uuid4().hex[:8]}.txt"
    out = await call(tool_ctx, "code_python", code=f"open('../output/{name}', 'w').write('x')\nprint('ok')")
    assert "ok" in out and f"output/{name}" in out

    job = await call(tool_ctx, "shell_exec_background", command="echo bg-remote")
    assert job["cwd"] == "workspace"
    output, status = "", {}
    for _ in range(100):
        status = await call(tool_ctx, "shell_poll", job_id=job["job_id"])
        output += status["output"]
        if status["status"] != "running":
            break
        await asyncio.sleep(0.1)
    assert status["status"] == "exited" and "bg-remote" in output
    procs = await call(tool_ctx, "shell_processes")
    assert job["job_id"] in {j["job_id"] for j in procs["jobs"]}

    found = await call(tool_ctx, "shell_which", names=["python" if sys.platform != "win32" else "git"])
    assert found["mode"] == "sandbox"


async def test_backend_reports_sandbox_auth_failure(tool_ctx, remote, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_token", "wrong-token")
    with pytest.raises(ToolError, match="SANDBOX_TOKEN"):
        await call(tool_ctx, "shell_exec", command="echo nope")


async def test_backend_reports_unreachable_sandbox(tool_ctx, monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(settings, "sandbox_url", "http://sandbox.test")
    monkeypatch.setattr(_exec, "_sandbox_transport", httpx.MockTransport(refuse))
    with pytest.raises(ToolError, match="unreachable"):
        await call(tool_ctx, "shell_exec", command="echo nope")
