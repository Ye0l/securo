from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import (
    asset_group_service,
    asset_service,
    asset_transaction_service,
    collection_service,
    rule_service,
)
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import parse_uuid, resolve_workspace_id


def _json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "id"):
        return {"id": str(value.id), "name": getattr(value, "name", None)}
    return value


@tool(
    name="list_rules",
    description="List all categorization/automation rules in the current workspace.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    tags=["read", "rules"],
)
async def list_rules(*, session: AsyncSession, ctx: CallContext) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    rows = await rule_service.get_rules(session, ws_id)
    items = [
        {
            "id": str(r.id), "name": r.name, "conditions_op": r.conditions_op,
            "conditions": r.conditions, "actions": r.actions, "priority": r.priority,
            "is_active": r.is_active,
        }
        for r in rows
    ]
    return {"items": items, "total": len(items)}


@tool(
    name="list_collections",
    description="List account/asset-wallet collections used to organize the finance UI.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    tags=["read", "collections"],
)
async def list_collections(*, session: AsyncSession, ctx: CallContext) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    rows = await collection_service.get_collections(session, ws_id)
    return {"items": _json(rows), "total": len(rows)}


@tool(
    name="list_asset_groups",
    description="List investment/asset wallets with roll-up values.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    tags=["read", "assets"],
)
async def list_asset_groups(*, session: AsyncSession, ctx: CallContext) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    rows = await asset_group_service.get_groups(session, ws_id, ctx.user_id)
    return {"items": _json(rows), "total": len(rows)}


@tool(
    name="get_asset",
    description="Get one investment/asset with current value, cost basis, gain/loss, ticker and ledger metadata.",
    parameters={
        "type": "object",
        "properties": {"asset_id": {"type": "string", "format": "uuid"}},
        "required": ["asset_id"],
        "additionalProperties": False,
    },
    tags=["read", "assets"],
)
async def get_asset(*, session: AsyncSession, ctx: CallContext, asset_id: str) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    aid = parse_uuid(asset_id)
    if aid is None:
        return {"error": "asset not found"}
    row = await asset_service.get_asset(session, aid, ws_id)
    return _json(row) if row is not None else {"error": "asset not found"}


@tool(
    name="list_asset_transactions",
    description="List buy/sell ledger entries across assets, optionally filtered by ticker or kind.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "kind": {"type": "string", "enum": ["buy", "sell"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 500},
        },
        "additionalProperties": False,
    },
    tags=["read", "assets", "transactions"],
)
async def list_asset_transactions(
    *, session: AsyncSession, ctx: CallContext, ticker: str | None = None,
    kind: str | None = None, limit: int = 500,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    rows = await asset_transaction_service.list_workspace_transactions(
        session, ws_id, ticker=ticker, kind=kind, limit=max(1, min(limit, 2000))
    )
    return {"items": _json(rows), "total": len(rows)}


@tool(
    name="get_asset_value_history",
    description="Get manual/synced value history for a single asset.",
    parameters={
        "type": "object",
        "properties": {"asset_id": {"type": "string", "format": "uuid"}},
        "required": ["asset_id"],
        "additionalProperties": False,
    },
    tags=["read", "assets"],
)
async def get_asset_value_history(
    *, session: AsyncSession, ctx: CallContext, asset_id: str
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    aid = parse_uuid(asset_id)
    if aid is None:
        return {"error": "asset not found"}
    rows = await asset_service.get_asset_values(session, aid, ws_id)
    if rows is None:
        return {"error": "asset not found"}
    return {"items": _json(rows), "total": len(rows)}
