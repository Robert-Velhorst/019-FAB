from typing import Any, Dict

try:
    from google.cloud import vision
except ImportError:
    vision = None

from src.document_processors.base import BaseProcessor
from src.document_processors.image_resource_limits import MAX_VISION_IMAGE_BYTES
from src.document_processors.tesseract_processor import TesseractProcessor


class VisionProcessor(BaseProcessor):
    """Process documents with Google Cloud Vision when it is configured."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.client = vision.ImageAnnotatorClient() if vision is not None else None

    def process_document(self, document_path: str) -> Dict[str, Any]:
        if self.client is None or vision is None:
            return {
                "ocr_text": "",
                "extracted_data": {},
                "language": "",
                "error": "Google Vision is not installed or configured.",
            }

        try:
            with open(document_path, "rb") as image_file:
                content = image_file.read(MAX_VISION_IMAGE_BYTES + 1)
            if len(content) > MAX_VISION_IMAGE_BYTES:
                raise ValueError(
                    f"Image exceeds the Google Vision {MAX_VISION_IMAGE_BYTES}-byte limit."
                )
            image = vision.Image(content=content)

            response = self.client.document_text_detection(image=image)
            error_message = getattr(getattr(response, "error", None), "message", "")
            if isinstance(error_message, str) and error_message.strip():
                raise RuntimeError(error_message)

            annotation = response.full_text_annotation
            full_text = str(getattr(annotation, "text", "") or "")
            language = self._detected_language(annotation)
            return {
                "ocr_text": full_text,
                "extracted_data": self._extract_data_from_text(full_text),
                "language": language,
            }
        except Exception as exc:
            return {
                "ocr_text": "",
                "extracted_data": {},
                "language": "",
                "error": str(exc),
            }

    @staticmethod
    def _detected_language(annotation: Any) -> str:
        pages = list(getattr(annotation, "pages", None) or [])
        if not pages:
            return "en"
        detected = list(getattr(getattr(pages[0], "property", None), "detected_languages", None) or [])
        return str(getattr(detected[0], "language_code", "en") or "en") if detected else "en"

    @staticmethod
    def _extract_data_from_text(text: str) -> Dict[str, Any]:
        return TesseractProcessor._extract_data_from_text(text)
