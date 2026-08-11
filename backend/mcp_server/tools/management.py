from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.asset import (
    AssetBuyCreate,
    AssetCreate,
    AssetTransactionCreate,
    AssetTransactionUpdate,
    AssetUpdate,
    AssetValueCreate,
)
from app.schemas.asset_group import AssetGroupCreate, AssetGroupUpdate
from app.schemas.budget import BudgetUpdate
from app.schemas.category import CategoryCreate, CategoryUpdate
from app.schemas.collection import CollectionCreate, CollectionUpdate
from app.schemas.goal import GoalUpdate
from app.schemas.group import GroupCreate, GroupMemberCreate, GroupMemberUpdate, GroupUpdate
from app.schemas.payee import PayeeCreate, PayeeUpdate
from app.schemas.rule import RuleCreate, RuleUpdate
from app.schemas.transaction import TransactionUpdate, TransferCreate
from app.services import (
    asset_group_service,
    asset_service,
    asset_transaction_service,
    budget_service,
    category_service,
    collection_service,
    goal_service,
    group_service,
    payee_service,
    recurring_transaction_service,
    rule_service,
    transaction_service,
)
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import parse_uuid, parse_uuid_list, resolve_workspace_id
from mcp_server.tools.proposals import _APPLY_FIELD, _PROPOSAL_PREFACE, _can_apply


_OPERATIONS = [
    "category.create", "category.update", "category.delete",
    "budget.update", "budget.delete",
    "goal.update", "goal.delete",
    "payee.create", "payee.update", "payee.delete", "payee.merge",
    "rule.create", "rule.update", "rule.delete", "rule.apply_all",
    "group.create", "group.update", "group.delete",
    "group.member_create", "group.member_update", "group.member_delete",
    "collection.create", "collection.update", "collection.delete",
    "asset_group.create", "asset_group.update", "asset_group.delete",
    "asset.create", "asset.update", "asset.delete", "asset.add_value", "asset.delete_value",
    "asset.add_transaction", "asset.update_transaction", "asset.delete_transaction", "asset.buy",
    "recurring.generate_pending",
    "transaction.update", "transaction.delete", "transaction.ignore", "transaction.unlink_recurring",
    "transaction.add_tags", "transaction.remove_tags", "transaction.transfer",
    "transaction.link_transfer", "transaction.create_counterpart",
]


def _uuid(value: str | None, label: str = "target_id"):
    parsed = parse_uuid(value) if value else None
    if parsed is None:
        raise ValueError(f"{label} is required and must be a UUID")
    return parsed


def _summary(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "id"):
        return {"id": str(value.id), "name": getattr(value, "name", None)}
    if isinstance(value, tuple):
        return [_summary(v) for v in value]
    return str(value)


@tool(
    name="propose_manage_finance_data",
    description=_PROPOSAL_PREFACE
    + (
        "General day-to-day Securo mutation tool covering categories, budgets, goals, payees, "
        "categorization rules, expense-sharing groups/members, collections, asset wallets, recurring "
        "generation, and transaction maintenance/transfers. Select an operation and pass its API-like "
        "fields in payload. target_id is the entity/transaction/group id; secondary_id is used for "
        "group.member_update/delete. This complements the more strongly typed dedicated tools."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Stable operation name. The server validates supported operations at runtime; "
                    "this field intentionally has no enum so newly-added operations do not require "
                    "clients to refresh a cached MCP tool schema."
                ),
            },
            "target_id": {"type": "string", "format": "uuid"},
            "secondary_id": {"type": "string", "format": "uuid"},
            "payload": {"type": "object", "additionalProperties": True, "default": {}},
            "apply": _APPLY_FIELD,
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
    is_proposal=True,
    tags=["propose", "management"],
)
async def propose_manage_finance_data(
    *,
    session: AsyncSession,
    ctx: CallContext,
    operation: str,
    target_id: str | None = None,
    secondary_id: str | None = None,
    payload: dict[str, Any] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    if operation not in _OPERATIONS:
        return {"error": "unsupported operation"}
    data = dict(payload or {})
    preview = {
        "kind": "manage_finance_data",
        "operation": operation,
        "target_id": target_id,
        "secondary_id": secondary_id,
        "payload": data,
    }
    if not _can_apply(ctx, apply):
        # Validate common payloads during preview so confirmation is meaningful.
        try:
            validators = {
                "category.create": CategoryCreate,
                "category.update": CategoryUpdate,
                "budget.update": BudgetUpdate,
                "goal.update": GoalUpdate,
                "payee.create": PayeeCreate,
                "payee.update": PayeeUpdate,
                "rule.create": RuleCreate,
                "rule.update": RuleUpdate,
                "group.create": GroupCreate,
                "group.update": GroupUpdate,
                "group.member_create": GroupMemberCreate,
                "group.member_update": GroupMemberUpdate,
                "collection.create": CollectionCreate,
                "collection.update": CollectionUpdate,
                "asset_group.create": AssetGroupCreate,
                "asset_group.update": AssetGroupUpdate,
                "asset.create": AssetCreate,
                "asset.update": AssetUpdate,
                "asset.add_value": AssetValueCreate,
                "asset.add_transaction": AssetTransactionCreate,
                "asset.update_transaction": AssetTransactionUpdate,
                "asset.buy": AssetBuyCreate,
                "transaction.update": TransactionUpdate,
                "transaction.transfer": TransferCreate,
            }
            validator = validators.get(operation)
            if validator is not None:
                validation_data = dict(data)
                if operation == "asset.update":
                    validation_data.pop("regenerate_growth", None)
                validator(**validation_data)
        except (ValidationError, ValueError) as exc:
            return {**preview, "error": str(exc)}
        return preview

    ws_id = await resolve_workspace_id(session, ctx)
    try:
        result: Any
        if operation == "category.create":
            result = await category_service.create_category(session, ws_id, ctx.user_id, CategoryCreate(**data))
        elif operation == "category.update":
            result = await category_service.update_category(session, _uuid(target_id), ws_id, CategoryUpdate(**data))
        elif operation == "category.delete":
            result = await category_service.delete_category(session, _uuid(target_id), ws_id)
        elif operation == "budget.update":
            result = await budget_service.update_budget(session, _uuid(target_id), ws_id, BudgetUpdate(**data))
        elif operation == "budget.delete":
            result = await budget_service.delete_budget(session, _uuid(target_id), ws_id)
        elif operation == "goal.update":
            result = await goal_service.update_goal(session, _uuid(target_id), ws_id, ctx.user_id, GoalUpdate(**data))
        elif operation == "goal.delete":
            result = await goal_service.delete_goal(session, _uuid(target_id), ws_id)
        elif operation == "payee.create":
            result = await payee_service.create_payee(session, ws_id, ctx.user_id, PayeeCreate(**data))
        elif operation == "payee.update":
            result = await payee_service.update_payee(session, _uuid(target_id), ws_id, PayeeUpdate(**data))
        elif operation == "payee.delete":
            result = await payee_service.delete_payee(session, _uuid(target_id), ws_id)
        elif operation == "payee.merge":
            target = _uuid(target_id)
            sources = parse_uuid_list(data.get("source_ids")) or []
            if not sources:
                raise ValueError("payload.source_ids is required")
            result = await payee_service.merge_payees(session, ws_id, target, sources)
        elif operation == "rule.create":
            rule_data = RuleCreate(**data)
            rule = await rule_service.create_rule(session, ws_id, ctx.user_id, rule_data)
            applied_count = 0
            if rule_data.apply_to_existing:
                applied_count = await rule_service.apply_single_rule(
                    session, ws_id, rule,
                    overwrite_existing_categories=rule_data.overwrite_existing_categories,
                )
            result = {"id": str(rule.id), "name": rule.name, "applied_count": applied_count}
        elif operation == "rule.update":
            rule_data = RuleUpdate(**data)
            rule = await rule_service.update_rule(session, _uuid(target_id), ws_id, rule_data)
            applied_count = 0
            if rule is not None and rule_data.apply_to_existing:
                applied_count = await rule_service.apply_single_rule(
                    session, ws_id, rule,
                    overwrite_existing_categories=rule_data.overwrite_existing_categories,
                )
            result = {"rule": _summary(rule), "applied_count": applied_count}
        elif operation == "rule.delete":
            result = await rule_service.delete_rule(session, _uuid(target_id), ws_id)
        elif operation == "rule.apply_all":
            result = {"applied_count": await rule_service.apply_all_rules(session, ws_id)}
        elif operation == "group.create":
            result = await group_service.create_group(session, ws_id, ctx.user_id, GroupCreate(**data))
        elif operation == "group.update":
            result = await group_service.update_group(session, _uuid(target_id), ws_id, ctx.user_id, GroupUpdate(**data))
        elif operation == "group.delete":
            result = await group_service.delete_group(session, _uuid(target_id), ws_id)
        elif operation == "group.member_create":
            result = await group_service.create_member(session, _uuid(target_id), ws_id, GroupMemberCreate(**data))
        elif operation == "group.member_update":
            result = await group_service.update_member(
                session, _uuid(target_id), _uuid(secondary_id, "secondary_id"), ws_id, GroupMemberUpdate(**data)
            )
        elif operation == "group.member_delete":
            result = await group_service.delete_member(
                session, _uuid(target_id), _uuid(secondary_id, "secondary_id"), ws_id
            )
        elif operation == "collection.create":
            result = await collection_service.create_collection(session, ws_id, ctx.user_id, CollectionCreate(**data))
        elif operation == "collection.update":
            result = await collection_service.update_collection(session, _uuid(target_id), ws_id, CollectionUpdate(**data))
        elif operation == "collection.delete":
            result = await collection_service.delete_collection(session, _uuid(target_id), ws_id)
        elif operation == "asset_group.create":
            result = await asset_group_service.create_group(session, ws_id, ctx.user_id, AssetGroupCreate(**data))
        elif operation == "asset_group.update":
            result = await asset_group_service.update_group(
                session, _uuid(target_id), ws_id, ctx.user_id, AssetGroupUpdate(**data)
            )
        elif operation == "asset_group.delete":
            result = await asset_group_service.delete_group(session, _uuid(target_id), ws_id)
        elif operation == "asset.create":
            result = await asset_service.create_asset(session, ws_id, ctx.user_id, AssetCreate(**data))
        elif operation == "asset.update":
            regenerate_growth = bool(data.pop("regenerate_growth", False))
            result = await asset_service.update_asset(
                session, _uuid(target_id), ws_id, ctx.user_id, AssetUpdate(**data), regenerate_growth=regenerate_growth
            )
        elif operation == "asset.delete":
            result = await asset_service.delete_asset(session, _uuid(target_id), ws_id)
        elif operation == "asset.add_value":
            result = await asset_service.add_asset_value(session, _uuid(target_id), ws_id, AssetValueCreate(**data))
        elif operation == "asset.delete_value":
            result = await asset_service.delete_asset_value(session, _uuid(target_id), ws_id)
        elif operation == "asset.add_transaction":
            result = await asset_transaction_service.add_transaction(
                session, _uuid(target_id), ws_id, AssetTransactionCreate(**data)
            )
        elif operation == "asset.update_transaction":
            result = await asset_transaction_service.update_transaction(
                session, _uuid(target_id), ws_id, AssetTransactionUpdate(**data)
            )
        elif operation == "asset.delete_transaction":
            result = await asset_transaction_service.delete_transaction(session, _uuid(target_id), ws_id)
        elif operation == "asset.buy":
            result = await asset_transaction_service.buy_into_holding(session, ws_id, ctx.user_id, AssetBuyCreate(**data))
        elif operation == "recurring.generate_pending":
            result = {"generated": await recurring_transaction_service.generate_pending(session, ctx.user_id)}
        elif operation == "transaction.update":
            result = await transaction_service.update_transaction(
                session, _uuid(target_id), ws_id, ctx.user_id, TransactionUpdate(**data)
            )
        elif operation == "transaction.delete":
            result = await transaction_service.delete_transaction(session, _uuid(target_id), ws_id)
        elif operation == "transaction.ignore":
            result = await transaction_service.toggle_ignore_transaction(session, _uuid(target_id), ws_id)
        elif operation == "transaction.unlink_recurring":
            result = await transaction_service.unlink_recurring_transaction(session, _uuid(target_id), ws_id)
        elif operation in {"transaction.add_tags", "transaction.remove_tags"}:
            ids = parse_uuid_list(data.get("transaction_ids")) or []
            tags = [str(t) for t in (data.get("tags") or [])]
            if not ids or not tags:
                raise ValueError("payload.transaction_ids and payload.tags are required")
            fn = transaction_service.bulk_add_tags if operation.endswith("add_tags") else transaction_service.bulk_remove_tags
            result = {"updated_count": await fn(session, ws_id, ids, tags)}
        elif operation == "transaction.transfer":
            result = await transaction_service.create_transfer(session, ws_id, ctx.user_id, TransferCreate(**data))
        elif operation == "transaction.link_transfer":
            ids = parse_uuid_list(data.get("transaction_ids")) or []
            result = await transaction_service.link_existing_as_transfer(session, ws_id, ids)
        elif operation == "transaction.create_counterpart":
            to_account_id = _uuid(data.get("to_account_id"), "payload.to_account_id")
            result = await transaction_service.create_transfer_counterpart(
                session, ws_id, ctx.user_id, _uuid(target_id), to_account_id
            )
        else:
            return {**preview, "error": "unsupported operation"}
    except (ValidationError, ValueError) as exc:
        return {**preview, "error": str(exc)}

    return {**preview, "applied": True, "result": _summary(result)}
