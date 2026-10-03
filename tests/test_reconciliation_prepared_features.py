import copy
import random

import pytest

from src.reconciliation.automated_reconciliation import AutomatedReconciliation


class UnpreparedReconciliation(AutomatedReconciliation):
    def _match_score(self, bank, document):
        return super()._match_score(bank, document)


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("config", [{}, {"reconciliation_match_threshold": 0, "reconciliation_date_tolerance_days": 2},
    {"reconciliation_use_absolute_amounts": False, "reconciliation_match_threshold": 0.8}])
def test_prepared_matching_preserves_complete_results_and_input(config, seed):
    rng = random.Random(seed)
    amounts = [None, False, 0, 4.28, -4.28, "EUR 1.234,56", "1234.56", "not-an-amount", "1e400"]
    dates = [None, "2026-09-30", "30/09/2026", "2026-10-02", "invalid"]
    vendors = ["", "Synthetic Shop", "SYNTHETIC SHOP", "Another Shop", "Cafe Belgie"]
    bank = [{"id": f"bank-{index}", "amount": rng.choice(amounts), "date": rng.choice(dates),
             "description": rng.choice(vendors)} for index in range(40)]
    documents = [{"document_id": f"doc-{index // 2}", "extracted_data": {
        "total_amount": rng.choice(amounts), "transaction_date": rng.choice(dates),
        "vendor_name": rng.choice(vendors)}} for index in range(30)]
    before = copy.deepcopy((bank, documents))
    assert AutomatedReconciliation(config).reconcile(bank, documents) == UnpreparedReconciliation(config).reconcile(bank, documents)
    assert (bank, documents) == before


def test_feature_parsing_is_linear_in_rows_not_comparisons(monkeypatch):
    engine = AutomatedReconciliation({})
    counts = {}
    for name in ("_amount", "_date", "_vendor_text"):
        original = getattr(engine, name)

        def observed(value, original=original, name=name):
            counts[name] = counts.get(name, 0) + 1
            return original(value)

        monkeypatch.setattr(engine, name, observed)
    bank = [{"id": f"bank-{i}", "amount": -1000 - i, "date": "2026-09-30"} for i in range(100)]
    documents = [{"document_id": f"doc-{i}", "total_amount": i + 1, "date": "2026-09-30"} for i in range(80)]
    result = engine.reconcile(bank, documents)
    assert len(result) == 180
    assert counts["_amount"] <= 180, counts
    assert counts["_date"] <= 180, counts
    assert counts["_vendor_text"] <= 180, counts


def test_custom_score_override_keeps_its_original_contract():
    class Custom(AutomatedReconciliation):
        def _match_score(self, bank, document):
            return (1.0, 0.0) if document["document_id"] == "chosen" else None

    result = Custom({}).reconcile([{"id": "bank"}], [{"document_id": "other"}, {"document_id": "chosen"}])
    assert result[0]["document_id"] == "chosen"


def test_empty_bank_batch_does_not_parse_document_payload(monkeypatch):
    engine = AutomatedReconciliation({})
    monkeypatch.setattr(engine, "_document_payload", lambda _: pytest.fail("Empty bank batch parsed financial fields"))
    assert engine.reconcile([], [{"document_id": "doc"}])[0]["type"] == "unmatched_document"


def test_identical_prepared_vendors_do_not_allocate_fuzzy_matchers(monkeypatch):
    bank = [{"id": f"bank-{i}", "amount": -4.28, "date": "2026-09-30", "description": "Synthetic Shop"} for i in range(20)]
    documents = [{"document_id": f"doc-{i}", "total_amount": 4.28, "date": "2026-09-30",
                  "vendor_name": "SYNTHETIC SHOP"} for i in range(10)]
    expected = UnpreparedReconciliation({}).reconcile(bank, documents)
    monkeypatch.setattr("src.reconciliation.automated_reconciliation.SequenceMatcher",
                        lambda *_: pytest.fail("Identical normalized vendors used fuzzy matching"))
    assert AutomatedReconciliation({}).reconcile(bank, documents) == expected


def test_prepared_features_do_not_survive_between_runs():
    engine = AutomatedReconciliation({})
    bank = [{"id": "bank", "amount": -4.28, "date": "2026-09-30", "description": "Synthetic Shop"}]
    documents = [{"document_id": "doc", "total_amount": 4.28, "date": "2026-09-30", "vendor_name": "Synthetic Shop"}]
    assert engine.reconcile(bank, documents)[0]["type"] == "match"
    documents[0]["total_amount"] = 99
    assert [row["type"] for row in engine.reconcile(bank, documents)] == ["unmatched_bank_transaction", "unmatched_document"]
