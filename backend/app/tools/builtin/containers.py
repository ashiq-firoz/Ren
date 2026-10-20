"""Docker and Kubernetes tools (disabled by default; enable them in Connectors → Built-in tools).

They drive the host's `docker` / `kubectl` CLIs with the operator's existing configuration (DOCKER_HOST, KUBECONFIG,
~/.kube/config…). Commands always run on the Ren server host — never in the code sandbox — because they manage
infrastructure, not user code. Reads are LOW risk; changes are HIGH/CRITICAL and require the admin scope.
"""
from __future__ import annotations

import json
import re
import shlex
import uuid
from collections.abc import Callable
from typing import Literal

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin._exec import TOOL_TIMEOUT, clip, fence, find_program, run_command, ws_path

docker = ToolGroup(
    "docker", "Docker", "Inspect and manage Docker containers and images on the Ren server host",
    prefix="docker", icon="container", category="infrastructure", default_enabled=False,
)
k8s = ToolGroup(
    "kubernetes", "Kubernetes", "Inspect and operate Kubernetes workloads with kubectl",
    prefix="k8s", icon="server", category="infrastructure", default_enabled=False,
)

_DOCKER_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
               "DOCKER_API_VERSION", "XDG_RUNTIME_DIR")
_KUBE_ENV = ("KUBECONFIG", "AWS_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_REGION",
             "AWS_DEFAULT_REGION", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG", "AZURE_CONFIG_DIR")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,252}$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@\-]{0,254}$")
_PORT = re.compile(r"^((\d{1,3}\.){3}\d{1,3}:)?(\d{1,5}:)?\d{1,5}(/(tcp|udp))?$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_K8S_RESOURCE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]*(,[A-Za-z][A-Za-z0-9.\-]*)*$")
_K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?(/[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?)?$")
_SELECTOR = re.compile(r"^[A-Za-z0-9._/=!,() \-]{1,500}$")
_SINCE = re.compile(r"^\d+(s|m|h)$|^\d{4}-\d{2}-\d{2}T[\d:.]+Z?$")
_DURATION = re.compile(r"^\d+(s|m|h)$")


def _require_cli(name: str) -> None:
    if find_program(name) is None:
        raise ToolError(f"The `{name}` CLI is not installed on the Ren server host, so these tools are unavailable. "
                        f"An admin can install {name} (and configure access to the cluster/daemon) or disable this "
                        "tool group.")


def _check(rx: re.Pattern, value: str, what: str) -> str:
    value = (value or "").strip()
    if not rx.match(value):
        raise ToolError(f"Invalid {what}: {value!r}")
    return value


_KLOG_PREFIX = re.compile(r"^[EWIF]\d{4} [\d:.]+\s+\d+ [\w.\-]+:\d+\]\s*")


def _error_text(res: dict) -> str:
    """Last distinct lines of a failed CLI call (klog prefixes stripped, repeated retries collapsed)."""
    raw = (res["stderr"] or res["stdout"] or "no output").replace("\r\n", "\n")
    lines: list[str] = []
    for line in raw.splitlines():
        line = _KLOG_PREFIX.sub("", line).strip()
        if line and line not in lines:
            lines.append(line)
    return clip("\n".join(lines[-12:]), 3000)


async def _docker(ctx: ToolContext, args: list[str], *, timeout: int = 60, check: bool = True) -> dict:
    _require_cli("docker")
    res = await run_command(ctx, ["docker", *args], cwd_virtual="temp", timeout=timeout, local=True,
                            user_paths=False, passthrough=_DOCKER_ENV)
    if check and res["exit_code"] != 0:
        raise ToolError(f"docker {args[0]} failed: {_error_text(res)}")
    return res


async def _kubectl(ctx: ToolContext, args: list[str], *, namespace: str | None = None, timeout: int = 60,
                   cwd: str = "temp", check: bool = True) -> dict:
    _require_cli("kubectl")
    ns = ["-n", _check(_K8S_NAME, namespace, "namespace")] if namespace else []
    res = await run_command(ctx, ["kubectl", "--request-timeout=30s", *ns, *args], cwd_virtual=cwd, timeout=timeout,
                            local=True, user_paths=False, passthrough=_KUBE_ENV)
    if check and res["exit_code"] != 0:
        raise ToolError(f"kubectl {args[0]} failed: {_error_text(res)}")
    return res


def _out(res: dict, empty: str = "(no output)") -> str:
    text = "\n".join(x for x in (res["stdout"].strip(), res["stderr"].strip()) if x)
    return clip(text.replace("\r\n", "\n"), 14_000, tail_ratio=0.5) or empty


def _cmd_preview(build: Callable[[dict], tuple[str, list[str]]]) -> Callable[[dict], str]:
    def preview(args: dict) -> str:
        try:
            title, argv = build(args)
        except Exception:  # noqa: BLE001
            return f"**{build.__name__}** {json.dumps(args, default=str)[:800]}"
        return f"**{title}** on the Ren server host\n\n{fence(' '.join(shlex.quote(str(a)) for a in argv), 'bash')}"

    return preview


# =========================================================================== docker
@docker.tool(name="list", risk=Risk.LOW, scope="read")
async def docker_list(ctx: ToolContext, all: bool = False) -> str:  # noqa: A002
    """List Docker containers (like `docker ps`): id, name, image, status and published ports.

    Args:
        all: Include stopped containers.
    """
    # explicit template: '{{json .}}' would make the daemon compute container sizes (slow on busy hosts)
    fmt = "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
    res = await _docker(ctx, ["ps", "--format", fmt, *(["--all"] if all else [])])
    rows = []
    for line in res["stdout"].splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        ports = parts[4].strip() if len(parts) > 4 else ""
        rows.append(f"{parts[0][:12]}  {parts[1]}  {parts[2]}  {parts[3]}" + (f"  ports: {ports}" if ports else ""))
    return "\n".join(rows) if rows else "No containers" + ("." if all else " running (all=true includes stopped).")


@docker.tool(risk=Risk.LOW, scope="read")
async def images(ctx: ToolContext) -> str:
    """List local Docker images: repository:tag, id, size and age."""
    res = await _docker(ctx, ["images", "--format", "{{json .}}"])
    rows = []
    for line in res["stdout"].splitlines():
        try:
            i = json.loads(line)
        except ValueError:
            continue
        rows.append(f"{i.get('Repository')}:{i.get('Tag')}  {i.get('ID', '')[:19]}  {i.get('Size')}  "
                    f"{i.get('CreatedSince')}")
    return "\n".join(rows) if rows else "No images."


@docker.tool(name="logs", risk=Risk.LOW, scope="read")
async def docker_logs(ctx: ToolContext, container: str, tail: int = 200, since: str | None = None,
                      timestamps: bool = False) -> str:
    """Show the recent logs (stdout and stderr) of a container.

    Args:
        container: Container name or id.
        tail: Number of most recent lines (1-5000).
        since: Only logs newer than this, e.g. "10m", "2h" or an RFC 3339 timestamp.
        timestamps: Prefix each line with its timestamp.
    """
    args = ["logs", "--tail", str(max(1, min(tail, 5000)))]
    if since:
        args += ["--since", _check(_SINCE, since, "since")]
    if timestamps:
        args.append("--timestamps")
    res = await _docker(ctx, [*args, _check(_NAME, container, "container")])
    return _out(res, "(no log output)")


def _redact_env(env: list | None) -> list[str]:
    return sorted({str(e).split("=", 1)[0] + "=…" for e in (env or [])})


@docker.tool(name="inspect", risk=Risk.LOW, scope="read")
async def docker_inspect(ctx: ToolContext, target: str) -> dict:
    """Inspect a container or image: state, image, command, ports, mounts, networks (environment values are hidden).

    Args:
        target: Container or image name/id.
    """
    res = await _docker(ctx, ["inspect", _check(_IMAGE, target, "container/image")])
    try:
        data = json.loads(res["stdout"])[0]
    except (ValueError, IndexError) as e:
        raise ToolError("Unexpected output from docker inspect.") from e
    cfg = data.get("Config") or {}
    out: dict = {"id": str(data.get("Id", ""))[:19], "created": data.get("Created")}
    if "State" in data:  # container
        st = data.get("State") or {}
        net = data.get("NetworkSettings") or {}
        out.update({
            "name": str(data.get("Name", "")).lstrip("/"), "image": cfg.get("Image"),
            "state": {k: st.get(k) for k in ("Status", "Running", "ExitCode", "Error", "StartedAt", "FinishedAt",
                                             "OOMKilled") if k in st},
            "health": (st.get("Health") or {}).get("Status"),
            "command": cfg.get("Cmd"), "entrypoint": cfg.get("Entrypoint"), "env": _redact_env(cfg.get("Env")),
            "ports": net.get("Ports"),
            "networks": {k: (v or {}).get("IPAddress") for k, v in (net.get("Networks") or {}).items()},
            "mounts": [f"{m.get('Source')} -> {m.get('Destination')}{'' if m.get('RW', True) else ' (ro)'}"
                       for m in data.get("Mounts") or []],
            "restart_policy": ((data.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name"),
            "labels": dict(list((cfg.get("Labels") or {}).items())[:25]),
        })
    else:  # image
        out.update({"tags": data.get("RepoTags"), "size_mb": round((data.get("Size") or 0) / 1e6, 1),
                    "architecture": data.get("Architecture"), "command": cfg.get("Cmd"),
                    "entrypoint": cfg.get("Entrypoint"), "workdir": cfg.get("WorkingDir"),
                    "exposed_ports": sorted((cfg.get("ExposedPorts") or {}).keys()),
                    "env": _redact_env(cfg.get("Env"))})
    return out


def _build_argv(args: dict) -> tuple[str, list[str]]:
    argv = ["docker", "build", "-t", _check(_IMAGE, args.get("tag", ""), "tag")]
    if args.get("dockerfile"):
        argv += ["-f", str(args["dockerfile"])]
    for k, v in (args.get("build_args") or {}).items():
        argv += ["--build-arg", f"{_check(_ENV_KEY, k, 'build arg')}={v}"]
    if args.get("no_cache"):
        argv.append("--no-cache")
    return f"Build Docker image from `{args.get('path')}`", [*argv, "."]


@docker.tool(risk=Risk.HIGH, scope="admin", timeout=TOOL_TIMEOUT, preview=_cmd_preview(_build_argv))
async def build(ctx: ToolContext, path: str, tag: str, dockerfile: str | None = None,
                build_args: dict[str, str] | None = None, no_cache: bool = False) -> str:
    """Build a Docker image from a folder in the workspace (the build runs Dockerfile steps on the host).

    Args:
        path: Workspace folder used as build context, e.g. "projects/api".
        tag: Image name and tag, e.g. "api:dev".
        dockerfile: Dockerfile path relative to the context (default "Dockerfile").
        build_args: Build-time variables, e.g. {"VERSION": "1.2"}.
        no_cache: Build without using cached layers.
    """
    context = ws_path(ctx, path, must_exist=True)
    if not context.is_dir():
        raise ToolError(f"{path} is not a folder.")
    if dockerfile:
        df = (context / dockerfile).resolve()
        if context.resolve() not in df.parents or not df.is_file():
            raise ToolError("dockerfile must be an existing file inside the build context.")
        dockerfile = df.relative_to(context.resolve()).as_posix()
    _, argv = _build_argv({"tag": tag, "dockerfile": dockerfile, "build_args": build_args, "no_cache": no_cache,
                           "path": path})
    _require_cli("docker")
    await ctx.progress(f"Building {tag}")
    res = await run_command(ctx, argv, cwd_virtual=ctx.workspace.virtual(context), timeout=600, local=True,
                            user_paths=False, passthrough=_DOCKER_ENV)
    if res["exit_code"] != 0:
        raise ToolError(f"docker build failed:\n{_out(res)}")
    return f"Built image {tag}.\n" + clip(_out(res), 4000, tail_ratio=0.9)


def _run_argv(ctx: ToolContext | None, args: dict, name: str) -> list[str]:
    argv = ["docker", "run", "--name", name, "--security-opt", "no-new-privileges"]
    if args.get("detach", True):
        argv.append("-d")
    if args.get("remove") or not args.get("detach", True):
        argv.append("--rm")
    for p in args.get("ports") or []:
        argv += ["-p", _check(_PORT, p, "port mapping")]
    for k, v in (args.get("env") or {}).items():
        argv += ["-e", f"{_check(_ENV_KEY, k, 'environment variable')}={v}"]
    for vol in args.get("volumes") or []:
        src, _, dst = str(vol).partition(":")
        if not dst.startswith("/"):
            raise ToolError(f"Volume {vol!r} must look like 'workspace/data:/data' (container path absolute).")
        mode = ""
        if dst.endswith((":ro", ":rw")):
            dst, mode = dst[:-3], dst[-3:]
        host = ws_path(ctx, src, must_exist=True) if ctx else src
        argv += ["-v", f"{host}:{dst}{mode}"]
    if ctx is not None:
        argv += ["--label", f"ren.org={ctx.org_id}", "--label", f"ren.user={ctx.workspace.user_id}"]
    argv.append(_check(_IMAGE, args.get("image", ""), "image"))
    if args.get("command"):
        argv += shlex.split(str(args["command"]))
    return argv


def _run_preview(args: dict) -> str:
    try:
        argv = _run_argv(None, args, args.get("name") or "<generated>")
    except Exception:  # noqa: BLE001
        argv = ["docker", "run", str(args.get("image"))]
    command = " ".join(shlex.quote(a) for a in argv)
    return f"**Run a Docker container** on the Ren server host\n\n{fence(command, 'bash')}"


@docker.tool(risk=Risk.HIGH, scope="admin", timeout=TOOL_TIMEOUT, preview=_run_preview)
async def run(ctx: ToolContext, image: str, command: str | None = None, name: str | None = None,
              ports: list[str] | None = None, env: dict[str, str] | None = None, volumes: list[str] | None = None,
              detach: bool = True, remove: bool = False) -> str:
    """Start a container from an image (runs that image's code on the host).

    Detached containers keep running (stop them with docker_stop); attached runs wait for the command to finish
    (max 10 minutes) and are removed afterwards. Only workspace folders can be mounted.

    Args:
        image: Image to run, e.g. "python:3.12-slim" or "api:dev".
        command: Command to run instead of the image default, e.g. "python -m http.server 8000".
        name: Container name (default: generated).
        ports: Port mappings "host:container", e.g. ["8080:80"].
        env: Environment variables for the container.
        volumes: Workspace mounts "workspace-path:/container/path[:ro]", e.g. ["projects/site:/srv/www:ro"].
        detach: Run in the background and return the container id.
        remove: Remove the container automatically when it exits.
    """
    _require_cli("docker")
    name = _check(_NAME, name, "container name") if name else f"ren-{uuid.uuid4().hex[:10]}"
    argv = _run_argv(ctx, {"image": image, "command": command, "ports": ports, "env": env, "volumes": volumes,
                           "detach": detach, "remove": remove}, name)
    res = await run_command(ctx, argv, cwd_virtual="temp", timeout=600 if not detach else 120, local=True,
                            user_paths=False, passthrough=_DOCKER_ENV)
    if res.get("timed_out"):
        await _docker(ctx, ["rm", "-f", name], check=False)
        raise ToolError(f"The container ran longer than the timeout and was removed.\n{_out(res)}")
    if res["exit_code"] != 0 and detach:
        raise ToolError(f"docker run failed: {_error_text(res)}")
    if detach:
        return f"Started container {name} ({res['stdout'].strip()[:12]}). Use docker_logs to follow it."
    return f"Container {name} exited with code {res['exit_code']}:\n{_out(res)}"


def _stop_argv(args: dict) -> tuple[str, list[str]]:
    return "Stop Docker container", ["docker", "stop", "-t", str(max(0, min(int(args.get("timeout") or 10), 300))),
                                     _check(_NAME, args.get("container", ""), "container")]


@docker.tool(risk=Risk.CRITICAL, scope="admin", preview=_cmd_preview(_stop_argv))
async def stop(ctx: ToolContext, container: str, timeout: int = 10) -> str:
    """Stop a running container (sends SIGTERM, then SIGKILL after `timeout` seconds).

    Args:
        container: Container name or id.
        timeout: Seconds to wait for a graceful shutdown before killing (0-300).
    """
    _, argv = _stop_argv({"container": container, "timeout": timeout})
    res = await _docker(ctx, argv[1:], timeout=max(60, timeout + 30))
    return f"Stopped {res['stdout'].strip() or container}."


# =========================================================================== kubernetes
@k8s.tool(risk=Risk.LOW, scope="read")
async def get(ctx: ToolContext, resource: str, name: str | None = None, namespace: str | None = None,
              all_namespaces: bool = False, selector: str | None = None,
              output: Literal["wide", "name", "yaml", "json"] = "wide") -> str:
    """List or fetch Kubernetes resources (kubectl get), e.g. pods, deployments, services, nodes, events.

    Secret values are never returned (secrets can only be listed).

    Args:
        resource: Resource type(s), e.g. "pods", "deploy", "svc,ingress".
        name: A specific resource name.
        namespace: Namespace (default: the current context's namespace).
        all_namespaces: List across all namespaces.
        selector: Label selector, e.g. "app=api,tier!=db".
        output: Output format.
    """
    resource = _check(_K8S_RESOURCE, resource, "resource")
    if "secret" in resource.lower() and output in ("yaml", "json"):
        raise ToolError("Secret contents are not exposed; use output='wide' or 'name' to list secrets.")
    args = ["get", resource]
    if name:
        args.append(_check(_K8S_NAME, name, "name"))
    if all_namespaces and not name:
        args.append("--all-namespaces")
    if selector:
        args += ["-l", _check(_SELECTOR, selector, "selector")]
    args += ["-o", output]
    res = await _kubectl(ctx, args, namespace=None if all_namespaces else namespace)
    return _out(res, "No resources found.")


@k8s.tool(risk=Risk.LOW, scope="read")
async def describe(ctx: ToolContext, resource: str, name: str, namespace: str | None = None) -> str:
    """Show detailed state and recent events of a Kubernetes resource (kubectl describe).

    Args:
        resource: Resource type, e.g. "pod", "deployment", "node".
        name: Resource name.
        namespace: Namespace (default: the current context's namespace).
    """
    res = await _kubectl(ctx, ["describe", _check(_K8S_RESOURCE, resource, "resource"),
                               _check(_K8S_NAME, name, "name")], namespace=namespace)
    return _out(res)


@k8s.tool(name="logs", risk=Risk.LOW, scope="read")
async def k8s_logs(ctx: ToolContext, pod: str, namespace: str | None = None, container: str | None = None,
                   tail: int = 200, previous: bool = False, since: str | None = None) -> str:
    """Show recent logs of a pod (or "deployment/<name>") container.

    Args:
        pod: Pod name, or "deployment/<name>" to pick one of its pods.
        namespace: Namespace (default: the current context's namespace).
        container: Container name for multi-container pods.
        tail: Number of most recent lines (1-5000).
        previous: Logs of the previous (crashed) container instance.
        since: Only logs newer than this duration, e.g. "15m" or "1h".
    """
    args = ["logs", _check(_K8S_NAME, pod, "pod"), f"--tail={max(1, min(tail, 5000))}"]
    if container:
        args += ["-c", _check(_K8S_NAME, container, "container")]
    if previous:
        args.append("--previous")
    if since:
        args.append("--since=" + _check(_DURATION, since, "since"))
    res = await _kubectl(ctx, args, namespace=namespace)
    return _out(res, "(no log output)")


def _apply_risk(args: dict) -> Risk:
    return Risk.LOW if args.get("dry_run") else Risk.CRITICAL


def _apply_preview(args: dict) -> str:
    target = f"`{args['path']}`" if args.get("path") else "the manifest below"
    head = (f"**kubectl apply** {target}" + (f" in namespace `{args['namespace']}`" if args.get("namespace") else "")
            + (" (server-side dry run)" if args.get("dry_run") else ""))
    if args.get("manifest"):
        head += "\n\n" + fence(clip(str(args["manifest"]), 6000), "yaml")
    return head


@k8s.tool(risk=Risk.CRITICAL, scope="admin", risk_fn=_apply_risk, preview=_apply_preview)
async def apply(ctx: ToolContext, manifest: str | None = None, path: str | None = None, namespace: str | None = None,
                dry_run: bool = False) -> str:
    """Create or update Kubernetes resources from a YAML manifest (kubectl apply). Use dry_run=true to validate first.

    Args:
        manifest: YAML manifest content (one or more documents).
        path: Alternatively, a workspace path to a manifest file or folder, e.g. "projects/api/k8s".
        namespace: Namespace to apply into (default: the manifest's or the context's namespace).
        dry_run: Validate on the server without changing anything.
    """
    if bool(manifest) == bool(path):
        raise ToolError("Pass either `manifest` (YAML text) or `path` (workspace file/folder), not both.")
    cleanup = None
    if manifest:
        if len(manifest) > 1_000_000:
            raise ToolError("The manifest is too large (max 1 MB).")
        tmp = ws_path(ctx, f"temp/k8s-{uuid.uuid4().hex[:10]}.yaml", for_write=True)
        tmp.write_text(manifest, encoding="utf-8")
        target, cleanup = tmp.name, tmp
    else:
        p = ws_path(ctx, path, must_exist=True)
        target = str(p)
    try:
        args = ["apply", "-f", target, *(["--dry-run=server"] if dry_run else [])]
        res = await _kubectl(ctx, args, namespace=namespace, timeout=120)
    finally:
        if cleanup is not None:
            cleanup.unlink(missing_ok=True)
    return _out(res)


def _workload_preview(verb: str):
    def preview(args: dict) -> str:
        extra = ""
        if "replicas" in args:
            extra = f" to **{args.get('replicas')}** replicas"
        if args.get("to_revision"):
            extra = f" to revision {args['to_revision']}"
        ns = f" in namespace `{args['namespace']}`" if args.get("namespace") else ""
        return f"**{verb}** `{args.get('resource', 'deployment')}/{args.get('name')}`{ns}{extra}"

    return preview


Workload = Literal["deployment", "statefulset", "daemonset"]


@k8s.tool(risk=Risk.CRITICAL, scope="admin", preview=_workload_preview("Rolling restart of"))
async def restart(ctx: ToolContext, name: str, resource: Workload = "deployment", namespace: str | None = None) -> str:
    """Restart a workload's pods with a rolling restart (kubectl rollout restart).

    Args:
        name: Workload name.
        resource: Workload kind.
        namespace: Namespace (default: the current context's namespace).
    """
    res = await _kubectl(ctx, ["rollout", "restart", f"{resource}/{_check(_K8S_NAME, name, 'name')}"],
                         namespace=namespace)
    return _out(res)


@k8s.tool(risk=Risk.CRITICAL, scope="admin", preview=_workload_preview("Scale"))
async def scale(ctx: ToolContext, name: str, replicas: int,
                resource: Literal["deployment", "statefulset"] = "deployment", namespace: str | None = None) -> str:
    """Change the number of replicas of a deployment or statefulset (kubectl scale).

    Args:
        name: Workload name.
        replicas: Desired number of replicas (0-1000).
        resource: Workload kind.
        namespace: Namespace (default: the current context's namespace).
    """
    if not 0 <= replicas <= 1000:
        raise ToolError("replicas must be between 0 and 1000.")
    res = await _kubectl(ctx, ["scale", f"{resource}/{_check(_K8S_NAME, name, 'name')}", f"--replicas={replicas}"],
                         namespace=namespace)
    return _out(res)


@k8s.tool(risk=Risk.CRITICAL, scope="admin", preview=_workload_preview("Roll back"))
async def rollback(ctx: ToolContext, name: str, resource: Workload = "deployment", namespace: str | None = None,
                   to_revision: int | None = None) -> str:
    """Roll a workload back to its previous (or a specific) revision (kubectl rollout undo).

    Args:
        name: Workload name.
        resource: Workload kind.
        namespace: Namespace (default: the current context's namespace).
        to_revision: Revision number to roll back to (default: the previous one).
    """
    args = ["rollout", "undo", f"{resource}/{_check(_K8S_NAME, name, 'name')}"]
    if to_revision:
        args.append(f"--to-revision={int(to_revision)}")
    res = await _kubectl(ctx, args, namespace=namespace)
    return _out(res)
