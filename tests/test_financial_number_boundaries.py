from decimal import Decimal

import pytest

from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService
from src.reconciliation.automated_reconciliation import AutomatedReconciliation
from src.validation.receipt_validator import ReceiptValidator


INVALID_NUMBERS = [True, False, float("nan"), float("inf"), float("-inf"),
                   Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), 10 ** 400,
                   "NaN", "Infinity", "1e400"]


@pytest.mark.parametrize("amount", [4.28, 27.81, 1234.56, 0.1])
def test_imported_decimal_amount_can_match_confirm_and_retry_without_false_staleness(tmp_path, amount):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    bank = LocalBankTransactionImportService(ledger)
    bank.import_transactions([{"id": "synthetic-decimal", "date": "2026-09-30", "amount": -amount,
                               "description": "Synthetic Shop"}])
    document = ledger.register_document({"source": "manual", "sourceDocumentId": "synthetic-decimal-doc",
                                        "processingStatus": "processed", "vendorName": "Synthetic Shop",
                                        "transactionDate": "2026-09-30", "totalAmount": amount})
    service = LocalReconciliationService(ledger)
    result = service.run(bank.transactions_for_reconciliation())
    match = result["results"][0]["reconciliationMatchId"]
    assert result["matchedCandidates"] == 1
    assert service.resolve_match(match, "approved")["success"]
    assert service.resolve_match(match, "approved")["alreadyFinal"]
    assert ledger.get_document_core(document)["reconciliation_status"] == "reconciled"


@pytest.mark.parametrize("value", INVALID_NUMBERS)
def test_invalid_financial_number_is_not_a_reconciliation_amount(value):
    assert AutomatedReconciliation._amount(value) is None


@pytest.mark.parametrize("value", INVALID_NUMBERS)
def test_invalid_bank_import_row_is_skipped_without_poisoning_valid_rows(tmp_path, value):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    result = LocalBankTransactionImportService(ledger).import_transactions([
        {"id": "invalid", "date": "2026-09-30", "amount": value},
        {"id": "valid", "date": "2026-09-30", "amount": "-4.28"},
    ])
    assert result["rowsImported"] == 1 and result["skipped"] == 1
    assert [row["transaction_id"] for row in ledger.list_bank_transactions()] == ["valid"]


@pytest.mark.parametrize("value", INVALID_NUMBERS)
def test_ledger_numeric_boundary_does_not_store_nonfinite_or_boolean_amount(value):
    assert LocalOperationsLedger._float(value) is None


def receipt(**fields):
    return {"extracted_data": {"vendor_name": "Synthetic Shop", "transaction_date": "2026-09-30",
                                "total_amount": 4.28, **fields}}


@pytest.mark.parametrize("value", INVALID_NUMBERS)
def test_invalid_receipt_total_is_blocked_without_crashing(value):
    result = ReceiptValidator({}).validate_receipt(receipt(total_amount=value))
    assert not result["is_valid"] and result["blocking"]


def test_numeric_receipt_string_is_validated_without_type_error():
    assert ReceiptValidator({}).validate_receipt(receipt(total_amount="4.28"))["is_valid"]


@pytest.mark.parametrize("value", INVALID_NUMBERS)
def test_present_invalid_vat_is_not_silently_treated_as_missing(value):
    result = ReceiptValidator({}).validate_receipt(receipt(vat_amount=value))
    assert not result["is_valid"] and not result["fieldControls"]["vat"]["valid"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, True])
def test_invalid_confidence_cannot_bypass_receipt_review(value):
    data = receipt()
    data["field_confidences"] = {"total_amount": value}
    assert not ReceiptValidator({}).validate_receipt(data)["is_valid"]


@pytest.mark.parametrize("config", [
    {"reconciliation_match_threshold": "NaN"}, {"reconciliation_match_threshold": -0.1},
    {"reconciliation_match_threshold": 1.1}, {"reconciliation_amount_tolerance": "NaN"},
    {"reconciliation_amount_tolerance": "Infinity"}, {"reconciliation_amount_tolerance": -1},
])
def test_invalid_match_configuration_cannot_disable_quality_thresholds(config):
    with pytest.raises(ValueError):
        AutomatedReconciliation(config)


@pytest.mark.parametrize("value", [0, False, "NaN", "invalid"])
def test_explicit_invalid_or_zero_document_total_cannot_be_replaced_by_alternate_amount(value):
    results = AutomatedReconciliation({}).reconcile(
        [{"id": "synthetic", "amount": -4.28, "date": "2026-09-30", "description": "Synthetic Shop"}],
        [{"document_id": "synthetic", "total_amount": value, "amount": 4.28,
          "transaction_date": "2026-09-30", "vendor_name": "Synthetic Shop"}],
    )
    assert not any(result["type"] == "match" for result in results)


@pytest.mark.parametrize("value", ["1e3", "1.5E2"])
def test_valid_scientific_amount_is_not_reinterpreted_by_stripping_exponent(tmp_path, value):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    result = LocalBankTransactionImportService(ledger).import_transactions([
        {"id": "synthetic-exponent", "date": "2026-09-30", "amount": value},
    ])
    assert result["rowsImported"] == 1
    assert ledger.list_bank_transactions()[0]["amount"] == float(value)
    assert AutomatedReconciliation._amount(value) == Decimal(value)


@pytest.mark.parametrize("value", ["NaN", "Infinity", -0.1, 1.1, True])
def test_invalid_receipt_confidence_threshold_is_rejected(value):
    with pytest.raises(ValueError):
        ReceiptValidator({"receipt_required_field_confidence_threshold": value})
