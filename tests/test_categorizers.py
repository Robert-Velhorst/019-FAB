import unittest
from unittest.mock import MagicMock, patch

from src.categorizers.rule_based_categorizer import RuleBasedCategorizer
from src.categorizers.fallback_categorizer import FallbackCategorizer
from src.categorizers.ml_categorizer import MLCategorizer
from src.categorizers.hybrid_categorizer import HybridCategorizer

class TestCategorizers(unittest.TestCase):

    def setUp(self):
        self.config = {
            "categorization_rules": {
                "Personal": {"keywords": ["supermarket", "restaurant"], "vendors": ["Albert Heijn"]},
                "Business": {"keywords": ["office supplies", "software"], "vendors": ["Microsoft"]},
                "Handicaps": {"keywords": ["therapy", "medical"], "vendors": ["Physio Clinic"]}
            },
            "default_fallback_category": "Manual Review",
            "ml_confidence_threshold": 0.7,
            "ml_model_path": "/tmp/ml_categorizer_model.joblib",
            "ml_vectorizer_path": "/tmp/tfidf_vectorizer.joblib"
        }

    def test_rule_based_categorizer(self):
        categorizer = RuleBasedCategorizer(self.config)
        
        # Test with vendor match
        processed_data = {"ocr_text": "", "extracted_data": {"vendor_name": "Albert Heijn"}}
        result = categorizer.categorize(processed_data)
        self.assertEqual(result["category"], "Personal")

        # Test with keyword match
        processed_data = {"ocr_text": "bought some office supplies", "extracted_data": {}}
        result = categorizer.categorize(processed_data)
        self.assertEqual(result["category"], "Business")

        # Test uncategorized
        processed_data = {"ocr_text": "random text", "extracted_data": {"vendor_name": "Unknown Vendor"}}
        result = categorizer.categorize(processed_data)
        self.assertEqual(result["category"], "Uncategorized")

    def test_fallback_categorizer(self):
        categorizer = FallbackCategorizer(self.config)
        processed_data = {"ocr_text": "any text"}
        result = categorizer.categorize(processed_data)
        self.assertEqual(result["category"], "Manual Review")
        self.assertEqual(result["confidence_score"], 0.1)

    @patch("src.categorizers.ml_categorizer.joblib")
    @patch("src.categorizers.ml_categorizer.os.path.exists")
    @patch("src.categorizers.ml_categorizer._ml_dependencies_available", return_value=True)
    def test_ml_categorizer(self, mock_dependencies, mock_exists, mock_joblib):
        mock_exists.return_value = True
        
        mock_model = MagicMock()
        mock_model.predict.return_value = ["Business"]
        mock_model.predict_proba.return_value = [[0.1, 0.9]] # 0.9 confidence for Business
        mock_joblib.load.return_value = mock_model

        mock_vectorizer = MagicMock()
        mock_vectorizer.transform.return_value = MagicMock()
        mock_joblib.load.side_effect = [mock_model, mock_vectorizer]

        categorizer = MLCategorizer(self.config)
        processed_data = {"ocr_text": "some business related text"}
        result = categorizer.categorize(processed_data)
        self.assertEqual(result["category"], "Business")
        self.assertAlmostEqual(result["confidence_score"], 0.9)

        # Test with no model loaded
        mock_exists.return_value = False
        categorizer = MLCategorizer(self.config)
        result = categorizer.categorize(processed_data)
        self.assertEqual(result["category"], "Uncategorized")

    @patch("src.categorizers.ml_categorizer.joblib")
    @patch("src.categorizers.ml_categorizer.os.path.exists")
    @patch("src.categorizers.ml_categorizer._ml_dependencies_available", return_value=True)
    def test_hybrid_categorizer(self, mock_dependencies, mock_exists, mock_joblib):
        # Mock ML categorizer to return low confidence initially
        mock_exists.return_value = True
        mock_model = MagicMock()
        mock_model.predict.return_value = ["ML_Category"]
        mock_model.predict_proba.return_value = [[0.6, 0.4]] # 0.6 confidence
        mock_joblib.load.return_value = mock_model
        mock_vectorizer = MagicMock()
        mock_vectorizer.transform.return_value = MagicMock()
        mock_joblib.load.side_effect = [mock_model, mock_vectorizer]

        categorizer = HybridCategorizer(self.config)

        # Test with rule-based match (should take precedence)
        processed_data = {"ocr_text": "", "extracted_data": {"vendor_name": "Microsoft"}}
        result = categorizer.categorize(processed_data)
        self.assertEqual(result["category"], "Business")

        # Test with ML match (rule-based fails, ML succeeds with high confidence)
        processed_data = {"ocr_text": "some ml related text", "extracted_data": {"vendor_name": "NoMatch"}}
        mock_model.predict.return_value = ["ML_Category"]
        mock_model.predict_proba.return_value = [[0.1, 0.9]] # 0.9 confidence
        result = categorizer.categorize(processed_data)
        self.assertEqual(result["category"], "ML_Category")

        # Test with fallback (both rule-based and ML fail or low confidence)
        processed_data = {"ocr_text": "some random text", "extracted_data": {"vendor_name": "NoMatch"}}
        mock_model.predict.return_value = ["ML_Category"]
        mock_model.predict_proba.return_value = [[0.3, 0.2]] # 0.3 confidence (below threshold)
        result = categorizer.categorize(processed_data)
        self.assertEqual(result["category"], "Manual Review")

    @patch("src.categorizers.ml_categorizer._ml_dependencies_available", return_value=False)
    @patch("src.categorizers.ml_categorizer.os.path.exists", return_value=True)
    def test_ml_artifacts_fall_back_when_optional_dependencies_are_missing(self, mock_exists, mock_dependencies):
        categorizer = MLCategorizer(self.config)

        result = categorizer.categorize({"ocr_text": "receipt text"})

        self.assertEqual(categorizer.unavailable_reason, "dependencies_missing")
        self.assertEqual(result["source"], "ml_unavailable")

if __name__ == "__main__":
    unittest.main()


