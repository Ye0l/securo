"""Minimal OAuth gateway for Google Gemini custom MCP apps.

Single-user adapter: use a fixed client ID/secret in Gemini's Advanced
features. Authorization is auto-approved; no login UI or dynamic client
registration is provided.
"""
from __future__ import annotations

import base64
import hashlib
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

CLIENT_ID = os.getenv("GEMINI_MCP_CLIENT_ID", "securo-gemini")
CLIENT_SECRET = os.getenv("GEMINI_MCP_CLIENT_SECRET", "")
PUBLIC_URL = os.getenv("GEMINI_MCP_PUBLIC_URL", "").rstrip("/")
UPSTREAM = os.getenv("GEMINI_MCP_UPSTREAM", "http://mcp-server:8765/mcp")
USER_ID = os.getenv("GEMINI_MCP_USER_ID", "")
WORKSPACE_ID = os.getenv("GEMINI_MCP_WORKSPACE_ID", "")
OAUTH_SECRET = os.getenv("GEMINI_MCP_OAUTH_SECRET") or os.getenv("AGENTS_MCP_JWT_SECRET", "")
MCP_JWT_SECRET = os.getenv("AGENTS_MCP_JWT_SECRET", "")

ISSUER = "securo-gemini-oauth"
AUDIENCE = "securo-gemini-mcp"
MCP_ISSUER = "securo-backend"
MCP_AUDIENCE = "securo-mcp"
_codes: dict[str, dict] = {}


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


def _mint_gateway_token(kind: str, ttl: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": USER_ID, "typ": kind, "iat": now, "exp": now + ttl},
        OAUTH_SECRET,
        algorithm="HS256",
    )


def _verify_gateway_access(token: str) -> bool:
    try:
        payload = jwt.decode(token, OAUTH_SECRET, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)
        return payload.get("typ") == "access"
    except JWTError:
        return False


def _mint_upstream_token() -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": MCP_ISSUER,
        "aud": MCP_AUDIENCE,
        "sub": USER_ID,
        "ext": True,
        "iat": now,
        "exp": now + 3600,
        "jti": str(uuid.uuid4()),
    }
    if WORKSPACE_ID:
        payload["ws_id"] = WORKSPACE_ID
    return jwt.encode(payload, MCP_JWT_SECRET, algorithm="HS256")


def _ready() -> tuple[bool, str]:
    missing = [name for name, value in (
        ("GEMINI_MCP_CLIENT_SECRET", CLIENT_SECRET),
        ("GEMINI_MCP_USER_ID", USER_ID),
        ("GEMINI_MCP_OAUTH_SECRET/AGENTS_MCP_JWT_SECRET", OAUTH_SECRET),
        ("AGENTS_MCP_JWT_SECRET", MCP_JWT_SECRET),
    ) if not value]
    return (not missing, ", ".join(missing))


@app.get("/health")
async def health():
    ok, missing = _ready()
    return JSONResponse({"status": "ok" if ok else "misconfigured", "missing": missing or None}, status_code=200 if ok else 503)


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
    if q.get("client_id") != CLIENT_ID:
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
    _codes[code] = {"redirect_uri": redirect_uri, "challenge": challenge, "expires": time.time() + 300}
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
    if client_id != CLIENT_ID or not secrets.compare_digest(client_secret, CLIENT_SECRET):
        return _json_error("invalid_client", "bad client credentials", 401)

    grant = form.get("grant_type")
    if grant == "authorization_code":
        data = _codes.pop(form.get("code", ""), None)
        if not data or data["expires"] < time.time():
            return _json_error("invalid_grant", "code missing or expired")
        if form.get("redirect_uri") != data["redirect_uri"]:
            return _json_error("invalid_grant", "redirect_uri mismatch")
        verifier = form.get("code_verifier", "")
        calculated = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not verifier or not secrets.compare_digest(calculated, data["challenge"]):
            return _json_error("invalid_grant", "PKCE verification failed")
    elif grant == "refresh_token":
        try:
            payload = jwt.decode(form.get("refresh_token", ""), OAUTH_SECRET, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)
            if payload.get("typ") != "refresh":
                raise JWTError("wrong token type")
        except JWTError:
            return _json_error("invalid_grant", "invalid refresh token")
    else:
        return _json_error("unsupported_grant_type", "use authorization_code or refresh_token")

    return {
        "access_token": _mint_gateway_token("access", 3600),
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": _mint_gateway_token("refresh", 30 * 86400),
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
    if not access or not _verify_gateway_access(access):
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'},
        )

    headers = {
        "Authorization": f"Bearer {_mint_upstream_token()}",
        "Accept": request.headers.get("accept", "application/json, text/event-stream"),
    }
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]

    async with httpx.AsyncClient(timeout=60) as client:
        upstream = await client.request(request.method, UPSTREAM, content=await request.body(), headers=headers)

    out_headers = {key: upstream.headers[key] for key in ("content-type", "mcp-session-id") if key in upstream.headers}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=out_headers)
