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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=2000)
    parser.add_argument("--payload-bytes", type=int, default=8192)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--batch-limit", type=int, default=500)
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


def compare(reference, optimized, samples):
    expected = reference()
    assert optimized() == expected
    timings = {"fullPayload": [], "lightweight": []}
    operations = {"fullPayload": reference, "lightweight": optimized}
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
        with ledger.write_transaction():
            for index in range(args.documents):
                identifier = ledger.register_document({
                    "source": "synthetic_benchmark", "sourceDocumentId": str(index),
                    "originalFilename": "synthetic-receipt.txt", "processingStatus": "imported",
                    "ocrText": payload, "metadata": {"payload": payload[:2048]},
                })
                ledger.create_review_item({"documentId": identifier, "reason": "synthetic",
                                           "status": "pending", "details": payload})
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
    return {
        "syntheticOnly": True, "providerWrites": False, "temporaryDataRemoved": True,
        "pythonVersion": platform.python_version(),
        "workload": {"documents": args.documents, "reviews": args.documents,
                     "ocrAndReviewPayloadBytesEach": args.payload_bytes, "batchLimit": limit,
                     "samplesPerRead": args.samples, "cacheState": "warm"},
        "boundedCounters": counters, "duplicateGraphProjection": graph,
        "measurementScope": "planning read paths only; traced Python allocations, not process RSS or whole-app resource usage",
    }


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), indent=2))
