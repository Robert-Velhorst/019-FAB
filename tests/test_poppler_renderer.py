import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from src.document_processors import poppler_renderer as renderer


def test_one_page_without_metadata_or_version_probes(tmp_path):
    source = tmp_path / "receipt with spaces.pdf"
    calls = []

    def run(arguments, timeout):
        calls.append((arguments, timeout))
        Image.new("RGB", (24, 12), "white").save(arguments[-1] + ".ppm")

    with patch.object(renderer, "_run_renderer", side_effect=run):
        pages = renderer.render_pdf_page(str(source), dpi=220, first_page=2,
            last_page=2, poppler_path=str(tmp_path), size=3000, timeout=3, thread_count=1)
    try:
        assert len(calls) == 1
        args, timeout = calls[0]
        assert timeout == 3
        assert args[1:-2] == ["-f", "2", "-l", "2", "-singlefile", "-r", "220", "-scale-to", "3000"]
        assert args[-2] == str(source.resolve())
        assert pages[0].size == (24, 12)
        assert pages[0].getpixel((0, 0)) == (255, 255, 255)
        assert not Path(args[-1]).parent.exists()
    finally:
        for page in pages:
            page.close()


@pytest.mark.parametrize("overrides", [
    {"size": 3001}, {"size": 0}, {"timeout": float("nan")},
    {"timeout": float("inf")}, {"timeout": 0}, {"timeout": 31},
    {"first_page": 0}, {"first_page": True}, {"last_page": 2},
    {"thread_count": 2}, {"dpi": 1000},
])
def test_invalid_limits_fail_before_spawning(overrides):
    config = {"dpi": 220, "first_page": 1, "last_page": 1,
              "size": 3000, "timeout": 30, "thread_count": 1, **overrides}
    with patch.object(renderer, "_run_renderer") as run:
        with pytest.raises(ValueError):
            renderer.render_pdf_page("source.pdf", **config)
        run.assert_not_called()


def test_real_child_timeout_is_reaped_and_redacted():
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="PDF rendering timed out") as error:
        renderer._run_renderer([sys.executable, "-c", "import time; time.sleep(30)"], .1)
    assert time.monotonic() - started < 5
    assert sys.executable not in str(error.value)


def test_real_nonzero_exit_does_not_disclose_stderr():
    with pytest.raises(RuntimeError, match="PDF rendering failed") as error:
        renderer._run_renderer([sys.executable, "-c", "import sys; print('private-source', file=sys.stderr); sys.exit(7)"], 5)
    assert "private-source" not in str(error.value)


def test_failure_cleans_temporary_files(tmp_path):
    paths = []

    def fail(arguments, timeout):
        paths.append(Path(arguments[-1]).parent)
        Path(arguments[-1] + ".ppm").write_bytes(b"partial")
        raise RuntimeError("PDF rendering timed out")

    with patch.object(renderer, "_run_renderer", side_effect=fail):
        with pytest.raises(RuntimeError):
            renderer.render_pdf_page("source.pdf", poppler_path=str(tmp_path))
    assert paths and not paths[0].exists()


def test_oversized_raster_rejected_before_decode(tmp_path):
    def render(arguments, timeout):
        Path(arguments[-1] + ".ppm").write_bytes(b"P6\n3001 1\n255\n")

    with patch.object(renderer, "_run_renderer", side_effect=render):
        with pytest.raises(ValueError, match="dimensions"):
            renderer.render_pdf_page("source.pdf", poppler_path=str(tmp_path))


def test_real_poppler_renders_ordinary_pdf(tmp_path):
    from src.utils.tesseract_runtime import resolve_poppler_path
    poppler = resolve_poppler_path()
    if not poppler:
        pytest.skip("Poppler is not installed")
    source = tmp_path / "ordinary.pdf"
    with Image.new("RGB", (100, 50), "white") as original:
        original.save(source, "PDF")
    pages = renderer.render_pdf_page(str(source), poppler_path=poppler, size=300)
    try:
        assert len(pages) == 1
        assert max(pages[0].size) == 300
    finally:
        for page in pages:
            page.close()


def test_renderer_timeout_reaches_ledger_review_and_blocks_export(tmp_path):
    import hashlib
    from unittest.mock import Mock
    from src.document_processors.processor_pipeline import ProcessorPipeline
    from src.operations.local_ledger import LocalOperationsLedger
    from src.operations.local_processing import LocalDocumentProcessor

    source = tmp_path / "source.pdf"
    original = b"%PDF-synthetic-timeout-fixture\n"
    source.write_bytes(original)
    ledger = LocalOperationsLedger(str(tmp_path / "ledger.sqlite3"))
    document_id = ledger.register_document({
        "source": "local_folder", "sourceDocumentId": "timeout-test",
        "originalFilename": source.name, "storagePath": str(source),
        "contentSha256": hashlib.sha256(original).hexdigest(),
        "mimeType": "application/pdf", "documentType": "receipt",
        "processingStatus": "imported",
    })
    config = {"enable_enhanced_preprocessing": False, "enable_template_matching": False,
              "enable_line_item_extraction": False, "primary_ocr_method": "tesseract"}
    with patch("src.document_processors.tesseract_processor.resolve_tesseract_command", return_value="tesseract"), \
            patch("src.document_processors.tesseract_processor.pdfinfo_from_path", return_value={"Pages": 1}), \
            patch.object(renderer, "_run_renderer", side_effect=RuntimeError("PDF rendering timed out; no OCR result accepted.")):
        pipeline = ProcessorPipeline(config)
        result = LocalDocumentProcessor(ledger, config, processor_pipeline=pipeline,
            categorizer=Mock(), validator=Mock()).process_document(document_id)
    document = ledger.get_document(document_id)
    assert result["status"] == "failed"
    assert source.read_bytes() == original
    assert not document.get("ocr_text")
    assert any(item["reason"] == "processing_failed" for item in document["review_items"])
    assert document["bookkeeping_record"]["export_status"] == "blocked_processing"
