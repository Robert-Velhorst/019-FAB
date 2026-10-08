"""Bounded synthetic matcher benchmark; no accounts, files, database or network."""

import gc
import json
from pathlib import Path
import platform
from statistics import median
import sys
from time import perf_counter
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.reconciliation.automated_reconciliation import AutomatedReconciliation


class UnpreparedReconciliation(AutomatedReconciliation):
    def _match_score(self, bank, document):
        return super()._match_score(bank, document)


def measure(engine, bank, documents, expected):
    assert engine.reconcile(bank, documents) == expected
    samples = []
    for _ in range(3):
        gc.collect()
        started = perf_counter()
        result = engine.reconcile(bank, documents)
        samples.append(perf_counter() - started)
        assert result == expected
    del result
    gc.collect()
    tracemalloc.start()
    try:
        result = engine.reconcile(bank, documents)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == expected
    return {"medianSeconds": median(samples), "samplesSeconds": samples, "peakTracedBytes": peak}


def main():
    reports = []
    for case in ("different_amounts", "same_amount_ties"):
        bank = [{"id": f"bank-{i}", "amount": -1000 - i if case == "different_amounts" else -4.28,
                 "date": "2026-09-30", "description": "Synthetic Shop"} for i in range(500)]
        documents = [{"document_id": f"doc-{i}", "total_amount": i + 1 if case == "different_amounts" else 4.28,
                      "transaction_date": "2026-09-30", "vendor_name": "Synthetic Shop"} for i in range(100)]
        baseline = UnpreparedReconciliation({})
        expected = baseline.reconcile(bank, documents)
        reports.append({"case": case, "bankRows": len(bank), "documentRows": len(documents),
                        "unprepared": measure(baseline, bank, documents, expected),
                        "default": measure(AutomatedReconciliation({}), bank, documents, expected),
                        "resultsIdentical": True})
    print(json.dumps({"syntheticOnly": True, "python": platform.python_version(), "samples": 3,
                      "memoryMetric": "tracemalloc Python allocation peak; not process RAM", "cases": reports}))


if __name__ == "__main__":
    main()
