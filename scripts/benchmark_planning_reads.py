"""Compare bounded planning reads on disposable synthetic ledger data only."""

import argparse
import gc
import json
from pathlib import Path
import platform
from statistics import median
import sys
import tempfile
from time import perf_counter
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_processing import duplicate_link_cycles


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=2000)
    parser.add_argument("--payload-bytes", type=int, default=8192)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--batch-limit", type=int, default=500)
    parser.add_argument("--confirmed-cycle", action="store_true", help="seed a synthetic cycle spanning all documents")
    args = parser.parse_args(argv)
    if not 1 <= args.documents <= 5000:
        parser.error("--documents must be between 1 and 5000")
    if not 1 <= args.payload_bytes <= 65536:
        parser.error("--payload-bytes must be between 1 and 65536")
    if args.documents * (2 * args.payload_bytes + min(args.payload_bytes, 2048) + 1024) > 128 * 1024 * 1024:
        parser.error("synthetic payload budget exceeds 128 MiB")
    if not 1 <= args.samples <= 10:
        parser.error("--samples must be between 1 and 10")
    if not 1 <= args.batch_limit <= 500:
        parser.error("--batch-limit must be between 1 and 500")
    return args


def compare(reference, optimized, samples, *, reference_name="fullPayload", optimized_name="lightweight"):
    expected = reference()
    assert optimized() == expected
    operations = {reference_name: reference, optimized_name: optimized}
    timings = {name: [] for name in operations}
    for index in range(samples):
        order = list(operations) if index % 2 == 0 else list(reversed(operations))
        for name in order:
            gc.collect()
            started = perf_counter()
            result = operations[name]()
            elapsed = perf_counter() - started
            assert result == expected
            timings[name].append(elapsed * 1000)
    report = {"equivalent": True}
    for name, operation in operations.items():
        gc.collect()
        tracemalloc.start()
        try:
            result = operation()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert result == expected
        report[name] = {"medianMs": median(timings[name]), "samplesMs": timings[name], "peakTracedPythonBytes": peak}
    return report


def run(args):
    with tempfile.TemporaryDirectory(prefix="fab-planning-benchmark-") as directory:
        ledger = LocalOperationsLedger(str(Path(directory) / "synthetic.sqlite3"))
        payload = "X" * args.payload_bytes
        previous_id = None
        first_id = None
        with ledger.write_transaction():
            for index in range(args.documents):
                identifier = ledger.register_document({
                    "source": "synthetic_benchmark", "sourceDocumentId": str(index),
                    "originalFilename": "synthetic-receipt.txt", "processingStatus": "imported",
                    "ocrText": payload, "metadata": {"payload": payload[:2048]},
                    "duplicateOfDocumentId": previous_id if args.confirmed_cycle else None,
                })
                first_id = identifier if first_id is None else first_id
                previous_id = identifier
                ledger.create_review_item({"documentId": identifier, "reason": "synthetic",
                                           "status": "pending", "details": payload})
            if args.confirmed_cycle:
                ledger.update_document(first_id, {"duplicateOfDocumentId": previous_id})
        limit = args.batch_limit
        counters = compare(
            lambda: (len(ledger.list_documents(status="imported", limit=limit)),
                     len(ledger.list_review_items(status=("pending", "in_review"), limit=limit))),
            lambda: (ledger.count_documents(status="imported", limit=limit),
                     ledger.count_review_items(status=("pending", "in_review"), limit=limit)),
            args.samples,
        )
        graph = compare(
            lambda: [{"id": row["id"], "duplicate_of_document_id": row["duplicate_of_document_id"]}
                     for row in ledger.list_documents(limit=limit)],
            lambda: ledger.list_document_duplicate_links(limit=limit),
            args.samples,
        )
        full_history = compare(
            lambda: duplicate_link_cycles(ledger, limit=5000),
            lambda: duplicate_link_cycles(ledger),
            args.samples, reference_name="boundedDiagnosticWindow", optimized_name="fullHistory",
        )
    return {
        "syntheticOnly": True, "providerWrites": False, "temporaryDataRemoved": True,
        "pythonVersion": platform.python_version(),
        "workload": {"documents": args.documents, "reviews": args.documents,
                     "ocrAndReviewPayloadBytesEach": args.payload_bytes, "batchLimit": limit,
                     "samplesPerRead": args.samples, "cacheState": "warm",
                     "confirmedDuplicateLinks": args.documents if args.confirmed_cycle else 0},
        "boundedCounters": counters, "duplicateGraphProjection": graph, "fullHistoryCycleScan": full_history,
        "measurementScope": "planning read paths only; traced Python allocations, not process RSS or whole-app resource usage",
    }


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), indent=2))
