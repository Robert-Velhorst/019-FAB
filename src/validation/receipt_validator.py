from typing import Dict, Any
import re

from src.validation.financial_consistency import (
    DEFAULT_VAT_MAX_TOTAL_RATIO,
    assess_record_date,
    assess_vat_amount,
    finite_number,
    vat_issue_message,
)

class ReceiptValidator:
    """Validates receipts for legal compliance and completeness."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.required_fields = self.config.get("receipt_validation_required_fields", [
            "vendor_name", "transaction_date", "total_amount"
        ])
        self.btw_number_pattern = self.config.get("btw_number_pattern", r"NL\d{9}B\d{2}")
        self.required_field_confidence_threshold = finite_number(
            self.config.get("receipt_required_field_confidence_threshold", 0.7)
        )
        if self.required_field_confidence_threshold is None or not 0 <= self.required_field_confidence_threshold <= 1:
            raise ValueError("receipt_required_field_confidence_threshold must be finite and between 0 and 1")
        try:
            self.vat_max_total_ratio = float(
                self.config.get("vat_max_total_ratio", DEFAULT_VAT_MAX_TOTAL_RATIO)
            )
        except (TypeError, ValueError):
            self.vat_max_total_ratio = DEFAULT_VAT_MAX_TOTAL_RATIO

    def validate_receipt(self, processed_data: Dict[str, Any]) -> Dict[str, Any]:
        """Performs validation checks on processed receipt data.

        Args:
            processed_data: A dictionary containing extracted data from the document.

        Returns:
            A dictionary with validation status and reasons for failure.
        """
        extracted_data = processed_data.get("extracted_data", {})
        field_confidences = processed_data.get("field_confidences", {}) or {}
        ocr_text = processed_data.get("ocr_text", "")
        errors = []
        warnings = []

        # 1. Check for required fields
        for field in self.required_fields:
            if not extracted_data.get(field):
                errors.append(f"Missing required field: {field}")
                continue
            confidence = field_confidences.get(field)
            if confidence is not None:
                number = finite_number(confidence)
                if number is None or not 0 <= number <= 1:
                    errors.append(f"Invalid confidence for required field: {field}")
                elif number < self.required_field_confidence_threshold:
                    errors.append(f"Low confidence for required field: {field} ({number:.2f})")

        if "vendor_name" in self.required_fields and not str(extracted_data.get("vendor_name") or "").strip():
            if "Missing required field: vendor_name" in errors:
                errors.remove("Missing required field: vendor_name")
            errors.append("Invalid or empty vendor_name")

        # 2. Validate VAT arithmetic before treating OCR tax evidence as financial data.
        vat_assessment = assess_vat_amount(
            extracted_data.get("vat_amount"),
            extracted_data.get("total_amount"),
            max_ratio=self.vat_max_total_ratio,
        )
        if not vat_assessment["valid"]:
            errors.append(vat_issue_message(vat_assessment))

        # 3. Validate BTW number (if present and relevant)
        if "btw_number" in extracted_data and extracted_data["btw_number"]:
            btw_number = str(extracted_data["btw_number"]).strip()
            if not re.match(self.btw_number_pattern, btw_number):
                errors.append("Invalid BTW number format")
            if btw_number not in ocr_text:
                errors.append("Extracted BTW number not found in OCR text")
        elif vat_assessment["valid"] and (vat_assessment.get("vatAmount") or 0) > 0:
            # If VAT is present but no BTW number, it might be an issue for business expenses
            if not re.search(self.btw_number_pattern, ocr_text, re.IGNORECASE):
                message = "VAT amount present but no valid BTW number found in document."
                if "btw_number" in self.required_fields:
                    errors.append(message)
                else:
                    warnings.append(message)

        # 4. Basic amount consistency check (e.g., total > 0)
        if extracted_data.get("total_amount") is not None:
            total = finite_number(extracted_data["total_amount"])
            if total is None:
                errors.append("Total amount must be a finite numeric value.")
            elif total <= 0:
                errors.append("Total amount is zero or negative.")

        # 5. Date plausibility validation. The extractor normalizes to ISO, but
        # retained OCR can contain valid-looking years that are not credible.
        transaction_date = extracted_data.get("transaction_date")
        transaction_date_assessment = assess_record_date(transaction_date)
        if transaction_date and not transaction_date_assessment["valid"]:
            if transaction_date_assessment["reason"] == "implausible_record_date_year":
                errors.append("Implausible transaction_date year")
            else:
                errors.append("Invalid transaction_date format")

        is_valid = len(errors) == 0
        reason = "; ".join(errors)

        return {
            "is_valid": is_valid,
            "errors": errors,
            "warnings": warnings,
            "reason": reason,
            "blocking": not is_valid,
            "fieldControls": {
                "transactionDate": transaction_date_assessment,
                "vat": vat_assessment,
            },
        }


