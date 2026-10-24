# Dependencies

This document lists the external libraries and tools required for the Automated Bookkeeping Solution.

## Python Libraries

The following Python libraries are used in the project. They are listed in `requirements.txt` for easy installation using `pip`.

- `google-api-python-client`: For interacting with Google APIs (Gmail, Drive, Photos).
- `google-auth-oauthlib`: For OAuth 2.0 authentication with Google services.
- `google-cloud-vision`: Google Cloud Vision API client library for OCR.
- `Pillow`: Python Imaging Library (PIL) fork, used for image processing.
- `pytesseract`: Python wrapper for Tesseract OCR.
- `python-freshdesk`: For interacting with the Freshdesk API.
- A separately controlled browser session is required only for supervised provider workflows; the always-on FAB runtime does not install or invoke Playwright.
- `openpyxl`: For streaming local `.xlsx` bank-export rows; CSV and financial summaries use the standard library.
- `numpy`: Installed by the optional ML categorization lock when required by the approved-feedback model.
- `scikit-learn` and `joblib`: Optional dependencies for the approved-feedback category model. They are excluded from the default local and container runtime; install the hash-locked `requirements-ml.lock` only when enabling ML categorization.
- `tensorflow` or `pytorch`: (Optional, depending on specific ML model implementation) For deep learning models.
- `cryptography`: For secure credential encryption.
- `python-dotenv`: For loading environment variables.
- `Flask` and `waitress`: Authenticated local operations API and production WSGI server.
- `requests`: For general HTTP requests to various APIs (e.g., Waveapps).
- `beautifulsoup4` or `lxml`: For parsing HTML/XML if needed (e.g., for specific web scraping scenarios, though Playwright handles most of this).
- `schedule`: For scheduling recurring tasks.
- `APScheduler`: (Alternative to schedule) For more advanced scheduling.
- `pyyaml`: For parsing YAML configuration files (if used).
- `loguru`: (Optional) For enhanced logging.
- `pytest`: For unit and integration testing.

## External Tools / Services

- **Google Cloud Platform (optional)**: Google Cloud Vision may be configured as an OCR provider. FAB does not deploy unauthenticated Google Cloud Functions.
- **Docker Compose**: Supported multi-service cloud/container runtime; remote deployment still requires authenticated TLS ingress and managed secrets.
- **Tesseract OCR Engine**: Required if Tesseract is used as a fallback OCR method. Needs to be installed in the execution environment.
- **Waveapps API**: For integration with Waveapps accounting software.
- **mijngeldzaken.nl**: The web application for which browser automation is performed.
- **Dutch Banking APIs**: Specific APIs for Dutch banks for real-time transaction data (availability varies by bank).

## Development Tools

- `git`: Version control.
- `docker`: For containerization and creating reproducible environments.
- `black`, `flake8`: Code formatting and linting tools.
- `mypy`: Static type checker.

## Installation

The launcher and container builds install the core dependencies from hash-locked
files. For the optional ML categorizer, stop FAB and install its separate lock:

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-ml.lock
.\.venv\Scripts\python.exe -m pip check
```

For Docker Compose, build with `FAB_ENABLE_ML=true`. Training and enabling a
model remain operator-controlled; deterministic categorization and review
continue to work without the optional packages.

External tools like Tesseract OCR Engine might require separate installation steps depending on the operating system and deployment environment.

