import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.document_processors import tesseract_processor as module


class TestTesseractResourceBounds(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.info = Mock(return_value={"Pages": 2})
        self.render = Mock(side_effect=self.render_page)
        self.ocr = Mock(side_effect=lambda page, **kwargs: self.read_page(page))
        fake_ocr = SimpleNamespace(pytesseract=SimpleNamespace(), image_to_string=self.ocr)
        for name, value in (
            ("pdfinfo_from_path", self.info), ("convert_from_path", self.render),
            ("pytesseract", fake_ocr), ("resolve_tesseract_command", lambda _: "tesseract"),
            ("resolve_poppler_path", lambda _: None),
        ):
            patcher = patch.object(module, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pages = []
        self.processor = module.TesseractProcessor({})

    def page(self, number):
        page = SimpleNamespace(number=number, close=Mock(side_effect=lambda: self.events.append(("close", number))))
        self.pages.append(page)
        return page

    def render_page(self, path, **kwargs):
        number = kwargs["first_page"]
        self.events.append(("render", number))
        return [self.page(number)]

    def read_page(self, page):
        self.events.append(("ocr", page.number))
        return "Page " + str(page.number)

    def test_pdf_pages_are_bounded_and_closed_before_next_render(self):
        result = self.processor.process_document("ordinary.pdf")
        self.assertEqual(result["ocr_text"], "Page 1\n\nPage 2")
        self.assertEqual(self.events, [("render", 1), ("ocr", 1), ("close", 1), ("render", 2), ("ocr", 2), ("close", 2)])
        self.assertGreater(self.info.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(self.info.call_args.kwargs["timeout"], 30)
        for call in self.render.call_args_list:
            self.assertEqual(call.kwargs["first_page"], call.kwargs["last_page"])
            self.assertTrue(0 < call.kwargs["size"] <= 3000)
            self.assertTrue(0 < call.kwargs["timeout"] <= 30)
        for call in self.ocr.call_args_list:
            self.assertTrue(0 < call.kwargs["timeout"] <= 30)

    def test_excess_pages_fail_before_render_not_truncated(self):
        self.info.return_value = {"Pages": 21}
        result = self.processor.process_document("many.pdf")
        self.assertIn("error", result)
        self.assertEqual(result["ocr_text"], "")
        self.render.assert_not_called()

    def test_invalid_page_counts_fail_before_render(self):
        for value in (0, -1, True, "2", None):
            with self.subTest(value=value):
                self.info.return_value = {"Pages": value}
                self.assertIn("error", self.processor.process_document("invalid.pdf"))
                self.render.assert_not_called()

    def test_ocr_timeout_closes_page_without_rendering_rest(self):
        self.ocr.side_effect = RuntimeError("OCR timeout")
        result = self.processor.process_document("ordinary.pdf")
        self.assertIn("error", result)
        self.assertEqual(result["ocr_text"], "")
        self.render.assert_called_once()
        self.pages[0].close.assert_called_once()

    def test_later_conversion_failure_discards_partial_text(self):
        self.render.side_effect = [ [self.page(1)], RuntimeError("conversion timeout") ]
        result = self.processor.process_document("ordinary.pdf")
        self.assertIn("error", result)
        self.assertEqual(result["ocr_text"], "")
        self.pages[0].close.assert_called_once()

    def test_missing_or_extra_rendered_pages_fail_closed_and_cleanup(self):
        for count in (0, 2):
            with self.subTest(count=count):
                pages = [self.page(i) for i in range(count)]
                self.render.side_effect = None
                self.render.return_value = pages
                self.assertIn("error", self.processor.process_document("ordinary.pdf"))
                for page in pages:
                    page.close.assert_called_once()

    def test_metadata_timeout_never_renders(self):
        self.info.side_effect = RuntimeError("metadata timeout")
        self.assertIn("error", self.processor.process_document("ordinary.pdf"))
        self.render.assert_not_called()

    def test_fallback_timeout_bounds_both_attempts_and_closes_images(self):
        self.info.return_value = {"Pages": 1}
        prepared = self.page(99)
        self.ocr.side_effect = ["", RuntimeError("fallback timeout")]
        with patch.object(self.processor, "_prepare_low_contrast_page", return_value=prepared):
            result = self.processor.process_document("ordinary.pdf")
        self.assertIn("error", result)
        for call in self.ocr.call_args_list:
            self.assertTrue(0 < call.kwargs["timeout"] <= 30)
        for page in self.pages:
            page.close.assert_called_once()

    def test_list_page_loader_cleans_all_pages_on_success_and_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                pages = [self.page(1), self.page(2)]
                self.ocr.side_effect = RuntimeError("OCR timeout") if failure else None
                self.ocr.return_value = "ordinary text"
                with patch.object(self.processor, "_load_pages", return_value=pages):
                    result = self.processor.process_document("ordinary.pdf")
                self.assertEqual("error" in result, failure)
                for page in pages:
                    page.close.assert_called_once()

    def test_ordinary_image_preserves_extracted_fields(self):
        page = self.page(1)
        self.ocr.side_effect = None
        self.ocr.return_value = "Shop\n01-09-2026\nTotaal EUR 12,10"
        with patch.object(module.Image, "open", return_value=page):
            result = self.processor.process_document("receipt.png")
        self.assertEqual(result["extracted_data"]["total_amount"], 12.1)
        self.assertEqual(result["ocr_strategy"], "standard")
        self.assertTrue(0 < self.ocr.call_args.kwargs["timeout"] <= 30)
        page.close.assert_called_once()
        self.render.assert_not_called()

    def test_document_deadline_stops_before_next_page_and_discards_partial_text(self):
        self.processor.config["tesseract_document_timeout_seconds"] = 2
        clock = [0.0]

        def ocr(page, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 2)
            clock[0] = 3.0
            return "Partial text"

        self.ocr.side_effect = ocr
        with patch.object(module, "monotonic", side_effect=lambda: clock[0], create=True):
            result = self.processor.process_document("ordinary.pdf")
        self.assertIn("deadline", result.get("error", "").lower())
        self.assertEqual(result["ocr_text"], "")
        self.assertEqual(self.render.call_count, 1)
        self.pages[0].close.assert_called_once()

    def test_document_deadline_covers_metadata_and_last_page(self):
        self.processor.config["tesseract_document_timeout_seconds"] = 2
        self.info.return_value = {"Pages": 1}
        clock = [0.0]

        def info(*args, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 2)
            clock[0] = 1.5
            return {"Pages": 1}

        def ocr(page, **kwargs):
            self.assertLessEqual(kwargs["timeout"], .5)
            clock[0] = 2.5
            return "Too late"

        self.info.side_effect = info
        self.ocr.side_effect = ocr
        with patch.object(module, "monotonic", side_effect=lambda: clock[0], create=True):
            result = self.processor.process_document("ordinary.pdf")
        self.assertIn("deadline", result.get("error", "").lower())
        self.assertEqual(result["ocr_text"], "")


if __name__ == "__main__":
    unittest.main()
