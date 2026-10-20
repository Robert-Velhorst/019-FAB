from unittest.mock import patch

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_processing import trusted_category_suggestion_candidates


def test_category_work_is_not_hidden_by_5001_newer_unrelated_reviews(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    with ledger.write_transaction():
        subject = ledger.register_document({
            "source": "synthetic", "sourceDocumentId": "eligible",
            "vendorName": "Praxis", "category": "Manual Review",
            "documentType": "receipt", "processingStatus": "needs_review",
        })
        category_review = ledger.create_review_item({
            "documentId": subject, "reason": "manual_review_category", "details": "Synthetic gate",
        })
        for _ in range(5001):
            ledger.create_review_item({"reason": "missing_receipt", "details": "Unrelated synthetic review"})
    candidates = trusted_category_suggestion_candidates(ledger, limit=1)
    assert [row["document"]["id"] for row in candidates] == [subject]
    assert [row["id"] for row in candidates[0]["reviewItems"]] == [category_review]


def test_category_cursor_covers_equal_timestamp_pages_and_skips_ineligible_vendors(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    identifiers = []
    with ledger.write_transaction(), patch.object(ledger, "_now", return_value="2026-01-01T00:00:00Z"):
        for index in range(205):
            document_id = ledger.register_document({
                "source": "synthetic", "sourceDocumentId": str(index),
                "vendorName": "Praxis" if index == 0 else "Unknown synthetic vendor",
                "category": "Manual Review", "documentType": "receipt",
                "processingStatus": "needs_review",
            })
            identifiers.append(document_id)
            ledger.create_review_item({"documentId": document_id, "reason": "manual_review_category"})
            ledger.create_review_item({"documentId": document_id, "reason": "validation_failed"})
    contexts = list(ledger.iter_category_review_context())
    assert [row["document"]["id"] for row in contexts] == list(reversed(identifiers))
    assert all({review["reason"] for review in row["reviewItems"]}
               == {"manual_review_category", "validation_failed"} for row in contexts)
    assert [row["document"]["id"] for row in trusted_category_suggestion_candidates(ledger, limit=1)] == identifiers[:1]


def test_category_planning_does_not_hydrate_unrelated_reviews_or_duplicate_context(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    with ledger.write_transaction():
        document_id = ledger.register_document({
            "source": "synthetic", "sourceDocumentId": "eligible", "vendorName": "Praxis",
            "category": "Manual Review", "documentType": "receipt", "processingStatus": "needs_review",
        })
        ledger.create_review_item({"documentId": document_id, "reason": "manual_review_category"})
        for _ in range(25):
            ledger.create_review_item({"reason": "missing_receipt", "details": "unrelated payload"})
    hydrate = ledger._row_to_dict

    def hydrate_only_selected(row):
        assert "details" not in row.keys() or row["details"] != "unrelated payload"
        return hydrate(row)

    with (patch.object(ledger, "_row_to_dict", side_effect=hydrate_only_selected) as hydrated,
          patch.object(ledger, "get_review_work_item_context", side_effect=AssertionError("Unneeded duplicate context")),
          patch.object(ledger, "list_review_items", side_effect=AssertionError("Global review page"))):
        assert len(trusted_category_suggestion_candidates(ledger)) == 1
    assert hydrated.call_count == 2


def test_category_context_excludes_closed_and_orphaned_reviews_and_preserves_full_group(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    with ledger.write_transaction():
        document_id = ledger.register_document({"source": "synthetic", "sourceDocumentId": "group"})
        ledger.create_review_item({"documentId": document_id, "reason": "manual_review_category"})
        for index in range(501):
            ledger.create_review_item({"documentId": document_id, "reason": f"synthetic_gate_{index}"})
        ledger.create_review_item({"documentId": document_id, "reason": "duplicate_candidate"})
        ledger.create_review_item({"documentId": document_id, "reason": "manual_review_category", "status": "resolved"})
        ledger.create_review_item({"documentId": document_id + 1000, "reason": "manual_review_category"})
        ledger.create_review_item({"reason": "manual_review_category"})
    rows = list(ledger.iter_category_review_context())
    assert len(rows) == 1
    assert len(rows[0]["reviewItems"]) == 503
    assert {row["status"] for row in rows[0]["reviewItems"]} == {"pending"}
    assert rows[0]["reviewItems"][0]["reason"] == "duplicate_candidate"
