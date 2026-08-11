"""Minimal OAuth gateway for Google Gemini custom MCP apps.

Supports multiple fixed OAuth clients. Each client ID/secret maps to one Securo
user (and optionally one workspace). This keeps Gemini setup simple while
preserving per-user Securo isolation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from urllib.parse import parse_qs, urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from jose import JWTError, jwt

app = FastAPI(title="Securo Gemini MCP OAuth Gateway", openapi_url=None, docs_url=None)

PUBLIC_URL = os.getenv("GEMINI_MCP_PUBLIC_URL", "").rstrip("/")
UPSTREAM = os.getenv("GEMINI_MCP_UPSTREAM", "http://mcp-server:8765/mcp")
OAUTH_SECRET = os.getenv("GEMINI_MCP_OAUTH_SECRET") or os.getenv("AGENTS_MCP_JWT_SECRET", "")
MCP_JWT_SECRET = os.getenv("AGENTS_MCP_JWT_SECRET", "")

ISSUER = "securo-gemini-oauth"
AUDIENCE = "securo-gemini-mcp"
MCP_ISSUER = "securo-backend"
MCP_AUDIENCE = "securo-mcp"
_codes: dict[str, dict] = {}


def _clients() -> dict[str, dict[str, str]]:
    """Load client_id -> {secret,user_id,workspace_id?} mapping.

    Preferred format:
      GEMINI_MCP_CLIENTS={"yeol":{"secret":"...","user_id":"uuid"},...}

    The original single-user variables remain supported for compatibility.
    """
    raw = os.getenv("GEMINI_MCP_CLIENTS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for client_id, cfg in parsed.items():
            if not isinstance(client_id, str) or not isinstance(cfg, dict):
                continue
            secret = cfg.get("secret")
            user_id = cfg.get("user_id")
            workspace_id = cfg.get("workspace_id", "")
            if isinstance(secret, str) and secret and isinstance(user_id, str) and user_id:
                out[client_id] = {
                    "secret": secret,
                    "user_id": user_id,
                    "workspace_id": workspace_id if isinstance(workspace_id, str) else "",
                }
        return out

    client_id = os.getenv("GEMINI_MCP_CLIENT_ID", "securo-gemini")
    secret = os.getenv("GEMINI_MCP_CLIENT_SECRET", "")
    user_id = os.getenv("GEMINI_MCP_USER_ID", "")
    workspace_id = os.getenv("GEMINI_MCP_WORKSPACE_ID", "")
    if client_id and secret and user_id:
        return {client_id: {"secret": secret, "user_id": user_id, "workspace_id": workspace_id}}
    return {}


def _base(request: Request) -> str:
    return PUBLIC_URL or str(request.base_url).rstrip("/")


def _json_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


def _client_credentials(request: Request, form: dict[str, str]) -> tuple[str, str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            raw = base64.b64decode(auth.split(" ", 1)[1]).decode()
            client_id, client_secret = raw.split(":", 1)
            return client_id, client_secret
        except Exception:
            return "", ""
    return form.get("client_id", ""), form.get("client_secret", "")


def _mint_gateway_token(kind: str, client_id: str, cfg: dict[str, str], ttl: int) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": cfg["user_id"],
        "client_id": client_id,
        "typ": kind,
        "iat": now,
        "exp": now + ttl,
    }
    if cfg.get("workspace_id"):
        payload["ws_id"] = cfg["workspace_id"]
    return jwt.encode(payload, OAUTH_SECRET, algorithm="HS256")


def _verify_gateway_access(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, OAUTH_SECRET, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)
        if payload.get("typ") != "access" or not payload.get("sub") or not payload.get("client_id"):
            return None
        return payload
    except JWTError:
        return None


def _mint_upstream_token(identity: dict) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": MCP_ISSUER,
        "aud": MCP_AUDIENCE,
        "sub": identity["sub"],
        "ext": True,
        "iat": now,
        "exp": now + 3600,
        "jti": str(uuid.uuid4()),
    }
    if identity.get("ws_id"):
        payload["ws_id"] = identity["ws_id"]
    return jwt.encode(payload, MCP_JWT_SECRET, algorithm="HS256")


def _ready() -> tuple[bool, str]:
    problems: list[str] = []
    clients = _clients()
    if not clients:
        problems.append("GEMINI_MCP_CLIENTS (or legacy single-user client settings)")
    if not OAUTH_SECRET:
        problems.append("GEMINI_MCP_OAUTH_SECRET/AGENTS_MCP_JWT_SECRET")
    if not MCP_JWT_SECRET:
        problems.append("AGENTS_MCP_JWT_SECRET")
    return (not problems, ", ".join(problems))


@app.get("/health")
async def health():
    ok, missing = _ready()
    return JSONResponse(
        {"status": "ok" if ok else "misconfigured", "clients": len(_clients()), "missing": missing or None},
        status_code=200 if ok else 503,
    )


@app.get("/.well-known/oauth-protected-resource")
async def protected_resource(request: Request):
    base = _base(request)
    return {"resource": f"{base}/mcp", "authorization_servers": [base], "scopes_supported": ["mcp"]}


@app.get("/.well-known/oauth-authorization-server")
async def authorization_server(request: Request):
    base = _base(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["mcp"],
    }


@app.get("/authorize")
async def authorize(request: Request):
    q = request.query_params
    client_id = q.get("client_id", "")
    cfg = _clients().get(client_id)
    if not cfg:
        return _json_error("unauthorized_client", "unknown client_id", 401)
    if q.get("response_type") != "code":
        return _json_error("unsupported_response_type", "only code is supported")
    redirect_uri = q.get("redirect_uri", "")
    if not redirect_uri.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        return _json_error("invalid_request", "redirect_uri must be HTTPS")
    challenge = q.get("code_challenge", "")
    if not challenge or q.get("code_challenge_method", "S256") != "S256":
        return _json_error("invalid_request", "PKCE S256 is required")

    code = secrets.token_urlsafe(32)
    _codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "challenge": challenge,
        "expires": time.time() + 300,
    }
    params = {"code": code}
    if q.get("state"):
        params["state"] = q["state"]
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(params)}", status_code=302)


@app.post("/token")
async def token(request: Request):
    form_raw = parse_qs((await request.body()).decode())
    form = {k: v[-1] for k, v in form_raw.items() if v}
    client_id, client_secret = _client_credentials(request, form)
    cfg = _clients().get(client_id)
    if not cfg or not secrets.compare_digest(client_secret, cfg["secret"]):
        return _json_error("invalid_client", "bad client credentials", 401)

    grant = form.get("grant_type")
    if grant == "authorization_code":
        data = _codes.pop(form.get("code", ""), None)
        if not data or data["expires"] < time.time():
            return _json_error("invalid_grant", "code missing or expired")
        if data.get("client_id") != client_id:
            return _json_error("invalid_grant", "authorization code belongs to another client")
        if form.get("redirect_uri") != data["redirect_uri"]:
            return _json_error("invalid_grant", "redirect_uri mismatch")
        verifier = form.get("code_verifier", "")
        calculated = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not verifier or not secrets.compare_digest(calculated, data["challenge"]):
            return _json_error("invalid_grant", "PKCE verification failed")
    elif grant == "refresh_token":
        try:
            payload = jwt.decode(
                form.get("refresh_token", ""),
                OAUTH_SECRET,
                algorithms=["HS256"],
                audience=AUDIENCE,
                issuer=ISSUER,
            )
            if payload.get("typ") != "refresh" or payload.get("client_id") != client_id:
                raise JWTError("wrong token type or client")
        except JWTError:
            return _json_error("invalid_grant", "invalid refresh token")
    else:
        return _json_error("unsupported_grant_type", "use authorization_code or refresh_token")

    return {
        "access_token": _mint_gateway_token("access", client_id, cfg, 3600),
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": _mint_gateway_token("refresh", client_id, cfg, 30 * 86400),
        "scope": "mcp",
    }


@app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
async def mcp_proxy(request: Request):
    ok, missing = _ready()
    base = _base(request)
    metadata = f"{base}/.well-known/oauth-protected-resource"
    if not ok:
        return JSONResponse({"error": f"gateway misconfigured: {missing}"}, status_code=503)

    auth = request.headers.get("authorization", "")
    access = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    identity = _verify_gateway_access(access) if access else None
    if not identity:
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'},
        )

    # If a client was removed from configuration, immediately revoke its access
    # even if a previously issued access token has not expired yet.
    if identity.get("client_id") not in _clients():
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    headers = {
        "Authorization": f"Bearer {_mint_upstream_token(identity)}",
        "Accept": request.headers.get("accept", "application/json, text/event-stream"),
    }
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]

    async with httpx.AsyncClient(timeout=60) as client:
        upstream = await client.request(request.method, UPSTREAM, content=await request.body(), headers=headers)

    out_headers = {key: upstream.headers[key] for key in ("content-type", "mcp-session-id") if key in upstream.headers}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=out_headers)
