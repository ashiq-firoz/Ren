"""OAuth2 helpers for the integration tools.

* Access tokens are cached in memory per connector — keyed by the stored connector id, or by a hash of the
  credentials for ad-hoc configs (connection tests, test overrides) — and refreshed ~60 s before expiry.
* Google: refresh-token grant, or a service-account JWT (RS256, signed with `cryptography`) with optional
  domain-wide delegation (`delegated_user`).
* Microsoft identity platform: delegated refresh-token grant (rotated refresh tokens are kept in memory and
  persisted back into the connector's encrypted secrets) or app-only client credentials → Graph / Azure.
* Salesforce: refresh-token grant, client-credentials grant, or a raw access token.
* Dropbox (refresh token) and Box (client-credentials grant / developer token), plus a generic `bearer(cfg)`.

Tokens are never logged and never returned to the model.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from app.tools.base import ToolError
from app.tools.builtin import _http as H

log = logging.getLogger("ren.tools.oauth")

REFRESH_SKEW = 60.0  # refresh this many seconds before the provider's expiry
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
MS_LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
SF_LOGIN = "https://login.salesforce.com"

_cache: dict[str, tuple[Any, float]] = {}  # key -> (token or (token, extra), expires_at epoch seconds)
_rotated: dict[str, str] = {}  # identity -> latest refresh token (providers that rotate them)
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()

_AUTH_ERRORS = {"invalid_grant", "invalid_client", "unauthorized_client", "access_denied", "invalid_scope",
                "invalid_request", "expired_token", "invalid_token"}


def _now() -> float:  # indirection so tests can simulate the passage of time
    return time.time()


def identity(cfg: dict[str, Any]) -> str:
    """Stable cache identity of a connector config (connector id, else a hash of its fields)."""
    cid = cfg.get("_connector_id")
    if cid:
        return f"c:{cid}"
    material = json.dumps({k: v for k, v in cfg.items() if not str(k).startswith("_")}, sort_keys=True, default=str)
    return "h:" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _lock(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _locks.get(loop)
    if locks is None:
        locks = {}
        _locks[loop] = locks
    lk = locks.get(key)
    if lk is None:
        lk = locks[key] = asyncio.Lock()
    return lk


async def cached(cfg: dict[str, Any], provider: str, fetch: Callable[[], Awaitable[tuple[Any, Any]]], *,
                 variant: str = "", default_ttl: float = 3300.0) -> Any:
    """Return a cached token for (connector, provider, variant), calling `fetch()` → (token, expires_in) when the
    cached one is missing or within REFRESH_SKEW seconds of expiry."""
    key = f"{identity(cfg)}|{provider}|{variant}"
    hit = _cache.get(key)
    if hit and hit[1] - REFRESH_SKEW > _now():
        return hit[0]
    async with _lock(key):
        hit = _cache.get(key)
        if hit and hit[1] - REFRESH_SKEW > _now():
            return hit[0]
        value, ttl = await fetch()
        try:
            ttl_s = float(ttl) if ttl else default_ttl
        except (TypeError, ValueError):
            ttl_s = default_ttl
        _cache[key] = (value, _now() + max(ttl_s, REFRESH_SKEW + 5))
        return value


def invalidate(cfg: dict[str, Any], provider: str | None = None) -> None:
    """Forget cached access tokens of a connector (e.g. after a 401) so the next call refreshes."""
    prefix = identity(cfg) + "|" + (f"{provider}|" if provider else "")
    for k in [k for k in _cache if k.startswith(prefix)]:
        _cache.pop(k, None)


def clear_cache() -> None:
    _cache.clear()
    _rotated.clear()


def bearer(cfg: dict[str, Any], *keys: str, service: str = "the integration") -> dict[str, str]:
    """Authorization header from the first non-empty of `keys` (default: token, access_token, api_key)."""
    for k in keys or ("token", "access_token", "api_key"):
        v = str(cfg.get(k) or "").strip()
        if v:
            return {"Authorization": f"Bearer {v}"}
    raise ToolError(f"The {service} connector is missing its access token — reconnect it in Connectors.")


async def token_request(url: str, form: dict[str, Any], *, service: str, reconnect: str | None = None,
                        auth: tuple[str, str] | None = None) -> dict[str, Any]:
    """POST an OAuth token request (form-encoded) with small retries; friendly errors, no secrets in messages."""
    reconnect = reconnect or service
    form = {k: v for k, v in form.items() if v not in (None, "")}
    delay = 0.5
    resp: httpx.Response | None = None
    for attempt in range(3):
        try:
            kwargs: dict[str, Any] = {"data": form, "headers": {"Accept": "application/json"}}
            if auth:
                kwargs["auth"] = auth
            resp = await H.client().post(url, **kwargs)
        except (httpx.UnsupportedProtocol, httpx.InvalidURL) as e:
            raise ToolError(f"The {service} sign-in URL is invalid — check the connector settings.") from e
        except httpx.TransportError as e:
            if attempt < 2:
                await H._sleep(delay)
                delay *= 2
                continue
            raise ToolError(f"Could not reach the {service} sign-in service ({type(e).__name__}). "
                            "Try again shortly.") from e
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            await H._sleep(delay)
            delay *= 2
            continue
        break
    assert resp is not None
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code < 400 and isinstance(data, dict) and data.get("access_token"):
        return data
    err = str(data.get("error") or "") if isinstance(data, dict) else ""
    desc = ""
    if isinstance(data, dict):
        desc = str(data.get("error_description") or data.get("message") or "")
    desc = desc.splitlines()[0][:300] if desc else ""
    detail = ": ".join(p for p in (err, desc) if p)
    if resp.status_code in (400, 401, 403) and (err in _AUTH_ERRORS or resp.status_code == 401 or not err):
        raise ToolError(f"{service} credentials are invalid or expired — reconnect {reconnect} in Connectors"
                        + (f" ({detail})." if detail else "."))
    raise ToolError(f"{service} sign-in failed (HTTP {resp.status_code})" + (f": {detail}." if detail else "."))


# --------------------------------------------------------------------------- Google
def load_service_account(cfg: dict[str, Any], *, field: str = "service_account_json",
                         service: str = "Google Cloud") -> dict[str, Any]:
    raw = cfg.get(field)
    if isinstance(raw, dict):
        info = raw
    else:
        try:
            info = json.loads(str(raw or ""))
        except json.JSONDecodeError as e:
            raise ToolError(f"The {service} service-account key is not valid JSON — paste the whole key file.") from e
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        raise ToolError(f"The {service} service-account key must contain client_email and private_key.")
    return info


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_jwt_rs256(claims: dict[str, Any], private_key_pem: str, key_id: str | None = None) -> str:
    """Compact JWS (RS256) signed with an RSA private key in PEM form."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    header: dict[str, Any] = {"alg": "RS256", "typ": "JWT"}
    if key_id:
        header["kid"] = key_id
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
                     + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    try:
        key = serialization.load_pem_private_key(private_key_pem.replace("\\n", "\n").encode(), password=None)
    except (ValueError, TypeError) as e:
        raise ToolError("The service-account private key could not be loaded — paste the full JSON key file.") from e
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ToolError("The service-account private key must be an RSA key.")
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


async def google_service_account_token(cfg: dict[str, Any], scopes: Sequence[str], *, subject: str | None = None,
                                       service: str = "Google Cloud", field: str = "service_account_json") -> str:
    info = load_service_account(cfg, field=field, service=service)
    token_uri = str(info.get("token_uri") or GOOGLE_TOKEN_URL)
    if not token_uri.startswith("https://oauth2.googleapis.com/"):
        token_uri = GOOGLE_TOKEN_URL  # never send assertions anywhere else
    scope = " ".join(scopes)

    async def fetch() -> tuple[str, Any]:
        now = int(_now())
        claims: dict[str, Any] = {"iss": info["client_email"], "scope": scope, "aud": token_uri, "iat": now,
                                  "exp": now + 3600}
        if subject:
            claims["sub"] = subject
        assertion = sign_jwt_rs256(claims, str(info["private_key"]), info.get("private_key_id"))
        data = await token_request(token_uri, {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                               "assertion": assertion}, service=service)
        return data["access_token"], data.get("expires_in")

    return await cached(cfg, "google-sa", fetch, variant=f"{scope}|{subject or ''}")


async def google_token(cfg: dict[str, Any], *, service: str = "Google", scopes: Sequence[str] = ()) -> str:
    """Access token for Google APIs: refresh-token grant (client_id/client_secret/refresh_token), or a
    service account (+ delegated_user for Workspace domain-wide delegation), or a raw access_token."""
    if cfg.get("service_account_json"):
        return await google_service_account_token(
            cfg, scopes or ("https://www.googleapis.com/auth/cloud-platform",),
            subject=(str(cfg.get("delegated_user") or "").strip() or None), service=service)
    refresh = str(cfg.get("refresh_token") or "").strip()
    if not refresh:
        tok = str(cfg.get("access_token") or "").strip()
        if tok:
            return tok
        raise ToolError(f"The {service} connector has no refresh token — reconnect {service} in Connectors.")
    H.need(cfg, ("client_id", "client_secret"), service)

    async def fetch() -> tuple[str, Any]:
        data = await token_request(GOOGLE_TOKEN_URL, {
            "grant_type": "refresh_token", "client_id": str(cfg["client_id"]).strip(),
            "client_secret": str(cfg["client_secret"]).strip(), "refresh_token": refresh,
        }, service=service)
        return data["access_token"], data.get("expires_in")

    return await cached(cfg, "google", fetch)


async def google_headers(cfg: dict[str, Any], *, service: str = "Google", scopes: Sequence[str] = ()) -> dict[str, str]:
    return {"Authorization": f"Bearer {await google_token(cfg, service=service, scopes=scopes)}"}


def google_client(cfg: dict[str, Any], base_url: str, *, service: str, scopes: Sequence[str] = ()) -> H.RestClient:
    async def auth() -> dict[str, str]:
        return await google_headers(cfg, service=service, scopes=scopes)

    return H.RestClient(service, base_url, auth=auth, on_unauthorized=lambda: invalidate(cfg))


# --------------------------------------------------------------------------- Microsoft identity platform
def ms_delegated(cfg: dict[str, Any]) -> bool:
    return bool(str(cfg.get("refresh_token") or "").strip())


async def persist_secret(cfg: dict[str, Any], field: str, value: str) -> None:
    """Store a rotated secret (e.g. a new refresh token) back into the connector's encrypted secrets."""
    cid = cfg.get("_connector_id")
    if not cid:
        return
    try:
        from app.core.db import session_scope
        from app.core.security import decrypt_json, encrypt_json
        from app.models import Connector

        async with session_scope() as db:
            row = await db.get(Connector, cid)
            if row is None:
                return
            secrets = decrypt_json(row.secrets_enc)
            if secrets.get(field) == value:
                return
            secrets[field] = value
            row.secrets_enc = encrypt_json(secrets)
    except Exception:  # noqa: BLE001
        log.warning("could not persist a rotated %s for connector %s", field, cid)


async def microsoft_client_credentials(cfg: dict[str, Any], scope: str, *, service: str = "Microsoft") -> str:
    H.need(cfg, ("tenant_id", "client_id", "client_secret"), service)
    tenant = str(cfg["tenant_id"]).strip()

    async def fetch() -> tuple[str, Any]:
        data = await token_request(f"{MS_LOGIN}/{quote(tenant)}/oauth2/v2.0/token", {
            "grant_type": "client_credentials", "client_id": str(cfg["client_id"]).strip(),
            "client_secret": str(cfg["client_secret"]).strip(), "scope": scope,
        }, service=service)
        return data["access_token"], data.get("expires_in")

    return await cached(cfg, "ms-cc", fetch, variant=scope)


async def microsoft_token(cfg: dict[str, Any], *, service: str = "Microsoft 365", scope: str | None = None) -> str:
    """Graph (or another resource) access token.

    Delegated: tenant_id (default "common") + client_id (+ client_secret for confidential apps) + refresh_token.
    App-only: tenant_id + client_id + client_secret (admin-consented application permissions)."""
    tenant = str(cfg.get("tenant_id") or "").strip() or "common"
    if ms_delegated(cfg):
        H.need(cfg, ("client_id",), service)
        rkey = identity(cfg) + "|ms-refresh"
        req_scope = scope or str(cfg.get("scopes") or "").strip() or f"{GRAPH_SCOPE} offline_access"
        if "offline_access" not in req_scope:
            req_scope += " offline_access"

        async def fetch() -> tuple[str, Any]:
            current = _rotated.get(rkey) or str(cfg["refresh_token"]).strip()
            form = {"grant_type": "refresh_token", "client_id": str(cfg["client_id"]).strip(),
                    "refresh_token": current, "scope": req_scope}
            if cfg.get("client_secret"):
                form["client_secret"] = str(cfg["client_secret"]).strip()
            data = await token_request(f"{MS_LOGIN}/{quote(tenant)}/oauth2/v2.0/token", form, service=service)
            new_rt = data.get("refresh_token")
            if new_rt and new_rt != current:
                _rotated[rkey] = new_rt
                await persist_secret(cfg, "refresh_token", new_rt)
            return data["access_token"], data.get("expires_in")

        return await cached(cfg, "ms", fetch, variant=req_scope)
    if cfg.get("client_id") and cfg.get("client_secret") and tenant not in ("common", "organizations", "consumers"):
        return await microsoft_client_credentials(cfg, scope or GRAPH_SCOPE, service=service)
    tok = str(cfg.get("access_token") or "").strip()
    if tok:
        return tok
    raise ToolError(f"The {service} connector needs a refresh token (delegated access) or tenant_id + client_id + "
                    "client_secret (app-only access). Update it in Connectors.")


def graph_user_base(cfg: dict[str, Any], *, service: str = "Microsoft 365") -> str:
    """`.../v1.0/me` for delegated tokens; `.../v1.0/users/{user}` for app-only access."""
    if ms_delegated(cfg) or (cfg.get("access_token") and not cfg.get("user")):
        return f"{GRAPH}/me"
    user = str(cfg.get("user") or cfg.get("mailbox") or "").strip()
    if not user:
        raise ToolError(f"{service} app-only access needs the 'user' field (mailbox UPN/email) in the connector, "
                        "or connect with a refresh token instead.")
    return f"{GRAPH}/users/{quote(user)}"


def graph_client(cfg: dict[str, Any], *, service: str = "Microsoft 365", reconnect: str | None = None,
                 user_scoped: bool = True) -> H.RestClient:
    """RestClient for Microsoft Graph; base is the user root (/me or /users/{user}) unless user_scoped=False."""

    async def auth() -> dict[str, str]:
        return {"Authorization": f"Bearer {await microsoft_token(cfg, service=service)}"}

    base = graph_user_base(cfg, service=service) if user_scoped else GRAPH
    return H.RestClient(service, base, auth=auth, reconnect=reconnect or service,
                        on_unauthorized=lambda: invalidate(cfg))


# --------------------------------------------------------------------------- Salesforce
async def salesforce_session(cfg: dict[str, Any]) -> tuple[str, str]:
    """(access_token, instance_url) via refresh token, client credentials, or a raw access token."""
    instance = str(cfg.get("instance_url") or "").strip().rstrip("/")
    if cfg.get("refresh_token") and cfg.get("client_id"):
        login = str(cfg.get("login_url") or "").strip().rstrip("/") or SF_LOGIN

        async def fetch_rt() -> tuple[tuple[str, str], Any]:
            form = {"grant_type": "refresh_token", "client_id": str(cfg["client_id"]).strip(),
                    "refresh_token": str(cfg["refresh_token"]).strip()}
            if cfg.get("client_secret"):
                form["client_secret"] = str(cfg["client_secret"]).strip()
            data = await token_request(f"{login}/services/oauth2/token", form, service="Salesforce")
            return (data["access_token"], str(data.get("instance_url") or instance).rstrip("/")), data.get("expires_in")

        return await cached(cfg, "sf", fetch_rt, default_ttl=1800)
    if cfg.get("client_id") and cfg.get("client_secret") and instance:

        async def fetch_cc() -> tuple[tuple[str, str], Any]:
            data = await token_request(f"{instance}/services/oauth2/token", {
                "grant_type": "client_credentials", "client_id": str(cfg["client_id"]).strip(),
                "client_secret": str(cfg["client_secret"]).strip()}, service="Salesforce")
            return (data["access_token"], str(data.get("instance_url") or instance).rstrip("/")), data.get("expires_in")

        return await cached(cfg, "sf-cc", fetch_cc, default_ttl=1800)
    tok = str(cfg.get("access_token") or "").strip()
    if tok and instance:
        return tok, instance
    raise ToolError("The Salesforce connector needs instance_url plus a refresh token (with client id/secret), "
                    "client credentials, or an access token.")


# --------------------------------------------------------------------------- Dropbox / Box
async def dropbox_token(cfg: dict[str, Any]) -> str:
    if cfg.get("refresh_token"):
        H.need(cfg, ("app_key",), "Dropbox")

        async def fetch() -> tuple[str, Any]:
            form = {"grant_type": "refresh_token", "refresh_token": str(cfg["refresh_token"]).strip(),
                    "client_id": str(cfg["app_key"]).strip()}
            if cfg.get("app_secret"):
                form["client_secret"] = str(cfg["app_secret"]).strip()
            data = await token_request("https://api.dropboxapi.com/oauth2/token", form, service="Dropbox")
            return data["access_token"], data.get("expires_in")

        return await cached(cfg, "dropbox", fetch, default_ttl=14000)
    tok = str(cfg.get("access_token") or "").strip()
    if tok:
        return tok
    raise ToolError("The Dropbox connector needs a refresh token (with app key/secret) or an access token.")


async def box_token(cfg: dict[str, Any]) -> str:
    if cfg.get("client_id") and cfg.get("client_secret") and cfg.get("subject_id"):

        async def fetch() -> tuple[str, Any]:
            data = await token_request("https://api.box.com/oauth2/token", {
                "grant_type": "client_credentials", "client_id": str(cfg["client_id"]).strip(),
                "client_secret": str(cfg["client_secret"]).strip(),
                "box_subject_type": str(cfg.get("subject_type") or "enterprise").strip(),
                "box_subject_id": str(cfg["subject_id"]).strip(),
            }, service="Box")
            return data["access_token"], data.get("expires_in")

        return await cached(cfg, "box", fetch)
    tok = str(cfg.get("developer_token") or cfg.get("access_token") or "").strip()
    if tok:
        return tok
    raise ToolError("The Box connector needs client credentials (client id, client secret, enterprise/user id) "
                    "or a developer token.")
