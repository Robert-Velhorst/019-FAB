import math
import re
from datetime import date
from typing import Any, Dict, Optional


DEFAULT_VAT_MAX_TOTAL_RATIO = 0.25


def assess_record_date(value: Any, *, today: Optional[date] = None) -> Dict[str, Any]:
    evidence = str(value or "").strip()
    result = {
        "present": bool(evidence),
        "valid": True,
        "reason": None,
        "evidenceValue": evidence or None,
        "normalizedValue": None,
    }
    if not evidence:
        return result

    parsed = _parse_record_date(evidence, today=today)
    if parsed is None:
        result.update({"valid": False, "reason": "invalid_or_ambiguous_record_date"})
        return result
    reference = today or date.today()
    if parsed.year < 1900 or parsed.year > reference.year + 1:
        result.update({"valid": False, "reason": "implausible_record_date_year"})
        return result
    result["normalizedValue"] = parsed.isoformat()
    return result


def assess_vat_amount(
    vat_amount: Any,
    total_amount: Any,
    *,
    max_ratio: float = DEFAULT_VAT_MAX_TOTAL_RATIO,
) -> Dict[str, Any]:
    vat = finite_number(vat_amount)
    total = finite_number(total_amount)
    ratio_limit = _ratio(max_ratio)
    result = {
        "present": vat_amount is not None and vat_amount != "",
        "valid": True,
        "reason": None,
        "vatAmount": vat,
        "totalAmount": total,
        "absoluteRatio": None,
        "maxAbsoluteRatio": ratio_limit,
    }
    if vat is None:
        if result["present"]:
            result.update({"valid": False, "reason": "invalid_vat_amount"})
        return result
    if vat == 0:
        return result
    if total is None or total == 0:
        result.update({"valid": False, "reason": "vat_without_nonzero_total"})
        return result
    if (vat < 0) != (total < 0):
        result.update({"valid": False, "reason": "vat_total_sign_mismatch"})
        return result
    absolute_ratio = abs(vat) / abs(total)
    result["absoluteRatio"] = absolute_ratio
    if absolute_ratio > ratio_limit:
        result.update({"valid": False, "reason": "vat_exceeds_total_ratio"})
    return result


def valid_vat_amount(
    vat_amount: Any,
    total_amount: Any,
    *,
    max_ratio: float = DEFAULT_VAT_MAX_TOTAL_RATIO,
) -> Optional[float]:
    assessment = assess_vat_amount(vat_amount, total_amount, max_ratio=max_ratio)
    return assessment["vatAmount"] if assessment["valid"] else None


def vat_issue_message(assessment: Dict[str, Any]) -> str:
    reason = str(assessment.get("reason") or "")
    if reason == "invalid_vat_amount":
        return "VAT amount must be a finite numeric value."
    if reason == "vat_without_nonzero_total":
        return "VAT amount cannot be verified without a non-zero total amount."
    if reason == "vat_total_sign_mismatch":
        return "VAT amount and total amount have conflicting signs."
    if reason == "vat_exceeds_total_ratio":
        percentage = float(assessment.get("maxAbsoluteRatio") or DEFAULT_VAT_MAX_TOTAL_RATIO) * 100
        return f"VAT amount exceeds the configured {percentage:.1f}% of total safety limit."
    return "VAT amount failed financial consistency validation."


def finite_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _ratio(value: Any) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return DEFAULT_VAT_MAX_TOTAL_RATIO
    if not math.isfinite(ratio) or ratio <= 0 or ratio > 1:
        return DEFAULT_VAT_MAX_TOTAL_RATIO
    return ratio


def _parse_record_date(value: str, *, today: Optional[date] = None) -> Optional[date]:
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass

    match = re.fullmatch(r"(\d{1,2})([./-])(\d{1,2})\2(\d{2}|\d{4})", value)
    if not match:
        return None
    day, month, raw_year = int(match.group(1)), int(match.group(3)), int(match.group(4))
    if len(match.group(4)) == 2:
        current_two_digit_year = (today or date.today()).year % 100
        raw_year += 2000 if raw_year <= current_two_digit_year + 1 else 1900
    try:
        return date(raw_year, month, day)
    except ValueError:
        return None
