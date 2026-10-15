"""Safe construction helpers for Wave money-transaction requests."""

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from typing import Any, Dict, Optional


WAVE_GRAPHQL_URL = "https://gql.waveapps.com/graphql/public"

MONEY_TRANSACTION_CREATE_MUTATION = """
mutation ($input: MoneyTransactionCreateInput!) {
  moneyTransactionCreate(input: $input) {
    didSucceed
    inputErrors {
      path
      message
      code
    }
    transaction {
      id
    }
  }
}
"""


def build_expense_transaction_input(
    data: Dict[str, Any],
    *,
    business_id: Any,
    anchor_account_id: Any,
    category_mapping: Any,
    category_account_ids: Any,
    default_category_account_id: Any = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a balanced Wave expense or vendor-credit transaction."""
    extracted = data.get("extracted_data") if isinstance(data.get("extracted_data"), dict) else {}
    document_type = _document_type(data, extracted)
    transaction_direction = str(
        data.get("transaction_direction")
        or extracted.get("transaction_direction")
        or ""
    ).strip().lower()
    is_credit = document_type == "credit_note" or transaction_direction == "deposit"
    category = str(data.get("category") or "").strip()
    mapped_category = str(_as_mapping(category_mapping).get(category) or category).strip()
    account_ids = _as_mapping(category_account_ids)
    resolved_description = str(description or extracted.get("description") or "Automated expense").strip()
    amount = _unsigned_amount(extracted.get("total_amount"))
    transaction_date = _date(extracted.get("transaction_date"))
    raw_line_items = extracted.get("line_items") if isinstance(extracted.get("line_items"), list) else []
    line_items, line_item_missing = _expense_line_items(
        raw_line_items,
        fallback_category=category,
        fallback_mapped_category=mapped_category,
        category_mapping=category_mapping,
        category_account_ids=account_ids,
        default_category_account_id=default_category_account_id,
        total_amount=amount,
        line_balance="DECREASE" if is_credit else "INCREASE",
    )
    missing = list(line_item_missing) + [
        name
        for name, value in (
            ("businessId", business_id),
            ("anchorAccountId", anchor_account_id),
            ("transactionDate", transaction_date),
            ("totalAmount", amount),
        )
        if value in (None, "")
    ]
    if missing:
        return {
            "success": False,
            "missingFields": missing,
            "mappedCategory": mapped_category or None,
            "message": "Wave transaction requires verified account mapping and financial fields: " + ", ".join(missing) + ".",
        }

    external_id = _external_id(data)
    return {
        "success": True,
        "mappedCategory": mapped_category,
        "input": {
            "businessId": str(business_id),
            "externalId": external_id,
            "date": transaction_date,
            "description": resolved_description,
            "anchor": {
                "accountId": str(anchor_account_id),
                "amount": amount,
                "direction": "DEPOSIT" if is_credit else "WITHDRAWAL",
            },
            "lineItems": line_items,
        },
    }


def _expense_line_items(
    raw_items: list,
    *,
    fallback_category: str,
    fallback_mapped_category: str,
    category_mapping: Any,
    category_account_ids: Dict[str, Any],
    default_category_account_id: Any,
    total_amount: Optional[float],
    line_balance: str,
) -> tuple[list[Dict[str, Any]], list[str]]:
    mappings = _as_mapping(category_mapping)
    if not raw_items:
        account_id = (
            category_account_ids.get(fallback_mapped_category)
            or category_account_ids.get(fallback_category)
        )
        if not account_id:
            return [], ["categoryAccountId"]
        return [{
            "accountId": str(account_id),
            "amount": total_amount,
            "balance": line_balance,
        }], []

    line_items = []
    missing = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            missing.append(f"lineItems[{index}]")
            continue
        line_category = str(item.get("category") or fallback_category).strip()
        mapped_category = str(mappings.get(line_category) or line_category or fallback_mapped_category).strip()
        account_label = str(item.get("account") or item.get("account_name") or "").strip()
        account_id = (
            category_account_ids.get(mapped_category)
            or category_account_ids.get(line_category)
            or category_account_ids.get(account_label)
        )
        line_amount = _unsigned_amount(item.get("amount"))
        if not account_id:
            missing.append(f"lineItems[{index}].accountId")
        if line_amount is None:
            missing.append(f"lineItems[{index}].amount")
        if account_id and line_amount is not None:
            line_items.append({
                "accountId": str(account_id),
                "amount": line_amount,
                "balance": line_balance,
            })

    if not missing and total_amount is not None:
        item_total = sum((Decimal(str(item["amount"])) for item in line_items), Decimal("0"))
        expected_total = Decimal(str(total_amount))
        if item_total.quantize(Decimal("0.01")) != expected_total.quantize(Decimal("0.01")):
            missing.append("lineItemTotal")
    return line_items, missing


def wave_request_failure(exc: Exception) -> Dict[str, Any]:
    """Return a safe result without claiming that an unverified write did not happen."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    definitive_rejection = (
        isinstance(status_code, int)
        and 400 <= status_code < 500
        and status_code not in {408, 409, 425, 429}
    )
    if definitive_rejection:
        return {
            "status": "failure",
            "message": "Wave rejected the transaction request. Review the provider setup before trying again.",
            "review_reason": "wave_api_request_rejected",
            "requires_manual_review": True,
        }
    return {
        "status": "outcome_unknown",
        "message": "Wave did not return a verifiable result. Check Wave for this transaction before taking further action.",
        "review_reason": "wave_api_outcome_unknown",
        "requires_manual_review": True,
    }


def _as_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _unsigned_amount(value: Any) -> Optional[float]:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return float(abs(amount)) if amount != 0 else None


def _document_type(data: Dict[str, Any], extracted: Dict[str, Any]) -> str:
    value = (
        data.get("document_type")
        or data.get("documentType")
        or extracted.get("document_type")
        or extracted.get("documentType")
        or ""
    )
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _date(value: Any) -> Optional[str]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def _external_id(data: Dict[str, Any]) -> str:
    for key in ("idempotency_key", "posting_attempt_id", "document_id"):
        value = data.get(key)
        if value not in (None, ""):
            return f"fab:{value}"
    return "fab:unidentified"
