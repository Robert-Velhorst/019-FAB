from unittest.mock import patch

import pytest

from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_processing import duplicate_link_cycles


def document(ledger, identifier, status="imported"):
    return ledger.register_document({"source": "synthetic", "sourceDocumentId": str(identifier),
        "originalFilename": "receipt.pdf", "processingStatus": status,
        "ocrText": "private synthetic OCR " * 100, "metadata": {"nested": {"evidence": "retained"}}})


def test_duplicate_cycle_scan_honors_requested_window_beyond_500_rows(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    with ledger.write_transaction():
        first = document(ledger, "cycle-first")
        second = document(ledger, "cycle-second")
        ledger.update_document(first, {"duplicateOfDocumentId": second})
        ledger.update_document(second, {"duplicateOfDocumentId": first})
        for index in range(501):
            document(ledger, index)
    assert duplicate_link_cycles(ledger) == [[first, second]]
    assert duplicate_link_cycles(ledger, limit=500) == []


def test_duplicate_graph_never_hydrates_ocr_or_json(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    identifier = document(ledger, "self-cycle")
    ledger.update_document(identifier, {"duplicateOfDocumentId": identifier})
    with patch.object(ledger, "_row_to_dict", side_effect=AssertionError("Unneeded payload hydration")):
        assert duplicate_link_cycles(ledger) == [[identifier]]


def test_default_duplicate_cycle_scan_covers_history_older_than_5000_rows(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    with ledger.write_transaction():
        first = document(ledger, "old-cycle-first")
        second = document(ledger, "old-cycle-second")
        ledger.update_document(first, {"duplicateOfDocumentId": second})
        ledger.update_document(second, {"duplicateOfDocumentId": first})
        for index in range(5001):
            identifier = document(ledger, index)
            ledger.update_document(identifier, {"duplicateOfDocumentId": first})
    assert duplicate_link_cycles(ledger) == [[first, second]]
    assert duplicate_link_cycles(ledger, limit=5000) == []


@pytest.mark.parametrize("reverse", [False, True])
def test_full_history_graph_handles_multiple_cycles_inbound_chains_and_missing_targets(reverse):
    from unittest.mock import MagicMock

    links = [(1, 1), (2, 3), (3, 2), (4, 2), (5, 99), (6, 5)]
    if reverse:
        links.reverse()
    ledger = MagicMock()
    ledger.iter_document_duplicate_links.return_value = iter(
        {"id": source, "duplicate_of_document_id": target} for source, target in links
    )
    ledger.list_document_duplicate_links.side_effect = AssertionError("Default scan cannot use a bounded window")
    assert duplicate_link_cycles(ledger) == [[1], [2, 3]]


def test_batch_counts_keep_the_500_row_ceiling_on_larger_queues(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    with ledger.write_transaction():
        for index in range(503):
            identifier = document(ledger, index)
            ledger.create_review_item({"documentId": identifier, "reason": "synthetic", "status": "pending"})
    assert ledger.count_documents(status="imported", limit=1000) == 500
    assert ledger.count_review_items(status="pending", limit=1000) == 500
    assert ledger.count_documents(status="imported", limit=25) == 25
    assert ledger.count_review_items(status="pending", limit=25) == 25


def test_cycle_recheck_reads_all_selected_ids_without_payloads_or_ui_cap(tmp_path):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    with ledger.write_transaction():
        identifiers = [document(ledger, index) for index in range(1003)]
        ledger.update_document(identifiers[-1], {"duplicateOfDocumentId": identifiers[0]})
    with patch.object(ledger, "_row_to_dict", side_effect=AssertionError("Unneeded graph payload")):
        targets = ledger.document_duplicate_targets(identifiers)
    assert len(targets) == len(identifiers)
    assert targets[identifiers[-1]] == identifiers[0]
    assert targets[identifiers[0]] is None
    assert ledger.document_duplicate_targets([]) == {}


@pytest.mark.parametrize("value", [True, 0, -1, "1", 1.5, 2 ** 63])
def test_cycle_recheck_rejects_invalid_identity_values(tmp_path, value):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    with pytest.raises(ValueError, match="positive integers"):
        ledger.document_duplicate_targets([value])


@pytest.mark.parametrize("status", [None, "imported", ("imported", "pending"), [], [None, ""], "pending') OR 1=1 --"])
@pytest.mark.parametrize("limit", [1, 3, 500, 1000, 0, "invalid"])
def test_bounded_counts_match_existing_list_filter_and_limit_semantics(tmp_path, status, limit):
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    with ledger.write_transaction():
        for index in range(9):
            identifier = document(ledger, index, status="imported" if index % 2 else "validated")
            ledger.create_review_item({"documentId": identifier, "reason": "synthetic",
                "status": "pending" if index % 2 else "resolved", "details": "retained evidence"})
    document_count = len(ledger.list_documents(status=status, limit=limit))
    review_count = len(ledger.list_review_items(status=status, limit=limit))
    with patch.object(ledger, "_row_to_dict", side_effect=AssertionError("Unneeded count hydration")):
        assert ledger.count_documents(status=status, limit=limit) == document_count
        assert ledger.count_review_items(status=status, limit=limit) == review_count
