import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.document_processors import image_resource_limits
from src.document_processors import vision_processor as module


class BoundedStream:
    def __init__(self, content):
        self.content = content
        self.requested_bytes = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        self.requested_bytes = size
        return self.content[:size]


class TestVisionProcessorLimits(unittest.TestCase):
    def test_oversized_image_is_rejected_before_provider_request(self):
        stream = BoundedStream(b"x" * (image_resource_limits.MAX_VISION_IMAGE_BYTES + 1))
        client = MagicMock()
        fake_vision = SimpleNamespace(
            Image=MagicMock(),
            ImageAnnotatorClient=MagicMock(return_value=client),
        )
        with (
            patch.object(module, "vision", fake_vision),
            patch("builtins.open", return_value=stream),
        ):
            result = module.VisionProcessor({}).process_document("oversized.png")

        self.assertEqual(stream.requested_bytes, image_resource_limits.MAX_VISION_IMAGE_BYTES + 1)
        self.assertIn("exceeds the Google Vision", result["error"])
        fake_vision.Image.assert_not_called()
        client.document_text_detection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
