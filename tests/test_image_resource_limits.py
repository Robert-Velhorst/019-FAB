import unittest
from unittest.mock import MagicMock, patch

from src.document_processors import image_resource_limits as limits


class TestImageResourceLimits(unittest.TestCase):
    def test_dimensions_accept_exact_pixel_and_edge_limits(self):
        size = (limits.MAX_RASTER_IMAGE_EDGE, limits.MAX_RASTER_IMAGE_PIXELS // limits.MAX_RASTER_IMAGE_EDGE)
        self.assertEqual(limits.validate_raster_dimensions(size), size)

    def test_dimensions_reject_excessive_pixels_or_edges(self):
        too_many_pixels = (5000, 4001)
        too_wide = (limits.MAX_RASTER_IMAGE_EDGE + 1, 1)
        for size in (too_many_pixels, too_wide):
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, "local processing limit"):
                limits.validate_raster_dimensions(size)

    def test_dimensions_reject_invalid_values(self):
        for size in ((0, 1), (-1, 1), (True, 1), ("2", 1), (1,)):
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, "missing or invalid"):
                limits.validate_raster_dimensions(size)

    def test_path_validation_reads_only_image_metadata(self):
        image = MagicMock()
        image.__enter__.return_value.size = (1200, 800)
        with patch.object(limits.Image, "open", return_value=image) as open_image:
            self.assertEqual(limits.validate_raster_path("receipt.png"), (1200, 800))
        open_image.assert_called_once_with("receipt.png")
        image.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
