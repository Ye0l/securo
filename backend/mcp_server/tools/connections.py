from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.bank_connection import ConnectionSettingsUpdate
from app.services import connection_service
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import parse_uuid, resolve_workspace_id
from mcp_server.tools.proposals import _APPLY_FIELD, _PROPOSAL_PREFACE, _can_apply


@tool(
    name="list_bank_connections",
    description="List configured bank/card data connections and their sync status. Credentials are never returned.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    tags=["read", "connections"],
)
async def list_bank_connections(*, session: AsyncSession, ctx: CallContext) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    rows = await connection_service.get_connections(session, ws_id)
    return {
        "items": [
            {
                "id": str(r.id),
                "provider": r.provider,
                "institution_name": r.institution_name,
                "display_name": r.display_name,
                "logo_url": r.logo_url,
                "status": r.status,
                "last_sync_at": r.last_sync_at.isoformat() if r.last_sync_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "settings": r.settings,
                "account_count": len(r.accounts or []),
            }
            for r in rows
        ],
        "total": len(rows),
    }


@tool(
    name="get_bank_oauth_url",
    description="Create an OAuth authorization URL for a configured bank provider. Does not expose credentials or complete the callback.",
    parameters={
        "type": "object",
        "properties": {
            "provider": {"type": "string"},
            "flow_params": {"type": "object", "additionalProperties": True},
        },
        "required": ["provider"],
        "additionalProperties": False,
    },
    tags=["read", "connections", "oauth"],
)
async def get_bank_oauth_url(
    *, session: AsyncSession, ctx: CallContext, provider: str, flow_params: dict[str, Any] | None = None
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    url = await connection_service.get_oauth_url(provider, ctx.user_id, ws_id, flow_params or {})
    return {"provider": provider, "url": url}


@tool(
    name="get_bank_reauth_url",
    description="Create a re-authorization URL for an existing bank connection.",
    parameters={
        "type": "object",
        "properties": {"connection_id": {"type": "string", "format": "uuid"}},
        "required": ["connection_id"],
        "additionalProperties": False,
    },
    tags=["read", "connections", "oauth"],
)
async def get_bank_reauth_url(
    *, session: AsyncSession, ctx: CallContext, connection_id: str
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    cid = parse_uuid(connection_id)
    if cid is None:
        return {"error": "connection not found"}
    try:
        url = await connection_service.get_reauth_url(session, cid, ws_id, ctx.user_id)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"connection_id": connection_id, "url": url}


@tool(
    name="propose_manage_bank_connection",
    description=_PROPOSAL_PREFACE
    + "Sync, change non-secret settings, or delete an existing bank connection. Never returns stored credentials.",
    parameters={
        "type": "object",
        "properties": {
            "connection_id": {"type": "string", "format": "uuid"},
            "action": {"type": "string", "enum": ["sync", "update_settings", "delete"]},
            "settings": {
                "type": "object",
                "properties": {
                    "display_name": {"type": "string"},
                    "payee_source": {"type": "string", "enum": ["auto", "merchant", "payment_data", "description", "none"]},
                    "import_pending": {"type": "boolean"},
                    "sync_assets": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            "trigger_provider_refresh": {"type": "boolean", "default": False},
            "apply": _APPLY_FIELD,
        },
        "required": ["connection_id", "action"],
        "additionalProperties": False,
    },
    is_proposal=True,
    tags=["propose", "connections"],
)
async def propose_manage_bank_connection(
    *,
    session: AsyncSession,
    ctx: CallContext,
    connection_id: str,
    action: str,
    settings: dict[str, Any] | None = None,
    trigger_provider_refresh: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    cid = parse_uuid(connection_id)
    if cid is None:
        return {"error": "connection not found"}
    connection = await connection_service.get_connection(session, cid, ws_id)
    if connection is None:
        return {"error": "connection not found"}
    preview = {
        "kind": "manage_bank_connection",
        "action": action,
        "connection": {"id": str(connection.id), "provider": connection.provider, "institution_name": connection.institution_name},
        "settings": settings or {},
        "trigger_provider_refresh": bool(trigger_provider_refresh),
    }
    if not _can_apply(ctx, apply):
        if action == "update_settings":
            ConnectionSettingsUpdate(**(settings or {}))
        return preview
    try:
        if action == "sync":
            synced, count = await connection_service.sync_connection(
                session, cid, ws_id, ctx.user_id, trigger_provider_refresh=trigger_provider_refresh
            )
            return {**preview, "applied": True, "synced_transactions": count, "status": synced.status}
        if action == "update_settings":
            payload = ConnectionSettingsUpdate(**(settings or {})).model_dump(exclude_unset=True)
            updated = await connection_service.update_connection_settings(session, cid, ws_id, payload)
            return {**preview, "applied": True, "id": str(updated.id) if updated else None}
        if action == "delete":
            deleted = await connection_service.delete_connection(session, cid, ws_id)
            return {**preview, "applied": True, "deleted": bool(deleted)}
    except ValueError as exc:
        return {**preview, "error": str(exc)}
    return {**preview, "error": "unsupported action"}
