from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction
from app.schemas.transaction import TransactionImport
from app.services import account_service, import_service
from app.services.credit_card_service import apply_effective_date
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import num, parse_date, parse_uuid, resolve_workspace_id
from mcp_server.tools.proposals import _APPLY_FIELD, _PROPOSAL_PREFACE, _can_apply


_TX_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "format": "date"},
        "description": {"type": "string", "minLength": 1, "maxLength": 500},
        "amount": {"type": "number", "exclusiveMinimum": 0},
        "type": {"type": "string", "enum": ["debit", "credit"]},
        "currency": {"type": "string"},
        "external_id": {"type": "string"},
        "payee_raw": {"type": "string"},
        "notes": {"type": "string"},
        "category_id": {"type": "string", "format": "uuid"},
        "force_uncategorized": {"type": "boolean", "default": False},
    },
    "required": ["date", "description", "amount", "type"],
    "additionalProperties": False,
}


@tool(
    name="propose_bulk_import_transactions",
    description=_PROPOSAL_PREFACE
    + (
        "Bulk-import many normalized transactions into one account in a single MCP call. "
        "Use for bank-statement imports instead of calling propose_create_transaction once per row. "
        "Duplicate detection is enabled by default and uses external_id+date when available, otherwise "
        "date+amount+type+description."
    ),
    parameters={
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "format": "uuid"},
            "transactions": {"type": "array", "items": _TX_ITEM_SCHEMA, "minItems": 1, "maxItems": 5000},
            "filename": {"type": "string", "default": "mcp-bulk-import"},
            "detected_format": {"type": "string", "default": "mcp"},
            "detect_duplicates": {"type": "boolean", "default": True},
            "rebase_opening_balance": {
                "type": "boolean",
                "default": False,
                "description": "For a complete statement, replace the manual opening balance so imported history ends at statement_ending_balance.",
            },
            "statement_ending_balance": {"type": "number"},
            "apply": _APPLY_FIELD,
        },
        "required": ["account_id", "transactions"],
        "additionalProperties": False,
    },
    is_proposal=True,
    tags=["propose", "transactions", "import", "bulk"],
)
async def propose_bulk_import_transactions(
    *,
    session: AsyncSession,
    ctx: CallContext,
    account_id: str,
    transactions: list[dict[str, Any]],
    filename: str = "mcp-bulk-import",
    detected_format: str = "mcp",
    detect_duplicates: bool = True,
    rebase_opening_balance: bool = False,
    statement_ending_balance: float | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    acc_id = parse_uuid(account_id)
    if acc_id is None:
        return {"error": "account not found"}
    account = await account_service.get_account(session, acc_id, ws_id)
    if account is None:
        return {"error": "account not found"}
    if len(transactions) > 5000:
        return {"error": "maximum 5000 transactions per bulk import"}

    normalized: list[TransactionImport] = []
    for idx, raw in enumerate(transactions):
        tx_date = parse_date(raw.get("date"))
        if tx_date is None:
            return {"error": f"invalid date at row {idx + 1}"}
        tx_type = raw.get("type")
        if tx_type not in {"debit", "credit"}:
            return {"error": f"invalid type at row {idx + 1}"}
        try:
            amount = Decimal(str(raw.get("amount")))
        except Exception:
            return {"error": f"invalid amount at row {idx + 1}"}
        if amount <= 0:
            return {"error": f"amount must be positive at row {idx + 1}"}
        category_id = parse_uuid(raw.get("category_id")) if raw.get("category_id") else None
        normalized.append(
            TransactionImport(
                date=tx_date,
                description=str(raw.get("description") or "").strip(),
                amount=amount,
                type=tx_type,
                currency=(raw.get("currency") or account.currency or "USD").upper(),
                external_id=raw.get("external_id"),
                payee_raw=raw.get("payee_raw"),
                notes=raw.get("notes"),
                category_id=category_id,
                force_uncategorized=bool(raw.get("force_uncategorized", False)),
            )
        )

    dates = [t.date for t in normalized]
    total_credit = sum((t.amount for t in normalized if t.type == "credit"), Decimal("0"))
    total_debit = sum((t.amount for t in normalized if t.type == "debit"), Decimal("0"))
    computed_opening_balance: Decimal | None = None
    if rebase_opening_balance:
        if statement_ending_balance is None:
            return {"error": "statement_ending_balance is required when rebase_opening_balance=true"}
        computed_opening_balance = Decimal(str(statement_ending_balance)) - total_credit + total_debit

    preview = {
        "kind": "bulk_import_transactions",
        "account": {"id": str(account.id), "name": account.name, "currency": account.currency},
        "count": len(normalized),
        "date_from": min(dates).isoformat(),
        "date_to": max(dates).isoformat(),
        "total_credit": num(total_credit),
        "total_debit": num(total_debit),
        "detect_duplicates": bool(detect_duplicates),
        "rebase_opening_balance": bool(rebase_opening_balance),
        "statement_ending_balance": float(statement_ending_balance) if statement_ending_balance is not None else None,
        "computed_opening_balance": num(computed_opening_balance),
        "opening_balance_date": (min(dates) - timedelta(days=1)).isoformat() if computed_opening_balance is not None else None,
        "filename": filename,
        "apply_endpoint": "POST /api/transactions/import",
    }
    if not _can_apply(ctx, apply):
        return preview

    if computed_opening_balance is not None:
        opening_result = await session.execute(
            select(Transaction).where(
                Transaction.account_id == account.id,
                Transaction.source == "opening_balance",
            )
        )
        opening_tx = opening_result.scalars().first()
        account.balance = computed_opening_balance
        opening_date = min(dates) - timedelta(days=1)
        if computed_opening_balance == Decimal("0"):
            if opening_tx is not None:
                await session.delete(opening_tx)
        else:
            opening_amount = abs(computed_opening_balance)
            is_credit = (computed_opening_balance > 0) == (account.type != "credit_card")
            opening_type = "credit" if is_credit else "debit"
            if opening_tx is None:
                opening_tx = Transaction(
                    user_id=ctx.user_id,
                    workspace_id=ws_id,
                    account_id=account.id,
                    description="Saldo inicial",
                    amount=opening_amount,
                    currency=account.currency,
                    date=opening_date,
                    type=opening_type,
                    source="opening_balance",
                )
                session.add(opening_tx)
            else:
                opening_tx.amount = opening_amount
                opening_tx.type = opening_type
                opening_tx.date = opening_date
            apply_effective_date(opening_tx, account)
        await session.flush()

    imported, skipped, excluded, import_log_id = await import_service.import_transactions(
        session,
        ws_id,
        ctx.user_id,
        account.id,
        normalized,
        "mcp_import",
        filename=filename,
        detected_format=detected_format or "mcp",
        detect_duplicates=detect_duplicates,
    )
    return {
        **preview,
        "applied": True,
        "imported": imported,
        "skipped": skipped,
        "excluded": excluded,
        "import_log_id": str(import_log_id),
    }
