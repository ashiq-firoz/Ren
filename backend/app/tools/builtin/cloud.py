"""Cloud infrastructure tools — three narrowly scoped groups, disabled by default:

* `aws`   — S3, EC2, Lambda, CloudWatch Logs Insights, ECS, RDS and Cost Explorer via boto3 (optional `[cloud]` extra).
* `azure` — resource groups, resources, VMs, storage containers/blobs and Monitor metrics via ARM REST with an app
            registration's client-credentials token (no SDK).
* `gcp`   — Cloud Storage, Compute Engine, Cloud Run and Cloud Logging via REST with a service-account JWT signed
            locally (RS256, `cryptography`), requesting the read-only cloud-platform scope.

No tool exposes arbitrary API calls; destructive operations are CRITICAL/admin and optional allowlists in the AWS
connector restrict which buckets, functions and instances can be touched. Prefer least-privilege credentials.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote

from app.tools.base import Risk, ToolContext, ToolError, ToolGroup
from app.tools.builtin import _http as H
from app.tools.builtin._oauth import (
    cached,
    google_service_account_token,
    invalidate,
    load_service_account,
    microsoft_client_credentials,
)
from app.tools.connectors import ConnectorField, ConnectorKindSpec, register_connector_kind

UTC = timezone.utc


def _lim(n: int, hi: int = 200) -> int:
    return max(1, min(int(n or 50), hi))


# =========================================================================== AWS (boto3)
aws = ToolGroup(
    "aws", "AWS",
    "Inspect and operate selected AWS resources: S3 objects, EC2 instances, Lambda functions, CloudWatch logs, ECS "
    "services, RDS databases and cost summaries.",
    prefix="aws", icon="cloud", category="infrastructure", connector_kinds=("aws",), requires=("boto3",),
    default_enabled=False,
)

_ALLOW_KEYS = {"bucket": "allowed_buckets", "function": "allowed_functions", "instance": "allowed_instances"}


def _allowed(cfg: dict, what: str, name: str) -> None:
    raw = str(cfg.get(_ALLOW_KEYS[what]) or "").strip()
    if not raw:
        return
    patterns = [p.strip() for p in raw.split(",") if p.strip()]
    if not any(fnmatch.fnmatchcase(name, p) for p in patterns):
        raise ToolError(f"The AWS connector doesn't allow access to {what} '{name}' (allowlist: {', '.join(patterns)}).")


def _is_allowed(cfg: dict, what: str, name: str) -> bool:
    try:
        _allowed(cfg, what, name)
        return True
    except ToolError:
        return False


def _boto3():
    try:
        import boto3  # noqa: F401
        import botocore  # noqa: F401
    except ImportError as e:
        raise ToolError("AWS tools need boto3 — install the optional dependency: pip install 'ren-backend[cloud]' "
                        "(or pip install boto3).") from e
    import boto3

    return boto3


def _aws_error(e: Exception) -> ToolError:
    resp = getattr(e, "response", None)
    if isinstance(resp, dict) and resp.get("Error"):
        code = resp["Error"].get("Code") or "Error"
        msg = resp["Error"].get("Message") or ""
        if code in ("InvalidClientTokenId", "SignatureDoesNotMatch", "UnrecognizedClientException", "ExpiredToken",
                    "InvalidAccessKeyId"):
            return ToolError(f"AWS credentials are invalid or expired ({code}) — reconnect AWS in Connectors.")
        if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "AuthorizationError"):
            return ToolError(f"AWS denied access ({code}): {msg[:300]} — the IAM policy lacks this permission.")
        return ToolError(f"AWS {code}: {msg[:400]}")
    name = type(e).__name__
    if name in ("NoCredentialsError", "PartialCredentialsError"):
        return ToolError("AWS credentials are missing — reconnect AWS in Connectors.")
    if name in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"):
        return ToolError(f"Could not reach AWS ({name}) — check the region and network access.")
    return ToolError(f"AWS error ({name}): {str(e)[:300]}")


async def _aws_session(cfg: dict):
    boto3 = _boto3()
    H.need(cfg, ("access_key_id", "secret_access_key"), "AWS")
    region = str(cfg.get("region") or "us-east-1").strip()
    base_kw: dict[str, Any] = {"aws_access_key_id": str(cfg["access_key_id"]).strip(),
                               "aws_secret_access_key": str(cfg["secret_access_key"]).strip(), "region_name": region}
    if cfg.get("session_token"):
        base_kw["aws_session_token"] = str(cfg["session_token"]).strip()
    role = str(cfg.get("role_arn") or "").strip()
    if not role:
        return boto3.session.Session(**base_kw)

    async def fetch():
        def assume():
            sts = boto3.session.Session(**base_kw).client("sts")
            args: dict[str, Any] = {"RoleArn": role, "RoleSessionName": "ren-agent", "DurationSeconds": 3600}
            if cfg.get("external_id"):
                args["ExternalId"] = str(cfg["external_id"]).strip()
            return sts.assume_role(**args)["Credentials"]

        try:
            creds = await asyncio.to_thread(assume)
        except Exception as e:  # noqa: BLE001
            raise _aws_error(e) from e
        return {"AccessKeyId": creds["AccessKeyId"], "SecretAccessKey": creds["SecretAccessKey"],
                "SessionToken": creds["SessionToken"]}, 3300

    creds = await cached(cfg, "aws-role", fetch)
    return boto3.session.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                                 aws_session_token=creds["SessionToken"], region_name=region)


async def _aws_call(ctx: ToolContext, service: str, fn, *, region: str | None = None) -> tuple[Any, dict]:
    """Run `fn(client)` in a thread with friendly error mapping. Returns (result, cfg)."""
    cfg = await ctx.connector("aws")
    session = await _aws_session(cfg)

    def run():
        client = session.client(service, region_name=region) if region else session.client(service)
        return fn(client)

    try:
        return await asyncio.to_thread(run), cfg
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _aws_error(e) from e


def _iso(v: Any) -> str | None:
    return H.iso(v) if isinstance(v, datetime) else (str(v) if v else None)


@aws.tool(risk=Risk.LOW, scope="read")
async def s3_list_buckets(ctx: ToolContext) -> dict:
    """List S3 buckets (filtered by the connector's bucket allowlist, if set)."""
    data, cfg = await _aws_call(ctx, "s3", lambda c: c.list_buckets())
    rows = [{"name": b["Name"], "created": _iso(b.get("CreationDate"))} for b in data.get("Buckets", [])
            if _is_allowed(cfg, "bucket", b["Name"])]
    return {"count": len(rows), "buckets": rows}


@aws.tool(risk=Risk.LOW, scope="read")
async def s3_list_objects(ctx: ToolContext, bucket: str, prefix: str = "", limit: int = 100,
                          continuation_token: str | None = None) -> dict:
    """List objects (and sub-folders) in an S3 bucket under a prefix.

    Args:
        bucket: Bucket name.
        prefix: Key prefix ("folder"), e.g. "reports/2026/".
        limit: Maximum keys (1-1000).
        continuation_token: Token from a previous call to get the next page.
    """
    cfg = await ctx.connector("aws")
    _allowed(cfg, "bucket", bucket)

    def op(c):
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix or "", "MaxKeys": _lim(limit, 1000), "Delimiter": "/"}
        if continuation_token:
            kw["ContinuationToken"] = continuation_token
        return c.list_objects_v2(**kw)

    data, _ = await _aws_call(ctx, "s3", op)
    return H.compact({
        "bucket": bucket, "prefix": prefix,
        "folders": [p["Prefix"] for p in data.get("CommonPrefixes", [])] or None,
        "objects": [{"key": o["Key"], "size": o.get("Size"), "modified": _iso(o.get("LastModified"))}
                    for o in data.get("Contents", [])],
        "next_token": data.get("NextContinuationToken") if data.get("IsTruncated") else None,
    })


@aws.tool(risk=Risk.LOW, scope="write")
async def s3_get_object(ctx: ToolContext, bucket: str, key: str) -> dict:
    """Download an S3 object into the workspace `downloads/` folder (max 100 MB).

    Args:
        bucket: Bucket name.
        key: Object key.
    """
    cfg = await ctx.connector("aws")
    _allowed(cfg, "bucket", bucket)

    def op(c):
        head = c.head_object(Bucket=bucket, Key=key)
        if int(head.get("ContentLength") or 0) > H.DEFAULT_MAX_DOWNLOAD:
            raise ToolError("The object is larger than 100 MB.")
        obj = c.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read(), obj.get("ContentType")

    (data, ctype), _ = await _aws_call(ctx, "s3", op)
    saved = await H.save_bytes(ctx, data, key.rstrip("/").rsplit("/", 1)[-1] or "object", "downloads")
    return {**saved, "source": f"s3://{bucket}/{key}", "content_type": ctype}


@aws.tool(risk=Risk.HIGH, scope="write",
          preview=lambda a: H.preview("Upload to S3", [("Bucket", a.get("bucket")), ("Key", a.get("key")),
                                                       ("From workspace file", a.get("path")),
                                                       ("Content type", a.get("content_type"))],
                                      note="Overwrites the object if the key already exists."))
async def s3_put_object(ctx: ToolContext, bucket: str, key: str, path: str, content_type: str | None = None) -> dict:
    """Upload a workspace file to S3 (overwrites an existing object with the same key).

    Args:
        bucket: Bucket name.
        key: Destination object key, e.g. "reports/q3.pdf".
        path: Workspace file path, e.g. "output/q3.pdf".
        content_type: MIME type (default: guessed from the file name).
    """
    cfg = await ctx.connector("aws")
    _allowed(cfg, "bucket", bucket)
    data, _, mime = await H.read_workspace_file(ctx, path, max_bytes=500 * 1024 * 1024)
    res, _ = await _aws_call(ctx, "s3", lambda c: c.put_object(Bucket=bucket, Key=key, Body=data,
                                                              ContentType=content_type or mime))
    return H.compact({"uploaded": True, "uri": f"s3://{bucket}/{key}", "size": len(data),
                      "etag": str(res.get("ETag") or "").strip('"'), "version_id": res.get("VersionId")})


def _instance(i: dict) -> dict:
    name = next((t["Value"] for t in i.get("Tags") or [] if t.get("Key") == "Name"), None)
    return H.compact({"id": i.get("InstanceId"), "name": name, "type": i.get("InstanceType"),
                      "state": (i.get("State") or {}).get("Name"), "az": (i.get("Placement") or {}).get("AvailabilityZone"),
                      "private_ip": i.get("PrivateIpAddress"), "public_ip": i.get("PublicIpAddress"),
                      "launched": _iso(i.get("LaunchTime"))})


@aws.tool(risk=Risk.LOW, scope="read")
async def ec2_list_instances(ctx: ToolContext, state: str | None = None, name: str | None = None, limit: int = 50) -> dict:
    """List EC2 instances in the connector's region.

    Args:
        state: Filter by state: pending, running, stopping, stopped, terminated.
        name: Filter by Name tag (wildcards allowed, e.g. "web-*").
        limit: Maximum instances.
    """
    filters = []
    if state:
        filters.append({"Name": "instance-state-name", "Values": [state]})
    if name:
        filters.append({"Name": "tag:Name", "Values": [name]})

    def op(c):
        out = []
        for page in c.get_paginator("describe_instances").paginate(Filters=filters):
            for r in page.get("Reservations", []):
                out += r.get("Instances", [])
            if len(out) >= limit:
                break
        return out

    data, _ = await _aws_call(ctx, "ec2", op)
    rows = [_instance(i) for i in data[:_lim(limit, 500)]]
    return {"count": len(rows), "instances": rows}


def _ec2_preview(action: str):
    return lambda a: H.preview(f"{action} EC2 instances", [("Instances", H.as_list(a.get("instance_ids"))),
                                                           ("Force", a.get("force") or None)],
                               note="This changes running infrastructure.")


async def _ec2_change(ctx: ToolContext, instance_ids: list[str], action: str, force: bool = False) -> dict:
    ids = H.as_list(instance_ids)
    if not ids:
        raise ToolError("Pass at least one instance id.")
    cfg = await ctx.connector("aws")
    for iid in ids:
        if not re.fullmatch(r"i-[0-9a-f]{8,17}", iid):
            raise ToolError(f"'{iid}' is not an EC2 instance id.")
        _allowed(cfg, "instance", iid)

    def op(c):
        if action == "start":
            return c.start_instances(InstanceIds=ids).get("StartingInstances", [])
        return c.stop_instances(InstanceIds=ids, Force=force).get("StoppingInstances", [])

    data, _ = await _aws_call(ctx, "ec2", op)
    return {"action": action, "instances": [{"id": s.get("InstanceId"),
                                             "previous": (s.get("PreviousState") or {}).get("Name"),
                                             "current": (s.get("CurrentState") or {}).get("Name")} for s in data]}


@aws.tool(risk=Risk.CRITICAL, scope="admin", preview=_ec2_preview("START"))
async def ec2_start(ctx: ToolContext, instance_ids: list[str]) -> dict:
    """Start stopped EC2 instances.

    Args:
        instance_ids: Instance ids, e.g. ["i-0abc123def4567890"].
    """
    return await _ec2_change(ctx, instance_ids, "start")


@aws.tool(risk=Risk.CRITICAL, scope="admin", preview=_ec2_preview("STOP"))
async def ec2_stop(ctx: ToolContext, instance_ids: list[str], force: bool = False) -> dict:
    """Stop running EC2 instances.

    Args:
        instance_ids: Instance ids.
        force: Force stop (no graceful OS shutdown).
    """
    return await _ec2_change(ctx, instance_ids, "stop", force)


@aws.tool(risk=Risk.LOW, scope="read")
async def lambda_list(ctx: ToolContext, limit: int = 50) -> dict:
    """List Lambda functions (filtered by the connector's function allowlist, if set).

    Args:
        limit: Maximum functions.
    """
    def op(c):
        out = []
        for page in c.get_paginator("list_functions").paginate():
            out += page.get("Functions", [])
            if len(out) >= limit:
                break
        return out

    data, cfg = await _aws_call(ctx, "lambda", op)
    rows = [H.compact({"name": f.get("FunctionName"), "runtime": f.get("Runtime"), "memory_mb": f.get("MemorySize"),
                       "timeout_s": f.get("Timeout"), "modified": f.get("LastModified"),
                       "description": H.clip(f.get("Description"), 160)})
            for f in data if _is_allowed(cfg, "function", f.get("FunctionName", ""))][:_lim(limit, 500)]
    return {"count": len(rows), "functions": rows}


@aws.tool(risk=Risk.HIGH, scope="write", content_fields=("payload",),
          preview=lambda a: H.preview("Invoke Lambda function", [("Function", a.get("function"))],
                                      a.get("payload") or "{}", body_label="Payload (JSON)"))
async def lambda_invoke(ctx: ToolContext, function: str, payload: str | None = None) -> dict:
    """Invoke a Lambda function synchronously and return its response and log tail.

    Args:
        function: Function name or ARN.
        payload: JSON payload as a string, e.g. '{"report": "weekly"}'.
    """
    cfg = await ctx.connector("aws")
    _allowed(cfg, "function", function.rsplit(":function:", 1)[-1].split(":")[0])
    try:
        body = json.dumps(json.loads(payload)) if payload else "{}"
    except json.JSONDecodeError as e:
        raise ToolError(f"payload must be valid JSON: {e}") from e

    def op(c):
        res = c.invoke(FunctionName=function, InvocationType="RequestResponse", LogType="Tail", Payload=body.encode())
        return {"status": res.get("StatusCode"), "error": res.get("FunctionError"),
                "payload": res["Payload"].read().decode("utf-8", "replace") if res.get("Payload") else "",
                "log": base64.b64decode(res.get("LogResult") or b"").decode("utf-8", "replace")}

    data, _ = await _aws_call(ctx, "lambda", op)
    return H.compact({"function": function, "status": data["status"], "function_error": data["error"],
                      "response": H.clip(data["payload"], 8000), "log_tail": H.clip(data["log"][-3000:], 3000)})


@aws.tool(risk=Risk.LOW, scope="read")
async def cloudwatch_logs_query(ctx: ToolContext, log_group: str, query: str = "", minutes: int = 60,
                                limit: int = 100) -> dict:
    """Search CloudWatch Logs with Logs Insights (plain words become a message filter).

    Args:
        log_group: Log group name, e.g. "/aws/lambda/my-fn" (comma-separate several).
        query: Logs Insights query (e.g. "fields @timestamp, @message | filter @message like /ERROR/") or plain words.
        minutes: Look-back window in minutes.
        limit: Maximum rows (1-1000).
    """
    q = query.strip()
    lim = _lim(limit, 1000)
    if not q:
        q = f"fields @timestamp, @message | sort @timestamp desc | limit {lim}"
    elif "|" not in q and not q.startswith(("fields", "filter", "stats", "parse", "sort", "display")):
        q = f"fields @timestamp, @message | filter @message like /{re.escape(q)}/ | sort @timestamp desc | limit {lim}"
    groups = [g.strip() for g in log_group.split(",") if g.strip()]
    end = int(time.time())
    start = end - max(1, min(int(minutes or 60), 60 * 24 * 14)) * 60

    def op(c):
        qid = c.start_query(logGroupNames=groups, startTime=start, endTime=end, queryString=q, limit=lim)["queryId"]
        deadline = time.time() + 40
        while time.time() < deadline:
            res = c.get_query_results(queryId=qid)
            if res.get("status") in ("Complete", "Failed", "Cancelled", "Timeout"):
                return res
            time.sleep(1.0)
        c.stop_query(queryId=qid)
        raise ToolError("The Logs Insights query took too long — narrow the time range or query.")

    res, _ = await _aws_call(ctx, "logs", op)
    if res.get("status") != "Complete":
        raise ToolError(f"Logs Insights query {str(res.get('status')).lower()}.")
    rows = [{f["field"]: H.clip(f.get("value"), 2000) for f in row if f.get("field") != "@ptr"}
            for row in res.get("results", [])]
    stats = res.get("statistics") or {}
    return {"query": q, "count": len(rows), "rows": rows, "records_scanned": stats.get("recordsScanned")}


@aws.tool(risk=Risk.LOW, scope="read")
async def ecs_services(ctx: ToolContext, cluster: str | None = None, limit: int = 50) -> dict:
    """List ECS services with desired/running task counts and deployment status.

    Args:
        cluster: Cluster name/ARN (default: all clusters, up to 5).
        limit: Maximum services.
    """
    def op(c):
        clusters = [cluster] if cluster else c.list_clusters(maxResults=5).get("clusterArns", [])
        out = []
        for cl in clusters:
            arns = c.list_services(cluster=cl, maxResults=min(100, limit)).get("serviceArns", [])
            for i in range(0, len(arns), 10):
                for s in c.describe_services(cluster=cl, services=arns[i:i + 10]).get("services", []):
                    out.append({"cluster": cl.rsplit("/", 1)[-1], **H.compact({
                        "name": s.get("serviceName"), "status": s.get("status"), "desired": s.get("desiredCount"),
                        "running": s.get("runningCount"), "pending": s.get("pendingCount"),
                        "launch_type": s.get("launchType"),
                        "task_definition": (s.get("taskDefinition") or "").rsplit("/", 1)[-1],
                        "deployments": len(s.get("deployments") or []),
                        "rollout": ((s.get("deployments") or [{}])[0]).get("rolloutState")})})
            if len(out) >= limit:
                break
        return out[:limit]

    rows, _ = await _aws_call(ctx, "ecs", op)
    return {"count": len(rows), "services": rows}


@aws.tool(risk=Risk.LOW, scope="read")
async def rds_instances(ctx: ToolContext, limit: int = 50) -> dict:
    """List RDS database instances (engine, class, status, endpoint, storage).

    Args:
        limit: Maximum instances.
    """
    def op(c):
        return c.describe_db_instances(MaxRecords=max(20, min(100, limit))).get("DBInstances", [])

    data, _ = await _aws_call(ctx, "rds", op)
    rows = [H.compact({"id": d.get("DBInstanceIdentifier"), "engine": d.get("Engine"), "version": d.get("EngineVersion"),
                       "class": d.get("DBInstanceClass"), "status": d.get("DBInstanceStatus"),
                       "multi_az": d.get("MultiAZ") or None, "storage_gb": d.get("AllocatedStorage"),
                       "endpoint": f"{(d.get('Endpoint') or {}).get('Address')}:{(d.get('Endpoint') or {}).get('Port')}"
                       if d.get("Endpoint") else None, "az": d.get("AvailabilityZone")})
            for d in data[:_lim(limit, 100)]]
    return {"count": len(rows), "instances": rows}


@aws.tool(risk=Risk.LOW, scope="read")
async def cost_summary(ctx: ToolContext, days: int = 30, granularity: Literal["MONTHLY", "DAILY"] = "MONTHLY",
                       group_by: Literal["SERVICE", "LINKED_ACCOUNT", "REGION", "USAGE_TYPE"] = "SERVICE",
                       top: int = 10) -> dict:
    """Summarize AWS spend from Cost Explorer (unblended cost), grouped by service (each call costs $0.01).

    Args:
        days: Look-back window in days.
        granularity: MONTHLY or DAILY buckets.
        group_by: Dimension to group by.
        top: Groups to list per period.
    """
    end = datetime.now(UTC).date()
    start = end - timedelta(days=max(1, min(int(days or 30), 365)))

    def op(c):
        return c.get_cost_and_usage(TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                                    Granularity=granularity, Metrics=["UnblendedCost"],
                                    GroupBy=[{"Type": "DIMENSION", "Key": group_by}])

    data, _ = await _aws_call(ctx, "ce", op, region="us-east-1")
    periods, grand, unit = [], 0.0, "USD"
    totals: dict[str, float] = {}
    for r in data.get("ResultsByTime", []):
        groups = []
        for g in r.get("Groups", []):
            m = (g.get("Metrics") or {}).get("UnblendedCost") or {}
            amt = float(m.get("Amount") or 0)
            unit = m.get("Unit") or unit
            key = (g.get("Keys") or ["?"])[0]
            totals[key] = totals.get(key, 0.0) + amt
            groups.append((key, amt))
        groups.sort(key=lambda x: -x[1])
        period_total = sum(a for _, a in groups)
        grand += period_total
        periods.append({"start": (r.get("TimePeriod") or {}).get("Start"), "total": round(period_total, 2),
                        "top": [{"name": k, "cost": round(a, 2)} for k, a in groups[:max(1, min(top, 50))] if a >= 0.005]})
    top_overall = sorted(totals.items(), key=lambda x: -x[1])[:max(1, min(top, 50))]
    return {"currency": unit, "range": {"start": start.isoformat(), "end": end.isoformat()}, "total": round(grand, 2),
            "top": [{"name": k, "cost": round(a, 2)} for k, a in top_overall],
            "periods": periods if granularity == "MONTHLY" else periods[-31:]}


# =========================================================================== Azure (ARM REST)
azure = ToolGroup(
    "azure", "Azure",
    "Read-only views of an Azure subscription: resource groups, resources, VMs, storage containers/blobs and "
    "Azure Monitor metrics.",
    prefix="azure", icon="cloud", category="infrastructure", connector_kinds=("azure",), default_enabled=False,
)
ARM = "https://management.azure.com"


def _arm(cfg: dict, scope: str = "https://management.azure.com/.default") -> H.RestClient:
    async def auth() -> dict[str, str]:
        return {"Authorization": f"Bearer {await microsoft_client_credentials(cfg, scope, service='Azure')}"}

    return H.RestClient("Azure", ARM, auth=auth, on_unauthorized=lambda: invalidate(cfg), reconnect="Azure")


async def _azure(ctx: ToolContext) -> tuple[H.RestClient, dict, str]:
    cfg = await ctx.connector("azure")
    H.need(cfg, ("tenant_id", "client_id", "client_secret", "subscription_id"), "Azure")
    return _arm(cfg), cfg, f"/subscriptions/{quote(str(cfg['subscription_id']).strip(), safe='')}"


def _rg_of(resource_id: str) -> str | None:
    m = re.search(r"/resourceGroups/([^/]+)", resource_id or "", re.I)
    return m.group(1) if m else None


async def _arm_list(api: H.RestClient, path: str, params: dict, limit: int) -> list[dict]:
    out: list[dict] = []
    url: str | None = path
    p: dict | None = params
    while url and len(out) < limit:
        data = await api.get(url, params=p) or {}
        out += data.get("value") or []
        url, p = data.get("nextLink"), None
    return out[:limit]


@azure.tool(risk=Risk.LOW, scope="read")
async def list_resource_groups(ctx: ToolContext, limit: int = 100) -> dict:
    """List the subscription's resource groups.

    Args:
        limit: Maximum groups.
    """
    api, _, sub = await _azure(ctx)
    rows = await _arm_list(api, f"{sub}/resourcegroups", {"api-version": "2021-04-01"}, _lim(limit, 1000))
    return {"count": len(rows), "resource_groups": [H.compact({
        "name": g.get("name"), "location": g.get("location"),
        "state": (g.get("properties") or {}).get("provisioningState"), "tags": g.get("tags") or None}) for g in rows]}


@azure.tool(risk=Risk.LOW, scope="read")
async def list_resources(ctx: ToolContext, resource_group: str | None = None, resource_type: str | None = None,
                         limit: int = 100) -> dict:
    """List resources in the subscription or one resource group.

    Args:
        resource_group: Limit to this resource group.
        resource_type: Filter by type, e.g. "Microsoft.Web/sites".
        limit: Maximum resources.
    """
    api, _, sub = await _azure(ctx)
    path = f"{sub}/resourceGroups/{quote(resource_group, safe='')}/resources" if resource_group else f"{sub}/resources"
    params: dict[str, Any] = {"api-version": "2021-04-01", "$top": min(_lim(limit, 1000), 1000)}
    if resource_type:
        params["$filter"] = f"resourceType eq '{resource_type.replace(chr(39), '')}'"
    rows = await _arm_list(api, path, params, _lim(limit, 1000))
    return {"count": len(rows), "resources": [H.compact({
        "name": r.get("name"), "type": r.get("type"), "location": r.get("location"),
        "resource_group": _rg_of(r.get("id", "")), "sku": (r.get("sku") or {}).get("name"), "id": r.get("id")})
        for r in rows]}


@azure.tool(risk=Risk.LOW, scope="read")
async def vm_list(ctx: ToolContext, resource_group: str | None = None, limit: int = 100) -> dict:
    """List virtual machines with size, OS and power state.

    Args:
        resource_group: Limit to this resource group.
        limit: Maximum VMs.
    """
    api, _, sub = await _azure(ctx)
    base = f"{sub}/resourceGroups/{quote(resource_group, safe='')}" if resource_group else sub
    rows = await _arm_list(api, f"{base}/providers/Microsoft.Compute/virtualMachines",
                           {"api-version": "2024-07-01", "statusOnly": "true"}, _lim(limit, 1000))
    out = []
    for v in rows:
        props = v.get("properties") or {}
        statuses = ((props.get("instanceView") or {}).get("statuses") or [])
        power = next((s.get("code", "").split("/", 1)[-1] for s in statuses if str(s.get("code", "")).startswith("PowerState/")), None)
        out.append(H.compact({"name": v.get("name"), "resource_group": _rg_of(v.get("id", "")), "location": v.get("location"),
                              "size": (props.get("hardwareProfile") or {}).get("vmSize"),
                              "os": ((props.get("storageProfile") or {}).get("osDisk") or {}).get("osType"),
                              "power_state": power, "provisioning": props.get("provisioningState")}))
    return {"count": len(out), "vms": out}


async def _storage_account_id(api: H.RestClient, sub: str, account: str, resource_group: str | None) -> str:
    if resource_group:
        return f"{sub}/resourceGroups/{quote(resource_group, safe='')}/providers/Microsoft.Storage/storageAccounts/{account}"
    rows = await _arm_list(api, f"{sub}/providers/Microsoft.Storage/storageAccounts", {"api-version": "2023-05-01"}, 1000)
    for a in rows:
        if (a.get("name") or "").lower() == account.lower():
            return a["id"]
    raise ToolError(f"Storage account '{account}' not found in the subscription.")


def _check_account(account: str) -> str:
    a = (account or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]{3,24}", a):
        raise ToolError("Storage account names are 3-24 lowercase letters and digits.")
    return a


@azure.tool(risk=Risk.LOW, scope="read")
async def storage_list_containers(ctx: ToolContext, account: str, resource_group: str | None = None,
                                  limit: int = 100) -> dict:
    """List blob containers of a storage account.

    Args:
        account: Storage account name.
        resource_group: The account's resource group (looked up if omitted).
        limit: Maximum containers.
    """
    api, _, sub = await _azure(ctx)
    acc_id = await _storage_account_id(api, sub, _check_account(account), resource_group)
    rows = await _arm_list(api, f"{acc_id}/blobServices/default/containers", {"api-version": "2023-05-01"},
                           _lim(limit, 1000))
    return {"account": account, "count": len(rows), "containers": [H.compact({
        "name": c.get("name"), "public_access": (c.get("properties") or {}).get("publicAccess"),
        "modified": (c.get("properties") or {}).get("lastModifiedTime"),
        "lease": (c.get("properties") or {}).get("leaseState")}) for c in rows]}


@azure.tool(risk=Risk.LOW, scope="read")
async def storage_list_blobs(ctx: ToolContext, account: str, container: str, prefix: str | None = None,
                             limit: int = 100) -> dict:
    """List blobs in a container (needs the 'Storage Blob Data Reader' role for the app).

    Args:
        account: Storage account name.
        container: Container name.
        prefix: Blob name prefix ("folder/").
        limit: Maximum blobs (1-5000).
    """
    cfg = await ctx.connector("azure")
    acc = _check_account(account)
    if not re.fullmatch(r"\$root|[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?", container or ""):
        raise ToolError("Invalid container name.")
    token = await microsoft_client_credentials(cfg, "https://storage.azure.com/.default", service="Azure Storage")
    resp = await H.api("GET", f"https://{acc}.blob.core.windows.net/{container}", service="Azure Storage",
                       reconnect="Azure", headers={"Authorization": f"Bearer {token}", "x-ms-version": "2023-11-03"},
                       params={"restype": "container", "comp": "list", "prefix": prefix or None,
                               "maxresults": _lim(limit, 5000), "delimiter": "/"}, raw=True)
    try:
        root = ET.fromstring(resp.content)  # bytes: Azure prefixes the XML with a UTF-8 BOM
    except ET.ParseError as e:
        raise ToolError("Azure Storage returned an unreadable listing.") from e
    blobs = []
    for b in root.iter("Blob"):
        props = b.find("Properties")
        blobs.append(H.compact({"name": b.findtext("Name"),
                                "size": int(props.findtext("Content-Length") or 0) if props is not None else None,
                                "modified": props.findtext("Last-Modified") if props is not None else None,
                                "type": props.findtext("Content-Type") if props is not None else None}))
    folders = [p.findtext("Name") for p in root.iter("BlobPrefix")]
    return H.compact({"account": acc, "container": container, "folders": folders or None, "blobs": blobs,
                      "next_marker": root.findtext("NextMarker") or None})


@azure.tool(risk=Risk.LOW, scope="read")
async def monitor_metrics(ctx: ToolContext, resource_id: str, metrics: str = "Percentage CPU", hours: int = 24,
                          interval: str = "PT1H", aggregation: str = "Average") -> dict:
    """Get Azure Monitor metrics for a resource (time series plus min/avg/max).

    Args:
        resource_id: Full resource id ("/subscriptions/.../resourceGroups/.../providers/...").
        metrics: Comma-separated metric names, e.g. "Percentage CPU,Network In Total" or "Requests,Http5xx".
        hours: Look-back window in hours.
        interval: ISO 8601 granularity: PT1M, PT5M, PT15M, PT1H, P1D.
        aggregation: Average, Total, Maximum, Minimum or Count.
    """
    api, _, _ = await _azure(ctx)
    if not re.match(r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/", resource_id or "", re.I):
        raise ToolError("resource_id must be a full Azure resource id (see azure_list_resources).")
    end = datetime.now(UTC)
    start = end - timedelta(hours=max(1, min(int(hours or 24), 24 * 31)))
    data = await api.get(f"{resource_id}/providers/Microsoft.Insights/metrics", params={
        "api-version": "2023-10-01", "metricnames": metrics, "timespan": f"{H.iso(start)}/{H.iso(end)}",
        "interval": interval, "aggregation": aggregation}) or {}
    key = aggregation.lower()
    out = []
    for m in data.get("value", []):
        points = []
        for ts in m.get("timeseries") or []:
            for d in ts.get("data") or []:
                if d.get(key) is not None:
                    points.append({"time": d.get("timeStamp"), "value": round(float(d[key]), 4)})
        vals = [p["value"] for p in points]
        out.append(H.compact({"metric": (m.get("name") or {}).get("value"), "unit": m.get("unit"),
                              "min": min(vals) if vals else None, "avg": round(sum(vals) / len(vals), 4) if vals else None,
                              "max": max(vals) if vals else None, "points": points[-60:]}))
    return {"resource": resource_id.rsplit("/", 1)[-1], "aggregation": aggregation, "interval": interval, "metrics": out}


# =========================================================================== Google Cloud (REST)
gcp = ToolGroup(
    "gcp", "Google Cloud",
    "Read-only views of a Google Cloud project: Cloud Storage buckets/objects, Compute Engine instances, Cloud Run "
    "services and Cloud Logging queries.",
    prefix="gcp", icon="cloud", category="infrastructure", connector_kinds=("gcp",), default_enabled=False,
)
GCP_SCOPES = ("https://www.googleapis.com/auth/cloud-platform.read-only",)


async def _gcp(ctx: ToolContext) -> tuple[H.RestClient, str, dict]:
    cfg = await ctx.connector("gcp")
    info = load_service_account(cfg, service="Google Cloud")
    project = str(cfg.get("project_id") or info.get("project_id") or "").strip()
    if not project:
        raise ToolError("Set the project ID in the Google Cloud connector.")

    async def auth() -> dict[str, str]:
        return {"Authorization": f"Bearer {await google_service_account_token(cfg, GCP_SCOPES, service='Google Cloud')}"}

    return H.RestClient("Google Cloud", "", auth=auth, on_unauthorized=lambda: invalidate(cfg)), project, cfg


@gcp.tool(risk=Risk.LOW, scope="read")
async def storage_list_buckets(ctx: ToolContext, limit: int = 100) -> dict:
    """List Cloud Storage buckets in the project.

    Args:
        limit: Maximum buckets.
    """
    api, project, _ = await _gcp(ctx)
    data = await api.get("https://storage.googleapis.com/storage/v1/b",
                         params={"project": project, "maxResults": _lim(limit, 1000)}) or {}
    return {"project": project, "buckets": [H.compact({"name": b.get("name"), "location": b.get("location"),
                                                       "class": b.get("storageClass"), "created": b.get("timeCreated")})
                                            for b in data.get("items", [])]}


@gcp.tool(risk=Risk.LOW, scope="read")
async def storage_list_objects(ctx: ToolContext, bucket: str, prefix: str | None = None, limit: int = 100,
                               page_token: str | None = None) -> dict:
    """List objects (and sub-folders) in a Cloud Storage bucket.

    Args:
        bucket: Bucket name.
        prefix: Object name prefix ("folder/").
        limit: Maximum objects.
        page_token: Token from a previous call for the next page.
    """
    api, _, _ = await _gcp(ctx)
    data = await api.get(f"https://storage.googleapis.com/storage/v1/b/{quote(bucket, safe='')}/o", params={
        "prefix": prefix or None, "maxResults": _lim(limit, 1000), "delimiter": "/", "pageToken": page_token}) or {}
    return H.compact({"bucket": bucket, "folders": data.get("prefixes") or None,
                      "objects": [H.compact({"name": o.get("name"), "size": int(o.get("size") or 0),
                                             "updated": o.get("updated"), "type": o.get("contentType")})
                                  for o in data.get("items", [])],
                      "next_page_token": data.get("nextPageToken")})


@gcp.tool(risk=Risk.LOW, scope="read")
async def compute_list_instances(ctx: ToolContext, zone: str | None = None, limit: int = 100) -> dict:
    """List Compute Engine VM instances (all zones unless one is given).

    Args:
        zone: Zone, e.g. "us-central1-a".
        limit: Maximum instances.
    """
    api, project, _ = await _gcp(ctx)
    base = f"https://compute.googleapis.com/compute/v1/projects/{quote(project, safe='')}"
    if zone:
        data = await api.get(f"{base}/zones/{quote(zone, safe='')}/instances", params={"maxResults": _lim(limit, 500)}) or {}
        items = data.get("items") or []
    else:
        data = await api.get(f"{base}/aggregated/instances", params={"maxResults": _lim(limit, 500),
                                                                     "returnPartialSuccess": True}) or {}
        items = [i for scope in (data.get("items") or {}).values() for i in scope.get("instances") or []]
    rows = []
    for i in items[:_lim(limit, 500)]:
        nic = (i.get("networkInterfaces") or [{}])[0]
        rows.append(H.compact({"name": i.get("name"), "zone": (i.get("zone") or "").rsplit("/", 1)[-1],
                               "status": i.get("status"), "machine_type": (i.get("machineType") or "").rsplit("/", 1)[-1],
                               "internal_ip": nic.get("networkIP"),
                               "external_ip": ((nic.get("accessConfigs") or [{}])[0]).get("natIP"),
                               "created": i.get("creationTimestamp")}))
    return {"project": project, "count": len(rows), "instances": rows}


@gcp.tool(risk=Risk.LOW, scope="read")
async def cloudrun_services(ctx: ToolContext, region: str | None = None, limit: int = 100) -> dict:
    """List Cloud Run services with URL, readiness and latest revision (all regions unless one is given).

    Args:
        region: Region, e.g. "europe-west1".
        limit: Maximum services.
    """
    api, project, _ = await _gcp(ctx)
    rows = []
    if region:
        data = await api.get(f"https://run.googleapis.com/v2/projects/{quote(project, safe='')}/locations/"
                             f"{quote(region, safe='')}/services", params={"pageSize": _lim(limit, 500)}) or {}
        for s in data.get("services", []):
            ready = next((c.get("state") for c in s.get("conditions") or [] if c.get("type") == "Ready"),
                         (s.get("terminalCondition") or {}).get("state"))
            rows.append(H.compact({"name": (s.get("name") or "").rsplit("/", 1)[-1], "region": region,
                                   "url": s.get("uri"), "ready": ready,
                                   "latest_revision": (s.get("latestReadyRevision") or "").rsplit("/", 1)[-1],
                                   "updated": s.get("updateTime")}))
    else:  # the v1 global endpoint lists services across all regions
        data = await api.get(f"https://run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{quote(project, safe='')}/services",
                             params={"limit": _lim(limit, 500)}) or {}
        for s in data.get("items", []):
            meta, status = s.get("metadata") or {}, s.get("status") or {}
            ready = next((c.get("status") for c in status.get("conditions") or [] if c.get("type") == "Ready"), None)
            rows.append(H.compact({"name": meta.get("name"), "region": (meta.get("labels") or {}).get("cloud.googleapis.com/location"),
                                   "url": status.get("url"), "ready": ready,
                                   "latest_revision": status.get("latestReadyRevisionName"),
                                   "created": meta.get("creationTimestamp")}))
    return {"project": project, "count": len(rows), "services": rows[:_lim(limit, 500)]}


@gcp.tool(risk=Risk.LOW, scope="read")
async def logging_query(ctx: ToolContext, filter: str = "severity>=ERROR", hours: int = 24, limit: int = 50) -> dict:
    """Query Cloud Logging entries (newest first).

    Args:
        filter: Logging query language filter, e.g. 'resource.type="cloud_run_revision" AND severity>=ERROR'.
        hours: Look-back window in hours.
        limit: Maximum entries (1-1000).
    """
    api, project, _ = await _gcp(ctx)
    since = H.iso(datetime.now(UTC) - timedelta(hours=max(1, min(int(hours or 24), 24 * 30))))
    flt = f'({filter}) AND timestamp >= "{since}"' if filter.strip() else f'timestamp >= "{since}"'
    data = await api.post("https://logging.googleapis.com/v2/entries:list", json={
        "resourceNames": [f"projects/{project}"], "filter": flt, "orderBy": "timestamp desc",
        "pageSize": _lim(limit, 1000)}) or {}
    rows = []
    for e in data.get("entries", []):
        payload = e.get("textPayload")
        if payload is None and e.get("jsonPayload") is not None:
            jp = e["jsonPayload"]
            payload = jp.get("message") or jp.get("msg") or json.dumps(jp, ensure_ascii=False)
        if payload is None and e.get("protoPayload") is not None:
            pp = e["protoPayload"]
            payload = f"{pp.get('methodName', '')} {(pp.get('status') or {}).get('message', '')}".strip()
        res = e.get("resource") or {}
        rows.append(H.compact({"time": e.get("timestamp"), "severity": e.get("severity"),
                               "resource": res.get("type"),
                               "service": (res.get("labels") or {}).get("service_name")
                               or (res.get("labels") or {}).get("function_name"),
                               "log": (e.get("logName") or "").rsplit("/", 1)[-1],
                               "message": H.clip(payload, 1500),
                               "http_status": (e.get("httpRequest") or {}).get("status")}))
    return {"project": project, "filter": flt, "count": len(rows), "entries": rows}


# =========================================================================== connector kinds
@H.connector_test
async def _test_aws(cfg: dict) -> tuple[bool, str]:
    try:
        _boto3()
    except ToolError as e:
        return False, str(e)
    session = await _aws_session(cfg)
    try:
        ident = await asyncio.to_thread(lambda: session.client("sts").get_caller_identity())
    except Exception as e:  # noqa: BLE001
        raise _aws_error(e) from e
    return True, f"Connected to AWS account {ident.get('Account')} as {ident.get('Arn')} ({session.region_name})."


@H.connector_test
async def _test_azure(cfg: dict) -> tuple[bool, str]:
    H.need(cfg, ("tenant_id", "client_id", "client_secret", "subscription_id"), "Azure")
    sub = await _arm(cfg).get(f"/subscriptions/{quote(str(cfg['subscription_id']).strip(), safe='')}",
                              params={"api-version": "2022-12-01"}) or {}
    return True, f"Connected to Azure subscription '{sub.get('displayName')}' ({sub.get('state')})."


@H.connector_test
async def _test_gcp(cfg: dict) -> tuple[bool, str]:
    info = load_service_account(cfg, service="Google Cloud")
    project = str(cfg.get("project_id") or info.get("project_id") or "").strip()
    token = await google_service_account_token(cfg, GCP_SCOPES, service="Google Cloud")
    try:
        data = await H.api("GET", f"https://cloudresourcemanager.googleapis.com/v1/projects/{quote(project, safe='')}",
                           service="Google Cloud", headers={"Authorization": f"Bearer {token}"}) or {}
        return True, f"Connected to Google Cloud project '{data.get('name') or project}' as {info.get('client_email')}."
    except H.ApiError as e:
        if e.status != 403:
            raise
        return True, f"Service account {info.get('client_email')} authenticated (project metadata not readable)."


register_connector_kind(ConnectorKindSpec(
    kind="aws", name="AWS", description="Amazon Web Services: S3, EC2, Lambda, CloudWatch, ECS, RDS, Cost Explorer.",
    icon="cloud", category="infrastructure", tool_groups=["aws"],
    fields=[
        ConnectorField("access_key_id", "Access key ID", required=True, placeholder="AKIA…"),
        ConnectorField("secret_access_key", "Secret access key", type="password", required=True, secret=True),
        ConnectorField("region", "Region", placeholder="us-east-1"),
        ConnectorField("session_token", "Session token (temporary credentials)", type="password", secret=True),
        ConnectorField("role_arn", "Role to assume (optional)", placeholder="arn:aws:iam::123456789012:role/ren-readonly",
                       help="Recommended: a least-privilege role; the key above only needs sts:AssumeRole."),
        ConnectorField("external_id", "External ID for the role (optional)"),
        ConnectorField("allowed_buckets", "Allowed S3 buckets (optional)", placeholder="reports-*, ren-exports",
                       help="Comma-separated names/wildcards. Empty = whatever the IAM policy allows."),
        ConnectorField("allowed_functions", "Allowed Lambda functions (optional)", placeholder="report-*"),
        ConnectorField("allowed_instances", "Allowed EC2 instance ids (optional)", placeholder="i-0abc…, i-0def…"),
    ],
    docs_url="https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_access-keys.html", test=_test_aws,
))

register_connector_kind(ConnectorKindSpec(
    kind="azure", name="Azure", description="Microsoft Azure subscription (read-only ARM + Monitor + Blob listing).",
    icon="cloud", category="infrastructure", tool_groups=["azure"],
    fields=[
        ConnectorField("tenant_id", "Directory (tenant) ID", required=True),
        ConnectorField("client_id", "Application (client) ID", required=True),
        ConnectorField("client_secret", "Client secret", type="password", required=True, secret=True),
        ConnectorField("subscription_id", "Subscription ID", required=True,
                       help="Grant the app the Reader role on the subscription (plus Storage Blob Data Reader to "
                            "list blobs)."),
    ],
    docs_url="https://learn.microsoft.com/azure/active-directory/develop/howto-create-service-principal-portal",
    test=_test_azure,
))

register_connector_kind(ConnectorKindSpec(
    kind="gcp", name="Google Cloud", description="Google Cloud project (read-only Storage, Compute, Cloud Run, Logging).",
    icon="cloud", category="infrastructure", tool_groups=["gcp"],
    fields=[
        ConnectorField("service_account_json", "Service account key (JSON)", type="textarea", required=True, secret=True,
                       help="IAM → Service accounts → Keys → Add key (JSON). Grant roles/viewer and roles/logging.viewer."),
        ConnectorField("project_id", "Project ID (optional)", help="Defaults to the key's project."),
    ],
    docs_url="https://cloud.google.com/iam/docs/keys-create-delete", test=_test_gcp,
))
