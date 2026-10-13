import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

try:
    import cv2
except ImportError:
    cv2 = None

from src.document_processors.base import BaseProcessor
from src.document_processors.image_resource_limits import validate_raster_path

class EnhancedProcessor(BaseProcessor):
    """Applies advanced image preprocessing techniques to documents."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.denoising_strength = _bounded_int(
            self.config.get("denoising_strength"),
            default=10,
            minimum=0,
            maximum=30,
        )
        self.deskew_threshold = _bounded_float(
            self.config.get("deskew_threshold"),
            default=0.5,
            minimum=0.1,
            maximum=5.0,
        )
        self.deskew_max_angle = _bounded_float(
            self.config.get("deskew_max_angle"),
            default=15.0,
            minimum=1.0,
            maximum=30.0,
        )
        configured_temp_dir = str(
            self.config.get("fab_preprocessing_temp_dir") or ""
        ).strip()
        self.temp_dir = Path(configured_temp_dir).expanduser() if configured_temp_dir else (
            Path(tempfile.gettempdir()) / "fab-preprocessed"
        )

    def process_document(self, document_path: str) -> Dict[str, Any]:
        source_path = Path(str(document_path or ""))
        if source_path.suffix.lower() not in {
            ".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"
        }:
            return _unchanged(document_path, "unsupported_image_type")
        if cv2 is None:
            return _unchanged(document_path, "opencv_unavailable")

        processed_path = None
        try:
            validate_raster_path(str(source_path))
            img = cv2.imread(str(source_path))
            if img is None:
                return _unchanged(document_path, "image_unreadable")

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            denoised = (
                cv2.fastNlMeansDenoising(
                    gray,
                    None,
                    self.denoising_strength,
                    7,
                    21,
                )
                if self.denoising_strength
                else gray
            )
            deskewed, deskew_angle = self._deskew(denoised)
            _, binarized = cv2.threshold(deskewed, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

            self.temp_dir.mkdir(parents=True, exist_ok=True)
            file_handle, processed_path = tempfile.mkstemp(
                prefix="fab-preprocessed-",
                suffix=".png",
                dir=str(self.temp_dir),
            )
            os.close(file_handle)
            if not cv2.imwrite(processed_path, binarized):
                raise OSError("OpenCV could not write the preprocessed image")
            try:
                os.chmod(processed_path, 0o600)
            except OSError:
                pass

            return {
                "processed_image_path": processed_path,
                "cleanup_paths": [processed_path],
                "preprocessing_applied": True,
                "deskew_angle": round(deskew_angle, 3),
                "extracted_data": {},
                "ocr_text": "",
                "language": "",
            }
        except Exception as exc:
            if processed_path:
                try:
                    os.remove(processed_path)
                except OSError:
                    pass
            result = _unchanged(document_path, "preprocessing_failed")
            result["error_type"] = type(exc).__name__
            return result

    def _deskew(self, image) -> Tuple[Any, float]:
        _, foreground = cv2.threshold(
            image,
            0,
            255,
            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
        )
        coordinates = cv2.findNonZero(foreground)
        if coordinates is None or len(coordinates) < 20:
            return image, 0.0

        raw_angle = float(cv2.minAreaRect(coordinates)[-1])
        if raw_angle > 45.0:
            magnitude = abs(raw_angle - 90.0)
        elif raw_angle < -45.0:
            magnitude = abs(90.0 + raw_angle)
        else:
            magnitude = abs(raw_angle)
        if magnitude < self.deskew_threshold or magnitude > self.deskew_max_angle:
            return image, 0.0

        height, width = image.shape[:2]
        center = (width / 2.0, height / 2.0)
        candidates = []
        for angle in (-magnitude, magnitude):
            matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
            rotated = cv2.warpAffine(
                image,
                matrix,
                (width, height),
                flags=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=255,
            )
            candidates.append((_horizontal_alignment_score(rotated), rotated, angle))
        original_score = _horizontal_alignment_score(image)
        best_score, best_image, best_angle = max(candidates, key=lambda item: item[0])
        if best_score <= original_score * 1.01:
            return image, 0.0
        return best_image, best_angle


def _unchanged(document_path: str, reason: str) -> Dict[str, Any]:
    return {
        "processed_image_path": document_path,
        "cleanup_paths": [],
        "preprocessing_applied": False,
        "preprocessing_reason": reason,
        "deskew_angle": 0.0,
        "extracted_data": {},
        "ocr_text": "",
        "language": "",
    }


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _horizontal_alignment_score(image) -> float:
    _, foreground = cv2.threshold(
        image,
        0,
        255,
        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
    )
    row_density = foreground.mean(axis=1)
    return float(row_density.var())


