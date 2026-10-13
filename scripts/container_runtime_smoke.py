"""Offline, synthetic-only smoke check for the built Linux Python image."""

import importlib
import importlib.metadata
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


def main():
    if not hasattr(os, "getuid") or os.getuid() != 10001:
        raise RuntimeError("Run inside the non-root FAB Python image")
    for name in (
        "cv2", "sklearn", "pandas", "openpyxl", "google.cloud.vision",
        "src.operations.local_api", "src.run_worker", "src.run_health_probe",
    ):
        importlib.import_module(name)
    import cv2
    import numpy as np
    import pytesseract
    from PIL import Image, ImageDraw, ImageFont
    from src.security.deployment_secrets import read_secret
    from src.operations.local_api import create_app

    converted = cv2.cvtColor(np.zeros((8, 8, 3), dtype=np.uint8), cv2.COLOR_RGB2GRAY)
    assert converted.shape == (8, 8)
    with tempfile.TemporaryDirectory(prefix="fab-synthetic-smoke-") as directory:
        root = Path(directory)
        operator = "synthetic-operator-token-for-container-smoke-1234567890"
        hai = "synthetic-hai-token-for-container-smoke-0987654321"
        app = create_app({"fab_local_ledger_path": str(root / "ledger.sqlite3"),
                          "fab_local_api_token": operator, "fab_hai_api_token": hai,
                          "fab_hai_connector_enabled": True})
        client = app.test_client()
        assert client.get("/api/live").status_code == 401
        assert client.get("/api/live", headers={"Authorization": "Bearer " + operator}).status_code == 200
        assert client.get("/api/hai/manifest", headers={"Authorization": "Bearer " + hai}).status_code == 200
        assert client.get("/api/health", headers={"Authorization": "Bearer " + hai}).status_code == 403
        token = root / "token"
        token.write_bytes(b"synthetic-token\r\n")
        assert read_secret("FAB_LOCAL_API_TOKEN", {"FAB_LOCAL_API_TOKEN_FILE": str(token)}) == "synthetic-token"
        special = root / "not-a-secret-file"
        os.mkfifo(special)
        try:
            read_secret("FAB_LOCAL_API_TOKEN", {"FAB_LOCAL_API_TOKEN_FILE": str(special)})
        except ValueError:
            pass
        else:
            raise AssertionError("FIFO must not be accepted as a secret")
        image = Image.new("RGB", (500, 100), "white")
        ImageDraw.Draw(image).text((15, 20), "FAB invoice 123", fill="black", font=ImageFont.load_default(size=32))
        text = pytesseract.image_to_string(image, lang="eng+nld", config="--psm 7", timeout=15)
        assert "invoice" in text.lower() and "123" in text, "Native OCR did not read the synthetic fixture"
        pdf = root / "synthetic.pdf"
        image.save(pdf, "PDF")
        subprocess.run(["pdfinfo", str(pdf)], check=True, capture_output=True, timeout=15)
        prefix = root / "rendered"
        subprocess.run(["pdftoppm", "-singlefile", "-scale-to", "300", "-png", str(pdf), str(prefix)],
                       check=True, capture_output=True, timeout=15)
        with Image.open(prefix.with_suffix(".png")) as rendered:
            assert max(rendered.size) <= 300
            extrema = rendered.convert("L").getextrema()
            assert extrema[0] < extrema[1], "Native PDF rendering was blank"
    inventory = sorted((distribution.metadata["Name"], distribution.version)
                       for distribution in importlib.metadata.distributions())
    inventory_hash = hashlib.sha256(json.dumps(inventory).encode("utf-8")).hexdigest()
    print(json.dumps({"nonRoot": True, "imports": True, "opencv": True, "secretFiles": True,
                      "nativeOcrEnglishDutch": True, "nativePdfRender": True, "apiLedgerAuth": True,
                      "haiScope": True, "syntheticOnly": True, "pythonDependenciesSha256": inventory_hash}))


if __name__ == "__main__":
    main()
