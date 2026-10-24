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
    parser.add_argument("--category-reviews", action="store_true", help="compare sparse category-review context reads")
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
    if args.category_reviews and args.confirmed_cycle:
        parser.error("category context and confirmed cycle workloads must be measured separately")
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
                reason = "manual_review_category" if args.category_reviews and index % 100 == 0 else "synthetic"
                ledger.create_review_item({"documentId": identifier, "reason": reason,
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
        category_context = None
        if args.category_reviews:
            def global_review_reference():
                reviews = []
                while len(reviews) < 5000:
                    page = ledger.list_review_items(status=("pending", "in_review"), limit=500, offset=len(reviews))
                    reviews.extend(page)
                    if len(page) < 500:
                        break
                ids = {int(row["document_id"]) for row in reviews
                       if row["reason"] in {"manual_review_category", "low_confidence_categorization"}}
                documents = {}
                for start in range(0, len(ids), 500):
                    documents.update(ledger.get_review_work_item_context(sorted(ids)[start:start + 500])["documents"])
                groups = {identifier: [] for identifier in ids}
                for review in reviews:
                    if review["document_id"] in groups:
                        groups[review["document_id"]].append(review)
                return [{"document": row, "reviewItems": groups[int(row["id"])]}
                        for row in sorted(documents.values(), key=lambda row: (row["updated_at"], row["id"]), reverse=True)]

            category_context = compare(
                global_review_reference, lambda: list(ledger.iter_category_review_context()), args.samples,
                reference_name="globalReviewWindow", optimized_name="filteredCategoryCursor",
            )
    return {
        "syntheticOnly": True, "providerWrites": False, "temporaryDataRemoved": True,
        "pythonVersion": platform.python_version(),
        "workload": {"documents": args.documents, "reviews": args.documents,
                     "ocrAndReviewPayloadBytesEach": args.payload_bytes, "batchLimit": limit,
                     "samplesPerRead": args.samples, "cacheState": "warm",
                     "categoryReviews": (args.documents + 99) // 100 if args.category_reviews else 0,
                     "confirmedDuplicateLinks": args.documents if args.confirmed_cycle else 0},
        "boundedCounters": counters, "duplicateGraphProjection": graph, "fullHistoryCycleScan": full_history,
        "categoryReviewContext": category_context,
        "measurementScope": "planning read paths only; traced Python allocations, not process RSS or whole-app resource usage",
    }


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), indent=2))
