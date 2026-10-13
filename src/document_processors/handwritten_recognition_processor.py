from typing import Any, Dict

try:
    import cv2
except ImportError:
    cv2 = None
try:
    import numpy as np
except ImportError:
    np = None
try:
    from PIL import Image
except ImportError:
    Image = None
try:
    import pytesseract
except ImportError:
    pytesseract = None

from src.document_processors.base import BaseProcessor
from src.document_processors.image_resource_limits import validate_raster_path
from src.document_processors.tesseract_processor import TesseractProcessor
from src.utils.tesseract_runtime import (
    configured_tesseract_languages,
    resolve_tesseract_command,
    tesseract_cli_config,
)

HANDWRITTEN_OCR_TIMEOUT_SECONDS = 30


class HandwrittenRecognitionProcessor(BaseProcessor):
    """Enhance handwriting contrast before running local Tesseract OCR."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.tesseract_cmd = resolve_tesseract_command(self.config)
        if pytesseract is not None:
            pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd or "tesseract"
        self.psm = self.config.get("handwritten_psm", 6)
        self.ocr_lang = self.config.get("handwritten_ocr_lang") or "+".join(
            configured_tesseract_languages(self.config)
        )

    def process_document(self, document_path: str) -> Dict[str, Any]:
        if cv2 is None or np is None or Image is None or pytesseract is None:
            return {
                "ocr_text": "",
                "extracted_data": {},
                "language": "",
                "error": "Handwriting OCR dependencies are not installed.",
            }

        try:
            validate_raster_path(document_path)
            image = cv2.imread(document_path)
            if image is None:
                raise ValueError(f"Could not load image from {document_path}")
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            threshold = cv2.adaptiveThreshold(
                gray,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY_INV,
                11,
                2,
            )
            kernel = np.ones((2, 2), np.uint8)
            prepared = Image.fromarray(cv2.dilate(threshold, kernel, iterations=1))
            config = tesseract_cli_config(self.config, f"--psm {self.psm} --oem 3")
            full_text = pytesseract.image_to_string(
                prepared,
                lang=self.ocr_lang,
                config=config,
                timeout=HANDWRITTEN_OCR_TIMEOUT_SECONDS,
            )
            return {
                "ocr_text": full_text,
                "extracted_data": self._extract_data_from_text(full_text),
                "language": self.ocr_lang,
            }
        except Exception as exc:
            return {
                "ocr_text": "",
                "extracted_data": {},
                "language": self.ocr_lang,
                "error": str(exc),
            }

    @staticmethod
    def _extract_data_from_text(text: str) -> Dict[str, Any]:
        return TesseractProcessor._extract_data_from_text(text)
