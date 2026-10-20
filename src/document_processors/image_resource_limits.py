"""Shared resource bounds for raster images before local decoding."""

from io import BytesIO
from typing import Any, Tuple

try:
    from PIL import Image
except ImportError:
    Image = None


MAX_RASTER_IMAGE_PIXELS = 20_000_000
MAX_RASTER_IMAGE_EDGE = 10_000
MAX_VISION_IMAGE_BYTES = 20_000_000


def validate_raster_dimensions(image_or_size: Any) -> Tuple[int, int]:
    size = getattr(image_or_size, "size", image_or_size)
    if not isinstance(size, (tuple, list)) or len(size) != 2:
        raise ValueError("Image dimensions are missing or invalid.")
    width, height = size
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        raise ValueError("Image dimensions are missing or invalid.")
    if max(width, height) > MAX_RASTER_IMAGE_EDGE:
        raise ValueError("Image edge exceeds the local processing limit.")
    if width * height > MAX_RASTER_IMAGE_PIXELS:
        raise ValueError("Image pixel count exceeds the local processing limit.")
    return width, height


def validate_raster_path(path: str) -> Tuple[int, int]:
    if Image is None:
        raise RuntimeError("Pillow is required to validate local image dimensions.")
    with open_raster_image(path) as image:
        return validate_raster_dimensions(image)


def open_raster_image(path: str) -> Any:
    if Image is None:
        raise RuntimeError("Pillow is required to validate local image dimensions.")
    try:
        return Image.open(path)
    except Exception:
        raise ValueError("Image could not be opened or validated.") from None


def read_vision_image_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as image_file:
            content = image_file.read(MAX_VISION_IMAGE_BYTES + 1)
    except OSError:
        raise ValueError("Image could not be read for OCR.") from None
    if len(content) > MAX_VISION_IMAGE_BYTES:
        raise ValueError(
            f"Image exceeds the Google Vision {MAX_VISION_IMAGE_BYTES}-byte limit."
        )
    if Image is None:
        raise RuntimeError("Pillow is required to validate Google Vision image dimensions.")
    try:
        with Image.open(BytesIO(content)) as image:
            validate_raster_dimensions(image)
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("Multi-frame images are not supported for OCR.")
    except ValueError:
        raise
    except Exception:
        raise ValueError("Image data is invalid or could not be validated.") from None
    return content
