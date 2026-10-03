import unittest
from unittest.mock import MagicMock, patch
import json
import os
import shutil
import tempfile
try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

from src.document_processors.vision_processor import VisionProcessor
from src.document_processors.tesseract_processor import TesseractProcessor
from src.document_processors.template_matching_processor import TemplateMatchingProcessor
from src.document_processors.line_item_extractor import LineItemExtractor
from src.document_processors.enhanced_processor import EnhancedProcessor
import src.document_processors.enhanced_processor as enhanced_module
from src.document_processors.processor_pipeline import ProcessorPipeline
from src.document_processors.dutch_ocr_processor import DutchOcrProcessor
from src.document_processors.handwritten_recognition_processor import HandwrittenRecognitionProcessor
from src.document_processors.bilingual_processor import BilingualProcessor
import src.document_processors.handwritten_recognition_processor as handwritten_module
import src.document_processors.vision_processor as vision_module

class TestDocumentProcessors(unittest.TestCase):

    def setUp(self):
        if Image is None:
            self.skipTest("Pillow is required for document processor image fixtures")

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        fake_tesseract = os.path.join(self.temp_dir.name, "tesseract.exe")
        with open(fake_tesseract, "wb") as handle:
            handle.write(b"test")
        tessdata_dir = os.path.join(self.temp_dir.name, "tessdata")
        os.makedirs(tessdata_dir)
        for language in ("eng", "nld"):
            with open(os.path.join(tessdata_dir, f"{language}.traineddata"), "wb") as handle:
                handle.write(b"test")
        self.config = {
            "google_vision_credentials_file": os.path.join(self.temp_dir.name, "vision_credentials.json"),
            "tesseract_cmd": fake_tesseract,
            "tesseract_data_dir": tessdata_dir,
            "tesseract_lang": "nld+eng",
            "dutch_ocr_lang": "nld",
            "handwritten_model_path": os.path.join(self.temp_dir.name, "handwritten_model.pth"),
            "template_matching_templates_dir": os.path.join(self.temp_dir.name, "templates"),
            "vendor_templates_file": os.path.join(self.temp_dir.name, "vendor_templates.json")
        }
        # Create dummy credential file for VisionProcessor
        with open(self.config["google_vision_credentials_file"], "w") as f:
            f.write("{}")
        os.makedirs(self.config["template_matching_templates_dir"], exist_ok=True)

        # Create a dummy image file for testing
        self.dummy_image_path = os.path.join(self.temp_dir.name, "dummy_receipt.png")
        img = Image.new("RGB", (100, 50), color = (255, 255, 255))
        img.save(self.dummy_image_path)

        # Create a dummy PDF file for testing
        self.dummy_pdf_path = os.path.join(self.temp_dir.name, "dummy_invoice.pdf")
        # This is a very basic way to create a dummy PDF, for real tests, use a library like reportlab
        with open(self.dummy_pdf_path, "w") as f:
            f.write("%PDF-1.4\n1 0 obj<</Type/Page/Contents 2 0 R>>endobj 2 0 obj<</Length 11>>stream\nHello World\nendstream endobj\nxref\n0 3\n0000000000 65535 f\n0000000009 00000 n\n0000000045 00000 n\ntrailer<</Size 3/Root 1 0 R>>startxref\n103\n%%EOF")

    def test_vision_processor(self):
        fake_vision = MagicMock()
        mock_client_instance = MagicMock()
        fake_vision.ImageAnnotatorClient.return_value = mock_client_instance
        mock_client_instance.document_text_detection.return_value = MagicMock(
            full_text_annotation=MagicMock(text="Vision OCR Text", pages=[])
        )

        with patch.object(vision_module, "vision", fake_vision):
            processor = VisionProcessor(self.config)
            result = processor.process_document(self.dummy_image_path)
        self.assertIn("Vision OCR Text", result["ocr_text"])

    @patch("src.document_processors.tesseract_processor.pytesseract.image_to_string")
    @patch("src.document_processors.tesseract_processor.Image.open")
    def test_tesseract_processor(self, mock_image_open, mock_image_to_string):
        mock_image_to_string.return_value = "Tesseract OCR Text"
        mock_image_open.return_value = MagicMock()

        processor = TesseractProcessor(self.config)
        result = processor.process_document(self.dummy_image_path)
        self.assertIn("Tesseract OCR Text", result["ocr_text"])
        self.assertEqual(result["language"], "nld+eng")
        self.assertEqual(result["ocr_strategy"], "standard")

    @patch("src.document_processors.tesseract_processor.pytesseract.image_to_string")
    def test_tesseract_processor_retries_blank_low_contrast_scan(self, mock_image_to_string):
        mock_image_to_string.side_effect = [
            "",
            "Lidl Arnhem\nBETALING 01/06/2023\nTotaal EUR 12,02\nBTW 0,99",
        ]

        result = TesseractProcessor(self.config).process_document(self.dummy_image_path)

        self.assertEqual(result["ocr_strategy"], "illumination_normalized_fallback")
        self.assertEqual(result["ocr_fallback_pages"], 1)
        self.assertEqual(result["ocr_fallback_recovered_pages"], 1)
        self.assertEqual(result["extracted_data"]["vendor_name"], "Lidl Arnhem")
        self.assertEqual(result["extracted_data"]["total_amount"], 12.02)
        self.assertEqual(result["extracted_data"]["vat_amount"], 0.99)
        self.assertIn("--psm 6", mock_image_to_string.call_args_list[1].kwargs["config"])

    @patch("src.document_processors.tesseract_processor.pytesseract.image_to_string", return_value="")
    def test_tesseract_processor_records_unsuccessful_fallback_attempt(self, mock_image_to_string):
        result = TesseractProcessor(self.config).process_document(self.dummy_image_path)

        self.assertEqual(result["ocr_text"], "")
        self.assertEqual(result["ocr_strategy"], "illumination_normalized_fallback")
        self.assertEqual(result["ocr_fallback_pages"], 1)
        self.assertEqual(result["ocr_fallback_recovered_pages"], 0)
        self.assertEqual(mock_image_to_string.call_count, 2)

    def test_tesseract_processor_extracts_dutch_receipt_fields(self):
        fields = TesseractProcessor._extract_data_from_text(
            "Winkel Arnhem\nDatum 19-07-2026\nBTW 21% 2,10\nTotaal EUR 12,10"
        )

        self.assertEqual(fields["vendor_name"], "Winkel Arnhem")
        self.assertEqual(fields["transaction_date"], "19-07-2026")
        self.assertEqual(fields["total_amount"], 12.1)
        self.assertEqual(fields["vat_amount"], 2.1)
        self.assertEqual(fields["currency"], "EUR")

        common_ocr_variant = TesseractProcessor._extract_data_from_text("BIW 21% 2,10\nTotaal EUR 12,10")
        self.assertEqual(common_ocr_variant["vat_amount"], 2.1)

    @patch("src.document_processors.tesseract_processor.pdfinfo_from_path")
    @patch("src.document_processors.tesseract_processor.convert_from_path")
    @patch("src.document_processors.tesseract_processor.pytesseract.image_to_string")
    def test_tesseract_processor_renders_and_reads_pdf_pages(self, mock_image_to_string, mock_convert, mock_pdfinfo):
        pages = [MagicMock(), MagicMock()]
        mock_pdfinfo.return_value = {"Pages": 2}
        mock_convert.side_effect = [[pages[0]], [pages[1]]]
        mock_image_to_string.side_effect = ["Page one", "Page two"]
        with open(os.path.join(self.temp_dir.name, "pdftoppm.exe"), "wb") as handle:
            handle.write(b"test")
        self.config["poppler_path"] = self.temp_dir.name

        result = TesseractProcessor(self.config).process_document(self.dummy_pdf_path)

        self.assertEqual(result["ocr_text"], "Page one\n\nPage two")
        self.assertEqual(mock_image_to_string.call_count, 2)
        mock_pdfinfo.assert_called_once_with(
            self.dummy_pdf_path,
            poppler_path=self.temp_dir.name,
            timeout=30,
        )
        self.assertEqual(mock_convert.call_count, 2)
        for number, render_call in enumerate(mock_convert.call_args_list, 1):
            self.assertEqual(render_call.args, (self.dummy_pdf_path,))
            self.assertEqual(render_call.kwargs, {
                "dpi": 220,
                "first_page": number,
                "last_page": number,
                "poppler_path": self.temp_dir.name,
                "size": 3000,
                "timeout": 30,
                "thread_count": 1,
            })
        for page in pages:
            page.close.assert_called_once()
        for ocr_call in mock_image_to_string.call_args_list:
            self.assertEqual(ocr_call.kwargs["timeout"], 30)

    @patch("src.document_processors.dutch_ocr_processor.TesseractProcessor")
    def test_dutch_ocr_processor(self, MockTesseractProcessor):
        MockTesseractProcessor.return_value.process_document.return_value = {
            "ocr_text": "Winkel\nTotaal EUR 12,10",
            "extracted_data": {},
            "language": "nld",
        }

        result = DutchOcrProcessor(self.config).process_document(self.dummy_image_path)
        self.assertIn("Winkel", result["ocr_text"])
        self.assertEqual(result["extracted_data"]["total_amount"], 12.1)
        self.assertEqual(result["language"], "nl")

    def test_handwritten_recognition_processor(self):
        fake_cv2 = MagicMock()
        fake_cv2.imread.return_value = MagicMock()
        fake_np = MagicMock()
        fake_image = MagicMock()
        fake_pytesseract = MagicMock()
        fake_pytesseract.image_to_string.return_value = "Handwritten Text"

        with (
            patch.object(handwritten_module, "cv2", fake_cv2),
            patch.object(handwritten_module, "np", fake_np),
            patch.object(handwritten_module, "Image", fake_image),
            patch.object(handwritten_module, "pytesseract", fake_pytesseract),
        ):
            processor = HandwrittenRecognitionProcessor(self.config)
            result = processor.process_document(self.dummy_image_path)
        self.assertIn("Handwritten Text", result["ocr_text"])

    def test_template_matching_processor(self):
        with open(self.config["vendor_templates_file"], "w", encoding="utf-8") as handle:
            json.dump({
                "VendorA": {
                    "keywords": ["Vendor A", "VendorA"],
                    "extraction_patterns": {
                        "transaction_date": r"Date: (\d{2}-\d{2}-\d{4})",
                        "total_amount": r"Total: (\d+[.,]\d{2})",
                        "vat_amount": r"VAT: (\d+[.,]\d{2})",
                        "currency": r"Currency: ([A-Z]{3})",
                    },
                    "line_item_pattern": r"^(?P<description>.+?)\s+(?P<total>\d+[.,]\d{2})$",
                }
            }, handle)
        config = {"vendor_templates_file": self.config["vendor_templates_file"]}
        processor = TemplateMatchingProcessor(config)
        result = processor.process_document(
            self.dummy_image_path,
            ocr_text=(
                "Vendor A\nDate: 31-07-2026\nCurrency: EUR\n"
                "Consulting 100,00\nVAT: 21,00\nTotal: 121,00"
            ),
        )

        self.assertEqual(result["extracted_data"]["vendor_name"], "VendorA")
        self.assertEqual(result["extracted_data"]["transaction_date"], "2026-07-31")
        self.assertEqual(result["extracted_data"]["total_amount"], 121.0)
        self.assertEqual(result["extracted_data"]["vat_amount"], 21.0)
        self.assertEqual(result["extracted_data"]["currency"], "EUR")
        self.assertEqual(
            result["extracted_data"]["line_items"],
            [{"description": "Consulting", "total": 100.0}],
        )
        self.assertEqual(result["field_confidences"]["vendor_name"], 0.95)
        self.assertEqual(result["field_evidence"]["total_amount"]["source"], "vendor_template")
        self.assertEqual(processor.template_errors, [])

    def test_template_matching_isolates_invalid_user_templates(self):
        processor = TemplateMatchingProcessor({
            "vendor_templates": {
                "Broken": {"keywords": "["},
                "Valid": {
                    "keywords": "Valid Vendor",
                    "total_pattern": r"Total: (\d+[.,]\d{2})",
                },
            }
        })

        result = processor.process_document(
            self.dummy_image_path,
            ocr_text="Valid Vendor\nTotal: 12,34",
        )

        self.assertEqual(result["extracted_data"]["vendor_name"], "Valid")
        self.assertEqual(result["extracted_data"]["total_amount"], 12.34)
        self.assertEqual(processor.template_errors[0]["path"], "Broken.keywords")

    def test_template_matching_isolates_invalid_extraction_schema(self):
        processor = TemplateMatchingProcessor({
            "vendor_templates": {
                "Schema Vendor": {
                    "keywords": "Schema Vendor",
                    "extraction_patterns": ["not", "an", "object"],
                },
            }
        })

        result = processor.process_document(
            self.dummy_image_path,
            ocr_text="Schema Vendor",
        )

        self.assertEqual(result["extracted_data"], {"vendor_name": "Schema Vendor"})
        self.assertEqual(
            processor.template_errors,
            [{
                "path": "Schema Vendor.extraction_patterns",
                "error": "extraction_patterns must be an object",
            }],
        )

    def test_line_item_extractor(self):
        config = {}
        processor = LineItemExtractor(config)
        ocr_text = "Item A 10.00\nItem B 20.00"
        result = processor.process_document(self.dummy_image_path, ocr_text=ocr_text)
        self.assertIn("line_items", result["extracted_data"])
        self.assertEqual(len(result["extracted_data"]["line_items"]), 2)

    @unittest.skipIf(enhanced_module.cv2 is None, "OpenCV is optional in this test runtime")
    def test_enhanced_processor(self):
        preprocessing_dir = os.path.join(self.temp_dir.name, "private-preprocessing")
        config = {
            "ocr_processor": "tesseract",
            "line_item_extraction_enabled": True,
            "fab_preprocessing_temp_dir": preprocessing_dir,
        }
        processor = EnhancedProcessor(config)
        result = processor.process_document(self.dummy_image_path)
        self.assertTrue(result["preprocessing_applied"])
        self.assertNotEqual(result["processed_image_path"], self.dummy_image_path)
        self.assertTrue(os.path.isfile(result["processed_image_path"]))
        self.assertEqual(result["cleanup_paths"], [result["processed_image_path"]])
        self.assertEqual(
            os.path.dirname(result["processed_image_path"]),
            preprocessing_dir,
        )
        self.assertFalse(os.path.exists(self.dummy_image_path.replace(".", "_processed.")))
        os.remove(result["processed_image_path"])

    def test_enhanced_processor_leaves_non_images_unchanged(self):
        processor = EnhancedProcessor({})

        result = processor.process_document(self.dummy_pdf_path)

        self.assertFalse(result["preprocessing_applied"])
        self.assertEqual(result["preprocessing_reason"], "unsupported_image_type")
        self.assertEqual(result["processed_image_path"], self.dummy_pdf_path)
        self.assertEqual(result["cleanup_paths"], [])

    @unittest.skipIf(enhanced_module.cv2 is None, "OpenCV is optional in this test runtime")
    def test_enhanced_processor_corrects_skew_without_doubling_it(self):
        preprocessing_dir = os.path.join(self.temp_dir.name, "deskew")
        skewed_path = os.path.join(self.temp_dir.name, "skewed.png")
        image = Image.new("RGB", (500, 240), color="white")
        drawing = ImageDraw.Draw(image)
        for index in range(5):
            drawing.rectangle(
                (40, 40 + index * 35, 440, 48 + index * 35),
                fill="black",
            )
        image.rotate(7, expand=False, fillcolor="white").save(skewed_path)
        processor = EnhancedProcessor({
            "fab_preprocessing_temp_dir": preprocessing_dir,
        })

        first = processor.process_document(skewed_path)
        second = processor.process_document(first["processed_image_path"])

        self.assertAlmostEqual(abs(first["deskew_angle"]), 7.0, delta=0.75)
        self.assertEqual(second["deskew_angle"], 0.0)
        for cleanup_path in first["cleanup_paths"] + second["cleanup_paths"]:
            os.remove(cleanup_path)

    @patch("src.document_processors.bilingual_processor.langdetect.detect")
    @patch("src.document_processors.bilingual_processor.TesseractProcessor")
    def test_bilingual_processor(self, MockTesseractProcessor, mock_langdetect):
        mock_langdetect.return_value = "nl"
        MockTesseractProcessor.return_value.process_document.return_value = {
            "ocr_text": "Winkel\nTotaal EUR 12,10",
            "extracted_data": {},
        }

        processor = BilingualProcessor(self.config)
        result = processor.process_document(self.dummy_image_path)
        self.assertEqual(result["language"], "nl")
        self.assertEqual(result["extracted_data"]["total_amount"], 12.1)

        mock_langdetect.return_value = "en"
        result = processor.process_document(self.dummy_image_path)
        self.assertEqual(result["language"], "en")

    def test_processor_pipeline(self):
        config = {
            "processor_pipeline_steps": [
                {"name": "tesseract", "type": "tesseract"},
                {"name": "line_item_extractor", "type": "line_item_extractor"}
            ]
        }
        # Mock the individual processors within the pipeline
        with patch("src.document_processors.processor_pipeline.ProcessorFactory.create_processor") as mock_create_processor:
            mock_tesseract = MagicMock()
            mock_tesseract.process_document.return_value = {"ocr_text": "Pipeline Test\nItem 1 10.00", "extracted_data": {}}
            mock_line_item = MagicMock()
            mock_line_item.process_document.return_value = {"ocr_text": "Pipeline Test\nItem 1 10.00", "extracted_data": {"line_items": [{"description": "Item 1", "total": 10.00}]}}

            mock_create_processor.side_effect = [mock_tesseract, mock_line_item]

            pipeline = ProcessorPipeline(config)
            result = pipeline.process_document(self.dummy_image_path)
            self.assertIn("Pipeline Test", result["ocr_text"])
            self.assertIn("line_items", result["extracted_data"])
            self.assertEqual(len(result["extracted_data"]["line_items"]), 1)

    def tearDown(self):
        # Clean up dummy files
        if os.path.exists(self.config["google_vision_credentials_file"]):
            os.remove(self.config["google_vision_credentials_file"])
        if os.path.exists(self.dummy_image_path):
            os.remove(self.dummy_image_path)
        if os.path.exists(self.dummy_pdf_path):
            os.remove(self.dummy_pdf_path)
        if os.path.exists(self.config["template_matching_templates_dir"]):
            shutil.rmtree(self.config["template_matching_templates_dir"])
        if os.path.exists(self.config["vendor_templates_file"]):
            os.remove(self.config["vendor_templates_file"])

if __name__ == "__main__":
    unittest.main()


