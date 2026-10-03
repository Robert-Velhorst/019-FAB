import pytest

from src.operations.local_bank_transactions import LocalBankTransactionImportService
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_reconciliation import LocalReconciliationService
from src.operations.local_review import LocalReviewService


@pytest.fixture
def arriving_receipt(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    bank = LocalBankTransactionImportService(ledger)
    bank.import_transactions([{
        "id": "synthetic-arrival", "date": "2026-09-30", "amount": -42.5,
        "description": "Synthetic Shop",
    }])
    service = LocalReconciliationService(ledger)
    missing_id = service.run(bank.transactions_for_reconciliation())["results"][0]["reconciliationMatchId"]
    missing_review = ledger.list_review_items(status="pending")[0]["id"]
    document_id = ledger.register_document({
        "source": "manual", "sourceDocumentId": "synthetic-arriving-document", "processingStatus": "processed",
        "vendorName": "Synthetic Shop", "transactionDate": "2026-09-30", "totalAmount": 42.5,
    })
    candidate_id = service.run(bank.transactions_for_reconciliation())["results"][0]["reconciliationMatchId"]
    return ledger, bank, service, missing_id, missing_review, document_id, candidate_id


def state(ledger):
    with ledger._connection() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {row[0]: [tuple(item) for item in connection.execute(
            'SELECT * FROM "' + row[0].replace('"', '""') + '" ORDER BY rowid'
        )] for row in tables}


@pytest.mark.parametrize("through_review", [False, True])
def test_confirmation_supersedes_its_missing_exception_without_deleting_history(arriving_receipt, through_review):
    ledger, _bank, service, missing_id, review_id, document_id, candidate_id = arriving_receipt
    original = ledger.get_reconciliation_match(missing_id)["metadata"]["bankTransaction"]
    if through_review:
        candidate_review = ledger.list_review_items(status="pending", document_id=document_id)[0]["id"]
        result = LocalReviewService(ledger).resolve_review_item(candidate_review, "approved", learn_rule=False)
    else:
        result = service.resolve_match(candidate_id, "approved")
    assert result["success"]
    assert ledger.get_review_item(review_id)["status"] == "resolved"
    missing = ledger.get_reconciliation_match(missing_id)
    assert missing["status"] == "resolved"
    assert missing["metadata"]["bankTransaction"] == original
    assert missing["metadata"]["supersededBy"]["reconciliationMatchId"] == candidate_id
    assert missing["metadata"]["supersededBy"]["documentId"] == document_id
    assert not ledger.list_review_items(status="pending")


def test_unconfirmed_receipt_does_not_silently_close_the_exception(arriving_receipt):
    ledger, _bank, _service, missing_id, review_id, _document_id, _candidate_id = arriving_receipt
    assert ledger.get_review_item(review_id)["status"] == "pending"
    assert ledger.get_reconciliation_match(missing_id)["status"] == "missing_receipt"


def test_supersession_failure_rolls_back_confirmation_and_exception_history(arriving_receipt, monkeypatch):
    ledger, _bank, service, _missing_id, _review_id, _document_id, candidate_id = arriving_receipt
    before = state(ledger)
    original = ledger.record_audit_event

    def fail(payload):
        if payload.get("action") == "local_reconciliation.missing_receipt.superseded":
            raise RuntimeError("Synthetic supersession failure")
        return original(payload)

    monkeypatch.setattr(ledger, "record_audit_event", fail)
    with pytest.raises(RuntimeError, match="Synthetic supersession failure"):
        service.resolve_match(candidate_id, "approved")
    assert state(ledger) == before


def test_confirmation_leaves_another_accounts_exception_open(arriving_receipt):
    ledger, bank, service, _missing_id, _review_id, _document_id, candidate_id = arriving_receipt
    bank.import_transactions([{"id": "synthetic-arrival", "date": "2026-09-30", "amount": -99}],
                             account_identifier="synthetic-other-account")
    other = next(row for row in bank.transactions_for_reconciliation() if row["account_identifier"] == "synthetic-other-account")
    other_id = service.run([other], document_ids=[999999])["results"][0]["reconciliationMatchId"]
    other_review = next(row["id"] for row in ledger.list_missing_receipt_review_items()
                        if row["corrected_data"]["reconciliationMatchId"] == other_id)
    assert service.resolve_match(candidate_id, "approved")["success"]
    assert ledger.get_review_item(other_review)["status"] == "pending"
    assert ledger.get_reconciliation_match(other_id)["status"] == "missing_receipt"


def add_matching_exceptions(ledger, original_id, count):
    original = ledger.get_reconciliation_match(original_id)
    ids = []
    with ledger.write_transaction():
        for _ in range(count):
            match_id = ledger.create_reconciliation_match({
                "bankTransactionId": original["bank_transaction_id"], "status": "missing_receipt",
                "metadata": original["metadata"],
            })
            review_id = ledger.create_review_item({
                "reason": "missing_receipt", "correctedData": {"reconciliationMatchId": match_id},
            })
            ids.append((match_id, review_id))
    return ids


def test_supersession_pages_changed_statuses_without_skipping_old_exceptions(arriving_receipt):
    ledger, _bank, service, missing_id, review_id, _document_id, candidate_id = arriving_receipt
    ids = add_matching_exceptions(ledger, missing_id, 205)
    assert service.resolve_match(candidate_id, "approved")["success"]
    for match_id, own_review in [(missing_id, review_id), *ids]:
        assert ledger.get_reconciliation_match(match_id)["status"] == "resolved"
        assert ledger.get_review_item(own_review)["status"] == "resolved"
    assert not ledger.list_review_items(status="pending")


def test_second_page_failure_rolls_back_every_supersession(arriving_receipt, monkeypatch):
    ledger, _bank, service, missing_id, _review_id, _document_id, candidate_id = arriving_receipt
    add_matching_exceptions(ledger, missing_id, 105)
    before = state(ledger)
    original = ledger.record_audit_event
    count = 0

    def fail(payload):
        nonlocal count
        if payload.get("action") == "local_reconciliation.missing_receipt.superseded":
            count += 1
            if count == 101:
                raise RuntimeError("Synthetic second page failure")
        return original(payload)

    monkeypatch.setattr(ledger, "record_audit_event", fail)
    with pytest.raises(RuntimeError, match="Synthetic second page failure"):
        service.resolve_match(candidate_id, "approved")
    assert state(ledger) == before


@pytest.mark.parametrize("closed_status", ["ignored", "rejected"])
def test_closed_human_disposition_is_not_rewritten_as_superseded(arriving_receipt, closed_status):
    ledger, _bank, service, missing_id, review_id, _document_id, candidate_id = arriving_receipt
    ledger.update_reconciliation_match(missing_id, {"status": closed_status})
    ledger.resolve_review_item(review_id, status=closed_status)
    old_match = ledger.get_reconciliation_match(missing_id)
    old_review = ledger.get_review_item(review_id)
    assert service.resolve_match(candidate_id, "approved")["success"]
    assert ledger.get_reconciliation_match(missing_id) == old_match
    assert ledger.get_review_item(review_id) == old_review


def test_conflicting_review_reference_stays_open_during_supersession(arriving_receipt):
    ledger, _bank, service, missing_id, review_id, _document_id, candidate_id = arriving_receipt
    ledger.resolve_review_item(review_id, status="pending", corrected_data={
        "reconciliationMatchId": missing_id, "reconciliation_match_id": missing_id + 100,
    })
    assert service.resolve_match(candidate_id, "approved")["success"]
    assert ledger.get_review_item(review_id)["status"] == "pending"


def test_missing_review_lookup_filters_before_returning_unrelated_backlog(arriving_receipt, monkeypatch):
    ledger, _bank, service, _missing_id, _review_id, _document_id, candidate_id = arriving_receipt
    with ledger.write_transaction():
        for index in range(205):
            ledger.create_review_item({"reason": "missing_receipt", "correctedData": {"reconciliationMatchId": 10000 + index}})
    original = ledger.list_missing_receipt_review_items
    returned = []

    def observed(*args, **kwargs):
        rows = original(*args, **kwargs)
        returned.extend(rows)
        return rows

    monkeypatch.setattr(ledger, "list_missing_receipt_review_items", observed)
    assert service.resolve_match(candidate_id, "approved")["success"]
    assert len(returned) == 1


@pytest.mark.parametrize("format_id", [lambda value: f"00{value}", lambda value: f"+{value}", lambda value: f" {value} "])
def test_native_review_filter_preserves_valid_integer_string_references(arriving_receipt, format_id):
    ledger, _bank, service, missing_id, review_id, _document_id, candidate_id = arriving_receipt
    ledger.resolve_review_item(review_id, status="pending", corrected_data={"reconciliationMatchId": format_id(missing_id)})
    assert service.resolve_match(candidate_id, "approved")["success"]
    assert ledger.get_review_item(review_id)["status"] == "resolved"
