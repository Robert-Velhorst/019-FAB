import logging
import importlib.util
import os
from typing import Dict, Any
from src.categorizers.base import BaseCategorizer


logger = logging.getLogger(__name__)
joblib = None


def _joblib_module():
    global joblib
    if joblib is not None:
        return joblib
    try:
        import joblib as loaded_joblib
    except ImportError:
        return None
    joblib = loaded_joblib
    return joblib


def _ml_dependencies_available():
    try:
        return _joblib_module() is not None and importlib.util.find_spec("sklearn") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False

class MLCategorizer(BaseCategorizer):
    """Categorizes documents using a trained Machine Learning model."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.model_path = self.config.get("ml_model_path", "data/models/ml_categorizer_model.joblib")
        self.vectorizer_path = self.config.get("ml_vectorizer_path", "data/models/tfidf_vectorizer.joblib")
        self.model = None
        self.vectorizer = None
        self.unavailable_reason = None
        self._load_model()

    def _load_model(self):
        if os.path.exists(self.model_path) and os.path.exists(self.vectorizer_path):
            if not _ml_dependencies_available():
                self.unavailable_reason = "dependencies_missing"
                logger.info("ML categorization is unavailable because optional dependencies are missing.")
                return
            loader = _joblib_module()
            try:
                self.model = loader.load(self.model_path)
                self.vectorizer = loader.load(self.vectorizer_path)
            except Exception:
                self.model = None
                self.vectorizer = None
                self.unavailable_reason = "model_load_failed"
                logger.exception("ML categorization artifacts could not be loaded; rule-based fallback remains active.")
                return
            logger.info("ML categorization model loaded.")
        else:
            self.unavailable_reason = "model_not_trained"
            logger.info("ML categorization is awaiting an approved training model.")

    def train_model(self, X_train: list, y_train: list):
        """Trains the ML model with provided data."""
        loader = _joblib_module()
        if loader is None:
            raise ImportError("scikit-learn and joblib are required to train the ML categorizer.")
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.linear_model import LogisticRegression
        except ImportError as exc:
            raise ImportError(
                "scikit-learn and joblib are required to train the ML categorizer."
            ) from exc

        self.vectorizer = TfidfVectorizer(max_features=1000)
        X_train_vectorized = self.vectorizer.fit_transform(X_train)
        
        self.model = LogisticRegression(max_iter=1000) # Increased max_iter for convergence
        self.model.fit(X_train_vectorized, y_train)

        os.makedirs(os.path.dirname(os.path.abspath(self.model_path)), exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(self.vectorizer_path)), exist_ok=True)
        loader.dump(self.model, self.model_path)
        loader.dump(self.vectorizer, self.vectorizer_path)
        self.unavailable_reason = None
        logger.info("ML categorization model trained and saved.")

    def categorize(self, processed_data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.model or not self.vectorizer:
            return {
                "category": "Uncategorized",
                "confidence_score": 0.0,
                "source": "ml_unavailable",
                "unavailable_reason": self.unavailable_reason or "model_unavailable",
            }

        ocr_text = processed_data.get("ocr_text", "")
        if not ocr_text:
            return {"category": "Uncategorized", "confidence_score": 0.0}

        text_vectorized = self.vectorizer.transform([ocr_text])
        
        predicted_category = self.model.predict(text_vectorized)[0]
        confidence_score = max(self.model.predict_proba(text_vectorized)[0])

        return {"category": predicted_category, "confidence_score": float(confidence_score)}


