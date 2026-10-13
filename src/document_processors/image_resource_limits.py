"""Shared resource bounds for raster images before local decoding."""

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
    with Image.open(path) as image:
        return validate_raster_dimensions(image)
