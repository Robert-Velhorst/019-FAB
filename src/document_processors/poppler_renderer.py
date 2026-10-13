"""Single-page Poppler rendering without untimed metadata/version subprocesses."""

import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def _run_renderer(arguments, timeout):
    try:
        result = subprocess.run(
            arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for this native process before raising.
        raise RuntimeError("PDF rendering timed out; no OCR result accepted.") from None
    except OSError:
        raise RuntimeError("PDF renderer could not start; check the Poppler installation.") from None
    if result.returncode:
        raise RuntimeError("PDF rendering failed; no OCR result accepted.")


def render_pdf_page(document_path, *, dpi=220, first_page=1, last_page=1,
                    poppler_path=None, size=3000, timeout=30, thread_count=1):
    from PIL import Image

    if (type(first_page) is not int or first_page < 1 or type(last_page) is not int
            or last_page != first_page or type(size) is not int or not 1 <= size <= 3000
            or type(dpi) is not int or not 100 <= dpi <= 400
            or type(thread_count) is not int or thread_count != 1
            or type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 30):
        raise ValueError("Invalid single-page PDF rendering limits.")
    executable = (str(Path(poppler_path) / ("pdftoppm.exe" if os.name == "nt" else "pdftoppm"))
                  if poppler_path else shutil.which("pdftoppm"))
    if not executable or (os.name == "nt" and not executable.lower().endswith(".exe")):
        raise RuntimeError("Poppler pdftoppm is required for PDF OCR.")

    with tempfile.TemporaryDirectory(prefix="fab-pdf-page-") as temporary:
        prefix = Path(temporary) / "page"
        # Raw PPM avoids encoding work and piping a second raster copy into Python.
        _run_renderer([
            executable, "-f", str(first_page), "-l", str(last_page), "-singlefile",
            "-r", str(dpi), "-scale-to", str(size),
            str(Path(document_path).resolve()), str(prefix),
        ], timeout)
        output = prefix.with_suffix(".ppm")
        if output.stat().st_size > size * size * 3 + 1024:
            raise ValueError("PDF raster exceeds the configured byte limit.")
        page = Image.open(output)
        try:
            if max(page.size) > size or min(page.size) < 1:
                raise ValueError("PDF raster dimensions exceed the configured limit.")
            page.load()
        except BaseException:
            page.close()
            raise
        # PPM loading closes the source handle; pixels survive temporary cleanup.
        return [page]
