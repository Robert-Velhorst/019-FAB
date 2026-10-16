"""Bounded synthetic matcher benchmark; no accounts, files, database or network."""

import argparse
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

MAX_BENCHMARK_SAMPLES = 10
MAX_CANDIDATE_PAIRS = 10_000_000


class UnpreparedReconciliation(AutomatedReconciliation):
    def _match_score(self, bank, document):
        return super()._match_score(bank, document)


def measure(engine, bank, documents, expected, sample_count):
    assert engine.reconcile(bank, documents) == expected
    samples = []
    for _ in range(sample_count):
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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Compare the optimized matcher with an unprepared synthetic baseline."
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help=f"timed runs per matcher and case (1-{MAX_BENCHMARK_SAMPLES}; default: 3)",
    )
    parser.add_argument(
        "--sparse-bank-rows",
        type=int,
        default=2000,
        help="bank rows in the sparse-large case (default: 2000)",
    )
    parser.add_argument(
        "--sparse-document-rows",
        type=int,
        default=400,
        help="document rows in the sparse-large case (default: 400)",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= MAX_BENCHMARK_SAMPLES:
        parser.error(f"--samples must be between 1 and {MAX_BENCHMARK_SAMPLES}")
    if args.sparse_bank_rows < 1 or args.sparse_document_rows < 1:
        parser.error("sparse workload row counts must be positive")
    if args.sparse_bank_rows * args.sparse_document_rows > MAX_CANDIDATE_PAIRS:
        parser.error(
            "benchmark workload exceeds the supported ceiling of "
            f"{MAX_CANDIDATE_PAIRS:,} bank/document pairs"
        )
    return args


def main(argv=None):
    args = parse_args(argv)
    reports = []
    for case in ("different_amounts", "same_amount_ties", "sparse_large"):
        bank_rows, document_rows = (
            (args.sparse_bank_rows, args.sparse_document_rows)
            if case == "sparse_large"
            else (500, 100)
        )
        bank = [{"id": f"bank-{i}", "amount": -1000 - i if case != "same_amount_ties" else -4.28,
                 "date": "2026-09-30", "description": "Synthetic Shop"} for i in range(bank_rows)]
        documents = [{"document_id": f"doc-{i}", "total_amount": i + 1 if case != "same_amount_ties" else 4.28,
                      "transaction_date": "2026-09-30", "vendor_name": "Synthetic Shop"} for i in range(document_rows)]
        baseline = UnpreparedReconciliation({})
        expected = baseline.reconcile(bank, documents)
        reports.append({"case": case, "bankRows": len(bank), "documentRows": len(documents),
                        "unprepared": measure(baseline, bank, documents, expected, args.samples),
                        "default": measure(AutomatedReconciliation({}), bank, documents, expected, args.samples),
                        "resultsIdentical": True})
    print(json.dumps({"syntheticOnly": True, "python": platform.python_version(), "samples": args.samples,
                      "memoryMetric": "tracemalloc Python allocation peak; not process RAM", "cases": reports}))


if __name__ == "__main__":
    main()
