from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import account_service
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import num, parse_date, parse_uuid, resolve_workspace_id


@tool(
    name="list_accounts",
    description=(
        "List the user's accounts (checking, savings, credit cards, wallets, etc.) "
        "with current balances. Closed accounts are excluded by default."
    ),
    parameters={
        "type": "object",
        "properties": {
            "include_closed": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    },
    tags=["read", "accounts"],
)
async def list_accounts(
    *,
    session: AsyncSession,
    ctx: CallContext,
    include_closed: bool = False,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    rows = await account_service.get_accounts(session, ws_id, include_closed=include_closed)
    # rows is already a list of dicts (per service contract), but normalize keys.
    items: list[dict[str, Any]] = []
    for r in rows:
        items.append({
            "id": str(r.get("id")) if r.get("id") else None,
            "name": r.get("name"),
            "type": r.get("type"),
            "currency": r.get("currency"),
            "balance": num(r.get("current_balance")),
            "current_balance": num(r.get("current_balance")),
            "stored_balance": num(r.get("balance")),
            "balance_primary": num(r.get("balance_primary")),
            "is_closed": bool(r.get("is_closed", False)),
            "institution": r.get("institution_name") or r.get("institution"),
            "credit_limit": num(r.get("credit_limit")),
            "available_credit": num(r.get("available_credit")),
            "statement_close_day": r.get("statement_close_day"),
            "payment_due_day": r.get("payment_due_day"),
            "next_due_date": (lambda v: v.isoformat() if v is not None and hasattr(v, "isoformat") else v)(r.get("next_due_date")),
            "minimum_payment": num(r.get("minimum_payment")),
            "interest_rate": num(r.get("interest_rate")),
            "original_principal": num(r.get("original_principal")),
            "scheduled_payment": num(r.get("scheduled_payment")),
            "maturity_date": (lambda v: v.isoformat() if v is not None and hasattr(v, "isoformat") else v)(r.get("maturity_date")),
            "loan_status": r.get("loan_status"),
            "notes": r.get("notes"),
            "card_brand": r.get("card_brand"),
            "card_level": r.get("card_level"),
        })
    return {"items": items, "total": len(items)}


@tool(
    name="get_mcp_read_probe",
    description=(
        "Read-only MCP discovery probe. Returns a small authenticated response so clients can "
        "verify that newly-added read tools are being discovered correctly."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    tags=["read", "diagnostics"],
)
async def get_mcp_read_probe(
    *,
    session: AsyncSession,
    ctx: CallContext,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    # Keep this zero-argument probe schema stable and make its response dynamic.
    # Clients that cache tools/list can still discover newly-added operations
    # supported by the forward-compatible generic management tool.
    from mcp_server.registry import REGISTRY
    from mcp_server.tools.management import _OPERATIONS

    return {
        "ok": True,
        "kind": "mcp_read_probe",
        "workspace_id": str(ws_id),
        "tool_count": len(REGISTRY),
        "management_operations": sorted(_OPERATIONS),
        "capability_hint": (
            "Use propose_manage_finance_data with one of management_operations. "
            "Its operation field is intentionally schema-stable for cached MCP clients."
        ),
    }


@tool(
    name="get_account_summary",
    description=(
        "Income, expenses, and net for a single account over a date range. "
        "Defaults to the current month if no range is provided."
    ),
    parameters={
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "format": "uuid"},
            "from_date": {"type": "string", "format": "date"},
            "to_date": {"type": "string", "format": "date"},
        },
        "required": ["account_id"],
        "additionalProperties": False,
    },
    tags=["read", "accounts"],
)
async def get_account_summary(
    *,
    session: AsyncSession,
    ctx: CallContext,
    account_id: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    account_uuid = parse_uuid(account_id)
    if account_uuid is None:
        return dict(error="account not found")

    summary = await account_service.get_account_summary(
        session,
        account_uuid,
        ws_id,
        date_from=parse_date(from_date),
        date_to=parse_date(to_date),
    )
    if summary is None:
        return {"error": "account not found"}
    # Normalize numeric fields.
    for k in list(summary.keys()):
        v = summary[k]
        if hasattr(v, "isoformat"):
            summary[k] = v.isoformat()
    return summary


@tool(
    name="get_account",
    description="Get one account with full card/loan metadata, current balance, due dates, and notes.",
    parameters={
        "type": "object",
        "properties": {"account_id": {"type": "string", "format": "uuid"}},
        "required": ["account_id"],
        "additionalProperties": False,
    },
    tags=["read", "accounts"],
)
async def get_account(*, session: AsyncSession, ctx: CallContext, account_id: str) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    acc_id = parse_uuid(account_id)
    if acc_id is None:
        return {"error": "account not found"}
    payload = await account_service.get_account_view(session, acc_id, ws_id)
    if payload is None:
        return {"error": "account not found"}
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif key in {
            "balance", "current_balance", "previous_balance", "balance_primary", "credit_limit",
            "available_credit", "minimum_payment", "interest_rate", "original_principal", "scheduled_payment"
        }:
            out[key] = num(value)
        else:
            out[key] = str(value) if key in {"id", "user_id", "connection_id", "external_id"} and value is not None else value
    out["stored_balance"] = num(payload.get("balance"))
    out["balance"] = num(payload.get("current_balance"))
    out["current_balance"] = num(payload.get("current_balance"))
    return out


@tool(
    name="get_account_balance_history",
    description="Get an account's daily balance history over an optional date range.",
    parameters={
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "format": "uuid"},
            "from_date": {"type": "string", "format": "date"},
            "to_date": {"type": "string", "format": "date"},
        },
        "required": ["account_id"],
        "additionalProperties": False,
    },
    tags=["read", "accounts"],
)
async def get_account_balance_history(
    *, session: AsyncSession, ctx: CallContext, account_id: str,
    from_date: str | None = None, to_date: str | None = None,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    acc_id = parse_uuid(account_id)
    if acc_id is None:
        return {"error": "account not found"}
    rows = await account_service.get_account_balance_history(
        session, acc_id, ws_id, date_from=parse_date(from_date), date_to=parse_date(to_date)
    )
    if rows is None:
        return {"error": "account not found"}
    return {"items": rows, "total": len(rows)}


@tool(
    name="list_credit_card_bills",
    description="List credit-card bills for an account, newest due date first.",
    parameters={
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "format": "uuid"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 24},
        },
        "required": ["account_id"],
        "additionalProperties": False,
    },
    tags=["read", "accounts", "credit_cards"],
)
async def list_credit_card_bills(
    *, session: AsyncSession, ctx: CallContext, account_id: str, limit: int = 24
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    acc_id = parse_uuid(account_id)
    if acc_id is None:
        return {"error": "account not found"}
    rows = await account_service.get_credit_card_bills(session, acc_id, ws_id, limit=max(1, min(limit, 200)))
    if rows is None:
        return {"error": "account not found"}
    return {
        "items": [
            {
                "id": str(r.id), "account_id": str(r.account_id), "external_id": r.external_id,
                "due_date": r.due_date.isoformat(), "total_amount": num(r.total_amount),
                "currency": r.currency, "minimum_payment": num(r.minimum_payment),
            }
            for r in rows
        ],
        "total": len(rows),
    }
